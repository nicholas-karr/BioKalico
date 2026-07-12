// Blocks Mainsail's automatic recursive directory prefetch specifically for
// the "home" file-manager root (biokalico_extras/moonraker/home_root.py),
// without touching Mainsail's own code.
//
// Mainsail's Configure page eagerly builds a full file tree for every
// registered root: on receiving a directory listing, its Vuex store
// immediately fires a follow-up server.files.get_directory call for every
// subdirectory found, recursively. Harmless for small roots (config/gcodes/
// logs), but the "home" root points at $HOME, which can be arbitrarily
// large and deep - observed directly: 11,000+ of these calls in 11 seconds,
// each one running Moonraker's file_manager listing synchronously on its
// event loop, long enough to trip its "EVENT LOOP BLOCKED" watchdog and
// drop the websocket connection entirely.
//
// There's no clean hook into Mainsail's internal Vue/Vuex state to disable
// this at the source (no __vue_app__, no active devtools hook, no exposed
// store reference in this production build) - so this intercepts at the
// WebSocket transport instead, but precisely: it only suppresses a
// get_directory request for a home/* path when it was fired as a direct
// consequence of *just* receiving that path's *parent* directory's
// listing (the exact cause-and-effect relationship the auto-recursion
// code implements), not requests in general. A manual click happens well
// after the user has looked at the (already-rendered, collapsed) tree and
// decided to expand something, so it's never mistaken for this and always
// goes through - browsing works normally at any depth, it just isn't
// eagerly pre-fetched.
(function () {
  'use strict';

  const CAUSATION_WINDOW_MS = 250;
  // A response that never arrives (closed socket, server error, dropped
  // connection) would otherwise leave its entry in pendingHomeRequests
  // forever - sweep anything older than this on an interval instead.
  const PENDING_REQUEST_TTL_MS = 60000;
  const PENDING_SWEEP_INTERVAL_MS = 30000;
  // path -> timestamp its listing response was received
  const recentHomeResponses = new Map();

  function parseFrame(payload) {
    if (typeof payload !== 'string') return null;
    try {
      return JSON.parse(payload);
    } catch (_) {
      return null;
    }
  }

  function homePathOf(msg) {
    if (!msg || msg.method !== 'server.files.get_directory') return null;
    const path = msg.params && msg.params.path;
    if (typeof path !== 'string') return null;
    if (path !== 'home' && !path.startsWith('home/')) return null;
    return path;
  }

  function isAutoRecursiveFollowup(path) {
    const lastSlash = path.lastIndexOf('/');
    if (lastSlash === -1) return false; // "home" itself has no parent
    const parent = path.slice(0, lastSlash);
    const respondedAt = recentHomeResponses.get(parent);
    return respondedAt !== undefined && Date.now() - respondedAt < CAUSATION_WINDOW_MS;
  }

  function noteResponseIfHomeDirectory(payload) {
    const msg = parseFrame(payload);
    // JSON-RPC result frames don't echo back the method name, only the
    // request id - match against a pending-request map populated from the
    // request side instead.
    if (!msg || msg.id === undefined || !pendingHomeRequests.has(msg.id)) return;
    const { path } = pendingHomeRequests.get(msg.id);
    pendingHomeRequests.delete(msg.id);
    recentHomeResponses.set(path, Date.now());
  }

  // request id -> { path: home/* path, addedAt: timestamp }
  const pendingHomeRequests = new Map();

  // A request whose response never arrives (closed socket, server error,
  // dropped connection) would otherwise leak its entry for the life of the
  // page - periodically purge anything that's been sitting too long.
  setInterval(() => {
    const now = Date.now();
    for (const [id, entry] of pendingHomeRequests) {
      if (now - entry.addedAt > PENDING_REQUEST_TTL_MS) pendingHomeRequests.delete(id);
    }
  }, PENDING_SWEEP_INTERVAL_MS);

  const OrigWebSocket = window.WebSocket;

  function PatchedWebSocket(...args) {
    const ws = new OrigWebSocket(...args);

    const origSend = ws.send.bind(ws);
    ws.send = function (data) {
      const msg = parseFrame(data);
      const path = homePathOf(msg);
      if (path) {
        if (isAutoRecursiveFollowup(path)) {
          return; // drop: this is the eager auto-recursion, not a user action
        }
        if (msg.id !== undefined) pendingHomeRequests.set(msg.id, { path, addedAt: Date.now() });
      }
      return origSend(data);
    };

    ws.addEventListener('message', (ev) => noteResponseIfHomeDirectory(ev.data));

    return ws;
  }
  PatchedWebSocket.prototype = OrigWebSocket.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) {
    PatchedWebSocket[k] = OrigWebSocket[k];
  }
  window.WebSocket = PatchedWebSocket;
})();
