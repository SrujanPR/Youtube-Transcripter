"""
YouTube Transcript Fetcher — Cloud-Compatible

Key discovery: www.googleapis.com/youtubei/v1/player with ANDROID client
bypasses YouTube's datacenter IP blocking, while the same call to
www.youtube.com is blocked with "Sign in to confirm you're not a bot".

Strategy order:
  1. googleapis.com + ANDROID         — primary, works from cloud IPs
  2. googleapis.com + ANDROID_VR      — fallback uncommon client
  3. googleapis.com + IOS             — fallback
  4. Watch page HTML extraction       — fallback with URL rewriting
  5. youtube.com clients              — last resort (likely blocked on cloud)
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

PROXY_URL = os.environ.get("PROXY_URL")

# ── Constants ─────────────────────────────────────────────────────────────────

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_ANDROID_UA = "com.google.android.youtube/19.09.37 (Linux; U; Android 14) gzip"
_IOS_UA = (
    "com.google.ios.youtube/19.45.4 "
    "(iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X)"
)

_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_SOCS = (
    "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODE1"
    "LjA3X3AxGgJlbiACGgYIgJnOlwY"
)

# Client configs — ordered by reliability on cloud IPs
# The googleapis.com domain bypasses YouTube's bot detection for datacenter IPs
_STRATEGIES = [
    # ── googleapis.com endpoints (bypass bot detection) ──
    {
        "name": "ANDROID",
        "endpoint": "https://www.googleapis.com/youtubei/v1/player",
        "ua": _ANDROID_UA,
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
    {
        "name": "ANDROID_VR",
        "endpoint": "https://www.googleapis.com/youtubei/v1/player",
        "ua": _ANDROID_UA,
        "context": {
            "client": {
                "clientName": "ANDROID_VR",
                "clientVersion": "1.57.29",
                "androidSdkVersion": 30,
                "hl": "en",
                "gl": "US",
            }
        },
    },
    {
        "name": "IOS",
        "endpoint": "https://www.googleapis.com/youtubei/v1/player",
        "ua": _IOS_UA,
        "context": {
            "client": {
                "clientName": "IOS",
                "clientVersion": "19.45.4",
                "deviceModel": "iPhone16,2",
                "hl": "en",
                "gl": "US",
            }
        },
    },
    # ── youtube.com endpoints (fallback, may be blocked on cloud) ──
    {
        "name": "WEB",
        "endpoint": "https://www.youtube.com/youtubei/v1/player",
        "ua": _CHROME_UA,
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20260222.03.00",
                "hl": "en",
                "gl": "US",
            },
            "thirdParty": {"embedUrl": "https://www.google.com/"},
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

def _create_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": _CHROME_UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    s.cookies.update({"SOCS": _SOCS, "CONSENT": "PENDING+987"})
    if PROXY_URL:
        s.proxies = {"http": PROXY_URL, "https": PROXY_URL}
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
    Fetch transcript using multiple strategies.
    Returns (result_dict | None, errors_list).
    """
    session = _create_session()
    all_errors = []

    # ── Phase 1: Innertube player API (googleapis.com first) ──────────
    for strat in _STRATEGIES:
        name = strat["name"]
        endpoint = strat["endpoint"]
        domain = endpoint.split("/")[2]
        label = f"{name} ({domain})"

        log.info("[%s] Trying: %s", vid, label)

        try:
            r = session.post(
                f"{endpoint}?key={_API_KEY}",
                json={"context": strat["context"], "videoId": vid},
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": strat["ua"],
                },
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

            log.info("[%s] %s: %d caption tracks", vid, label, len(tracks))

            result, errs = _fetch_timedtext(session, vid, tracks, label)
            if result:
                return result, []
            all_errors.extend(errs)

        except Exception as e:
            all_errors.append(f"{label}: {e}")
            log.warning("[%s] %s error: %s", vid, label, e)

    # ── Phase 2: Watch page HTML extraction ───────────────────────────
    log.info("[%s] Trying: Watch page HTML extraction", vid)
    try:
        r = session.get(
            f"https://www.youtube.com/watch?v={vid}",
            timeout=15,
        )
        log.info("[%s] Watch page: status=%d cookies=%d",
                 vid, r.status_code, len(session.cookies))

        if r.status_code == 200:
            m = re.search(r"ytInitialPlayerResponse\s*=\s*", r.text)
            if m:
                player = _extract_json_at(r.text, m.end())
                if player:
                    tracks = (
                        player.get("captions", {})
                        .get("playerCaptionsTracklistRenderer", {})
                        .get("captionTracks", [])
                    )
                    if tracks:
                        log.info("[%s] HTML: %d caption tracks", vid, len(tracks))
                        result, errs = _fetch_timedtext(
                            session, vid, tracks, "html-extraction"
                        )
                        if result:
                            return result, []
                        all_errors.extend(errs)
                    else:
                        all_errors.append("HTML: no caption tracks in player response")

                    # Check overall video status
                    ps = player.get("playabilityStatus", {})
                    if ps.get("status") != "OK":
                        reason = ps.get("reason", "")
                        if reason:
                            all_errors.append(f"Video: {reason}")
                else:
                    all_errors.append("HTML: could not parse ytInitialPlayerResponse")
            else:
                all_errors.append("HTML: ytInitialPlayerResponse not found")
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
    return jsonify(
        error="Could not fetch transcript. "
              "The video may not have captions, or YouTube blocked the request.",
        details=errors,
    ), 500


@app.route("/api/health")
def health():
    return jsonify(
        status="ok",
        strategies=[s["name"] for s in _STRATEGIES] + ["html-extraction"],
        proxy_configured=bool(PROXY_URL),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
