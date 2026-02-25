"""
YouTube Transcript Fetcher — Residential Proxy Edition

Routes all YouTube requests through a residential proxy so YouTube
sees a normal home IP instead of a datacenter/cloud IP.

Setup:
  1. Buy a residential proxy (recommended: IPRoyal, Webshare, or Smartproxy)
  2. Set PROXY_URL env var in Vercel:
       http://username:password@proxy-host:port
  3. Deploy and done!

The proxy only costs ~$1.75/GB (IPRoyal) and transcript text is tiny
(5-50KB per request), so $5 lasts thousands of requests.
"""

from flask import Flask, request, jsonify, send_from_directory
import json
import logging
import os
import re
import traceback
from html import unescape
import xml.etree.ElementTree as ET

import requests

app = Flask(__name__, static_folder="static")
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

PROXY_URL = os.environ.get("PROXY_URL", "")

# ── Constants ─────────────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_CLIENT_HINTS = {
    "Sec-Ch-Ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Ch-Ua-Arch": '"x86"',
    "Sec-Ch-Ua-Bitness": '"64"',
    "Sec-Ch-Ua-Full-Version-List": (
        '"Not:A-Brand";v="99.0.0.0", '
        '"Google Chrome";v="145.0.7632.110", '
        '"Chromium";v="145.0.7632.110"'
    ),
    "Sec-Ch-Ua-Platform-Version": '"19.0.0"',
    "Sec-Ch-Ua-Wow64": "?0",
}

_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

# Consent cookies to skip the EU consent wall
_CONSENT_COOKIES = (
    "SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODE1"
    "LjA3X3AxGgJlbiACGgYIgJnOlwY; CONSENT=PENDING+987"
)

# Innertube clients to try, in order
_CLIENTS = [
    {
        "name": "WEB",
        "endpoint": "https://www.youtube.com/youtubei/v1/player",
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20260222.03.00",
                "hl": "en",
                "gl": "US",
            }
        },
    },
    {
        "name": "ANDROID",
        "endpoint": "https://www.googleapis.com/youtubei/v1/player",
        "ua": "com.google.android.youtube/19.09.37 (Linux; U; Android 14) gzip",
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": "20.10.38",
                "androidSdkVersion": 30,
                "hl": "en",
                "gl": "US",
            }
        },
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_video_id(url: str):
    for p in [
        r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]:
        m = re.search(p, url.strip())
        if m:
            return m.group(1)
    return None


def _fmt_ts(seconds):
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def _seg(start_ms, dur_ms, text):
    start = start_ms / 1000
    return {
        "timestamp": _fmt_ts(start),
        "start": round(start, 2),
        "duration": round(dur_ms / 1000, 2),
        "text": text,
    }


def _seg_sec(start, dur, text):
    return {
        "timestamp": _fmt_ts(start),
        "start": round(start, 2),
        "duration": round(dur, 2),
        "text": text,
    }


def _dedup(segs):
    if not segs:
        return segs
    out = [segs[0]]
    for s in segs[1:]:
        if s["text"] != out[-1]["text"]:
            out.append(s)
    return out


def _pick_track(tracks, lang="en"):
    for t in tracks:
        if t.get("languageCode") == lang and t.get("kind", "") != "asr":
            return t
    for t in tracks:
        if t.get("languageCode") == lang:
            return t
    for t in tracks:
        if (t.get("languageCode") or "").startswith(lang):
            return t
    return tracks[0] if tracks else None


def _track_label(track):
    name = track.get("name", {})
    if isinstance(name, dict):
        runs = name.get("runs")
        if runs and isinstance(runs, list):
            return runs[0].get("text", "")
        return name.get("simpleText", "")
    return str(name) if name else ""


# ── Caption parsers ───────────────────────────────────────────────────────────

def _parse_json3(raw):
    data = json.loads(raw) if isinstance(raw, str) else raw
    segs = []
    for ev in data.get("events", []):
        parts = [s.get("utf8", "") for s in ev.get("segs", [])]
        text = "".join(parts).strip()
        if text and text != "\n":
            segs.append(_seg(ev.get("tStartMs", 0), ev.get("dDurationMs", 0), text))
    return segs


def _parse_srv3(raw):
    segs = []
    root = ET.fromstring(raw)
    for p in root.findall(".//p"):
        parts = [s.text or "" for s in p.findall(".//s")]
        if not parts and p.text:
            parts = [p.text]
        text = unescape("".join(parts).strip())
        if text:
            segs.append(_seg(int(p.get("t", 0)), int(p.get("d", 0)), text))
    return segs


def _parse_xml(raw):
    segs = []
    root = ET.fromstring(raw)
    for el in root.findall(".//text"):
        text = unescape((el.text or "").strip())
        if text:
            segs.append(_seg_sec(
                float(el.get("start", 0)), float(el.get("dur", 0)), text
            ))
    return segs


def _parse_captions(raw):
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return _parse_json3(raw)
        except Exception:
            pass
    if "<timedtext" in raw[:200]:
        return _parse_srv3(raw)
    if "<transcript" in raw[:200] or raw.startswith("<?xml"):
        return _parse_xml(raw)
    for parser in [_parse_srv3, _parse_xml]:
        try:
            return parser(raw)
        except Exception:
            pass
    return []


# ── JSON extraction from HTML ─────────────────────────────────────────────────

def _extract_json_at(html, idx):
    if idx >= len(html) or html[idx] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    i = idx
    while i < len(html):
        c = html[i]
        if esc:
            esc = False
            i += 1
            continue
        if c == "\\" and in_str:
            esc = True
            i += 1
            continue
        if c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[idx : i + 1])
                    except json.JSONDecodeError:
                        return None
        i += 1
    return None


# ── URL rewriting helpers ─────────────────────────────────────────────────────

def _strip_exp(url):
    """Remove exp=xpe parameter that causes empty timedtext responses."""
    url = re.sub(r"[&?]exp=[^&]*", "", url)
    url = re.sub(r"(sparams=[^&]*)(?:,exp|exp,)", r"\1", url)
    return url


# ── Session factory ──────────────────────────────────────────────────────────

def _create_session(ua=None):
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua or _UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": _CONSENT_COOKIES,
    })
    if not ua or ua == _UA:
        s.headers.update(_CLIENT_HINTS)

    if PROXY_URL:
        s.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        log.info("Proxy configured: %s", PROXY_URL.split("@")[-1] if "@" in PROXY_URL else "yes")
    else:
        log.warning("No PROXY_URL set — requests go from Vercel's datacenter IP (may be blocked)")

    return s


# ── Timedtext fetcher ────────────────────────────────────────────────────────

def _fetch_timedtext(session, vid, tracks, source):
    """Pick best track, fetch and parse caption content."""
    errors = []
    chosen = _pick_track(tracks)
    if not chosen:
        return None, ["No suitable caption track found"]

    cap_url = chosen.get("baseUrl", "")
    if not cap_url:
        return None, ["Caption track has no URL"]

    label = _track_label(chosen)
    lang_code = chosen.get("languageCode", "")
    is_generated = chosen.get("kind", "") == "asr"
    has_exp = "exp=" in cap_url

    log.info("[%s] Fetching timedtext (lang=%s, src=%s, exp=%s)",
             vid, lang_code, source, has_exp)

    # Build URL list — try with exp stripped first if present
    urls = []
    if has_exp:
        urls.append(("no-exp", _strip_exp(cap_url)))
    urls.append(("original", cap_url))

    for url_tag, base_url in urls:
        for fmt in ["json3", "srv3", ""]:
            url = base_url
            if fmt:
                if "fmt=" in url:
                    url = re.sub(r"fmt=[^&]*", f"fmt={fmt}", url)
                else:
                    url += f"&fmt={fmt}"

            try:
                r = session.get(url, timeout=15)
                clen = len(r.text.strip())
                log.info("[%s] Timedtext (%s/%s): status=%d len=%d",
                         vid, url_tag, fmt or "default", r.status_code, clen)

                if r.status_code == 404:
                    errors.append(f"Timedtext ({url_tag}/{fmt or 'default'}): 404")
                    break  # URL variant is wrong, skip other formats
                if r.status_code != 200:
                    errors.append(f"Timedtext ({url_tag}/{fmt or 'default'}): HTTP {r.status_code}")
                    continue
                if clen == 0:
                    errors.append(f"Timedtext ({url_tag}/{fmt or 'default'}): empty")
                    continue

                segs = _dedup(_parse_captions(r.text))
                if not segs:
                    errors.append(f"Timedtext ({url_tag}/{fmt or 'default'}): 0 segments")
                    continue

                return {
                    "video_id": vid,
                    "language": label or lang_code,
                    "language_code": lang_code,
                    "is_generated": is_generated,
                    "segments": segs,
                    "full_text": " ".join(s["text"] for s in segs),
                    "source": source,
                }, []

            except Exception as e:
                errors.append(f"Timedtext ({url_tag}/{fmt or 'default'}): {e}")

    return None, errors


# ── Main transcript fetcher ──────────────────────────────────────────────────

def fetch_transcript(vid):
    """
    Fetch transcript via residential proxy.
    Strategy: Innertube WEB → Innertube ANDROID → Watch page HTML scraping.
    """
    all_errors = []

    # ── Phase 1: Innertube player API ─────────────────────────────────
    for client in _CLIENTS:
        name = client["name"]
        endpoint = client["endpoint"]
        ua = client.get("ua", _UA)
        label = f"{name} ({endpoint.split('/')[2]})"

        log.info("[%s] Trying: %s", vid, label)

        session = _create_session(ua=ua)

        try:
            r = session.post(
                f"{endpoint}?key={_API_KEY}&prettyPrint=false",
                json={"context": client["context"], "videoId": vid},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )

            if r.status_code != 200:
                all_errors.append(f"{label}: HTTP {r.status_code}")
                continue

            data = r.json()
            ps = data.get("playabilityStatus", {})
            status = ps.get("status", "")

            if status != "OK":
                reason = ps.get("reason", status or "unknown")
                all_errors.append(f"{label}: {reason}")
                continue

            tracks = (
                data.get("captions", {})
                .get("playerCaptionsTracklistRenderer", {})
                .get("captionTracks", [])
            )

            if not tracks:
                all_errors.append(f"{label}: no caption tracks")
                continue

            log.info("[%s] %s: %d caption tracks found", vid, label, len(tracks))

            result, errs = _fetch_timedtext(session, vid, tracks, label)
            if result:
                return result, []
            all_errors.extend(errs)

        except Exception as e:
            all_errors.append(f"{label}: {e}")
            log.warning("[%s] %s error: %s", vid, label, e)

    # ── Phase 2: Watch page HTML extraction ───────────────────────────
    log.info("[%s] Trying: Watch page HTML extraction", vid)
    session = _create_session()

    try:
        r = session.get(
            f"https://www.youtube.com/watch?v={vid}&hl=en",
            timeout=15,
        )
        log.info("[%s] Watch page: status=%d", vid, r.status_code)

        if r.status_code == 200:
            # Check for bot detection
            if "Sign in to confirm" in r.text or "confirm you're not a bot" in r.text:
                all_errors.append("Watch page: bot detection triggered")
            else:
                m = re.search(r"ytInitialPlayerResponse\s*=\s*", r.text)
                if m:
                    player = _extract_json_at(r.text, m.end())
                    if player:
                        ps = player.get("playabilityStatus", {})
                        if ps.get("status") != "OK":
                            reason = ps.get("reason", "")
                            if reason:
                                all_errors.append(f"Video: {reason}")

                        tracks = (
                            player.get("captions", {})
                            .get("playerCaptionsTracklistRenderer", {})
                            .get("captionTracks", [])
                        )
                        if tracks:
                            log.info("[%s] HTML: %d caption tracks", vid, len(tracks))
                            result, errs = _fetch_timedtext(
                                session, vid, tracks, "watch-page-html"
                            )
                            if result:
                                return result, []
                            all_errors.extend(errs)
                        else:
                            all_errors.append("Watch page: no caption tracks in player response")
                    else:
                        all_errors.append("Watch page: could not parse ytInitialPlayerResponse")
                else:
                    all_errors.append("Watch page: ytInitialPlayerResponse not found")
        else:
            all_errors.append(f"Watch page: HTTP {r.status_code}")

    except Exception as e:
        all_errors.append(f"Watch page: {e}")

    return None, all_errors


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/transcript", methods=["POST"])
def get_transcript_route():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    vid = body.get("video_id", "").strip()

    if not vid and not url:
        return jsonify(error="Please provide a YouTube video URL."), 400
    if not vid:
        vid = extract_video_id(url)
    if not vid:
        return jsonify(error="Invalid YouTube URL. Please check and try again."), 400

    log.info("=== Transcript request: video=%s ===", vid)

    try:
        result, errors = fetch_transcript(vid)
    except Exception as e:
        log.error("Unexpected error for %s: %s", vid, traceback.format_exc())
        return jsonify(
            error=f"Unexpected error: {e}",
            details=[traceback.format_exc()],
        ), 500

    if result:
        log.info("=== Success: video=%s, source=%s, segments=%d ===",
                 vid, result["source"], len(result["segments"]))
        return jsonify(result)

    log.error("=== Failed: video=%s, errors=%s ===", vid, errors)

    hint = ""
    if not PROXY_URL:
        hint = (
            " No residential proxy configured. Set PROXY_URL environment "
            "variable (e.g. http://user:pass@proxy-host:port) to route "
            "requests through a residential IP."
        )

    return jsonify(
        error="Could not fetch transcript. "
              "The video may not have captions, or YouTube blocked the request."
              + hint,
        details=errors,
    ), 500


@app.route("/api/health")
def health():
    return jsonify(
        status="ok",
        proxy_configured=bool(PROXY_URL),
        proxy_host=PROXY_URL.split("@")[-1] if "@" in PROXY_URL else ("yes" if PROXY_URL else "none"),
        strategies=["WEB-innertube", "ANDROID-innertube", "watch-page-html"],
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
