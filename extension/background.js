/* video2project Capture — background service worker.
 *
 * Thin relay: the popup asks it to trigger a capture on the active tab, and it
 * forwards the request to the content script there. Kept minimal — all real
 * work happens in the content script (page context) and the local server.
 */

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "v2p-run-active-tab") {
    // Fire-and-forget: we ack immediately, then dispatch to the content script
    // without awaiting its (multi-minute) response. The popup can close and
    // reopen; status is streamed via chrome.storage.local, not response.
    (async () => {
      try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab || !tab.id) {
          sendResponse({ ok: false, error: "no active tab" });
          return;
        }
        if (!/youtube\.com\/watch/.test(tab.url || "")) {
          sendResponse({ ok: false, error: "not a YouTube watch page" });
          return;
        }
        // Ack the popup right away.
        sendResponse({ ok: true, started: true });
        // Kick off the capture; ignore the eventual response — content.js
        // writes final result to chrome.storage.local ("v2pLast").
        try {
          await chrome.tabs.sendMessage(tab.id, { type: "v2p-run" });
        } catch (e) {
          // Content script may have gone away (tab reload). Store the error.
          await chrome.storage.local.set({
            v2pLast: { ok: false, error: "content script not ready — reload the YouTube tab and try again" },
          });
        }
      } catch (e) {
        try {
          sendResponse({ ok: false, error: String(e && e.message ? e.message : e) });
        } catch (_) { /* popup closed before ack landed; that's fine */ }
      }
    })();
    return true; // will call sendResponse (once) synchronously below
  }
  return false;
});
