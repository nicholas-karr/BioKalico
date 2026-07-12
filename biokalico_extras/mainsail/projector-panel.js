// Projector status panel for Mainsail dashboard.
// Polls /printer/objects/query?image_display every 5 s and injects a half-width
// Vuetify 2 card below the other dashboard panels. Runs as a plain <script> tag
// in index.html.

(function () {
  'use strict';

  // ── data ──────────────────────────────────────────────────────────────────

  async function fetchState() {
    const r = await fetch('/printer/objects/query?image_display');
    if (!r.ok) return null;
    const j = await r.json();
    return j?.result?.status?.image_display ?? null;
  }

  // ── formatters ────────────────────────────────────────────────────────────

  function fmtDateTime(unix) {
    if (unix == null) return '—';
    return new Date(unix * 1000).toLocaleString(undefined, {
      year:   'numeric',
      month:  '2-digit',
      day:    '2-digit',
      hour:   '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
  }

  function fmtDuration(sec) {
    if (sec == null) return '—';
    const s = Math.round(sec);
    if (s < 60)   return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  }

  // ── DOM helpers ───────────────────────────────────────────────────────────

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'style' && typeof v === 'object') {
        Object.assign(node.style, v);
      } else {
        node.setAttribute(k, v);
      }
    }
    for (const c of children) {
      if (typeof c === 'string') node.insertAdjacentHTML('beforeend', c);
      else if (c) node.appendChild(c);
    }
    return node;
  }

  function labelEl(text) {
    return el('div', {
      class: 'overline grey--text',
      style: { fontSize: '.65rem', lineHeight: '1.4', marginBottom: '2px' },
    }, text);
  }

  function valueEl(id) {
    return el('div', { class: 'body-2', id });
  }

  function col(...children) {
    return el('div', {
      class: 'col-6',
      style: { padding: '4px 12px' },
    }, ...children);
  }

  // ── panel construction ────────────────────────────────────────────────────

  const WRAPPER_ID = 'projector-panel-wrapper';

  function buildPanel() {
    const projectorIcon = `
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"
           style="margin-right:10px;opacity:.65;flex-shrink:0">
        <path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0
                 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 14H4V6h16v12zM9 8h2v8H9zm4 0h2v8h-2z"/>
      </svg>`;

    const statusChip = el('span', {
      id: 'proj-chip',
      style: {
        fontSize: '.75rem',
        fontWeight: '400',
        padding: '2px 10px',
        borderRadius: '12px',
        background: 'rgba(255,255,255,.1)',
        letterSpacing: '.02em',
      },
    }, '—');

    const titleRow = el('div', {
      class: 'v-card__title',
      style: {
        padding: '12px 16px',
        fontSize: '.875rem',
        fontWeight: '500',
        display: 'flex',
        alignItems: 'center',
      },
    }, projectorIcon, 'Projector',
       el('div', { style: { flex: '1' } }),
       statusChip);

    const divider = el('hr', { class: 'v-divider theme--dark', role: 'separator' });

    // Two-column grid (half-width card), then a full-width row for last-active
    const fieldsGrid = el('div', {
      class: 'row',
      style: { margin: '0' },
    },
      col(labelEl('Display'),    valueEl('f-display-name')),
      col(labelEl('Resolution'), valueEl('f-resolution')),
      col(labelEl('Ready'),      valueEl('f-ready')),
      col(labelEl('Content'),    valueEl('f-content')),
      col(labelEl('Displays'),   valueEl('f-displays')),
      col(labelEl('Idle'),       valueEl('f-idle')),
      col(labelEl('Standby'),    valueEl('f-standby')),
      el('div', {
        class: 'col-12',
        style: { padding: '4px 12px' },
      },
        labelEl('Last Active'),
        valueEl('f-last-active')),
    );

    const cardText = el('div', {
      class: 'v-card__text',
      style: { padding: '10px 4px 10px' },
    }, fieldsGrid);

    const card = el('div', { class: 'v-card theme--dark' },
      titleRow, divider, cardText);

    const outerCol = el('div', { class: 'col-12 col-md-6', style: { padding: '4px' } }, card);
    const outerRow = el('div', { class: 'row', style: { margin: '0' } }, outerCol);

    const wrapper = el('div', { id: WRAPPER_ID }, outerRow);
    return wrapper;
  }

  // ── update ────────────────────────────────────────────────────────────────

  function set(wrap, id, text, color) {
    const node = wrap.querySelector('#' + id);
    if (!node) return;
    node.textContent = text;
    node.style.color = color || '';
  }

  function updatePanel(wrap, state) {
    const chip = wrap.querySelector('#proj-chip');

    if (!state) {
      chip.textContent = 'Offline';
      chip.style.color = '#ef9a9a';
      ['f-display-name','f-resolution','f-ready','f-content','f-displays',
       'f-idle','f-standby','f-last-active'].forEach(id => set(wrap, id, '—'));
      return;
    }

    const on    = Boolean(state.projector_on);
    const avail = Boolean(state.projector_available);
    const ready = Boolean(state.display_ready);

    // Chip reflects true display readiness, not just serial power: the window
    // must be bound to the projector output at its native resolution.
    if (ready) {
      chip.textContent = 'READY';
      chip.style.color = '#a5d6a7';
    } else if (on) {
      chip.textContent = 'ON (no display)';
      chip.style.color = '#ffcc80';
    } else if (avail) {
      chip.textContent = 'Available';
      chip.style.color = '#ffcc80';
    } else {
      chip.textContent = 'OFF';
      chip.style.color = '#ef9a9a';
    }

    set(wrap, 'f-display-name', state.window_display_name || '—');
    set(wrap, 'f-resolution',
        state.window_width ? state.window_width + '×' + state.window_height : '—');
    set(wrap, 'f-ready',
        ready ? 'Yes' : (avail ? 'Binding…' : 'No'),
        ready ? '#a5d6a7' : (avail ? '#ffcc80' : '#ef9a9a'));
    set(wrap, 'f-content',
        state.has_content ? 'Showing' : (on ? 'Black' : '—'));

    const dc = state.display_count ?? (state.displays?.length ?? 0);
    set(wrap, 'f-displays', dc > 0 ? String(dc) : '—');

    set(wrap, 'f-idle', fmtDuration(state.idle_seconds));

    if (state.standby_armed && state.standby_in_seconds != null) {
      set(wrap, 'f-standby', 'Off in ' + fmtDuration(state.standby_in_seconds), '#ffcc80');
    } else {
      set(wrap, 'f-standby', 'Not armed');
    }

    set(wrap, 'f-last-active', fmtDateTime(state.last_activity_unix_time));
  }

  // ── polling ───────────────────────────────────────────────────────────────

  let pollTimer = null;

  function startPolling(wrap) {
    async function poll() {
      if (!document.body.contains(wrap)) {
        clearInterval(pollTimer);
        return;
      }
      try {
        updatePanel(wrap, await fetchState());
      } catch (_) {
        updatePanel(wrap, null);
      }
    }
    clearInterval(pollTimer);
    poll();
    pollTimer = setInterval(poll, 5000);
  }

  // ── injection ─────────────────────────────────────────────────────────────

  function isOnDashboard() {
    const p = window.location.pathname;
    const h = window.location.hash;
    return p === '/' || p === '' || h === '#/' || h === '#';
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
      if (n) return n;
    }
    return null;
  }

  function tryInject() {
    const existing = document.getElementById(WRAPPER_ID);

    if (!isOnDashboard()) {
      if (existing) existing.remove();
      return;
    }

    if (existing && document.body.contains(existing)) return; // already there

    const container = getContainer();
    if (!container) return;

    const wrap = buildPanel();
    container.appendChild(wrap);
    startPolling(wrap);
  }

  // ── bootstrap ─────────────────────────────────────────────────────────────

  let debounce = null;

  new MutationObserver(() => {
    clearTimeout(debounce);
    debounce = setTimeout(tryInject, 150);
  }).observe(document.documentElement, { childList: true, subtree: true });

  // Intercept SPA navigation
  for (const method of ['pushState', 'replaceState']) {
    const orig = history[method].bind(history);
    history[method] = function (...a) { orig(...a); setTimeout(tryInject, 300); };
  }
  window.addEventListener('popstate', () => setTimeout(tryInject, 300));

  // Initial try after DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(tryInject, 500));
  } else {
    setTimeout(tryInject, 500);
  }

})();
