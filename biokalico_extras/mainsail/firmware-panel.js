// Firmware build/flash panel for Mainsail's Settings page.
// Backed by the firmware_build Moonraker component
// (biokalico_extras/moonraker/firmware_build.py). Polls
// /server/firmware/targets and /server/firmware/status; POSTs to
// /server/firmware/build and /server/firmware/build_and_flash. There is
// deliberately no "Flash All" button - flashing always goes through
// build_and_flash so a stale binary can never be flashed without also
// being rebuilt. Runs as a plain <script> tag in index.html, same
// injection technique as projector-panel.js.

(function () {
  'use strict';

  const WRAPPER_ID = 'firmware-panel-wrapper';

  // ── data ──────────────────────────────────────────────────────────────────

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error('GET ' + url + ' -> ' + r.status);
    const j = await r.json();
    return j && j.result;
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = (j && j.error && j.error.message) || (r.status + ' ' + r.statusText);
      throw new Error(msg);
    }
    return j && j.result;
  }

  // ── DOM helpers (same pattern as projector-panel.js) ─────────────────────

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined) continue;
      if (k === 'style' && typeof v === 'object') {
        Object.assign(node.style, v);
      } else if (k === 'onclick') {
        node.addEventListener('click', v);
      } else {
        node.setAttribute(k, v);
      }
    }
    for (const c of children) {
      if (typeof c === 'string') node.appendChild(document.createTextNode(c));
      else if (c) node.appendChild(c);
    }
    return node;
  }

  function button(text, onclick, opts) {
    opts = opts || {};
    return el('button', {
      class: 'v-btn theme--dark',
      style: {
        margin: '4px 8px 4px 0',
        padding: '6px 14px',
        borderRadius: '4px',
        border: '1px solid rgba(255,255,255,.25)',
        background: opts.danger ? 'rgba(239,154,154,.15)' : 'rgba(255,255,255,.08)',
        color: opts.danger ? '#ef9a9a' : 'inherit',
        cursor: 'pointer',
        fontSize: '.8rem',
      },
      disabled: opts.disabled ? 'disabled' : null,
      onclick,
    }, text);
  }

  // ── panel construction ────────────────────────────────────────────────────

  function buildPanel() {
    const titleRow = el('div', {
      class: 'v-card__title',
      style: {
        padding: '12px 16px',
        fontSize: '.875rem',
        fontWeight: '500',
        display: 'flex',
        alignItems: 'center',
      },
    }, 'Firmware',
       el('div', { style: { flex: '1' } }),
       el('span', {
         id: 'fw-status-chip',
         style: {
           fontSize: '.75rem',
           padding: '2px 10px',
           borderRadius: '12px',
           background: 'rgba(255,255,255,.1)',
         },
       }, 'idle'));

    const divider = el('hr', { class: 'v-divider theme--dark', role: 'separator' });

    const targetsBody = el('tbody', { id: 'fw-targets-body' });
    const table = el('table', { style: { width: '100%', fontSize: '.8rem', borderCollapse: 'collapse' } },
      el('thead', {},
        el('tr', {},
          el('th', { style: { textAlign: 'left', padding: '4px 8px' } }, 'Target'),
          el('th', { style: { textAlign: 'left', padding: '4px 8px' } }, 'Preset'),
          el('th', { style: { textAlign: 'left', padding: '4px 8px' } }, 'Device'),
          el('th', { style: { textAlign: 'left', padding: '4px 8px' } }, 'Status'))),
      targetsBody);

    const buttonsRow = el('div', { style: { padding: '10px 8px', display: 'flex', flexWrap: 'wrap' } },
      button('Build All', onBuildAll, {}),
      button('Build & Flash All', onBuildAndFlashAll, {}),
      button('Cancel', onCancel, { danger: true }));
    const cancelBtn = buttonsRow.querySelector('button:last-child');
    cancelBtn.id = 'fw-cancel-btn';
    cancelBtn.style.display = 'none'; // only shown while a job is active

    const log = el('pre', {
      id: 'fw-log',
      style: {
        margin: '0 8px 12px',
        padding: '8px',
        maxHeight: '220px',
        overflowY: 'auto',
        background: 'rgba(0,0,0,.35)',
        borderRadius: '4px',
        fontSize: '.7rem',
        lineHeight: '1.4',
        whiteSpace: 'pre-wrap',
        display: 'none',
      },
    }, '');

    const cardText = el('div', { class: 'v-card__text', style: { padding: '4px' } },
      table, buttonsRow, log);

    const card = el('div', { class: 'v-card theme--dark' }, titleRow, divider, cardText);
    const outerCol = el('div', { class: 'col-12', style: { padding: '4px' } }, card);
    const outerRow = el('div', { class: 'v-row', style: { margin: '0' } }, outerCol);
    return el('div', { id: WRAPPER_ID }, outerRow);
  }

  // ── target/status rendering ──────────────────────────────────────────────

  let latestTargets = {};
  let pollFast = false;

  function renderTargets(wrap, targets) {
    latestTargets = targets || {};
    const body = wrap.querySelector('#fw-targets-body');
    body.innerHTML = '';
    for (const [name, info] of Object.entries(latestTargets)) {
      const statusText = info.mismatch
        ? '⚠ ' + info.mismatch
        : (info.mcu_version ? 'up to date' : 'unknown (mcu disconnected)');
      const statusColor = info.mismatch ? '#ffcc80' : (info.mcu_version ? '#a5d6a7' : '#9e9e9e');
      body.appendChild(el('tr', {},
        el('td', { style: { padding: '4px 8px' } }, name),
        el('td', { style: { padding: '4px 8px' } }, info.preset || '—'),
        el('td', { style: { padding: '4px 8px', fontFamily: 'monospace', fontSize: '.7rem' } }, info.device || '—'),
        el('td', { style: { padding: '4px 8px', color: statusColor } }, statusText)));
    }
  }

  function renderStatus(wrap, status) {
    const chip = wrap.querySelector('#fw-status-chip');
    const cancelBtn = wrap.querySelector('#fw-cancel-btn');
    const logEl = wrap.querySelector('#fw-log');

    pollFast = !!status.active;
    cancelBtn.style.display = status.active ? '' : 'none';

    // Builds finish in a second or two once ccache is warm, so "active"
    // flips back to false almost immediately - don't hide the log or reset
    // the chip to "idle" just because the job isn't running anymore. Keep
    // showing the last job's outcome until a new one starts (job_status
    // only gets reseeded server-side when a new build/build_and_flash
    // begins, so "has any status entries at all" is exactly "has run at
    // least once this session").
    const hasRun = status.status && Object.keys(status.status).length > 0;

    if (status.active) {
      chip.textContent = status.action + '…';
      chip.style.color = '';
    } else if (status.error) {
      chip.textContent = 'error';
      chip.style.color = '#ef9a9a';
    } else if (hasRun) {
      chip.textContent = 'done';
      chip.style.color = '#a5d6a7';
    } else {
      chip.textContent = 'idle';
      chip.style.color = '';
    }

    if (!status.active && !status.error && !hasRun) {
      logEl.style.display = 'none';
      return;
    }

    logEl.style.display = 'block';
    const lines = [];
    for (const [target, entry] of Object.entries(status.status || {})) {
      lines.push('[' + target + '] ' + entry.phase);
      for (const l of (entry.log || []).slice(-30)) lines.push('  ' + l);
    }
    if (status.error) lines.push('ERROR: ' + status.error);
    logEl.textContent = lines.join('\n');
    logEl.scrollTop = logEl.scrollHeight;
  }

  // ── actions ───────────────────────────────────────────────────────────────

  // Set by startPolling to its current closure's pollStatus/pollTargets, so
  // a button click can force an immediate refresh instead of waiting for
  // the next scheduled tick (up to 10s away) to notice the job started.
  let pokeStatus = null;
  let pokeTargets = null;

  async function onBuildAll() {
    try {
      await postJSON('/server/firmware/build', { targets: 'all' });
      pollFast = true;
      if (pokeStatus) pokeStatus();
    } catch (e) {
      alert('Build failed to start: ' + e.message);
    }
  }

  async function onBuildAndFlashAll() {
    const names = Object.keys(latestTargets);
    const ok = confirm(
      'Build and flash ALL firmware targets (' + names.join(', ') + ')?\n\n' +
      'This stops the klipper service, enters the bootloader on each ' +
      'board in turn, and flashes new firmware. On some boards entering ' +
      'the bootloader (DFU) can briefly power the heater - verify heaters ' +
      'are disconnected or otherwise safe before continuing.\n\n' +
      'Do not do this while a print is running.'
    );
    if (!ok) return;
    try {
      await postJSON('/server/firmware/build_and_flash', { targets: 'all' });
      pollFast = true;
      if (pokeStatus) pokeStatus();
    } catch (e) {
      alert('Build & flash failed to start: ' + e.message);
    }
  }

  async function onCancel() {
    try {
      await postJSON('/server/firmware/cancel', {});
      if (pokeStatus) pokeStatus();
    } catch (e) {
      alert('Cancel failed: ' + e.message);
    }
  }

  // ── polling ───────────────────────────────────────────────────────────────

  function startPolling(wrap) {
    // Local to this call, not module-level: each re-injection (navigate
    // away from Settings and back) gets its own independent timers, so a
    // stale chain from a earlier injection can only ever clear its own
    // timer, never a newer injection's live one.
    let targetsTimer = null;
    let statusTimer = null;

    async function pollTargets() {
      if (!document.body.contains(wrap)) { clearInterval(targetsTimer); return; }
      try {
        const data = await getJSON('/server/firmware/targets');
        renderTargets(wrap, data && data.targets);
      } catch (_) { /* transient - keep last known state */ }
    }
    async function pollStatus() {
      if (!document.body.contains(wrap)) { clearTimeout(statusTimer); return; }
      try {
        const data = await getJSON('/server/firmware/status');
        if (data) renderStatus(wrap, data);
      } catch (_) { /* transient */ }
      clearTimeout(statusTimer);
      statusTimer = setTimeout(pollStatus, pollFast ? 1000 : 10000);
    }
    pokeStatus = pollStatus;
    pokeTargets = pollTargets;
    pollTargets();
    targetsTimer = setInterval(pollTargets, 10000);
    pollStatus();
  }

  // ── injection ─────────────────────────────────────────────────────────────

  function isOnSettings() {
    // Not actually "Settings": this Mainsail version's route table has
    // path:"/settings/machine" hardcoded to redirect:"/config" with
    // component:null - it's a legacy stub, never a real page. The "Machine
    // Settings" content itself (card-class "machine-settings-panel") lives
    // as one panel among several on the Configure page instead, so that's
    // the route that actually renders when a user looks for machine/MCU
    // settings. Confirmed by loading this Mainsail build directly (see
    // biokalico_extras/firmware_flash.md).
    const p = window.location.pathname;
    const h = window.location.hash;
    return p.indexOf('/config') !== -1 || h.indexOf('/config') !== -1;
  }

  function getContainer() {
    for (const s of [
      '.v-main__wrap .v-container',
      '.v-main__wrap .container',
      '.v-main__wrap',
      '.v-main',
      'main',
    ]) {
      const n = document.querySelector(s);
      if (n) return { node: n, selector: s };
    }
    return null;
  }

  let warnedNoContainer = false;

  function tryInject() {
    const existing = document.getElementById(WRAPPER_ID);

    if (!isOnSettings()) {
      if (existing) existing.remove();
      return;
    }

    if (existing && document.body.contains(existing)) return; // already there

    const found = getContainer();
    if (!found) {
      if (!warnedNoContainer) {
        console.warn('firmware-panel.js: no Configure-page container found - Mainsail internals may have changed, see biokalico_extras/firmware_flash.md');
        warnedNoContainer = true;
      }
      return;
    }

    const wrap = buildPanel();
    found.node.insertBefore(wrap, found.node.firstChild);
    startPolling(wrap);
  }

  // ── bootstrap ─────────────────────────────────────────────────────────────

  let debounce = null;

  new MutationObserver(() => {
    clearTimeout(debounce);
    debounce = setTimeout(tryInject, 150);
  }).observe(document.documentElement, { childList: true, subtree: true });

  for (const method of ['pushState', 'replaceState']) {
    const orig = history[method].bind(history);
    history[method] = function (...a) { orig(...a); setTimeout(tryInject, 300); };
  }
  window.addEventListener('popstate', () => setTimeout(tryInject, 300));

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(tryInject, 500));
  } else {
    setTimeout(tryInject, 500);
  }

})();
