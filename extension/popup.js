/* video2project Capture — popup logic. */

const runBtn = document.getElementById("run");
const statusEl = document.getElementById("status");

function setStatus(text) {
  statusEl.textContent = text;
}

// Reflect live status pushed from the content script.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "v2p-status") {
    setStatus(msg.status);
  }
});

// On open, pull the current status from the active tab's content script.
(async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab && tab.id && /youtube\.com\/watch/.test(tab.url || "")) {
      const resp = await chrome.tabs.sendMessage(tab.id, {
        type: "v2p-status-request",
      });
      if (resp && resp.status) {
        setStatus(resp.status);
        runBtn.disabled = !!resp.running;
      }
    }
  } catch (e) {
    /* content script not present yet; ignore */
  }
})();

runBtn.addEventListener("click", () => {
  runBtn.disabled = true;
  setStatus("starting…");
  chrome.runtime.sendMessage({ type: "v2p-run-active-tab" }, (resp) => {
    runBtn.disabled = false;
    if (chrome.runtime.lastError) {
      setStatus("error: " + chrome.runtime.lastError.message);
      return;
    }
    if (!resp) {
      setStatus("error: no response");
      return;
    }
    if (resp.ok) {
      const r = resp.result || {};
      setStatus(
        `done ✓  ${r.n_frames || 0} frames, ${r.n_needs_human || 0} need human check`
      );
    } else {
      setStatus("error: " + (resp.error || "unknown"));
    }
  });
});
