/**
 * YouTube Transcript Proxy — Google Apps Script (v2)
 *
 * This runs on Google's own infrastructure. It fetches the YouTube
 * watch page HTML (like a normal browser), extracts caption track
 * URLs from the embedded player response, and fetches the captions.
 *
 * ── SETUP (5 minutes) ──────────────────────────────────────────
 *
 *  1. Go to https://script.google.com
 *  2. Click "New project"
 *  3. Delete the default code and paste THIS ENTIRE FILE
 *  4. Click  Deploy  ▸  New deployment
 *  5. Click the gear icon ⚙ next to "Select type" → choose "Web app"
 *  6. Set:
 *       • Description:  YouTube Transcript Proxy
 *       • Execute as:   Me
 *       • Who has access:  Anyone
 *  7. Click "Deploy"
 *  8. Click "Authorize access" → choose your Google account → Allow
 *  9. Copy the "Web app URL" (looks like https://script.google.com/macros/s/XXXX/exec)
 * 10. In Vercel → your project → Settings → Environment Variables:
 *       • Name:  GAS_PROXY_URL
 *       • Value: <paste the URL from step 9>
 *     Click Save, then redeploy.
 *
 * ── UPDATE EXISTING DEPLOYMENT ─────────────────────────────────
 *
 *  If you already deployed v1 and just need to update the code:
 *  1. Paste this new code over the old code
 *  2. Click  Deploy  ▸  Manage deployments
 *  3. Click the pencil ✏ icon on your existing deployment
 *  4. Under "Version", select "New version"
 *  5. Click "Deploy"
 *  (The URL stays the same — no need to update Vercel env vars)
 *
 * ────────────────────────────────────────────────────────────────
 */


/* ── Entry point: POST {videoId: "dQw4w9WgXcQ"} ── */

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    var videoId = body.videoId;

    if (!videoId) {
      return respond({ error: "Missing videoId" });
    }

    // Strategy 1: Watch page HTML scraping (most reliable)
    var result = tryWatchPage(videoId);
    if (result.success) {
      return respond(result.data);
    }

    // Strategy 2: Innertube API on youtube.com (WEB client)
    var result2 = tryInnertube(videoId);
    if (result2.success) {
      return respond(result2.data);
    }

    var errorMsg = "No captions found for this video.";
    if (result.error)  errorMsg += " Watch page: " + result.error + ".";
    if (result2.error) errorMsg += " Innertube: " + result2.error + ".";

    return respond({ error: errorMsg });

  } catch (err) {
    return respond({ error: "Script error: " + err.message });
  }
}


/* ── Strategy 1: Watch page HTML scraping ── */

function tryWatchPage(videoId) {
  try {
    var url = "https://www.youtube.com/watch?v=" + videoId + "&hl=en";
    var resp = UrlFetchApp.fetch(url, {
      muteHttpExceptions: true,
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODE1LjA3X3AxGgJlbiACGgYIgJnOlwY; CONSENT=PENDING+987"
      },
      followRedirects: true
    });

    var code = resp.getResponseCode();
    if (code !== 200) {
      return { success: false, error: "HTTP " + code };
    }

    var html = resp.getContentText();

    // Check for bot detection page
    if (html.indexOf("Sign in to confirm") !== -1 ||
        html.indexOf("confirm you're not a bot") !== -1) {
      return { success: false, error: "Bot detection triggered" };
    }

    // Extract ytInitialPlayerResponse
    var marker = "ytInitialPlayerResponse";
    var idx = html.indexOf(marker);
    if (idx === -1) {
      return { success: false, error: "ytInitialPlayerResponse not found in page" };
    }

    // Find the JSON object start
    var jsonStart = html.indexOf("{", idx);
    if (jsonStart === -1 || jsonStart > idx + 200) {
      return { success: false, error: "Could not locate JSON start" };
    }

    // Extract JSON by brace matching
    var jsonStr = extractJson(html, jsonStart);
    if (!jsonStr) {
      return { success: false, error: "Could not extract player JSON" };
    }

    var player = JSON.parse(jsonStr);

    // Check playability
    var ps = player.playabilityStatus || {};
    if (ps.status && ps.status !== "OK") {
      return { success: false, error: ps.reason || ps.status };
    }

    // Get caption tracks
    var tracks = [];
    try {
      tracks = player.captions.playerCaptionsTracklistRenderer.captionTracks;
    } catch (ex) { /* no captions object */ }

    if (!tracks || tracks.length === 0) {
      return { success: false, error: "No caption tracks in player response" };
    }

    // Pick best track and fetch content
    return fetchCaptions(videoId, tracks, "watch-page");

  } catch (err) {
    return { success: false, error: err.message };
  }
}


/* ── Strategy 2: Innertube API (WEB client on youtube.com) ── */

function tryInnertube(videoId) {
  try {
    var endpoint = "https://www.youtube.com/youtubei/v1/player?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8&prettyPrint=false";
    var payload = {
      context: {
        client: {
          clientName: "WEB",
          clientVersion: "2.20260220.01.00",
          hl: "en",
          gl: "US"
        }
      },
      videoId: videoId
    };

    var resp = UrlFetchApp.fetch(endpoint, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Cookie": "SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODE1LjA3X3AxGgJlbiACGgYIgJnOlwY; CONSENT=PENDING+987"
      }
    });

    if (resp.getResponseCode() !== 200) {
      return { success: false, error: "HTTP " + resp.getResponseCode() };
    }

    var data = JSON.parse(resp.getContentText());
    var ps = data.playabilityStatus || {};

    if (ps.status !== "OK") {
      return { success: false, error: ps.reason || ps.status || "Not OK" };
    }

    var tracks = [];
    try {
      tracks = data.captions.playerCaptionsTracklistRenderer.captionTracks;
    } catch (ex) { /* no captions */ }

    if (!tracks || tracks.length === 0) {
      return { success: false, error: "No caption tracks" };
    }

    return fetchCaptions(videoId, tracks, "innertube-WEB");

  } catch (err) {
    return { success: false, error: err.message };
  }
}


/* ── Fetch caption content from a list of tracks ── */

function fetchCaptions(videoId, tracks, source) {
  var track = pickTrack(tracks);
  if (!track || !track.baseUrl) {
    return { success: false, error: "No suitable track with URL" };
  }

  var baseUrl = track.baseUrl;

  // Strip exp=xpe parameter that can cause empty responses
  baseUrl = baseUrl.replace(/[&?]exp=[^&]*/g, "");
  baseUrl = baseUrl.replace(/(sparams=[^&]*)(?:,exp|exp,)/g, "$1");

  // Try json3 format first, then raw
  var formats = ["json3", "srv3", ""];
  for (var f = 0; f < formats.length; f++) {
    var url = baseUrl;
    var fmt = formats[f];
    if (fmt) {
      if (url.indexOf("fmt=") !== -1) {
        url = url.replace(/fmt=[^&]*/, "fmt=" + fmt);
      } else {
        url += "&fmt=" + fmt;
      }
    }

    try {
      var capResp = UrlFetchApp.fetch(url, {
        muteHttpExceptions: true,
        headers: {
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        }
      });

      if (capResp.getResponseCode() !== 200) continue;

      var content = capResp.getContentText();
      if (!content || content.trim().length === 0) continue;

      // Verify content has actual caption data
      if (fmt === "json3") {
        var parsed = JSON.parse(content);
        if (!parsed.events || parsed.events.length === 0) continue;
      }

      return {
        success: true,
        data: {
          track: {
            languageCode: track.languageCode || "",
            name:         track.name || {},
            kind:         track.kind || ""
          },
          content:    content,
          format:     fmt || "default",
          client:     source,
          trackCount: tracks.length
        }
      };
    } catch (ex) {
      // try next format
    }
  }

  return { success: false, error: "All caption format fetches failed" };
}


/* ── Extract JSON object from HTML by brace matching ── */

function extractJson(html, startIdx) {
  var depth = 0;
  var inStr = false;
  var esc = false;
  var i = startIdx;
  var len = html.length;

  // Safety limit: don't scan more than 2MB
  var maxScan = Math.min(len, startIdx + 2000000);

  while (i < maxScan) {
    var c = html.charAt(i);

    if (esc) {
      esc = false;
      i++;
      continue;
    }

    if (c === "\\" && inStr) {
      esc = true;
      i++;
      continue;
    }

    if (c === '"') {
      inStr = !inStr;
    } else if (!inStr) {
      if (c === "{") {
        depth++;
      } else if (c === "}") {
        depth--;
        if (depth === 0) {
          return html.substring(startIdx, i + 1);
        }
      }
    }
    i++;
  }
  return null;
}


/* ── Pick the best caption track ── */

function pickTrack(tracks) {
  // 1. Manual English
  for (var i = 0; i < tracks.length; i++) {
    if (tracks[i].languageCode === "en" && tracks[i].kind !== "asr") return tracks[i];
  }
  // 2. Auto-generated English
  for (var i = 0; i < tracks.length; i++) {
    if (tracks[i].languageCode === "en") return tracks[i];
  }
  // 3. Any English variant
  for (var i = 0; i < tracks.length; i++) {
    if ((tracks[i].languageCode || "").indexOf("en") === 0) return tracks[i];
  }
  // 4. First available
  return tracks[0] || null;
}


/* ── JSON response helper ── */

function respond(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}


/* ── GET handler (health check / quick test) ── */

function doGet(e) {
  var videoId = (e && e.parameter && e.parameter.v) ? e.parameter.v : null;

  if (videoId) {
    // Quick test: visit ...exec?v=dQw4w9WgXcQ in browser
    var result = tryWatchPage(videoId);
    if (!result.success) {
      result = tryInnertube(videoId);
    }
    if (result.success) {
      return respond({
        status: "ok",
        videoId: videoId,
        client: result.data.client,
        trackCount: result.data.trackCount,
        language: result.data.track.languageCode,
        contentLength: result.data.content.length
      });
    }
    return respond({ status: "error", videoId: videoId, error: result.error });
  }

  return respond({
    status: "ok",
    message: "YouTube Transcript Proxy v2 is running. POST {videoId: '...'} to fetch transcripts. GET ?v=VIDEO_ID to test."
  });
}
