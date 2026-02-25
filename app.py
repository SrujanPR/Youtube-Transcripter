"""
YouTube Transcript Generator — Cloud-compatible Backend

Strategy: Session-based innertube approach
  1. GET the YouTube watch page → establishes session cookies
  2. POST innertube player API (ANDROID client) → gets caption track URLs
  3. GET timedtext URL with session cookies → fetches actual captions

Why this works on cloud (Vercel / AWS):
  The session cookies from step 1 authenticate the timedtext request in step 3.
  Without cookies, YouTube blocks timedtext from datacenter IPs.
  With cookies from a valid session, the request is allowed.

Environment variables (all optional):
  PROXY_URL    Residential proxy for extra reliability, e.g. http://user:pass@host:port
"""

from flask import Flask, request, jsonify, send_from_directory
import re
import os
import json
import logging
import traceback
from html import unescape
import xml.etree.ElementTree as ET

import requests

app = Flask(__name__, static_folder="static")
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

PROXY_URL = os.environ.get("PROXY_URL")


# ── Helpers ───────────────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def extract_video_id(url: str):
    """Extract YouTube video ID from various URL formats."""
    for p in [
        r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]:
        m = re.search(p, url.strip())
        if m:
            return m.group(1)
    return None


def _seg(start_ms, dur_ms, text):
    """Build a segment dict from millisecond values."""
    start = start_ms / 1000
    dur = dur_ms / 1000
    return {
        "timestamp": f"{int(start // 60):02d}:{int(start % 60):02d}",
        "start": round(start, 2),
        "duration": round(dur, 2),
        "text": text,
    }


def _seg_sec(start, dur, text):
    """Build a segment dict from second values."""
    return {
        "timestamp": f"{int(start // 60):02d}:{int(start % 60):02d}",
        "start": round(start, 2),
        "duration": round(dur, 2),
        "text": text,
    }


def dedup(segs):
    """Remove consecutive segments with identical text."""
    if not segs:
        return segs
    out = [segs[0]]
    for s in segs[1:]:
        if s["text"] != out[-1]["text"]:
            out.append(s)
    return out


def _pick_track(tracks, lang="en"):
    """Choose the best caption track, preferring the given language."""
    for t in tracks:
        if t.get("languageCode", "") == lang and t.get("kind", "") != "asr":
            return t
    for t in tracks:
        if t.get("languageCode", "") == lang:
            return t
    for t in tracks:
        if t.get("languageCode", "").startswith(lang):
            return t
    return tracks[0] if tracks else None


def _parse_srv3_xml(raw):
    """Parse srv3 XML format: <timedtext><body><p t='ms' d='ms'>text</p>..."""
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


def _parse_xml_transcript(raw):
    """Parse classic XML: <transcript><text start='sec' dur='sec'>text</text>..."""
    segs = []
    root = ET.fromstring(raw)
    for el in root.findall(".//text"):
        text = unescape((el.text or "").strip())
        if text:
            segs.append(_seg_sec(
                float(el.get("start", 0)), float(el.get("dur", 0)), text
            ))
    return segs


def _parse_json3(raw):
    """Parse YouTube JSON3 caption format."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    segs = []
    for ev in data.get("events", []):
        parts = [s.get("utf8", "") for s in ev.get("segs", [])]
        text = "".join(parts).strip()
        if text and text != "\n":
            segs.append(_seg(ev.get("tStartMs", 0), ev.get("dDurationMs", 0), text))
    return segs


def _parse_captions(raw):
    """Auto-detect and parse caption data."""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return _parse_json3(raw)
        except Exception:
            pass
    if "<timedtext" in raw[:200]:
        return _parse_srv3_xml(raw)
    if "<transcript" in raw[:200] or raw.startswith("<?xml"):
        return _parse_xml_transcript(raw)
    try:
        return _parse_srv3_xml(raw)
    except Exception:
        pass
    try:
        return _parse_xml_transcript(raw)
    except Exception:
        pass
    return []


def _get_track_label(track):
    """Extract human-readable label from a caption track."""
    name = track.get("name", {})
    if isinstance(name, dict):
        runs = name.get("runs")
        if runs and isinstance(runs, list):
            return runs[0].get("text", "")
        return name.get("simpleText", "")
    return str(name) if name else ""


# ── Main transcript fetching logic ────────────────────────────────────────────


def fetch_transcript(vid):
    """
    Fetch transcript using session-based innertube approach.

    Steps:
      1. GET watch page -> session cookies
      2. POST innertube player (ANDROID) -> caption tracks
      3. GET timedtext with session -> caption content

    Returns (result_dict, errors_list). result_dict is None on failure.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": _UA,
        "Accept-Language": "en-US,en;q=0.9",
    })

    if PROXY_URL:
        session.proxies = {"http": PROXY_URL, "https": PROXY_URL}

    errors = []
    page_text = ""

    # ── Step 1: Fetch watch page to establish cookies ─────────────────────
    log.info("[%s] Step 1: Fetching watch page...", vid)
    try:
        page_r = session.get(
            f"https://www.youtube.com/watch?v={vid}",
            timeout=15,
        )
        page_text = page_r.text
        log.info("[%s] Watch page: status=%d, cookies=%d",
                 vid, page_r.status_code, len(session.cookies))
        if page_r.status_code != 200:
            errors.append(f"Watch page returned status {page_r.status_code}")
    except Exception as e:
        errors.append(f"Watch page fetch failed: {e}")
        log.warning("[%s] Watch page failed: %s", vid, e)

    # ── Step 2: Extract API key from page ─────────────────────────────────
    api_key = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
    try:
        key_m = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', page_text)
        if key_m:
            api_key = key_m.group(1)
    except Exception:
        pass

    # ── Step 3: Call innertube player API ──────────────────────────────────
    client_configs = [
        {
            "name": "ANDROID",
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
            "name": "IOS",
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
        {
            "name": "WEB",
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20260222.03.00",
                    "hl": "en",
                    "gl": "US",
                }
            },
        },
    ]

    tracks = None
    player_source = None

    for cfg in client_configs:
        cname = cfg["name"]
        log.info("[%s] Step 2: Trying innertube player (%s)...", vid, cname)
        try:
            player_r = session.post(
                f"https://www.youtube.com/youtubei/v1/player?key={api_key}",
                json={"context": cfg["context"], "videoId": vid},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if player_r.status_code != 200:
                errors.append(f"Player ({cname}): status {player_r.status_code}")
                continue

            pdata = player_r.json()
            ps = pdata.get("playabilityStatus", {})
            if ps.get("status") != "OK":
                reason = ps.get("reason", "unknown")
                errors.append(f"Player ({cname}): {reason}")
                continue

            found_tracks = (
                pdata.get("captions", {})
                .get("playerCaptionsTracklistRenderer", {})
                .get("captionTracks", [])
            )
            if found_tracks:
                tracks = found_tracks
                player_source = cname
                log.info("[%s] Player (%s): found %d caption tracks",
                         vid, cname, len(tracks))
                break
            else:
                errors.append(f"Player ({cname}): no caption tracks")

        except Exception as e:
            errors.append(f"Player ({cname}): {e}")
            log.warning("[%s] Player (%s) error: %s", vid, cname, e)

    # ── Step 3b: Try extracting tracks from page HTML as fallback ─────────
    if not tracks and page_text:
        log.info("[%s] Trying page HTML extraction...", vid)
        try:
            m = re.search(
                r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;\s*(?:var\s|</script>)",
                page_text, re.DOTALL,
            )
            if m:
                page_player = json.loads(m.group(1))
                found_tracks = (
                    page_player.get("captions", {})
                    .get("playerCaptionsTracklistRenderer", {})
                    .get("captionTracks", [])
                )
                if found_tracks:
                    tracks = found_tracks
                    player_source = "page-html"
                    log.info("[%s] Page HTML: found %d caption tracks",
                             vid, len(tracks))
                else:
                    errors.append("Page HTML: no caption tracks")
            else:
                errors.append("Page HTML: ytInitialPlayerResponse not found")
        except Exception as e:
            errors.append(f"Page HTML: {e}")

    if not tracks:
        return None, errors

    # ── Step 4: Pick best track and fetch captions ────────────────────────
    chosen = _pick_track(tracks)
    if not chosen:
        errors.append("No suitable caption track found")
        return None, errors

    cap_url = chosen.get("baseUrl", "")
    if not cap_url:
        errors.append("Caption track has no URL")
        return None, errors

    label = _get_track_label(chosen)
    lang_code = chosen.get("languageCode", "")
    is_generated = chosen.get("kind", "") == "asr"

    log.info("[%s] Step 3: Fetching timedtext (lang=%s, generated=%s)...",
             vid, lang_code, is_generated)

    for fmt_suffix in ["&fmt=json3", "&fmt=srv3", ""]:
        fetch_url = cap_url
        if fmt_suffix and "fmt=" not in fetch_url:
            fetch_url += fmt_suffix
        elif fmt_suffix:
            fetch_url = re.sub(r"&fmt=[^&]*", fmt_suffix, fetch_url)

        try:
            tt_r = session.get(fetch_url, timeout=15)
            log.info("[%s] Timedtext: status=%d, length=%d, fmt=%s",
                     vid, tt_r.status_code, len(tt_r.text),
                     fmt_suffix or "default")

            if tt_r.status_code != 200:
                errors.append(f"Timedtext ({fmt_suffix or 'default'}): status {tt_r.status_code}")
                continue

            if not tt_r.text.strip():
                errors.append(f"Timedtext ({fmt_suffix or 'default'}): empty response")
                continue

            segs = dedup(_parse_captions(tt_r.text))
            if not segs:
                errors.append(f"Timedtext ({fmt_suffix or 'default'}): parsed 0 segments")
                continue

            return {
                "video_id": vid,
                "language": label or lang_code,
                "language_code": lang_code,
                "is_generated": is_generated,
                "segments": segs,
                "full_text": " ".join(s["text"] for s in segs),
                "source": f"innertube ({player_source})",
            }, []

        except Exception as e:
            errors.append(f"Timedtext ({fmt_suffix or 'default'}): {e}")
            log.warning("[%s] Timedtext error: %s", vid, e)

    return None, errors


# ── Routes ────────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/transcript", methods=["POST"])
def get_transcript():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()

    if not url:
        return jsonify(error="Please provide a YouTube video URL."), 400

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
        strategy="session-based innertube",
        proxy_configured=bool(PROXY_URL),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
