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
    // Persist so a closed-then-reopened popup still sees the latest state.
    try { chrome.storage.local.set({ v2pStatus: msg, v2pRunning: running }); } catch (e) {}
    // Best-effort live update to an open popup (fails silently when closed).
    try { chrome.runtime.sendMessage({ type: "v2p-status", status: msg }); } catch (e) {}
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

  // How long the video must stay paused before we auto-stop the capture.
  // Short pauses (buffer, scrubbing) shouldn't kill an in-progress run.
  const PAUSE_AUTOSTOP_MS = 10_000;

  // Live capture state for stop-button / mark-button handling from the popup.
  let activeRec = null;
  let stopRequested = false;
  const manualMarks = []; // {timestamp_s, image_b64} — merged into captureFrames

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
    activeRec = rec;
    stopRequested = false;
    const chunks = [];
    rec.ondataavailable = (e) => e.data && e.data.size && chunks.push(e.data);

    const stopped = new Promise((res) => (rec.onstop = res));

    // Ensure the video is actually playing so audio flows.
    if (video.paused) {
      try { await video.play(); } catch (e) {}
    }

    rec.start(CAPTURE_TIMESLICE_MS);
    setStatus("recording audio… click Stop when done, or just pause the video");

    // Record until any of: (1) natural end, (2) explicit stop request from
    // popup/background, (3) user paused the video and left it paused for
    // PAUSE_AUTOSTOP_MS. Short scrubs/buffers don't kill the capture.
    await new Promise((resolve) => {
      let pauseTimer = null;
      const cleanup = () => {
        clearTimeout(pauseTimer);
        video.removeEventListener("ended", onEnded);
        video.removeEventListener("pause", onPause);
        video.removeEventListener("play", onPlay);
      };
      const onEnded = () => { cleanup(); resolve("ended"); };
      const onPause = () => {
        setStatus("paused — will finalize in 10s if not resumed. (Or click Stop.)");
        pauseTimer = setTimeout(() => { cleanup(); resolve("paused-timeout"); },
          PAUSE_AUTOSTOP_MS);
      };
      const onPlay = () => {
        clearTimeout(pauseTimer); pauseTimer = null;
        setStatus("recording audio… click Stop when done, or just pause the video");
      };
      const stopPoll = setInterval(() => {
        if (stopRequested) {
          clearInterval(stopPoll); cleanup(); resolve("stop-requested");
        }
      }, 250);
      if (video.ended) { clearInterval(stopPoll); return resolve("ended"); }
      video.addEventListener("ended", onEnded, { once: true });
      video.addEventListener("pause", onPause);
      video.addEventListener("play", onPlay);
      if (video.paused) onPause();  // already paused when we started
    });

    try { rec.stop(); } catch (e) {}
    await stopped;
    activeRec = null;

    const blob = new Blob(chunks, { type: mime });
    if (!blob.size) throw new Error("no audio captured (stopped too early?)");
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
    // Captures auto-selected timestamps from Whisper.
    for (let i = 0; i < timestamps.length; i++) {
      const ts = timestamps[i];
      setStatus(`capturing frame ${i + 1}/${timestamps.length} @ ${ts.toFixed(1)}s…`);
      try {
        const image_b64 = await captureFrameAt(video, ts);
        frames.push({ timestamp_s: ts, image_b64 });
      } catch (e) {
        console.warn("v2p frame capture failed at", ts, e);
      }
    }
    // Adds user's manual marks from the popup button (captured during Pass 1).
    // We already have the image_b64; just append.
    if (manualMarks.length > 0) {
      setStatus(`adding ${manualMarks.length} manual mark(s)…`);
      frames.push(...manualMarks);
    }
    // Dedupe by timestamp (rounded to 0.1s) — user may have marked a frame
    // that was also auto-selected. Keep the manual one (pushed last wins).
    const seenTs = new Set();
    const deduped = [];
    for (const f of frames.slice().reverse()) {
      const key = Math.round(f.timestamp_s * 10);
      if (seenTs.has(key)) continue;
      seenTs.add(key);
      deduped.push(f);
    }
    deduped.reverse();  // restore original order
    if (!deduped.length) throw new Error("no frames captured");
    setStatus("uploading frames…");
    const result = await post("/api/frames", { metadata, frames: deduped });
    return result;
  }

  // ── orchestration ─────────────────────────────────────────────────

  let running = false;

  async function runCapture() {
    if (running) return { ok: false, error: "already running" };
    running = true;
    manualMarks.length = 0;
    stopRequested = false;
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
      try {
        chrome.storage.local.set({
          v2pLast: { ok: true, result, status: window.__v2pStatus },
          v2pRunning: false,
        });
      } catch (e) {}
      return { ok: true, result };
    } catch (e) {
      const msg = String(e && e.message ? e.message : e);
      setStatus("error: " + msg);
      try {
        chrome.storage.local.set({
          v2pLast: { ok: false, error: msg, status: window.__v2pStatus },
          v2pRunning: false,
        });
      } catch (_) {}
      return { ok: false, error: msg };
    } finally {
      running = false;
    }
  }

  // ── messages from popup/background ────────────────────────────────

  // Manual-mark relay: the review page (a DIFFERENT document on localhost:8765)
  // POSTs a mark *request* to the server; we poll the server for pending marks
  // while our YouTube tab is open, capture the frame, and POST the result to
  // /api/mark/complete. (A DOM CustomEvent can't cross documents, so the server
  // mediates the hand-off.)
  const MARK_POLL_MS = 2000;
  async function pollMarks() {
    try {
      const r = await fetch(SERVER + "/api/mark/pending");
      if (!r.ok) return;
      const data = await r.json().catch(() => ({}));
      const pend = (data && data.pending) || [];
      for (const mark of pend) {
        const ts = mark.timestamp_s;
        const video = getVideo();
        if (ts == null || !video) continue;
        try {
          const wasPaused = video.paused;
          if (!wasPaused) { try { video.pause(); } catch (e) {} }
          const image_b64 = await captureFrameAt(video, ts);
          await fetch(SERVER + "/api/mark/complete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: mark.id, timestamp_s: ts, image_b64 }),
          });
        } catch (e) {
          console.warn("v2p manual frame capture failed", e);
        }
      }
    } catch (e) {
      /* server down; stay quiet and retry next tick */
    }
  }
  setInterval(pollMarks, MARK_POLL_MS);

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "v2p-run") {
      runCapture().then(sendResponse);
      return true; // async response
    }
    if (msg && msg.type === "v2p-status-request") {
      sendResponse({ ok: true, status: window.__v2pStatus || "idle", running });
      return false;
    }
    if (msg && msg.type === "v2p-stop") {
      // Ask captureAudio() to finalize with whatever audio we've collected.
      stopRequested = true;
      sendResponse({ ok: running, wasRunning: running });
      return false;
    }
    if (msg && msg.type === "v2p-mark-current") {
      // Snapshot the CURRENT playhead frame and stash it in manualMarks[].
      // captureFrames() will merge these into the frames POST when Pass 2 runs.
      // Note: seeking the video during Pass 1 would corrupt the audio recording,
      // so we DON'T seek — we grab from the current playhead.
      (async () => {
        try {
          const video = getVideo();
          if (!video) throw new Error("no <video> element");
          if (!running) throw new Error("start a capture first (Analyze this video)");
          const ts = video.currentTime;
          // Draw current frame without seeking (safe during audio recording).
          const canvas = document.createElement("canvas");
          const scale = FRAME_HEIGHT / video.videoHeight;
          canvas.width = Math.round(video.videoWidth * scale);
          canvas.height = FRAME_HEIGHT;
          canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
          const image_b64 = canvas
            .toDataURL("image/png")
            .replace(/^data:image\/png;base64,/, "");
          manualMarks.push({ timestamp_s: ts, image_b64 });
          sendResponse({ ok: true, timestamp_s: ts, total: manualMarks.length });
        } catch (e) {
          sendResponse({ ok: false, error: String(e && e.message ? e.message : e) });
        }
      })();
      return true;
    }
    return false;
  });
})();
