"""
YouTube Transcript Generator — Multi-strategy Backend

Strategies (tried in order):
  1. Invidious API   → Free public instances that proxy YouTube requests
  2. Piped API       → Another free YouTube frontend with an API
  3. Direct          → youtube_transcript_api (works locally or with PROXY_URL)

Environment variables (all optional):
  PROXY_URL              Residential proxy, e.g. http://user:pass@host:port
  INVIDIOUS_INSTANCES    Comma-separated Invidious base URLs
  PIPED_INSTANCES        Comma-separated Piped API base URLs
"""

from flask import Flask, request, jsonify, send_from_directory
import re
import os
import html
import json
import logging
import xml.etree.ElementTree as ET

import requests

app = Flask(__name__, static_folder="static")
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_DEFAULT_INVIDIOUS = [
    "https://inv.nadeko.net",
    "https://yewtu.be",
    "https://vid.puffyan.us",
]

_DEFAULT_PIPED = [
    "https://pipedapi.kavin.rocks",
]


def _env_list(key, default):
    v = os.environ.get(key, "")
    return [u.strip() for u in v.split(",") if u.strip()] if v else default


INVIDIOUS = _env_list("INVIDIOUS_INSTANCES", _DEFAULT_INVIDIOUS)
PIPED = _env_list("PIPED_INSTANCES", _DEFAULT_PIPED)
PROXY_URL = os.environ.get("PROXY_URL")
TIMEOUT = 5  # seconds per request

# ── Helpers ───────────────────────────────────────────────────────────────────


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


def _seg(start, dur, text):
    """Build a segment dict."""
    return {
        "timestamp": f"{int(start // 60):02d}:{int(start % 60):02d}",
        "start": round(start, 2),
        "duration": round(dur, 2),
        "text": text,
    }


def _vtt_ts(h, m, s, ms):
    """Convert VTT timestamp parts → seconds."""
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


# ── Caption Parsers ───────────────────────────────────────────────────────────


def parse_vtt(raw):
    """Parse WebVTT format into segments."""
    segs = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.strip().split("\n")
        for i, ln in enumerate(lines):
            m = re.match(
                r"(?:(\d+):)?(\d{2}):(\d{2})[\.,](\d{3})\s*-->\s*"
                r"(?:(\d+):)?(\d{2}):(\d{2})[\.,](\d{3})",
                ln.strip(),
            )
            if m:
                start = _vtt_ts(m[1], m[2], m[3], m[4])
                end = _vtt_ts(m[5], m[6], m[7], m[8])
                txt = " ".join(
                    re.sub(r"<[^>]+>", "", l.strip())
                    for l in lines[i + 1 :] if l.strip()
                )
                txt = html.unescape(txt)
                if txt:
                    segs.append(_seg(start, end - start, txt))
                break
    return segs


def parse_xml(raw):
    """Parse YouTube XML transcript (<transcript> or srv3 <timedtext>)."""
    segs = []
    root = ET.fromstring(raw)

    # Format 1: <transcript><text start="..." dur="...">...</text>
    for el in root.findall(".//text"):
        t = html.unescape((el.text or "").strip())
        if t:
            segs.append(
                _seg(float(el.get("start", 0)), float(el.get("dur", 0)), t)
            )

    # Format 2 (srv3): <timedtext><body><p t="ms" d="ms"><s>...</s></p>
    if not segs:
        for p in root.findall(".//p"):
            parts = [s.text or "" for s in p.findall(".//s")]
            if not parts and p.text:
                parts = [p.text]
            t = html.unescape("".join(parts).strip())
            if t:
                segs.append(
                    _seg(int(p.get("t", 0)) / 1000, int(p.get("d", 0)) / 1000, t)
                )
    return segs


def parse_json3(raw):
    """Parse YouTube JSON3 caption format."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    segs = []
    for ev in data.get("events", []):
        parts = [s.get("utf8", "") for s in ev.get("segs", [])]
        t = "".join(parts).strip()
        if t and t != "\n":
            segs.append(
                _seg(
                    ev.get("tStartMs", 0) / 1000,
                    ev.get("dDurationMs", 0) / 1000,
                    t,
                )
            )
    return segs


def parse_captions(raw):
    """Auto-detect caption format and parse."""
    raw = raw.strip()
    if raw.startswith("<?xml") or raw.startswith("<transcript") or raw.startswith("<timedtext"):
        return parse_xml(raw)
    if raw.startswith("{"):
        try:
            return parse_json3(raw)
        except Exception:
            pass
    return parse_vtt(raw)


def dedup(segs):
    """Remove consecutive segments with identical text."""
    if not segs:
        return segs
    out = [segs[0]]
    for s in segs[1:]:
        if s["text"] != out[-1]["text"]:
            out.append(s)
    return out


def pick(caps, lang="en"):
    """Choose the best caption track (prefer exact language match)."""
    for c in caps:
        if c.get("languageCode", c.get("code", "")) == lang:
            return c
    for c in caps:
        if c.get("languageCode", c.get("code", "")).startswith(lang):
            return c
    return caps[0] if caps else None


# ── Strategy 1: YouTube Innertube API ────────────────────────────────────────

# Public innertube API key (embedded in YouTube's web player JS)
_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_INNERTUBE_URL = "https://www.youtube.com/youtubei/v1/player"

# Client configs to try (different clients have different blocking rules)
_INNERTUBE_CLIENTS = [
    {
        "clientName": "WEB",
        "clientVersion": "2.20250101.00.00",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    },
    {
        "clientName": "ANDROID",
        "clientVersion": "19.09.37",
        "androidSdkVersion": 30,
        "userAgent": "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip",
    },
    {
        "clientName": "IOS",
        "clientVersion": "19.09.3",
        "deviceModel": "iPhone14,3",
        "userAgent": "com.google.ios.youtube/19.09.3 (iPhone14,3; U; CPU iOS 15_6 like Mac OS X)",
    },
]


def try_innertube(vid):
    """Fetch transcript via YouTube's own innertube player API.

    This calls the same internal API that YouTube's apps use.
    It may work from cloud IPs where the website is blocked.
    """
    for client in _INNERTUBE_CLIENTS:
        try:
            payload = {
                "videoId": vid,
                "context": {
                    "client": {
                        "clientName": client["clientName"],
                        "clientVersion": client["clientVersion"],
                        "hl": "en",
                        "gl": "US",
                    }
                },
            }

            # Add optional client-specific fields
            ctx = payload["context"]["client"]
            for field in ("androidSdkVersion", "deviceModel"):
                if field in client:
                    ctx[field] = client[field]

            headers = {
                "Content-Type": "application/json",
                "User-Agent": client.get("userAgent", ""),
                "X-Goog-Api-Key": _INNERTUBE_KEY,
            }

            r = requests.post(
                f"{_INNERTUBE_URL}?key={_INNERTUBE_KEY}",
                json=payload,
                headers=headers,
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                continue

            data = r.json()

            # Extract caption tracks from player response
            tracks = (
                data.get("captions", {})
                .get("playerCaptionsTracklistRenderer", {})
                .get("captionTracks", [])
            )
            if not tracks:
                continue

            # Pick best caption track
            chosen = None
            for t in tracks:
                if t.get("languageCode", "") == "en":
                    chosen = t
                    break
            if not chosen:
                for t in tracks:
                    if t.get("languageCode", "").startswith("en"):
                        chosen = t
                        break
            if not chosen:
                chosen = tracks[0]

            # Fetch caption content (prefer json3 format for easier parsing)
            cap_url = chosen.get("baseUrl", "")
            if not cap_url:
                continue
            if "fmt=" not in cap_url:
                cap_url += "&fmt=json3"

            cr = requests.get(
                cap_url,
                timeout=TIMEOUT,
                headers={"User-Agent": client.get("userAgent", "")},
            )
            if cr.status_code != 200:
                continue

            segs = dedup(parse_captions(cr.text))
            if not segs:
                continue

            label = chosen.get("name", {})
            if isinstance(label, dict):
                label = label.get("simpleText", "")
            kind = chosen.get("kind", "")

            return dict(
                video_id=vid,
                language=label or chosen.get("languageCode", ""),
                language_code=chosen.get("languageCode", ""),
                is_generated=kind == "asr",
                segments=segs,
                full_text=" ".join(s["text"] for s in segs),
                source=f"innertube ({client['clientName']})",
            )
        except Exception as e:
            log.debug("innertube %s: %s", client["clientName"], e)
    return None


# ── Strategy 2: Invidious ────────────────────────────────────────────────────


def try_invidious(vid):
    """Fetch transcript via Invidious public API instances."""
    for base in INVIDIOUS:
        try:
            r = requests.get(
                f"{base}/api/v1/captions/{vid}",
                timeout=TIMEOUT,
                headers={"Accept": "application/json"},
            )
            if r.status_code != 200:
                continue

            caps = r.json().get("captions", [])
            if not caps:
                continue

            ch = pick(caps)
            if not ch:
                continue

            url = ch.get("url", "")
            if url.startswith("/"):
                url = base + url

            cr = requests.get(url, timeout=TIMEOUT)
            if cr.status_code != 200:
                continue

            segs = dedup(parse_captions(cr.text))
            if not segs:
                continue

            label = ch.get("label", "")
            return dict(
                video_id=vid,
                language=label or ch.get("languageCode", ""),
                language_code=ch.get("languageCode", ""),
                is_generated="auto" in label.lower(),
                segments=segs,
                full_text=" ".join(s["text"] for s in segs),
                source="invidious",
            )
        except Exception as e:
            log.debug("invidious %s: %s", base, e)
    return None


# ── Strategy 3: Piped ────────────────────────────────────────────────────────


def try_piped(vid):
    """Fetch transcript via Piped public API instances."""
    for base in PIPED:
        try:
            r = requests.get(f"{base}/streams/{vid}", timeout=TIMEOUT)
            if r.status_code != 200:
                continue

            subs = r.json().get("subtitles", [])
            if not subs:
                continue

            ch = pick(subs)
            if not ch:
                continue

            url = ch.get("url", "")
            if not url:
                continue

            sr = requests.get(url, timeout=TIMEOUT)
            if sr.status_code != 200:
                continue

            segs = dedup(parse_captions(sr.text))
            if not segs:
                continue

            return dict(
                video_id=vid,
                language=ch.get("code", ""),
                language_code=ch.get("code", ""),
                is_generated=ch.get("autoGenerated", False),
                segments=segs,
                full_text=" ".join(s["text"] for s in segs),
                source="piped",
            )
        except Exception as e:
            log.debug("piped %s: %s", base, e)
    return None


# ── Strategy 4: Direct (local dev or residential proxy) ──────────────────────


def try_direct(vid):
    """Fetch transcript via youtube_transcript_api (works locally or with proxy)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        ytt = YouTubeTranscriptApi()

        kw = {}
        if PROXY_URL:
            kw["proxies"] = {"http": PROXY_URL, "https": PROXY_URL}

        # list() may or may not accept proxies depending on library version
        try:
            tl = ytt.list(vid, **kw)
        except TypeError:
            tl = ytt.list(vid)

        meta = None
        try:
            meta = tl.find_manually_created_transcript()
        except Exception:
            try:
                meta = tl.find_generated_transcript()
            except Exception:
                for t in tl:
                    meta = t
                    break

        if not meta:
            return None

        fetched = meta.fetch()
        segs = [_seg(sn.start, sn.duration, sn.text) for sn in fetched.snippets]

        return dict(
            video_id=vid,
            language=fetched.language,
            language_code=fetched.language_code,
            is_generated=fetched.is_generated,
            segments=segs,
            full_text=" ".join(s["text"] for s in segs),
            source="direct" + (" (proxy)" if PROXY_URL else ""),
        )
    except Exception as e:
        log.debug("direct: %s", e)
        return None


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

    strategies = [
        ("Innertube", try_innertube),
        ("Invidious", try_invidious),
        ("Piped", try_piped),
        ("Direct", try_direct),
    ]

    errors = []
    for name, fn in strategies:
        try:
            result = fn(vid)
            if result:
                return jsonify(result)
            errors.append(f"{name}: no transcript found")
        except Exception as e:
            errors.append(f"{name}: {e}")

    return jsonify(
        error="Could not fetch transcript from any source. "
              "The video may not have captions, or services are temporarily unavailable.",
        details=errors,
    ), 500


@app.route("/api/health")
def health():
    """Debug endpoint showing configured strategies."""
    return jsonify(
        strategies=["innertube", "invidious", "piped", "direct"],
        invidious_instances=INVIDIOUS,
        piped_instances=PIPED,
        proxy_configured=bool(PROXY_URL),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
