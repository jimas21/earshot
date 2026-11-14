/*
 * Earshot menu for Steam's VR UI (ARM64 SteamOS, e.g. Steam Frame).
 *
 * Injected into two Steam pages by earshot-steamui.py:
 *  - the VR bottom bar (valve.steam.gamepadui.bar): a phone indicator next to the
 *    headset battery (call and unread badge); a click opens the quick-access popup;
 *  - the quick-access popup (valve.steam.gamepadui.barpopup.*): a Phone tab with
 *    calls, notifications, messages and settings.
 *
 * The helper pushes state into the page (window.__earshotMenu.setState) and the page
 * sends actions back through the binding window.__BINDING__(jsonString). This file
 * never talks to the network. Pages signal each other over a BroadcastChannel.
 *
 * Unofficial: Steam's markup can change in an update. Everything is located
 * defensively; if an anchor is missing, nothing is added.
 */
(() => {
  const VERSION = '__VERSION__';
  const BINDING = '__BINDING__';
  const prev = window.__earshotMenu;
  if (prev && prev.version === VERSION) { prev.ensure(); return 'already ' + VERSION; }
  if (prev && prev.teardown) { try { prev.teardown(); } catch (e) { } }

  // Steam names these overlay pages "SteamVR - valve.steam.gamepadui.bar (id)" and
  // "SteamVR - valve.steam.gamepadui.barpopup.<id> (id)".
  const ROLE = /gamepadui\.barpopup\./.test(document.title) ? 'popup'
    : (/gamepadui\.bar(?!popup)/.test(document.title) ? 'bar' : 'other');
  if (ROLE === 'other') return 'not a target page';
  if (!document.body) return 'not ready';

  const cleanups = [];
  let dead = false;  // set by teardown: late callbacks must not redraw
  try {
  const channel = new BroadcastChannel('earshot');
  cleanups.push(() => channel.close());

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

  // ---------- view preferences kept in this browser profile ----------
  const UI_DEFAULTS = { tab: 'calls' };
  function loadUi() {
    try { return Object.assign({}, UI_DEFAULTS, JSON.parse(localStorage.getItem('earshot.ui') || '{}')); }
    catch (e) { return Object.assign({}, UI_DEFAULTS); }
  }
  let ui = loadUi();
  function saveUi(patch) {
    ui = Object.assign({}, ui, patch);
    try { localStorage.setItem('earshot.ui', JSON.stringify(ui)); } catch (e) { }
  }

  // ---------- helper link ----------
  let S = null;                 // last state pushed by the helper
  const queue = [];             // actions waiting for the binding to exist
  let queueTimer = null;
  const drafts = {};            // thread -> unsent text
  const local = {};             // cid -> { thread, text } for messages we sent from this page
  let cidCounter = 0;
  function act(action) {
    const fn = window[BINDING];
    if (typeof fn === 'function') {
      try { fn(JSON.stringify(action)); return true; } catch (e) { return false; }
    }
    queue.push(action);
    if (!queueTimer) {
      let tries = 0;
      queueTimer = setInterval(() => {
        const f = window[BINDING];
        if (typeof f === 'function' || ++tries > 20) {
          clearInterval(queueTimer); queueTimer = null;
          if (typeof f === 'function') while (queue.length) { try { f(JSON.stringify(queue.shift())); } catch (e) { } }
          else queue.length = 0;
        }
      }, 500);
    }
    return false;
  }
  cleanups.push(() => { clearInterval(queueTimer); queueTimer = null; });

  function setState(next) {
    if (dead) return;
    S = next;
    maybeOpenForPair();
    render();
  }

  // ---------- styles ----------
  const STYLE_ID = 'earshot-menu-style';
  const CSS = `
  #earshot-overlay { position:absolute; left:0; right:0; top:0; z-index:50; background:rgb(14,20,27); color:#dcdedf;
    font-family:"Motiva Sans",Arial,Helvetica,sans-serif; font-size:14px; overflow-y:auto; overflow-x:hidden; display:none; box-sizing:border-box; padding:0 0 12px; }
  #earshot-overlay.es-open { display:block; }
  .es-head { display:flex; align-items:center; gap:10px; padding:10px 14px 4px; }
  .es-head .es-name { flex:1; font-size:15px; font-weight:700; color:#fff; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .es-dot { width:9px; height:9px; border-radius:50%; background:#6b7480; flex:none; }
  .es-dot.es-ok { background:#59bf40; }
  .es-chip { font-size:11px; padding:2px 8px; border-radius:10px; background:#23262e; color:#b8bcbf; white-space:nowrap; }
  .es-chip.es-hi { background:#1a9fff; color:#fff; }
  .es-sub { padding:0 14px 6px; font-size:11px; color:#8b929a; }
  .es-tabs { position:sticky; top:0; z-index:2; display:flex; gap:4px; padding:8px 10px; background:rgb(14,20,27); }
  .es-tabs button { position:relative; flex:1; height:30px; border:0; border-radius:3px; background:#23262e; color:#b8bcbf; font:inherit; font-size:12px; cursor:pointer; }
  .es-tabs button.es-on { background:#1a9fff; color:#fff; }
  .es-badge { display:inline-block; min-width:16px; padding:0 4px; margin-left:4px; border-radius:8px; background:#ED18BF; color:#fff; font-size:10px; line-height:16px; }
  .es-tabbadge { position:absolute; top:2px; right:4px; margin:0; pointer-events:none; }
  .es-newchip { margin:0; }
  .es-item.es-new { background:#14263a; box-shadow:inset 3px 0 0 #ED18BF; }
  .es-sec { padding:6px 14px 2px; }
  #earshot-overlay.es-open.es-convmode { display:flex; flex-direction:column; overflow:hidden; padding-bottom:0; }
  .es-convmode .es-tabs, .es-convmode #earshot-call { flex:none; }
  .es-convmode #earshot-body { flex:1; min-height:0; display:flex; flex-direction:column; }
  .es-conv { flex:1; min-height:0; display:flex; flex-direction:column; }
  .es-conv .es-msgs { flex:1; min-height:0; overflow-y:auto; padding:4px 10px 8px; }
  .es-conv .es-bub { font-size:17px; line-height:1.3; padding:8px 12px; }
  .es-conv .es-bub .es-meta { font-size:11px; }
  .es-bub.es-sentok { opacity:.85; }
  .es-reply { flex:none; padding:4px 8px 8px; background:#10161d; border-top:1px solid #23262e; }
  .es-quick { display:flex; flex-wrap:wrap; gap:6px; max-height:86px; overflow-y:auto; padding:2px 0 6px; }
  .es-qr { border:0; border-radius:14px; background:#23262e; color:#dcdedf; font:inherit; font-size:14px; padding:7px 12px; cursor:pointer; }
  .es-draftrow { display:flex; gap:8px; align-items:stretch; padding-bottom:6px; }
  .es-draft { flex:1; min-width:0; min-height:36px; max-height:64px; overflow-y:auto; box-sizing:border-box; padding:7px 10px; border:1px solid #3d4450; border-radius:4px; background:#0a0f14; color:#fff; font-size:17px; line-height:1.25; white-space:pre-wrap; overflow-wrap:anywhere; }
  .es-hint { color:#6b7480; }
  .es-caret { display:inline-block; width:2px; height:1.1em; margin-left:1px; vertical-align:text-bottom; background:#1a9fff; }
  .es-sendbtn { min-width:76px; font-size:16px; font-weight:700; }
  .es-sendbtn:disabled { opacity:.45; cursor:default; }
  .es-krow { display:flex; gap:4px; margin-bottom:4px; }
  .es-key { flex:1 1 0; min-width:0; height:40px; border:0; border-radius:5px; background:#2c313a; color:#fff; font:inherit; font-size:18px; cursor:pointer; padding:0; }
  .es-key.es-k-shift, .es-key.es-k-back, .es-key.es-k-mode { flex:1.6 1 0; background:#3d4450; font-size:14px; }
  .es-key.es-k-space { flex:4 1 0; font-size:14px; color:#b8bcbf; }
  .es-key.es-on { background:#1a9fff; }
  .es-h { font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:#8b929a; margin:14px 0 6px; }
  .es-msg { color:#b8bcbf; padding:12px 14px; line-height:1.35; }
  .es-card { margin:8px 10px; padding:10px 12px; border-radius:4px; background:#1b2b1d; border:1px solid #2f6d2c; }
  .es-card .es-who { font-size:16px; font-weight:700; color:#fff; }
  .es-card .es-state { font-size:12px; color:#9fd58f; margin-bottom:8px; }
  .es-card .es-err { font-size:12px; color:#ffcf99; margin-top:6px; }
  .es-btns { display:flex; gap:8px; }
  .es-btn { border:0; border-radius:3px; background:#3d4450; color:#fff; font:inherit; font-size:13px; padding:8px 14px; cursor:pointer; }
  .es-btn.es-go { background:#3c9d2f; } .es-btn.es-stop { background:#c0392b; } .es-btn.es-blue { background:#1a9fff; }
  .es-btn.es-small { font-size:11px; padding:4px 8px; }
  .es-setup { margin:8px 10px; padding:10px 12px; border-radius:4px; background:#23262e; }
  .es-setup b { color:#fff; }
  .es-row { display:flex; align-items:center; justify-content:space-between; gap:10px; min-height:38px; border-bottom:1px solid #23262e; }
  .es-row > span { flex:1; line-height:1.25; }
  .es-row.es-col { flex-direction:column; align-items:stretch; justify-content:center; padding:6px 0; gap:4px; }
  .es-item { display:flex; gap:10px; padding:8px 14px; border-bottom:1px solid #23262e; cursor:default; }
  .es-item.es-click { cursor:pointer; }
  .es-item .es-main { flex:1; min-width:0; }
  .es-item .es-top { display:flex; gap:8px; align-items:baseline; }
  .es-item .es-app { font-weight:700; color:#fff; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .es-item .es-time { font-size:11px; color:#8b929a; white-space:nowrap; }
  .es-item .es-title { color:#dcdedf; overflow-wrap:anywhere; }
  .es-item .es-text { color:#9aa1a8; font-size:12px; overflow-wrap:anywhere; margin-top:1px; }
  .es-item .es-unread { align-self:center; }
  .es-sw { position:relative; width:44px; height:24px; flex:none; border-radius:12px; background:#3d4450; border:0; cursor:pointer; }
  .es-sw::after { content:""; position:absolute; top:3px; left:3px; width:18px; height:18px; border-radius:50%; background:#dcdedf; transition:left .12s; }
  .es-sw.es-on { background:#1a9fff; } .es-sw.es-on::after { left:23px; background:#fff; }
  .es-choice { display:flex; flex-wrap:wrap; gap:4px; }
  .es-choice button { border:0; border-radius:3px; background:#23262e; color:#b8bcbf; font:inherit; font-size:12px; padding:6px 8px; cursor:pointer; }
  .es-choice button.es-on { background:#1a9fff; color:#fff; }
  .es-note { font-size:11px; color:#8b929a; line-height:1.3; }
  .es-bar { display:flex; align-items:center; gap:8px; padding:6px 14px; }
  .es-bar .es-grow { flex:1; font-weight:700; color:#fff; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .es-msgs { padding:4px 10px 8px; display:flex; flex-direction:column; gap:4px; }
  .es-bub { max-width:82%; padding:6px 10px; border-radius:12px; background:#23262e; color:#dcdedf; overflow-wrap:anywhere; align-self:flex-start; white-space:pre-wrap; }
  .es-bub.es-out { align-self:flex-end; background:#1a73b8; color:#fff; }
  .es-bub.es-pend { opacity:.6; }
  .es-bub.es-fail { background:#6b2a22; }
  .es-bub .es-meta { display:block; font-size:10px; color:#c9d1d9; opacity:.8; margin-top:2px; }
  .es-compose { display:flex; gap:6px; padding:6px 10px; }
  .es-compose input { flex:1; min-width:0; height:36px; box-sizing:border-box; border:1px solid #3d4450; border-radius:3px; background:#10161d; color:#fff; font:inherit; padding:0 10px; }
  #earshot-bar-indicator .es-cnt { margin-left:3px; }
  .es-pair { padding:6px 14px 14px; }
  .es-ph { font-size:20px; font-weight:700; color:#fff; margin:10px 0 8px; }
  .es-pstep { font-size:16px; line-height:1.4; color:#dcdedf; margin:6px 0 10px; }
  .es-pstep b { color:#fff; }
  .es-ptext { font-size:14px; line-height:1.4; color:#b8bcbf; margin:6px 0 10px; }
  .es-pbtn { display:block; width:100%; box-sizing:border-box; min-height:56px; margin:8px 0; padding:12px 16px; border:0; border-radius:6px; background:#3d4450; color:#fff; font:inherit; font-size:18px; font-weight:700; text-align:center; cursor:pointer; }
  .es-pbtn.es-blue { background:#1a9fff; } .es-pbtn.es-go { background:#3c9d2f; } .es-pbtn.es-stop { background:#c0392b; }
  .es-pbtn.es-quiet { background:#23262e; color:#b8bcbf; font-weight:400; font-size:15px; min-height:48px; }
  .es-pbtn:disabled { opacity:.45; cursor:default; }
  #earshot-pair:empty, .es-tabs:empty { display:none; }
  .es-pair.es-confirm { padding-top:2px; }
  .es-confirm .es-ph { font-size:16px; text-align:center; color:#9fd58f; margin:4px 0 0; }
  .es-confirm .es-code { font-size:clamp(56px,16vw,96px); line-height:1.05; white-space:nowrap; margin:2px 0 6px; }
  .es-prow { display:flex; gap:10px; }
  .es-prow .es-pbtn { flex:2; margin:4px 0 8px; } .es-prow .es-pbtn.es-go { flex:3; }
  .es-pdev { display:flex; align-items:center; justify-content:space-between; gap:10px; width:100%; box-sizing:border-box; min-height:60px; margin:6px 0; padding:10px 16px; border:0; border-radius:6px; background:#23262e; color:#fff; font:inherit; font-size:18px; text-align:left; cursor:pointer; }
  .es-pdev .es-tag { font-size:12px; color:#8b929a; white-space:nowrap; }
  .es-pdev.es-phone { background:#1b3a55; }
  .es-code { font-size:64px; font-weight:700; letter-spacing:.12em; text-align:center; color:#fff; margin:14px 0 12px; font-variant-numeric:tabular-nums; }
  .es-pwait { font-size:18px; color:#9fd58f; margin:12px 0; }
  .es-count { font-size:28px; font-weight:700; color:#fff; text-align:center; margin:10px 0 4px; font-variant-numeric:tabular-nums; }
  .es-chk { display:flex; gap:12px; align-items:flex-start; padding:10px 0; border-bottom:1px solid #23262e; }
  .es-chk .es-mark { flex:none; width:28px; height:28px; border-radius:50%; background:#3d4450; color:#fff; text-align:center; line-height:28px; font-size:16px; }
  .es-chk.es-on .es-mark { background:#3c9d2f; } .es-chk.es-off .es-mark { background:#c0392b; }
  .es-chk .es-lab { font-size:16px; font-weight:700; color:#fff; }
  .es-chk .es-note { font-size:13px; }
  `;
  function ensureStyle() {
    const old = document.getElementById(STYLE_ID);
    if (old && old.dataset.esVersion === VERSION) return;
    if (old) old.remove();
    const s = document.createElement('style');
    s.id = STYLE_ID; s.textContent = CSS; s.dataset.esVersion = VERSION;
    (document.head || document.documentElement).appendChild(s);
  }
  cleanups.push(() => { const s = document.getElementById(STYLE_ID); if (s) s.remove(); });

  const PHONE_ICON = '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">' +
    '<path d="M8 1.5h8A2.5 2.5 0 0 1 18.5 4v16a2.5 2.5 0 0 1-2.5 2.5H8A2.5 2.5 0 0 1 5.5 20V4A2.5 2.5 0 0 1 8 1.5zm0 1.5a1 1 0 0 0-1 1v14.5h10V4a1 1 0 0 0-1-1H8zm0 16v1a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-1H8z"/></svg>';

  // ---------- formatting ----------
  function whenText(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '';
    const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return d.toDateString() === new Date().toDateString() ? hm : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + hm;
  }
  const badge = (n) => (n > 0 ? `<span class="es-badge">${n > 99 ? '99+' : n}</span>` : '');

  // ---------- unread alerts ----------
  // An alert is unread until the Alerts tab has been on screen with it in the list. What was seen is kept in this
  // browser profile (so reopening the menu, or a page reload, does not make old alerts look new). The very first
  // state ever seen is the baseline: what the phone already had is not announced as new.
  const SEEN_KEY = 'earshot.seen';
  const SEEN_MAX = 300;
  let seen = null;              // Set of alert keys, loaded on the first state
  const fresh = new Set();      // keys that were unread when the Alerts tab opened: highlighted while it stays open
  const alertKey = (n) => [n.uid, n.timestamp || 0, n.app_id || ''].join('|');
  const alertList = () => (S && Array.isArray(S.notifications) ? S.notifications : []);
  function saveSeen() {
    while (seen.size > SEEN_MAX) seen.delete(seen.values().next().value);
    try { localStorage.setItem(SEEN_KEY, JSON.stringify([...seen])); } catch (e) { }
  }
  function syncSeen() {
    if (seen) return;
    let raw = null;
    try { raw = JSON.parse(localStorage.getItem(SEEN_KEY)); } catch (e) { }
    if (Array.isArray(raw)) { seen = new Set(raw.map(String)); return; }
    seen = new Set(alertList().map(alertKey));
    saveSeen();
  }
  function unreadAlerts() {
    if (!S) return 0;       // nothing is known yet: no baseline either
    syncSeen();
    return alertList().filter((n) => !seen.has(alertKey(n))).length;
  }
  function markAlertsSeen() {
    if (!S) return;
    syncSeen();
    let changed = false;
    for (const n of alertList()) {
      const k = alertKey(n);
      if (!seen.has(k)) { seen.add(k); fresh.add(k); changed = true; }
    }
    if (changed) saveSeen();
  }

  // ---------- pieces ----------
  function headHtml() {
    if (!S) return '<div class="es-head"><div class="es-name">Phone</div></div>';
    const p = S.phone || {}, pr = S.priority || {};
    const where = pr.frame_active ? 'Notifications here' : 'Notifications on phone';
    let sub = '';
    if (S.setup) sub = '';
    else if (!p.connected) sub = p.reason || 'Waiting for the iPhone.';
    else if (!pr.frame_active) sub = 'Headset not in use: the phone handles calls and notifications.';
    return `<div class="es-head"><span class="es-dot${p.connected && !S.setup ? ' es-ok' : ''}"></span>` +
      `<div class="es-name">${esc(p.name || 'iPhone')}</div>` +
      (typeof p.battery === 'number' ? `<span class="es-chip">${p.battery}%</span>` : '') +
      `<span class="es-chip${pr.frame_active ? ' es-hi' : ''}">${esc(where)}</span></div>` +
      (sub ? `<div class="es-sub">${esc(sub)}</div>` : '');
  }

  function callCardHtml() {
    const c = S && S.calls && S.calls.incoming;
    if (!c) return '';
    const who = c.name || c.number || 'Unknown caller';
    const arg = c.source === 'ancs' ? `data-b="${esc(c.uid)}"` : `data-a="${esc(c.path)}"`;
    const canAnswer = c.source !== 'ancs' || c.can_answer;
    const canDecline = c.source !== 'ancs' || c.can_decline;
    return `<div class="es-card"><div class="es-who">${esc(who)}</div><div class="es-state">Incoming call</div><div class="es-btns">` +
      (canAnswer ? `<button class="es-btn es-go" data-act="call-answer" ${arg}>Answer</button>` : '') +
      (canDecline ? `<button class="es-btn es-stop" data-act="call-decline" ${arg}>Decline</button>` : '') +
      `</div>${S.calls.error ? `<div class="es-err">${esc(S.calls.error)}</div>` : ''}</div>`;
  }

  function tabsHtml() {
    const unread = S ? (S.unread || 0) : 0;
    const ringing = S && S.calls && S.calls.incoming ? 1 : 0;
    const notifs = S ? unreadAlerts() : 0;
    const tabs = [['calls', 'Calls', ringing], ['messages', 'Messages', unread], ['alerts', 'Notifications', notifs], ['settings', 'Settings', 0]];
    return tabs.map(([k, label, n]) => `<button class="${ui.tab === k ? 'es-on' : ''}" data-act="tab" data-a="${k}">${label}${badge(n)}</button>`).join('');
  }

  function setupHtml(setup) {
    return `<div class="es-setup"><b>${esc(setup.title)}</b><div class="es-note" style="margin-top:4px">${esc(setup.text)}</div>` +
      (setup.action && /^[a-z-]+$/.test(setup.action.act)
        ? `<button class="es-pbtn es-blue" data-act="${esc(setup.action.act)}">${esc(setup.action.label)}</button>`
        : `<div class="es-btns" style="margin-top:10px"><button class="es-btn es-blue" data-act="retry">Check again</button></div>`) + `</div>`;
  }

  function pageCalls() {
    const calls = S.calls || {};
    let h = '';
    const live = (calls.items || []).filter((c) => !c.ringing);
    if (live.length) {
      h += '<div class="es-h es-sec">Active</div>';
      h += live.map((c) => `<div class="es-item"><div class="es-main"><div class="es-app">${esc(c.name || c.number || 'Unknown caller')}</div>` +
        `<div class="es-text">${esc(c.state)}</div></div><button class="es-btn es-stop es-small" data-act="call-decline" data-a="${esc(c.path)}">Hang up</button></div>`).join('');
    }
    h += '<div class="es-h es-sec">Recent</div>';
    if (!(calls.recent || []).length) {
      h += `<div class="es-msg">${S.phone && S.phone.calls ? 'No recent calls.' : 'Calls appear here once the iPhone has connected Hands-Free and shared Phone Recents.'}</div>`;
    } else {
      h += calls.recent.map((c) => {
        const kind = c.type === 'missed' ? 'Missed' : c.type === 'dialed' ? 'Outgoing' : 'Incoming';
        return `<div class="es-item"><div class="es-main"><div class="es-app"${c.type === 'missed' ? ' style="color:#ff8a80"' : ''}>${esc(c.name || c.number || 'Unknown caller')}</div>` +
          `<div class="es-text">${kind}${c.name && c.number ? ' · ' + esc(c.number) : ''}</div></div><span class="es-time">${esc(whenText(c.timestamp))}</span></div>`;
      }).join('');
    }
    return h;
  }

  function pageAlerts() {
    const list = S.notifications || [];
    if (!list.length) return '<div class="es-msg">No notifications.</div>';
    let h = `<div class="es-bar"><span class="es-grow">${list.length} notification${list.length === 1 ? '' : 's'}</span>` +
      `<button class="es-btn es-small" data-act="clear-all" title="Hides them here. The phone keeps them.">Clear all</button></div>`;
    h += list.map((n) => `<div class="es-item${fresh.has(alertKey(n)) ? ' es-new' : ''}"><div class="es-main"><div class="es-top"><span class="es-app">${esc(n.app || 'iPhone')}</span>` +
      (fresh.has(alertKey(n)) ? '<span class="es-badge es-newchip">New</span>' : '') +
      `<span class="es-time">${esc(whenText(n.timestamp))}</span></div>` +
      (n.title ? `<div class="es-title">${esc(n.title)}</div>` : '') +
      (n.text ? `<div class="es-text">${esc(n.text)}</div>` : '') +
      (!n.title && !n.text ? '<div class="es-text">New notification</div>' : '') + `</div>` +
      (n.can_dismiss ? `<button class="es-btn es-small" data-act="dismiss" data-b="${esc(n.uid)}" title="Dismiss on the iPhone">Dismiss</button>` : '') + `</div>`).join('');
    return h;
  }

  function threadList() {
    const list = S.threads || [];
    if (!list.length) {
      return `<div class="es-msg">${S.phone && S.phone.messages ? 'No conversations yet.' : 'Replying to texts is not available on this headset yet. New texts show up as notifications.'}</div>`;
    }
    return list.map((t) => `<div class="es-item es-click" data-act="open-thread" data-a="${esc(t.thread)}"><div class="es-main"><div class="es-top">` +
      `<span class="es-app">${esc(t.name || 'Unknown')}</span><span class="es-time">${esc(whenText(t.timestamp))}</span></div>` +
      `<div class="es-text">${esc(t.preview)}</div></div>${t.unread ? `<span class="es-unread">${badge(t.unread)}</span>` : ''}</div>`).join('');
  }

  function threadMeta() {
    const o = S.open;
    return (S.threads || []).find((t) => t.thread === o.thread) || { name: 'Conversation', repliable: true };
  }

  // ---------- messages: a conversation with an on-screen keyboard ----------
  // Steam's own keyboard is a separate overlay: opening it takes focus away from the quick menu, which then closes
  // and the reply is lost. So the keyboard is drawn here, as plain buttons, and nothing in the menu ever takes focus:
  // there is no <input> and no <textarea>, only a div that shows the draft. The draft itself is kept by the helper
  // (it survives the menu closing), the page keeps its own copy while it is open.
  const KB_ROWS = {
    abc: [['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'], ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'],
      ['shift', 'z', 'x', 'c', 'v', 'b', 'n', 'm', 'back'], ['mode', ',', 'space', '.', '?']],
    num: [['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'], ['-', '/', ':', ';', '(', ')', '$', '&', '@', '"'],
      ['.', ',', '?', '!', "'", '%', '+', '=', 'back'], ['mode', '#', 'space', '*', '_']],
  };
  const kb = { page: 'abc', shift: 'off' };     // shift: off | once | lock | lower (a capital the sentence start asked for, declined)
  const MAX_DRAFT = 2000;
  const autoCap = (d) => /^\s*$/.test(d) || /[.!?]\s+$/.test(d);
  const isUpper = (d) => kb.shift === 'lock' || kb.shift === 'once' || (kb.shift === 'off' && autoCap(d));
  function draftOf(thread) {
    if (drafts[thread] !== undefined) return drafts[thread];
    const o = S && S.open;
    return o && o.thread === thread && typeof o.draft === 'string' ? o.draft : '';
  }
  function keyLabel(k, up) {
    if (k === 'shift') return kb.shift === 'lock' ? 'SHIFT' : 'Shift';
    if (k === 'back') return 'Del';
    if (k === 'mode') return kb.page === 'abc' ? '123' : 'ABC';
    if (k === 'space') return 'space';
    return kb.page === 'abc' && up ? k.toUpperCase() : k;
  }
  function kbHtml(d) {
    const up = isUpper(d);
    return KB_ROWS[kb.page].map((row) => '<div class="es-krow">' + row.map((k) =>
      `<button class="es-key es-k-${k.length > 1 ? k : 'c'}${k === 'shift' && up ? ' es-on' : ''}" data-act="key" data-a="${esc(k)}">${esc(keyLabel(k, up))}</button>`).join('') + '</div>').join('');
  }
  function draftRowHtml(d) {
    return `<div class="es-draft" id="earshot-draft">${d ? esc(d) + '<span class="es-caret"></span>' : '<span class="es-hint">Tap the keys to write a reply</span>'}</div>` +
      `<button class="es-btn es-blue es-sendbtn" data-act="send"${d.trim() ? '' : ' disabled'}>Send</button>`;
  }
  function quickHtml() {
    const list = S && Array.isArray(S.quick_replies) ? S.quick_replies : [];
    return list.map((t, i) => `<button class="es-qr" data-act="quick" data-a="${i}">${esc(t)}</button>`).join('');
  }
  function refreshReply() {
    if (!S || !S.open) return;
    const d = draftOf(S.open.thread);
    setHtml('earshot-quick', quickHtml());
    setHtml('earshot-draftrow', draftRowHtml(d));
    setHtml('earshot-kb', kbHtml(d));
    const box = document.getElementById('earshot-draft');
    if (box) box.scrollTop = box.scrollHeight;
  }
  function setDraft(thread, d) {
    drafts[thread] = d;
    act({ type: 'draft', thread, text: d });
    refreshReply();
  }
  function typeKey(k) {
    const o = S && S.open;
    if (!o) return;
    let d = draftOf(o.thread);
    if (k === 'shift') {
      kb.shift = kb.shift === 'once' ? 'lock' : kb.shift === 'lock' ? 'off' : kb.shift === 'lower' ? 'once' : (autoCap(d) ? 'lower' : 'once');
      refreshReply(); return;
    }
    if (k === 'mode') { kb.page = kb.page === 'abc' ? 'num' : 'abc'; refreshReply(); return; }
    if (k === 'back') d = Array.from(d).slice(0, -1).join('');
    else {
      const ch = k === 'space' ? ' ' : (kb.page === 'abc' && isUpper(d) ? k.toUpperCase() : k);
      if (d.length + ch.length > MAX_DRAFT) return;
      d += ch;
      if (kb.shift === 'once' || kb.shift === 'lower') kb.shift = 'off';
    }
    setDraft(o.thread, d);
  }

  function bubblesHtml() {
    const o = S.open;
    const msgs = o.messages || [];
    let h = msgs.map((m) => `<div class="es-bub${m.outgoing ? ' es-out' : ''}">${esc(m.body)}<span class="es-meta">${esc(whenText(m.timestamp))}</span></div>`).join('');
    const status = {};
    for (const s of (S.sends || [])) status[s.cid] = s;
    for (const cid of Object.keys(local)) {
      const l = local[cid];
      if (l.thread !== o.thread) continue;
      const s = status[cid];
      // Once the conversation itself shows what was sent, the local copy is not needed any more.
      if (s && s.status === 'sent' && msgs.slice(-10).some((m) => m.outgoing && String(m.body).trim() === l.text.trim())) { delete local[cid]; continue; }
      const failed = s && s.status === 'failed', sent = s && s.status === 'sent';
      h += `<div class="es-bub es-out ${failed ? 'es-fail' : sent ? 'es-sentok' : 'es-pend'}">${esc(l.text)}<span class="es-meta">${failed ? 'Not sent: ' + esc(s.error || 'no reason given') : sent ? 'Sent' : 'Sending…'}` +
        (failed ? ` <button class="es-btn es-small" data-act="resend" data-a="${esc(cid)}">Try again</button> <button class="es-btn es-small" data-act="discard" data-a="${esc(cid)}">Discard</button>` : '') + '</span></div>';
    }
    return h || '<div class="es-msg">No messages loaded yet.</div>';
  }

  function threadView() {
    const meta = threadMeta();
    const can = meta.repliable !== false;
    const d = draftOf(S.open.thread);
    return `<div class="es-conv" id="earshot-conv" data-thread="${esc(S.open.thread)}" data-can="${can}">` +
      `<div class="es-bar"><button class="es-btn es-small" data-act="back">Back</button><span class="es-grow" id="earshot-convname">${esc(meta.name || 'Conversation')}</span></div>` +
      `<div class="es-msgs" id="earshot-msgs">${bubblesHtml()}</div>` +
      (can
        ? `<div class="es-reply"><div class="es-quick" id="earshot-quick">${quickHtml()}</div><div class="es-draftrow" id="earshot-draftrow">${draftRowHtml(d)}</div><div class="es-kb" id="earshot-kb">${kbHtml(d)}</div></div>`
        : `<div class="es-msg">${esc(meta.reply_reason || 'This conversation cannot be replied to.')}</div>`) + '</div>';
  }

  // A conversation opens at its newest message, like on a phone.
  function scrollNewest() {
    const box = document.getElementById('earshot-msgs');
    if (box) box.scrollTop = box.scrollHeight;
  }

  function pageMessages() {
    return S.open ? threadView() : threadList();
  }

  const toggle = (label, on, key, note) =>
    `<div class="es-row"><span>${esc(label)}${note ? `<div class="es-note">${esc(note)}</div>` : ''}</span>` +
    `<button class="es-sw${on ? ' es-on' : ''}" data-act="setting-toggle" data-a="${key}" data-b="${on ? '0' : '1'}" aria-pressed="${on ? 'true' : 'false'}"></button></div>`;

  function updateText(u) {
    const st = u && u.status;
    if (!st) return { busy: false, text: '' };
    const busy = (st.state === 'checking' || st.state === 'updating') && st.time && (Date.now() / 1000 - st.time) < 900;
    return { busy, text: busy || !(st.state === 'checking' || st.state === 'updating') ? st.message : 'The last update check did not finish. Try again.' };
  }

  function pageSettings() {
    const st = S.settings || {}, pr = S.priority || {}, p = S.phone || {};
    const modes = [['auto', 'Auto'], ['frame', 'Always Frame'], ['phone', 'Always phone']];
    let h = '<div class="es-sec"><div class="es-h">Who gets calls and notifications</div><div class="es-row es-col"><div class="es-choice">' +
      modes.map(([k, l]) => `<button class="${st.priority === k ? 'es-on' : ''}" data-act="setting-set" data-a="priority" data-b="${k}">${l}</button>`).join('') + '</div>' +
      `<div class="es-note">Right now: ${pr.frame_active ? 'the headset' : 'the phone'}. ` +
      (st.priority === 'auto' ? 'Auto follows whether SteamVR is running until the headset can tell when it is worn. ' : '') +
      'When the phone has priority, Earshot lets go of the call link so the phone rings normally.</div></div>';
    h += '<div class="es-h">Notifications</div>' +
      toggle('Show notifications in the headset', !!st.alerts, 'alerts', 'Pop-ups appear while the headset has priority.') +
      toggle('Show message text in notifications', !!st.preview, 'preview', 'Off shows only who it is from.');
    const apps = S.apps || [];
    h += '<div class="es-h">Mute apps</div>';
    h += apps.length ? apps.map((a) => `<div class="es-row"><span>${esc(a.name)}</span><button class="es-sw${a.muted ? ' es-on' : ''}" data-act="mute" data-a="${esc(a.id)}" data-b="${a.muted ? '0' : '1'}" aria-label="Mute ${esc(a.name)}" aria-pressed="${a.muted ? 'true' : 'false'}"></button></div>`).join('') +
      '<div class="es-note">On means no pop-up. The app still shows under Notifications.</div>'
      : '<div class="es-note">Apps that send notifications will be listed here.</div>';
    const bt = S.bluetooth || {};
    h += '<div class="es-h">Bluetooth</div><div class="es-note" style="font-size:12px;line-height:1.7">' +
      `${bt.ready ? '<b>Bluetooth is ready.</b>' : esc(bt.text || 'Checking Bluetooth...')}` +
      (bt.notes || []).map((n) => `<br>${esc(n)}`).join('') + '</div>';
    h += '<div class="es-h">Phone</div><div class="es-note" style="font-size:12px;line-height:1.7">' +
      `Messages: ${p.messages ? 'connected' : 'not connected'}<br>Notifications: ${p.notifications ? 'connected' : (p.ancs_reason ? esc(p.ancs_reason) : 'not connected')}<br>Calls: ${p.calls ? 'connected' : p.calls_restore === 'pending' ? 'reconnecting...' : 'not connected'}` +
      (p.calls_restore === 'unverified' ? '<br>The call link did not come back after the headset took it back. Turn Bluetooth off and on again on the iPhone.' : '') +
      (p.profile_reason ? `<br>${esc(p.profile_reason)}` : '') + '</div>' +
      '<div class="es-row es-col"><div class="es-choice">' +
      `<button data-act="pair-start">${p.paired ? 'Pair a different iPhone' : 'Pair iPhone'}</button>` +
      (p.paired ? '<button data-act="forget-ask">Forget this iPhone</button>' : '') + '</div></div>';
    const u = updateText(S.update);
    const versions = (S.update && S.update.versions) || {};
    h += '<div class="es-h">Updates</div>' + toggle('Update automatically', st.auto_update !== false, 'auto_update', 'Checks for a new Earshot every few hours and installs it.') +
      `<div class="es-row es-col"><div class="es-choice"><button data-act="check-updates"${u.busy ? ' disabled' : ''}>${u.busy ? esc(u.text) : 'Check for updates'}</button></div>` +
      (u.text && !u.busy ? `<div class="es-note">${esc(u.text)}</div>` : '') + '</div>' +
      (Object.keys(versions).length ? `<div class="es-note" style="margin-top:8px">Installed: ${Object.keys(versions).map((k) => esc(k) + ' ' + esc(versions[k])).join(', ')}</div>` : '');
    return h + '</div>';
  }

  // ---------- pairing: every screen is drawn from S.pair, which lives in the helper ----------
  const pbtn = (act, label, cls, extra) => `<button class="es-pbtn${cls ? ' ' + cls : ''}" data-act="${act}"${extra || ''}>${esc(label)}</button>`;
  const phead = (t) => `<div class="es-ph">${esc(t)}</div>`;
  const clock = (n) => Math.floor(n / 60) + ':' + String(n % 60).padStart(2, '0');
  const spaced = (code) => String(code || '').replace(/^(\d{3})(\d{3})$/, '$1 $2');
  function leftSeconds(p) { return Math.max(0, Math.round((p.ends_at || 0) - Date.now() / 1000)); }

  function pairHtml(p) {
    let h = '';
    switch (p.phase) {
      case 'blocked':
        h = phead('Earshot cannot pair yet') +
          (p.reasons || []).map((r) => `<div class="es-pstep">${esc(r)}</div>`).join('') +
          pbtn('pair-start', 'Check again', 'es-blue') + pbtn('pair-close', 'Close', 'es-quiet');
        break;
      case 'listen': {
        const left = leftSeconds(p), over = p.expired || left <= 0;
        h = phead('Pair your iPhone') +
          `<div class="es-pstep">On your iPhone, open <b>Settings &gt; Bluetooth</b> and tap <b>${esc(p.headset || 'this headset')}</b> under <b>Other Devices</b>.</div>` +
          `<div class="es-ptext" id="earshot-vis-note">${over ? 'The headset is not visible any more.' : 'The headset is visible for:'}</div>` +
          `<div class="es-count" id="earshot-count">${over ? '0:00' : clock(left)}</div>` +
          pbtn('pair-listen-again', 'Make visible again', 'es-blue', (over ? '' : ' disabled') + ' id="earshot-visible-again"') +
          pbtn('pair-other', 'Pair a different way', 'es-quiet') + pbtn('pair-close', 'Cancel', 'es-quiet');
        break;
      }
      case 'search': {
        h = phead('Pair your iPhone') +
          (p.fallback ? '<div class="es-ptext">This Earshot cannot wait for the iPhone, so it will search for it instead.</div>' : '') +
          '<div class="es-pstep">On your iPhone, open <b>Settings &gt; Bluetooth</b> and keep that screen open.</div>';
        if (!p.searched) h += pbtn('pair-search', 'Search', 'es-blue');
        else {
          if (p.searching) h += '<div class="es-pwait">Searching…</div>';
          h += (p.devices || []).map((d) => `<button class="es-pdev${d.iphone ? ' es-phone' : ''}" data-act="pair-pick" data-a="${esc(d.address)}"><span>${esc(d.name)}</span>` +
            `<span class="es-tag">${d.redo ? 'Paired before. Pair again' : d.iphone ? 'iPhone' : 'Other device'}</span></button>`).join('');
          if (!p.searching) {
            if (!(p.devices || []).length && p.message) h += `<div class="es-ptext">${esc(p.message)}</div>`;
            h += pbtn('pair-search', 'Search again', (p.devices || []).length ? '' : 'es-blue');
          }
        }
        if (p.can_listen) h += pbtn('pair-listen', 'Wait for the iPhone instead', 'es-quiet');
        h += pbtn('pair-close', 'Cancel', 'es-quiet');
        break;
      }
      case 'pairing':
        h = phead(p.name ? 'Pairing with ' + p.name : 'Pairing') +
          `<div class="es-pwait">${esc(p.progress || 'Working…')}</div>` +
          '<div class="es-ptext">Keep the iPhone unlocked and on this screen.</div>' +
          (p.can_close ? pbtn('pair-close', 'Close', 'es-quiet') : '');
        break;
      case 'confirm':
        h = phead('Check the code') + `<div class="es-code">${esc(spaced(p.code))}</div>` +
          '<div class="es-prow">' + pbtn('pair-confirm', 'Confirm', 'es-go', ` data-a="${esc(p.op)}"`) + pbtn('pair-close', 'Not this code', 'es-stop') + '</div>' +
          '<div class="es-pstep">Check the iPhone shows the same code, then tap <b>Pair</b> on the iPhone and <b>Confirm</b> here.</div>';
        break;
      case 'failed':
        h = phead(p.title) + `<div class="es-ptext" style="font-size:16px">${esc(p.text)}</div>` +
          pbtn('pair-retry', p.button || 'Try again', 'es-blue') + pbtn('pair-close', 'Close', 'es-quiet');
        break;
      case 'weak':
        h = phead('Paired, but notifications will not work') +
          '<div class="es-ptext" style="font-size:16px">Your iPhone and this headset are paired, but without the part notifications need. Texts and contacts can still work. ' +
          'To get notifications, tap <b>Forget This Device</b> for this headset on your iPhone (Settings &gt; Bluetooth), then tap Try again.</div>' +
          pbtn('pair-retry', 'Try again', 'es-blue') + pbtn('pair-keep', 'Keep it without notifications', 'es-quiet');
        break;
      case 'almost': {
        h = phead('Almost done') +
          `<div class="es-pstep">On your iPhone, open <b>Settings &gt; Bluetooth</b>, tap the <b>(i)</b> next to <b>${esc(p.headset || 'this headset')}</b> and turn these on. They can take a minute to appear: close and reopen that screen.</div>` +
          (p.connected ? '' : `<div class="es-ptext">Waiting for the iPhone to connect…${p.reason ? ' ' + esc(p.reason) : ''}</div>`) +
          (p.items || []).map((i) => `<div class="es-chk es-${i.state === 'on' ? 'on' : i.state === 'off' ? 'off' : 'wait'}" data-item="${esc(i.id)}"><span class="es-mark">${i.state === 'on' ? '&#10003;' : i.state === 'off' ? '!' : '&middot;'}</span>` +
            `<div><div class="es-lab">${esc(i.label)}</div><div class="es-note">${esc(i.note)}</div></div></div>`).join('') +
          (p.ready ? pbtn('pair-done', 'Done', 'es-go') : pbtn('pair-done', 'I will finish later', 'es-quiet'));
        break;
      }
      case 'forget':
        h = phead('Forget this iPhone?') + '<div class="es-ptext" style="font-size:16px">Earshot will stop using it. You can pair it again later.</div>' +
          pbtn('forget-do', 'Forget this iPhone', 'es-stop') + pbtn('pair-close', 'Keep it', 'es-quiet');
        break;
      case 'forgetting':
        h = phead('Forgetting…') + '<div class="es-pwait">One moment.</div>';
        break;
      case 'forgotten':
        h = phead('Now finish on the iPhone') +
          `<div class="es-pstep">On your iPhone, open <b>Settings &gt; Bluetooth</b>, tap the <b>(i)</b> next to <b>${esc(p.headset || 'this headset')}</b> and tap <b>Forget This Device</b>.</div>` +
          pbtn('pair-start', 'Pair iPhone', 'es-blue') + pbtn('pair-done', 'Done', 'es-quiet');
        break;
      default:
        h = phead('Pairing') + pbtn('pair-close', 'Close', 'es-quiet');
    }
    return `<div class="es-pair${p.phase === 'confirm' ? ' es-confirm' : ''}" data-phase="${esc(p.phase)}">${h}</div>`;
  }

  // The countdown is the only thing that moves without a state push.
  let pairTick = null;
  function tickPair() {
    const el = document.getElementById('earshot-count');
    if (!el || !S || !S.pair || S.pair.phase !== 'listen') return;
    const left = leftSeconds(S.pair);
    el.textContent = clock(left);
    const over = S.pair.expired || left <= 0;
    const b = document.getElementById('earshot-visible-again'); if (b) b.disabled = !over;
    const note = document.getElementById('earshot-vis-note');
    if (note) note.textContent = over ? 'The headset is not visible any more.' : 'The headset is visible for:';
  }
  pairTick = setInterval(tickPair, 1000);
  cleanups.push(() => clearInterval(pairTick));

  // ---------- popup: Phone tab ----------
  const TAB_ID = 'earshot-qam-tab';
  const OVERLAY_ID = 'earshot-overlay';
  let menuOpen = false;
  let interacting = false, renderQueued = false, releaseTimer = null;
  const tabStyles = new Map();
  let stripListener = null, listenedStrip = null;
  let wantOpenUntil = 0;
  // A popup page that appears while a pairing is going on (the quick menu was closed and opened again, or
  // opened on one of Steam's own tabs) opens straight on the Phone tab, at the pairing panel.
  let pendingPairOpen = true, lastPairPhase = null;
  function maybeOpenForPair() {
    if (ROLE !== 'popup' || !pendingPairOpen || !S || !S.pair || menuOpen) return;
    if (!document.getElementById(OVERLAY_ID) || !popupParts()) return;
    pendingPairOpen = false;
    setMenuOpen(true);
  }

  function popupParts() {
    const panel = document.querySelector('[role="tabpanel"]');
    if (!panel) return null;
    const labelled = panel.getAttribute('aria-labelledby');
    let tab = labelled && document.getElementById(labelled);
    if (!tab) tab = document.querySelector('[aria-label="Battery Info"],[aria-label="Quick Settings"]');
    const strip = tab && tab.parentElement;
    if (!strip || strip.children.length < 2) return null;
    const stripWrap = strip.parentElement;
    const root = stripWrap && stripWrap.parentElement;
    if (!root) return null;
    const content = [...root.children].find((c) => c !== stripWrap && c.contains(panel));
    if (!content) return null;
    return { panel, strip, stripWrap, root, content };
  }
  function activeClasses(strip) {
    const tabs = [...strip.children].filter((t) => t.id !== TAB_ID && !/^earshot-/.test(t.id || '') && !/^magicpods-/.test(t.id || ''));
    if (!tabs.length) return { common: [], extra: [], active: null };
    const lists = tabs.map((t) => new Set(t.classList));
    const common = [...lists[0]].filter((c) => lists.every((l) => l.has(c)));
    const active = tabs.find((t) => [...t.classList].some((c) => !common.includes(c) && !/^gpfocus/.test(c)));
    const extra = active ? [...active.classList].filter((c) => !common.includes(c) && !/^gpfocus/.test(c)) : [];
    return { common, extra, active };
  }
  let lastPresent = null;
  function reportPresence(present) {
    if (present === lastPresent) return;
    lastPresent = present;
    channel.postMessage({ type: 'popup-visible', visible: present });
  }

  function updateTabBadge() {
    const tab = document.getElementById(TAB_ID);
    if (!tab) return;
    const n = unreadAlerts();
    let b = tab.querySelector('.es-tabbadge');
    if (!n) { if (b) b.remove(); tab.setAttribute('aria-label', 'Phone'); return; }
    if (!b) { b = document.createElement('span'); b.className = 'es-badge es-tabbadge'; tab.appendChild(b); }
    const t = n > 99 ? '99+' : String(n);
    if (b.textContent !== t) b.textContent = t;
    tab.setAttribute('aria-label', 'Phone, ' + n + ' new notification' + (n === 1 ? '' : 's'));
  }

  function ensurePopup() {
    const p = popupParts();
    reportPresence(!!p);
    if (!p) {  // popup closed: next open shows Steam's own tab
      menuOpen = false; pendingPairOpen = true; fresh.clear();
      const gone = document.getElementById(OVERLAY_ID); if (gone) gone.classList.remove('es-open');
      return;
    }
    ensureStyle();
    const { strip, stripWrap, root } = p;
    let tab = document.getElementById(TAB_ID);
    if (!tab || tab.parentElement !== strip || tab.dataset.esVersion !== VERSION) {
      if (tab) tab.remove();
      const { common } = activeClasses(strip);
      tab = document.createElement('div');
      tab.id = TAB_ID;
      tab.dataset.esVersion = VERSION;
      tab.className = common.join(' ');
      tab.setAttribute('aria-label', 'Phone');
      tab.setAttribute('role', 'button');
      tab.innerHTML = PHONE_ICON.replace('width="20" height="20"', 'width="26" height="26"');
      tab.style.display = 'flex'; tab.style.alignItems = 'center'; tab.style.justifyContent = 'center'; tab.style.color = 'currentColor'; tab.style.position = 'relative';
      tab.addEventListener('click', (ev) => { ev.preventDefault(); ev.stopPropagation(); setMenuOpen(true); });
      strip.appendChild(tab);
    }
    const n = strip.children.length;
    for (const t of strip.children) {
      if (t.id !== TAB_ID && !tabStyles.has(t)) tabStyles.set(t, { width: t.style.width, minWidth: t.style.minWidth, flex: t.style.flex });
      t.style.width = (100 / n) + '%'; t.style.minWidth = '0'; t.style.flex = '1 1 0';
    }
    if (listenedStrip !== strip) {
      if (listenedStrip && stripListener) listenedStrip.removeEventListener('click', stripListener, true);
      stripListener = (ev) => {
        const t = document.getElementById(TAB_ID);
        if (t && t.contains(ev.target)) return;
        const clicked = [...strip.children].find((c) => c.contains(ev.target));
        setMenuOpen(false, clicked);
      };
      strip.addEventListener('click', stripListener, true);
      listenedStrip = strip;
    }
    let overlay = document.getElementById(OVERLAY_ID);
    if (!overlay || overlay.parentElement !== root || overlay.dataset.esVersion !== VERSION) {
      if (overlay) overlay.remove();
      overlay = document.createElement('div');
      overlay.id = OVERLAY_ID;
      overlay.dataset.esVersion = VERSION;
      overlay.innerHTML = '<div id="earshot-pair"></div><div id="earshot-head"></div><div id="earshot-call"></div><div class="es-tabs" id="earshot-tabs"></div><div id="earshot-body"></div>';
      overlay.addEventListener('click', onMenuEvent);
      const release = () => { clearTimeout(releaseTimer); interacting = false; if (renderQueued) { renderQueued = false; render(); } };
      overlay.addEventListener('pointerdown', () => { interacting = true; clearTimeout(releaseTimer); releaseTimer = setTimeout(release, 5000); }, true);
      overlay.addEventListener('pointerup', () => { clearTimeout(releaseTimer); releaseTimer = setTimeout(release, 400); }, true);
      overlay.addEventListener('pointercancel', release, true);
      if (getComputedStyle(root).position === 'static') root.style.position = 'relative';
      root.appendChild(overlay);
    }
    overlay.style.height = Math.max(0, root.clientHeight - stripWrap.offsetHeight) + 'px';
    if (wantOpenUntil > Date.now()) { wantOpenUntil = 0; setMenuOpen(true); }
    applyTabState(p);
    maybeOpenForPair();
    updateTabBadge();
  }
  function applyTabState(p, clicked) {
    const parts = p || popupParts();
    if (!parts) return;
    const overlay = document.getElementById(OVERLAY_ID);
    const tab = document.getElementById(TAB_ID);
    if (!overlay || !tab) return;
    overlay.classList.toggle('es-open', menuOpen);
    const { extra, active } = activeClasses(parts.strip);
    if (menuOpen) {
      if (active && extra.length) { parts.strip.__esNativeActive = active; parts.strip.__esExtra = extra; active.classList.remove(...extra); }
      if (parts.strip.__esExtra) tab.classList.add(...parts.strip.__esExtra);
    } else {
      if (parts.strip.__esExtra) tab.classList.remove(...parts.strip.__esExtra);
      const native = parts.strip.__esNativeActive;
      // React re-highlights a newly clicked tab itself; restore only the previous one.
      if (native && native.isConnected && parts.strip.__esExtra && (!clicked || clicked === native)) native.classList.add(...parts.strip.__esExtra);
      parts.strip.__esNativeActive = null;
    }
  }
  function setMenuOpen(open, clicked) {
    if (menuOpen === open) { applyTabState(); return; }
    menuOpen = open;
    if (!open) fresh.clear();
    if (open) pendingPairOpen = false;
    applyTabState(null, clicked);
    if (open) {
      act({ type: 'hello' });
      const ov = document.getElementById(OVERLAY_ID); if (ov) ov.scrollTop = 0;
      lastPairPhase = null;
      render(true);
      if (ov) ov.scrollTop = 0;   // whatever the render changed, the top is what a person should see first
    }
  }

  // ---------- events ----------
  function sendText(thread, text) {
    text = String(text || '').trim();
    if (!text) return;
    const cid = 'c' + Date.now().toString(36) + (++cidCounter);
    local[cid] = { thread, text };
    act({ type: 'send', thread, text, cid });
    render();
    scrollNewest();
  }
  function doSend(resendCid) {
    if (!S || !S.open) return;
    const thread = S.open.thread;
    if (resendCid) {
      const l = local[resendCid]; if (!l) return;
      delete local[resendCid];
      sendText(l.thread, l.text);
      return;
    }
    const d = draftOf(thread);
    if (!d.trim()) return;
    drafts[thread] = '';
    act({ type: 'draft', thread, text: '' });
    sendText(thread, d);
    refreshReply();
  }
  function onMenuEvent(ev) {
    const el = ev.target.closest && ev.target.closest('[data-act]');
    if (!el || el.disabled) return;
    ev.preventDefault(); ev.stopPropagation();
    interacting = false; renderQueued = false;
    const a = el.getAttribute('data-a'), b = el.getAttribute('data-b');
    switch (el.getAttribute('data-act')) {
      case 'tab': saveUi({ tab: a }); if (a !== 'messages' && S && S.open) act({ type: 'close_thread' }); render(true); break;
      case 'retry': act({ type: 'retry' }); break;
      case 'call-answer': act({ type: 'call', op: 'answer', path: a || undefined, uid: b ? Number(b) : undefined }); break;
      case 'call-decline': act({ type: 'call', op: 'decline', path: a || undefined, uid: b ? Number(b) : undefined }); break;
      case 'clear-all': act({ type: 'clear_all' }); break;
      case 'dismiss': act({ type: 'notif_action', uid: Number(b), action: 'negative' }); break;
      case 'open-thread': act({ type: 'open_thread', thread: a }); if (S) S.open = { thread: a, messages: [] }; render(true); break;
      case 'back': act({ type: 'close_thread' }); if (S) S.open = null; render(true); break;
      case 'send': doSend(); break;
      case 'key': typeKey(a); break;
      case 'quick': if (S && S.open) sendText(S.open.thread, (S.quick_replies || [])[Number(a)]); break;
      case 'resend': doSend(a); break;
      case 'discard': delete local[a]; render(); break;
      case 'setting-set': act({ type: 'set_setting', key: a, value: b }); break;
      case 'setting-toggle': act({ type: 'set_setting', key: a, value: b === '1' }); break;
      case 'mute': act({ type: 'mute', app_id: a, muted: b === '1' }); break;
      case 'check-updates': act({ type: 'check_updates' }); break;
      case 'pair-start': act({ type: 'pair_start' }); break;
      case 'pair-search': act({ type: 'pair_search' }); break;
      case 'pair-pick': act({ type: 'pair_pick', address: a }); break;
      case 'pair-confirm': act({ type: 'pair_confirm', op: a, accept: true }); break;
      case 'pair-retry': act({ type: 'pair_retry' }); break;
      case 'pair-keep': act({ type: 'pair_keep' }); break;
      case 'pair-close': act({ type: 'pair_close' }); break;
      case 'pair-done': act({ type: 'pair_done' }); break;
      case 'pair-listen-again': act({ type: 'pair_listen_again' }); break;
      case 'pair-listen': act({ type: 'pair_listen' }); break;
      case 'pair-other': act({ type: 'pair_other' }); break;
      case 'forget-ask': act({ type: 'forget_ask' }); break;
      case 'forget-do': act({ type: 'forget_do' }); break;
    }
  }

  // ---------- render ----------
  function setHtml(id, html) {
    const el = document.getElementById(id);
    if (el && el.__esHtml !== html) { el.innerHTML = html; el.__esHtml = html; }
    return el;
  }
  function render(force) {
    if (dead) return;
    if (ROLE === 'bar') { ensureBar(); return; }
    const overlay = document.getElementById(OVERLAY_ID);
    if (!overlay || !menuOpen) { updateTabBadge(); return; }
    if (interacting && !force) { renderQueued = true; return; }
    const inConv = !!(S && !S.pair && !(S.setup && ui.tab !== 'settings') && ui.tab === 'messages' && S.open);
    overlay.classList.toggle('es-convmode', inConv);
    if (S && !S.pair && !(S.setup && ui.tab !== 'settings') && ui.tab === 'alerts') markAlertsSeen(); else fresh.clear();
    updateTabBadge();
    const body = document.getElementById('earshot-body');
    if (S && S.pair) {
      // A pairing is in progress (or finishing): its panel is the first thing in the tab, above everything else,
      // and nothing else is drawn but a ringing call, which stays answerable below it.
      setHtml('earshot-head', '');
      setHtml('earshot-tabs', '');
      setHtml('earshot-body', '');
      setHtml('earshot-pair', pairHtml(S.pair));
      setHtml('earshot-call', callCardHtml());
      if (lastPairPhase !== S.pair.phase) overlay.scrollTop = 0;   // a new step starts at its top
      lastPairPhase = S.pair.phase;
      return;
    }
    lastPairPhase = null;
    setHtml('earshot-pair', '');
    setHtml('earshot-head', inConv ? '' : headHtml());
    setHtml('earshot-call', callCardHtml());
    setHtml('earshot-tabs', tabsHtml());
    if (!S) { setHtml('earshot-body', '<div class="es-msg">Waiting for the Earshot helper…</div>'); return; }
    if (S.setup && ui.tab !== 'settings') { setHtml('earshot-body', setupHtml(S.setup)); return; }
    if (ui.tab === 'settings') { setHtml('earshot-body', pageSettings()); return; }
    if (ui.tab === 'alerts') { setHtml('earshot-body', pageAlerts()); return; }
    if (ui.tab === 'messages') {
      const conv = document.getElementById('earshot-conv');
      if (S.open) {
        const can = threadMeta().repliable !== false;
        if (conv && conv.dataset.thread === S.open.thread && conv.dataset.can === String(can) && body.contains(conv)) {
          // Refresh in place: the keyboard and the scroll position of the messages stay where they are.
          const box = document.getElementById('earshot-msgs');
          const wasNear = !box || box.scrollHeight - box.scrollTop - box.clientHeight < 80;
          setHtml('earshot-msgs', bubblesHtml());
          if (wasNear) scrollNewest();
          const name = document.getElementById('earshot-convname'); if (name) name.textContent = threadMeta().name || 'Conversation';
          if (can) refreshReply();
          body.__esHtml = null;
          return;
        }
        setHtml('earshot-body', threadView());
        scrollNewest();
        requestAnimationFrame(scrollNewest);
        return;
      }
      setHtml('earshot-body', threadList());
      return;
    }
    setHtml('earshot-body', pageCalls());
  }

  // ---------- bar: indicator ----------
  const IND_ID = 'earshot-bar-indicator';
  let popupVisible = false;
  function ensureBar() {
    const anchor = document.querySelector('[aria-label^="Headset Battery"]');
    if (!anchor || !anchor.parentElement) return;
    let el = document.getElementById(IND_ID);
    if (!el || el.parentElement !== anchor.parentElement || el.dataset.esVersion !== VERSION) {
      if (el) el.remove();
      el = document.createElement('div');
      el.id = IND_ID;
      el.dataset.esVersion = VERSION;
      el.setAttribute('role', 'button');
      el.style.cssText = 'display:none;align-items:center;gap:3px;margin-right:14px;cursor:pointer;pointer-events:auto;align-self:center;';
      el.innerHTML = '<i class="es-ic" style="display:flex;font-style:normal"></i><span></span>';
      el.addEventListener('click', (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        channel.postMessage({ type: 'open' });
        if (!popupVisible) {
          const a = document.querySelector('[aria-label^="Headset Battery"]');
          if (a) a.click();
        }
      });
      anchor.parentElement.insertBefore(el, anchor);
    }
    const ref = anchor.querySelector('span');  // match Steam's own label (it may render after us)
    const span = el.querySelector('span');
    if (ref) {
      if (span.className !== ref.className) span.className = ref.className;
      const cs = getComputedStyle(ref);
      for (const k of ['fontSize', 'fontWeight', 'lineHeight', 'fontFamily', 'letterSpacing']) {
        if (span.style[k] !== cs[k]) span.style[k] = cs[k];
      }
    }
    // Only show while the phone is connected (or a call is ringing), so it costs no space otherwise.
    const ringing = !!(S && S.calls && S.calls.incoming);
    const show = !!(S && !S.setup && S.phone && S.phone.connected) || ringing;
    if (!show) { el.style.display = 'none'; return; }
    el.style.display = 'flex';
    const unread = S.unread || 0;
    const ic = el.querySelector('.es-ic');
    const icon = PHONE_ICON.replace('fill="currentColor"', ringing ? 'fill="#59bf40"' : 'fill="currentColor"');
    if (ic.__esHtml !== icon) { ic.innerHTML = icon; ic.__esHtml = icon; }
    span.textContent = ringing ? 'Call' : (unread > 0 ? String(unread > 99 ? '99+' : unread) : '');
    el.style.opacity = S.priority && S.priority.frame_active ? '1' : '0.5';
    const title = ringing ? 'Incoming call' : 'Phone' + (unread ? ', ' + unread + ' unread' : '');
    el.setAttribute('aria-label', title); el.title = title;
  }

  cleanups.push(() => {
    if (ROLE === 'popup') { try { setMenuOpen(false); } catch (e) { } }
    if (listenedStrip && stripListener) listenedStrip.removeEventListener('click', stripListener, true);
    for (const [t, st] of tabStyles) { if (t.isConnected) { t.style.width = st.width; t.style.minWidth = st.minWidth; t.style.flex = st.flex; } }
    tabStyles.clear();
    clearTimeout(releaseTimer);
  });
  cleanups.push(() => { for (const id of [IND_ID, TAB_ID, OVERLAY_ID]) { const e = document.getElementById(id); if (e) e.remove(); } });

  // ---------- wiring ----------
  function ensure() {
    if (dead) return;
    try { if (ROLE === 'bar') ensureBar(); else ensurePopup(); } catch (e) { console.warn('earshot:', e); }
  }
  channel.onmessage = (ev) => {
    const m = ev.data || {};
    if (m.type === 'open' && ROLE === 'popup') {
      wantOpenUntil = Date.now() + 3000;
      ensure();
      if (popupParts()) { wantOpenUntil = 0; setMenuOpen(true); }
    } else if (m.type === 'hello' && ROLE === 'popup') {
      lastPresent = null; ensure();
    } else if (m.type === 'popup-visible' && ROLE === 'bar') {
      popupVisible = !!m.visible;
    }
  };
  let scheduled = false;
  const observer = new MutationObserver((records) => {
    if (records.every((r) => r.target.id === OVERLAY_ID || (r.target.closest && r.target.closest('#' + OVERLAY_ID)))) return;
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; ensure(); });
  });
  observer.observe(document.body, { childList: true, subtree: true });
  cleanups.push(() => observer.disconnect());

  window.__earshotMenu = {
    version: VERSION, role: ROLE, ensure, setState,
    teardown() { dead = true; for (const c of cleanups.reverse()) { try { c(); } catch (e) { } } delete window.__earshotMenu; },
  };
  ensure();
  act({ type: 'hello' });
  if (ROLE === 'bar') channel.postMessage({ type: 'hello' });
  return 'installed ' + VERSION + ' (' + ROLE + ')';
  } catch (e) {
    dead = true;
    for (const c of cleanups.reverse()) { try { c(); } catch (_) { } }
    if (window.__earshotMenu && window.__earshotMenu.version === VERSION) delete window.__earshotMenu;
    throw e;
  }
})()
