/* video2project Capture — content script.
 *
 * Runs on the YouTube watch page. Does the two capture passes locally in the
 * trusted browser context (so the flagged/throttled local IP never touches
 * YouTube directly):
 *
 *   Pass 1: captureStream() → MediaRecorder records the audio of the playing
 *           <video> in real time → POST blob to the local server → server
 *           transcribes (Whisper) and returns candidate frame timestamps.
 *   Pass 2: seek to each returned timestamp and canvas.drawImage() the frame
 *           → POST PNGs to the local server → server OCRs + finalizes.
 *
 * The server is expected at http://localhost:8765 (video2project capture).
 */

(() => {
  const SERVER = "http://localhost:8765";
  const FRAME_HEIGHT = 720; // match pipeline's FRAME_HEIGHT
  const CAPTURE_TIMESLICE_MS = 1000; // MediaRecorder chunk cadence

  if (window.__v2pCaptureInjected) return;
  window.__v2pCaptureInjected = true;

  // ── helpers ───────────────────────────────────────────────────────

  function getVideo() {
    return document.querySelector("video");
  }

  function videoIdFromUrl() {
    const u = new URL(location.href);
    return u.searchParams.get("v") || "";
  }

  function readMetadata() {
    // ytInitialPlayerResponse carries title/channel/duration/upload date.
    let pr = null;
    try {
      pr = window.ytInitialPlayerResponse || null;
    } catch (e) {
      pr = null;
    }
    const vd = (pr && pr.videoDetails) || {};
    const micro = (pr && pr.microformat && pr.microformat.playerMicroformatRenderer) || {};
    const video = getVideo();
    return {
      platform: "youtube",
      video_id: videoIdFromUrl(),
      url: location.href.split("&")[0],
      title: vd.title || document.title.replace(/ - YouTube$/, ""),
      channel: vd.author || "",
      duration_s: Math.round(video && video.duration ? video.duration : (vd.lengthSeconds ? +vd.lengthSeconds : 0)),
      upload_date: (micro.uploadDate || "").replace(/-/g, ""),
    };
  }

  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onloadend = () => resolve(r.result); // data URL (server strips prefix)
      r.onerror = reject;
      r.readAsDataURL(blob);
    });
  }

  async function post(path, body) {
    const resp = await fetch(SERVER + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${resp.status} from ${path}`);
    }
    return data;
  }

  async function health() {
    try {
      const r = await fetch(SERVER + "/api/health");
      return r.ok;
    } catch (e) {
      return false;
    }
  }

  function setStatus(msg) {
    window.__v2pStatus = msg;
    // Best-effort popup update; popup reads on open.
    try {
      chrome.runtime.sendMessage({ type: "v2p-status", status: msg });
    } catch (e) {}
  }

  function waitForSeek(video, ts) {
    return new Promise((resolve) => {
      const onSeeked = () => {
        video.removeEventListener("seeked", onSeeked);
        // Small settle delay so the frame is fully rendered.
        setTimeout(resolve, 120);
      };
      video.addEventListener("seeked", onSeeked);
      video.currentTime = Math.min(ts, (video.duration || ts) - 0.05);
    });
  }

  async function captureFrameAt(video, ts) {
    await waitForSeek(video, ts);
    const scale = FRAME_HEIGHT / (video.videoHeight || FRAME_HEIGHT);
    const w = Math.round((video.videoWidth || 1280) * scale);
    const h = FRAME_HEIGHT;
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, w, h);
    const blob = await new Promise((res) => canvas.toBlob(res, "image/png"));
    return blobToBase64(blob);
  }

  // ── Pass 1: audio capture ─────────────────────────────────────────

  async function captureAudio(video, metadata) {
    if (!video.captureStream) {
      throw new Error("video.captureStream() unavailable in this browser");
    }
    const stream = video.captureStream();
    const audioTracks = stream.getAudioTracks();
    if (!audioTracks.length) {
      throw new Error("no audio track on the video stream");
    }
    const audioStream = new MediaStream(audioTracks);
    const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus"
      : "audio/webm";
    const rec = new MediaRecorder(audioStream, { mimeType: mime });
    const chunks = [];
    rec.ondataavailable = (e) => e.data && e.data.size && chunks.push(e.data);

    const stopped = new Promise((res) => (rec.onstop = res));

    // Ensure the video is actually playing so audio flows.
    const wasPaused = video.paused;
    if (wasPaused) {
      try { await video.play(); } catch (e) {}
    }

    rec.start(CAPTURE_TIMESLICE_MS);
    setStatus("recording audio… (plays through in real time)");

    // Record until the video ends.
    await new Promise((resolve) => {
      if (video.ended) return resolve();
      video.addEventListener("ended", resolve, { once: true });
    });

    rec.stop();
    await stopped;

    const blob = new Blob(chunks, { type: mime });
    setStatus(`uploading audio (${(blob.size / 1e6).toFixed(1)} MB)…`);
    const audio_b64 = await blobToBase64(blob);

    const result = await post("/api/audio", { metadata, audio: audio_b64 });
    return result.timestamps || [];
  }

  // ── Pass 2: frame capture ─────────────────────────────────────────

  async function captureFrames(video, metadata, timestamps) {
    const wasPaused = video.paused;
    if (!wasPaused) {
      try { video.pause(); } catch (e) {}
    }
    const frames = [];
    for (let i = 0; i < timestamps.length; i++) {
      const ts = timestamps[i];
      setStatus(`capturing frame ${i + 1}/${timestamps.length} @ ${ts.toFixed(1)}s…`);
      try {
        const image_b64 = await captureFrameAt(video, ts);
        frames.push({ timestamp_s: ts, image_b64 });
      } catch (e) {
        // Skip a frame that failed to seek/capture rather than abort all.
        console.warn("v2p frame capture failed at", ts, e);
      }
    }
    if (!frames.length) {
      throw new Error("no frames captured");
    }
    setStatus("uploading frames…");
    const result = await post("/api/frames", { metadata, frames });
    return result;
  }

  // ── orchestration ─────────────────────────────────────────────────

  let running = false;

  async function runCapture() {
    if (running) return { ok: false, error: "already running" };
    running = true;
    try {
      if (!(await health())) {
        throw new Error(
          "local server not reachable — run `video2project capture` first"
        );
      }
      const video = getVideo();
      if (!video) throw new Error("no <video> element on this page");

      const metadata = readMetadata();
      if (!metadata.video_id) throw new Error("no video id in URL");

      setStatus("starting…");
      await post("/api/start", metadata);

      const timestamps = await captureAudio(video, metadata);
      setStatus(`transcribed; ${timestamps.length} frame timestamps`);

      const result = await captureFrames(video, metadata, timestamps);
      setStatus(
        `done: ${result.n_frames} frames, ${result.n_needs_human} need human check`
      );
      return { ok: true, result };
    } catch (e) {
      setStatus("error: " + (e && e.message ? e.message : String(e)));
      return { ok: false, error: String(e && e.message ? e.message : e) };
    } finally {
      running = false;
    }
  }

  // ── messages from popup/background ────────────────────────────────

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "v2p-run") {
      runCapture().then(sendResponse);
      return true; // async response
    }
    if (msg && msg.type === "v2p-status-request") {
      sendResponse({ ok: true, status: window.__v2pStatus || "idle", running });
      return false;
    }
    return false;
  });
})();
