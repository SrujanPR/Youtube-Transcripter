from flask import Flask, request, jsonify, send_from_directory
from youtube_transcript_api import YouTubeTranscriptApi
import re

app = Flask(__name__, static_folder="static")
ytt_api = YouTubeTranscriptApi()


def extract_video_id(url: str) -> str | None:
    """Extract the YouTube video ID from various URL formats."""
    patterns = [
        r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url.strip())
        if match:
            return match.group(1)
    return None


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/transcript", methods=["POST"])
def get_transcript():
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "Please provide a YouTube video URL."}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid YouTube URL. Please check and try again."}), 400

    try:
        transcript_list = ytt_api.list(video_id)

        # Try to get manually created transcript first, then auto-generated
        transcript_meta = None
        try:
            transcript_meta = transcript_list.find_manually_created_transcript()
        except Exception:
            try:
                transcript_meta = transcript_list.find_generated_transcript()
            except Exception:
                pass

        if transcript_meta is None:
            # Fallback: grab whatever is available
            for t in transcript_list:
                transcript_meta = t
                break

        if transcript_meta is None:
            return jsonify({"error": "No transcript available for this video."}), 404

        fetched = transcript_meta.fetch()

        # Build structured response from FetchedTranscript
        segments = []
        full_text_parts = []
        for snippet in fetched.snippets:
            start = snippet.start
            duration = snippet.duration
            text = snippet.text

            mins = int(start // 60)
            secs = int(start % 60)
            timestamp = f"{mins:02d}:{secs:02d}"

            segments.append({
                "timestamp": timestamp,
                "start": round(start, 2),
                "duration": round(duration, 2),
                "text": text,
            })
            full_text_parts.append(text)

        return jsonify({
            "video_id": video_id,
            "language": fetched.language,
            "language_code": fetched.language_code,
            "is_generated": fetched.is_generated,
            "segments": segments,
            "full_text": " ".join(full_text_parts),
        })

    except Exception as e:
        error_msg = str(e)
        if "disabled" in error_msg.lower():
            return jsonify({"error": "Transcripts are disabled for this video."}), 403
        if "no transcript" in error_msg.lower():
            return jsonify({"error": "No transcript found for this video."}), 404
        return jsonify({"error": f"Failed to fetch transcript: {error_msg}"}), 500


if __name__ == "__main__":
    app.run(debug=False)
