/* video2project Capture — popup logic.
 *
 * The capture takes multiple minutes; the popup can't hold a live response
 * channel that long (Chrome tears down when the popup closes). So:
 *   1. click Analyze -> background dispatches capture -> popup gets ack (start)
 *   2. content script writes status/result to chrome.storage.local
 *   3. popup polls storage while open, re-renders whatever's current
 */

const runBtn = document.getElementById("run");
const stopBtn = document.getElementById("stop");
const markBtn = document.getElementById("mark");
const statusEl = document.getElementById("status");

let pollTimer = null;

function setStatus(text) { statusEl.textContent = text; }

function setRunningUI(running) {
  runBtn.disabled = running;
  stopBtn.style.display = running ? "block" : "none";
  markBtn.style.display = running ? "block" : "none";
}

// Live in-popup update (only when popup is open).
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "v2p-status") setStatus(msg.status);
});

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id || !/youtube\.com\/watch/.test(tab.url || "")) return null;
  return tab;
}

// Render whatever state storage holds — works whether capture is running,
// done, or errored, and works even after the popup was closed for minutes.
async function renderFromStorage() {
  try {
    const { v2pStatus, v2pRunning, v2pLast } = await chrome.storage.local.get([
      "v2pStatus", "v2pRunning", "v2pLast",
    ]);
    setRunningUI(!!v2pRunning);
    // If we have a final result, prefer showing that; else current status.
    if (!v2pRunning && v2pLast) {
      if (v2pLast.ok) {
        const r = v2pLast.result || {};
        setStatus(`done ✓  ${r.n_frames || 0} frames, ${r.n_needs_human || 0} need human check`);
      } else {
        setStatus("error: " + (v2pLast.error || "unknown"));
      }
    } else if (v2pStatus) {
      setStatus(v2pStatus);
    }
  } catch (e) {
    /* storage denied? ignore */
  }
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(renderFromStorage, 800);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

window.addEventListener("unload", stopPolling);

(async () => {
  // Initial paint from storage + ask content script for the live in-memory state
  await renderFromStorage();
  try {
    const tab = await getActiveTab();
    if (tab) {
      const resp = await chrome.tabs.sendMessage(tab.id, { type: "v2p-status-request" });
      if (resp && resp.status) {
        setStatus(resp.status);
        setRunningUI(!!resp.running);
      }
    }
  } catch (e) { /* content script may not be injected yet */ }
  startPolling();
})();

runBtn.addEventListener("click", () => {
  // Clear stale "done"/"error" so we don't confuse the user.
  chrome.storage.local.remove(["v2pLast"]);
  setRunningUI(true);
  setStatus("starting…");
  // Fire-and-forget: we only wait for the "started" ack, not the final result.
  chrome.runtime.sendMessage({ type: "v2p-run-active-tab" }, (resp) => {
    if (chrome.runtime.lastError) {
      setRunningUI(false);
      setStatus("error: " + chrome.runtime.lastError.message);
      return;
    }
    if (!resp) return setStatus("error: no ack from background");
    if (!resp.ok) {
      setRunningUI(false);
      setStatus("error: " + (resp.error || "unknown"));
      return;
    }
    // Ack received; the capture is now running in the content script.
    // The poller will render status/result updates.
    setStatus("running… (safe to close this popup)");
  });
});

stopBtn.addEventListener("click", async () => {
  stopBtn.disabled = true;
  setStatus("stopping — finalizing with captured audio…");
  try {
    const tab = await getActiveTab();
    if (!tab) throw new Error("open a YouTube watch tab first");
    await chrome.tabs.sendMessage(tab.id, { type: "v2p-stop" });
  } catch (e) {
    setStatus("stop failed: " + (e && e.message ? e.message : e));
  } finally {
    stopBtn.disabled = false;
  }
});

markBtn.addEventListener("click", async () => {
  markBtn.disabled = true;
  setStatus("marking current frame…");
  try {
    const tab = await getActiveTab();
    if (!tab) throw new Error("open a YouTube watch tab first");
    const resp = await chrome.tabs.sendMessage(tab.id, { type: "v2p-mark-current" });
    if (resp && resp.ok) {
      setStatus(`marked frame @ ${resp.timestamp_s.toFixed(1)}s ✓  (${resp.total} total)`);
    } else {
      setStatus("mark failed: " + (resp && resp.error ? resp.error : "unknown"));
    }
  } catch (e) {
    setStatus("mark failed: " + (e && e.message ? e.message : e));
  } finally {
    markBtn.disabled = false;
  }
});
