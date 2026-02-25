/**
 * YouTube Transcript Proxy — Google Apps Script
 *
 * This runs on Google's own infrastructure, so YouTube does NOT
 * block it (Google won't block itself).
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
 * ────────────────────────────────────────────────────────────────
 */

var API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8";

var CLIENTS = [
  {
    name: "ANDROID",
    version: "20.10.38",
    ua: "com.google.android.youtube/19.09.37 (Linux; U; Android 14) gzip",
    extra: { androidSdkVersion: 30 }
  },
  {
    name: "ANDROID_VR",
    version: "1.57.29",
    ua: "com.google.android.youtube/19.09.37 (Linux; U; Android 14) gzip",
    extra: { androidSdkVersion: 30 }
  },
  {
    name: "IOS",
    version: "19.45.4",
    ua: "com.google.ios.youtube/19.45.4 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X)",
    extra: { deviceModel: "iPhone16,2" }
  }
];


/* ── Entry point: POST {videoId: "dQw4w9WgXcQ"} ── */

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    var videoId = body.videoId;

    if (!videoId) {
      return respond({ error: "Missing videoId" });
    }

    for (var i = 0; i < CLIENTS.length; i++) {
      var result = tryClient(videoId, CLIENTS[i]);
      if (result.success) {
        return respond(result.data);
      }
    }

    return respond({ error: "No captions found for this video" });

  } catch (err) {
    return respond({ error: err.message });
  }
}


/* ── Try one innertube client ── */

function tryClient(videoId, client) {
  var endpoint = "https://www.googleapis.com/youtubei/v1/player?key=" + API_KEY;

  var ctx = {
    clientName: client.name,
    clientVersion: client.version,
    hl: "en",
    gl: "US"
  };
  // merge extra fields (androidSdkVersion, deviceModel, etc.)
  for (var k in client.extra) {
    ctx[k] = client.extra[k];
  }

  var payload = {
    context: { client: ctx },
    videoId: videoId
  };

  var resp = UrlFetchApp.fetch(endpoint, {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
    headers: { "User-Agent": client.ua }
  });

  if (resp.getResponseCode() !== 200) {
    return { success: false };
  }

  var data = JSON.parse(resp.getContentText());

  if (!data.playabilityStatus || data.playabilityStatus.status !== "OK") {
    return { success: false };
  }

  var tracks = [];
  try {
    tracks = data.captions.playerCaptionsTracklistRenderer.captionTracks;
  } catch (ex) { /* no captions */ }

  if (!tracks || tracks.length === 0) {
    return { success: false };
  }

  var track = pickTrack(tracks);
  if (!track || !track.baseUrl) {
    return { success: false };
  }

  // Fetch the actual caption text (json3 format)
  var capUrl = track.baseUrl + "&fmt=json3";
  var capResp = UrlFetchApp.fetch(capUrl, { muteHttpExceptions: true });

  if (capResp.getResponseCode() !== 200) {
    return { success: false };
  }

  var content = capResp.getContentText();
  if (!content || content.trim().length === 0) {
    return { success: false };
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
      format:     "json3",
      client:     client.name,
      trackCount: tracks.length
    }
  };
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


/* ── GET handler (health check) ── */

function doGet() {
  return respond({
    status: "ok",
    message: "YouTube Transcript Proxy is running. POST {videoId: '...'} to fetch transcripts."
  });
}
