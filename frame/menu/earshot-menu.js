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
  .es-sec { padding:6px 14px 2px; }
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
  .es-banner { margin:8px 10px; padding:8px 10px; border-radius:3px; background:#3d2a1a; color:#ffcf99; }
  .es-banner code, .es-setup code { display:block; margin-top:4px; padding:4px 6px; background:#10161d; color:#dcdedf; border-radius:3px; font-family:monospace; font-size:11px; user-select:all; word-break:break-all; }
  .es-setup { margin:8px 10px; padding:10px 12px; border-radius:4px; background:#23262e; }
  .es-setup b { color:#fff; }
  .es-setup .es-step { margin-top:8px; font-size:12px; color:#b8bcbf; }
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

  // ---------- pieces ----------
  function headHtml() {
    if (!S) return '<div class="es-head"><div class="es-name">Phone</div></div>';
    const p = S.phone || {}, pr = S.priority || {};
    const where = pr.frame_active ? 'Alerts here' : 'Alerts on phone';
    let sub = '';
    if (S.setup) sub = '';
    else if (!p.connected) sub = p.reason || 'Waiting for the iPhone.';
    else if (!pr.frame_active) sub = 'Headset not in use: the phone handles calls and alerts.';
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
    const notifs = S ? (S.notifications || []).length : 0;
    const tabs = [['calls', 'Calls', ringing], ['alerts', 'Alerts', notifs], ['messages', 'Messages', unread], ['settings', 'Settings', 0]];
    return tabs.map(([k, label, n]) => `<button class="${ui.tab === k ? 'es-on' : ''}" data-act="tab" data-a="${k}">${label}${badge(n)}</button>`).join('');
  }

  function setupHtml(setup) {
    return `<div class="es-setup"><b>${esc(setup.title)}</b><div class="es-note" style="margin-top:4px">${esc(setup.text)}</div>` +
      (setup.steps || []).map((s) => `<div class="es-step">${esc(s.what)}${s.command ? `<code>${esc(s.command)}</code>` : ''}</div>`).join('') +
      `<div class="es-btns" style="margin-top:10px"><button class="es-btn es-blue" data-act="retry">Check again</button></div></div>`;
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
    h += list.map((n) => `<div class="es-item"><div class="es-main"><div class="es-top"><span class="es-app">${esc(n.app || 'iPhone')}</span>` +
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
      return `<div class="es-msg">${S.phone && S.phone.messages ? 'No conversations yet.' : 'Messages need Show Notifications and Sync Contacts on the iPhone for this headset, and Bluetooth messaging allowed.'}</div>`;
    }
    return list.map((t) => `<div class="es-item es-click" data-act="open-thread" data-a="${esc(t.thread)}"><div class="es-main"><div class="es-top">` +
      `<span class="es-app">${esc(t.name || 'Unknown')}</span><span class="es-time">${esc(whenText(t.timestamp))}</span></div>` +
      `<div class="es-text">${esc(t.preview)}</div></div>${t.unread ? `<span class="es-unread">${badge(t.unread)}</span>` : ''}</div>`).join('');
  }

  function threadMeta() {
    const o = S.open;
    return (S.threads || []).find((t) => t.thread === o.thread) || { name: 'Conversation', repliable: true };
  }

  function bubblesHtml() {
    const o = S.open;
    let h = (o.messages || []).map((m) => `<div class="es-bub${m.outgoing ? ' es-out' : ''}">${esc(m.body)}<span class="es-meta">${esc(whenText(m.timestamp))}</span></div>`).join('');
    const status = {};
    for (const s of (S.sends || [])) status[s.cid] = s;
    for (const cid of Object.keys(local)) {
      const l = local[cid];
      if (l.thread !== o.thread) continue;
      const s = status[cid];
      if (s && s.status === 'sent') { delete local[cid]; continue; }
      const failed = s && s.status === 'failed';
      h += `<div class="es-bub es-out ${failed ? 'es-fail' : 'es-pend'}">${esc(l.text)}<span class="es-meta">${failed ? esc(s.error || 'Not sent') : 'Sending…'}` +
        (failed ? ` <button class="es-btn es-small" data-act="resend" data-a="${esc(cid)}">Retry</button> <button class="es-btn es-small" data-act="discard" data-a="${esc(cid)}">Discard</button>` : '') + '</span></div>';
    }
    return h || '<div class="es-msg">No messages loaded yet.</div>';
  }

  function threadView() {
    const meta = threadMeta();
    const can = meta.repliable !== false;
    return `<div class="es-bar"><button class="es-btn es-small" data-act="back">Back</button><span class="es-grow">${esc(meta.name || 'Conversation')}</span></div>` +
      `<div class="es-msgs" id="earshot-msgs">${bubblesHtml()}</div>` +
      (can
        ? `<div class="es-compose" id="earshot-composer" data-thread="${esc(S.open.thread)}"><input type="text" id="earshot-input" enterkeyhint="send" autocomplete="off" autocapitalize="sentences" placeholder="Message" aria-label="Message">` +
          `<button class="es-btn es-blue" data-act="send">Send</button></div>`
        : `<div class="es-msg">${esc(meta.reply_reason || 'This conversation cannot be replied to.')}</div>`);
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
    let h = '<div class="es-sec"><div class="es-h">Who gets calls and alerts</div><div class="es-row es-col"><div class="es-choice">' +
      modes.map(([k, l]) => `<button class="${st.priority === k ? 'es-on' : ''}" data-act="setting-set" data-a="priority" data-b="${k}">${l}</button>`).join('') + '</div>' +
      `<div class="es-note">Right now: ${pr.frame_active ? 'the headset' : 'the phone'}. ` +
      (st.priority === 'auto' ? 'Auto follows whether SteamVR is running until the headset can tell when it is worn. ' : '') +
      'When the phone has priority, Earshot lets go of the call link so the phone rings normally.</div></div>';
    h += '<div class="es-h">Alerts</div>' +
      toggle('Show alerts in the headset', !!st.alerts, 'alerts', 'Pop-ups appear while the headset has priority.') +
      toggle('Show message text in alerts', !!st.preview, 'preview', 'Off shows only who it is from.');
    const apps = S.apps || [];
    h += '<div class="es-h">Mute apps</div>';
    h += apps.length ? apps.map((a) => `<div class="es-row"><span>${esc(a.name)}</span><button class="es-sw${a.muted ? ' es-on' : ''}" data-act="mute" data-a="${esc(a.id)}" data-b="${a.muted ? '0' : '1'}" aria-label="Mute ${esc(a.name)}" aria-pressed="${a.muted ? 'true' : 'false'}"></button></div>`).join('') +
      '<div class="es-note">On means no pop-up. The app still shows under Alerts.</div>'
      : '<div class="es-note">Apps that send notifications will be listed here.</div>';
    h += '<div class="es-h">Phone</div><div class="es-note" style="font-size:12px;line-height:1.7">' +
      `Messages: ${p.messages ? 'connected' : 'not connected'}<br>Notifications: ${p.notifications ? 'connected' : (p.ancs_reason ? esc(p.ancs_reason) : 'not connected')}<br>Calls: ${p.calls ? 'connected' : p.calls_restore === 'pending' ? 'reconnecting...' : 'not connected'}` +
      (p.calls_restore === 'unverified' ? '<br>The call link did not come back after the headset took it back. Turn Bluetooth off and on again on the iPhone.' : '') +
      (p.profile_reason ? `<br>${esc(p.profile_reason)}` : '') + '</div>';
    const u = updateText(S.update);
    const versions = (S.update && S.update.versions) || {};
    h += '<div class="es-h">Updates</div>' + toggle('Update automatically', st.auto_update !== false, 'auto_update', 'Checks for a new Earshot every few hours and installs it.') +
      `<div class="es-row es-col"><div class="es-choice"><button data-act="check-updates"${u.busy ? ' disabled' : ''}>${u.busy ? esc(u.text) : 'Check for updates'}</button></div>` +
      (u.text && !u.busy ? `<div class="es-note">${esc(u.text)}</div>` : '') + '</div>' +
      (Object.keys(versions).length ? `<div class="es-note" style="margin-top:8px">Installed: ${Object.keys(versions).map((k) => esc(k) + ' ' + esc(versions[k])).join(', ')}</div>` : '');
    return h + '</div>';
  }

  // ---------- popup: Phone tab ----------
  const TAB_ID = 'earshot-qam-tab';
  const OVERLAY_ID = 'earshot-overlay';
  let menuOpen = false;
  let interacting = false, renderQueued = false, releaseTimer = null;
  const tabStyles = new Map();
  let stripListener = null, listenedStrip = null;
  let wantOpenUntil = 0;

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

  function ensurePopup() {
    const p = popupParts();
    reportPresence(!!p);
    if (!p) { menuOpen = false; return; }  // popup closed: next open shows Steam's own tab
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
      tab.style.display = 'flex'; tab.style.alignItems = 'center'; tab.style.justifyContent = 'center'; tab.style.color = 'currentColor';
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
      overlay.innerHTML = '<div id="earshot-head"></div><div id="earshot-call"></div><div class="es-tabs" id="earshot-tabs"></div><div id="earshot-body"></div>';
      overlay.addEventListener('click', onMenuEvent);
      overlay.addEventListener('keydown', onKey);
      overlay.addEventListener('input', onInput);
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
    applyTabState(null, clicked);
    if (open) {
      act({ type: 'hello' });
      const ov = document.getElementById(OVERLAY_ID); if (ov) ov.scrollTop = 0;
      render(true);
    }
  }

  // ---------- events ----------
  function composerInput() { return document.getElementById('earshot-input'); }
  function doSend(resendCid) {
    if (!S || !S.open) return;
    const thread = S.open.thread;
    let text, cid;
    if (resendCid) {
      const l = local[resendCid]; if (!l) return;
      text = l.text; delete local[resendCid];
    } else {
      const input = composerInput(); if (!input) return;
      text = input.value.trim(); input.value = ''; drafts[thread] = '';
    }
    if (!text) return;
    cid = 'c' + Date.now().toString(36) + (++cidCounter);
    local[cid] = { thread, text };
    act({ type: 'send', thread, text, cid });
    render();
    const box = document.getElementById('earshot-msgs'); if (box) box.scrollTop = box.scrollHeight;
  }
  function onInput(ev) {
    if (ev.target && ev.target.id === 'earshot-input' && S && S.open) drafts[S.open.thread] = ev.target.value;
  }
  function onKey(ev) {
    if (ev.target && ev.target.id === 'earshot-input' && ev.key === 'Enter') { ev.preventDefault(); doSend(); }
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
      case 'resend': doSend(a); break;
      case 'discard': delete local[a]; render(); break;
      case 'setting-set': act({ type: 'set_setting', key: a, value: b }); break;
      case 'setting-toggle': act({ type: 'set_setting', key: a, value: b === '1' }); break;
      case 'mute': act({ type: 'mute', app_id: a, muted: b === '1' }); break;
      case 'check-updates': act({ type: 'check_updates' }); break;
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
    if (!overlay || !menuOpen) return;
    if (interacting && !force) { renderQueued = true; return; }
    setHtml('earshot-head', headHtml());
    setHtml('earshot-call', callCardHtml());
    setHtml('earshot-tabs', tabsHtml());
    const body = document.getElementById('earshot-body');
    if (!S) { setHtml('earshot-body', '<div class="es-msg">Waiting for the Earshot helper…</div>'); return; }
    if (S.setup && ui.tab !== 'settings') { setHtml('earshot-body', setupHtml(S.setup)); return; }
    if (ui.tab === 'settings') { setHtml('earshot-body', pageSettings()); return; }
    if (ui.tab === 'alerts') { setHtml('earshot-body', pageAlerts()); return; }
    if (ui.tab === 'messages') {
      const composer = document.getElementById('earshot-composer');
      if (S.open && composer && composer.dataset.thread === S.open.thread && body.contains(composer)) {
        // Keep the text box (and Steam's keyboard) alive: only refresh the bubbles and title.
        const box = setHtml('earshot-msgs', bubblesHtml());
        const near = box && box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        if (near && box) box.scrollTop = box.scrollHeight;
        return;
      }
      setHtml('earshot-body', pageMessages());
      const input = composerInput();
      if (input && S.open) {
        input.value = drafts[S.open.thread] || '';
        const box = document.getElementById('earshot-msgs'); if (box) box.scrollTop = box.scrollHeight;
      }
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
