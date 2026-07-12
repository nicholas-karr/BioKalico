// Auto-reconnect for Mainsail's "Connection Lost" dialog.
//
// Mainsail's own websocket client gives up automatically after just 2
// failed reconnect attempts, about a second apart (its Vuex `socket`
// module state: `maxReconnects: 2, reconnectInterval: 1000`), then leaves
// up a dialog with a "Try Again" button that a human has to click by hand
// to make it try again. Klippy restarts, brief network blips, and MCU
// firmware restarts on the printer host all burn through that 2-attempt
// budget in under 2 seconds, so on a host that isn't rock-solid the
// dialog shows up (and just sits there) far more than it needs to.
//
// There's no exposed Vue app or Vuex store reference in this production
// build to just raise maxReconnects at the source (same constraint noted
// in home-root-throttle.js), so this watches for the dialog in the DOM
// and clicks its button for you instead, the same click a human would
// make, just automatic. The dialog component is only ever mounted in the
// DOM while disconnected (Mainsail's own root wraps it in a v-if) and
// carries a fixed, non-translated `the-connection-dialog` card-class,
// which is matched here instead of relying on the dialog itself having a
// stable structure. Within it, the retry button is preferred by its
// visible text ("Try Again"/"Retry", case-insensitive) and only falls
// back to "first enabled button" if that text isn't found. Nothing
// outside this specific dialog is ever touched (other confirm/delete
// dialogs elsewhere in the app are left alone).
(function () {
  'use strict';

  const MIN_CLICK_INTERVAL_MS = 1500;
  const MAX_CLICK_INTERVAL_MS = 18000;
  const BACKOFF_MULTIPLIER = 1.5;
  let lastClickAt = 0;
  // Increasing backoff between clicks while the dialog stays up (i.e. each
  // previous click didn't reconnect us) - reset to the fast interval as
  // soon as the dialog is gone again (connection recovered).
  let currentIntervalMs = MIN_CLICK_INTERVAL_MS;

  // Prefer the button by its visible text ("Try Again" / "Retry"), so an
  // unrelated second enabled button (e.g. a dismiss/close action) never
  // gets clicked instead. Falls back to the first enabled button if no
  // text match is found, in case Mainsail's wording changes.
  function findRetryButton(dialog) {
    const enabled = Array.from(dialog.querySelectorAll('button:not([disabled])'));
    const byText = enabled.find((b) => /retry|try again/i.test((b.textContent || '').trim()));
    return byText || enabled[0] || null;
  }

  function tryReconnect() {
    const dialog = document.querySelector('.the-connection-dialog');
    if (!dialog) {
      currentIntervalMs = MIN_CLICK_INTERVAL_MS; // not disconnected (or just recovered) - reset backoff
      return;
    }

    const btn = findRetryButton(dialog);
    if (!btn) return;

    const now = Date.now();
    if (now - lastClickAt < currentIntervalMs) return;
    lastClickAt = now;
    btn.click();
    currentIntervalMs = Math.min(currentIntervalMs * BACKOFF_MULTIPLIER, MAX_CLICK_INTERVAL_MS);
  }

  let debounce = null;
  new MutationObserver(() => {
    clearTimeout(debounce);
    debounce = setTimeout(tryReconnect, 150);
  }).observe(document.documentElement, { childList: true, subtree: true });

  // Mainsail flips back to its failed state within ~2s of a fresh
  // failure without necessarily producing a DOM mutation the observer
  // above is guaranteed to catch (e.g. while the dialog is mid-render),
  // so also poll as a backstop.
  setInterval(tryReconnect, 2000);
})();
