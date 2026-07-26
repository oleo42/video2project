/* video2project Capture — background service worker.
 *
 * Thin relay: the popup asks it to trigger a capture on the active tab, and it
 * forwards the request to the content script there. Kept minimal — all real
 * work happens in the content script (page context) and the local server.
 */

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "v2p-run-active-tab") {
    (async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab || !tab.id) {
        sendResponse({ ok: false, error: "no active tab" });
        return;
      }
      if (!/youtube\.com\/watch/.test(tab.url || "")) {
        sendResponse({ ok: false, error: "not a YouTube watch page" });
        return;
      }
      try {
        const resp = await chrome.tabs.sendMessage(tab.id, { type: "v2p-run" });
        sendResponse(resp || { ok: false, error: "no response from page" });
      } catch (e) {
        sendResponse({
          ok: false,
          error:
            "content script not ready — reload the YouTube tab and try again",
        });
      }
    })();
    return true; // async response
  }
  return false;
});
