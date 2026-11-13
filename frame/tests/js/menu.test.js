'use strict';
// Tests for frame/menu/earshot-menu.js under jsdom, with a stub of Steam's quick-menu page structure.
// Run: npm install && npm test   (in frame/tests/js)
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'menu', 'earshot-menu.js'), 'utf8');
const VERSION = 'abc123def456';
const SCRIPT = SOURCE.replaceAll('__VERSION__', VERSION).replaceAll('__BINDING__', 'earshotAction');

const POPUP_HTML = `<div id="root"><div id="stripwrap"><div id="strip">
  <div id="t1" class="tab">Battery Info</div><div id="t2" class="tab sel">Quick Settings</div></div></div>
  <div id="content"><div role="tabpanel" aria-labelledby="t1">native</div></div></div>`;
const BAR_HTML = `<div id="barrow"><div aria-label="Headset Battery 80%"><span class="lbl">80%</span></div></div>`;

const windows = [];
test.after(() => { for (const w of windows) { try { w.close(); } catch (e) { } } });

function page(kind, html) {
  const title = kind === 'popup' ? 'SteamVR - valve.steam.gamepadui.barpopup.1 (1)' : 'SteamVR - valve.steam.gamepadui.bar (2)';
  const dom = new JSDOM(`<!doctype html><title>${title}</title><body>${html}</body>`,
    { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window;
  w.actions = [];
  w.earshotAction = (text) => w.actions.push(JSON.parse(text));
  w.BroadcastChannel = class { constructor() { this.sent = []; } postMessage(m) { this.sent.push(m); } close() { } };
  windows.push(w);
  return { dom, w, doc: w.document, last: () => w.actions[w.actions.length - 1] };
}
const inject = (p) => p.w.eval(SCRIPT);
const tick = () => new Promise((r) => setTimeout(r, 30));
const click = (p, el) => el.dispatchEvent(new p.w.MouseEvent('click', { bubbles: true, cancelable: true }));
const byAct = (p, act, a) => p.doc.querySelector(`[data-act="${act}"]${a != null ? `[data-a="${a}"]` : ''}`);
const text = (p) => p.doc.getElementById('earshot-overlay').textContent;

const BASE = {
  v: 1, setup: null,
  phone: { name: 'Test Phone', connected: true, battery: 80, messages: true, notifications: true, calls: true, reason: '', profile_reason: '', ancs_reason: '' },
  priority: { mode: 'auto', frame_active: true, worn: true },
  calls: { items: [], incoming: null, recent: [{ name: 'Pat Example', number: '+15550100', type: 'missed', timestamp: 1000 }], error: '' },
  notifications: [{ uid: 7, app_id: 'com.example.chat', app: 'Chat', title: 'Hello', text: 'there', timestamp: 1000, category: 0, can_dismiss: true, can_accept: false }],
  threads: [{ thread: 'tel:+15550100', name: 'Pat Example', preview: 'hi', timestamp: 1000, unread: 2, repliable: true, reply_reason: '' }],
  open: null, sends: [], apps: [{ id: 'com.example.chat', name: 'Chat', muted: false }],
  settings: { priority: 'auto', alerts: true, preview: true, auto_update: true },
  update: { status: null, versions: { engine: '0.3.5' } }, unread: 2,
};
const withState = (patch) => Object.assign(JSON.parse(JSON.stringify(BASE)), patch);

function openPopup() {
  const p = page('popup', POPUP_HTML);
  assert.equal(inject(p), `installed ${VERSION} (popup)`);
  click(p, p.doc.getElementById('earshot-qam-tab'));
  return p;
}

test('adds a Phone tab and an overlay, and says hello to the helper', () => {
  const p = page('popup', POPUP_HTML);
  assert.equal(inject(p), `installed ${VERSION} (popup)`);
  assert.ok(p.doc.getElementById('earshot-qam-tab'));
  assert.ok(p.doc.getElementById('earshot-overlay'));
  assert.deepEqual(p.w.actions[0], { type: 'hello' });
  assert.equal(inject(p), `already ${VERSION}`);
  assert.equal(p.doc.querySelectorAll('#earshot-qam-tab').length, 1);
});

test('ignores pages that are not the bar or the popup', () => {
  const dom = new JSDOM('<!doctype html><title>SharedJSContext</title><body></body>', { runScripts: 'outside-only' });
  assert.equal(dom.window.eval(SCRIPT), 'not a target page');
});

test('opens on click and shows a waiting note until state arrives', () => {
  const p = openPopup();
  assert.ok(p.doc.getElementById('earshot-overlay').classList.contains('es-open'));
  assert.match(text(p), /Waiting for the Earshot helper/);
});

test('renders the calls tab with recent calls', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  const t = text(p);
  assert.match(t, /Test Phone/);
  assert.match(t, /80%/);
  assert.match(t, /Alerts here/);
  assert.match(t, /Pat Example/);
  assert.match(t, /Missed/);
});

test('incoming call card sends answer and decline', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(withState({ calls: { items: [], recent: [], error: '', incoming: { source: 'hfp', path: '/c1', name: 'Pat Example', number: '+15550100', ringing: true } } }));
  assert.match(text(p), /Incoming call/);
  click(p, byAct(p, 'call-answer'));
  assert.deepEqual(p.last(), { type: 'call', op: 'answer', path: '/c1' });
  click(p, byAct(p, 'call-decline'));
  assert.deepEqual(p.last(), { type: 'call', op: 'decline', path: '/c1' });
});

test('incoming call over ANCS passes the notification uid', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(withState({ calls: { items: [], recent: [], error: 'Not allowed', incoming: { source: 'ancs', uid: 80, name: 'Pat', ringing: true, can_answer: true, can_decline: false } } }));
  click(p, byAct(p, 'call-answer'));
  assert.deepEqual(p.last(), { type: 'call', op: 'answer', uid: 80 });
  assert.equal(byAct(p, 'call-decline'), null);
  assert.match(text(p), /Not allowed/);
});

test('alerts tab: dismiss acts on the phone, clear all is its own action', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  click(p, byAct(p, 'tab', 'alerts'));
  assert.match(text(p), /Chat/);
  assert.match(text(p), /Hello/);
  click(p, byAct(p, 'dismiss'));
  assert.deepEqual(p.last(), { type: 'notif_action', uid: 7, action: 'negative' });
  click(p, byAct(p, 'clear-all'));
  assert.deepEqual(p.last(), { type: 'clear_all' });
  p.w.__earshotMenu.setState(withState({ notifications: [] }));
  assert.match(text(p), /No notifications/);
});

test('a notification offering no dismiss action has no dismiss button', () => {
  const p = openPopup();
  const s = withState({});
  s.notifications[0].can_dismiss = false;
  p.w.__earshotMenu.setState(s);
  click(p, byAct(p, 'tab', 'alerts'));
  assert.equal(byAct(p, 'dismiss'), null);
});

test('all values are escaped', () => {
  const p = openPopup();
  const s = withState({});
  s.notifications[0].title = '<img src=x onerror="window.pwned=1">';
  s.notifications[0].text = '<script>window.pwned=2</script>';
  p.w.__earshotMenu.setState(s);
  click(p, byAct(p, 'tab', 'alerts'));
  assert.equal(p.doc.querySelector('#earshot-overlay img'), null);
  assert.equal(p.doc.querySelector('#earshot-overlay script'), null);
  assert.match(text(p), /<img src=x/);
  assert.equal(p.w.pwned, undefined);
});

test('messages: list, open a thread, type with the text field, send, fail, retry, sent', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  click(p, byAct(p, 'tab', 'messages'));
  assert.match(text(p), /Pat Example/);
  click(p, byAct(p, 'open-thread', 'tel:+15550100'));
  assert.deepEqual(p.last(), { type: 'open_thread', thread: 'tel:+15550100' });
  const open = { thread: 'tel:+15550100', messages: [{ handle: 'm1', body: 'hello there', timestamp: 1000, outgoing: false, read: true }] };
  p.w.__earshotMenu.setState(withState({ open }));
  const input = p.doc.getElementById('earshot-input');
  assert.ok(input, 'a plain text input, so Steam shows its own keyboard');
  assert.equal(input.getAttribute('type'), 'text');
  assert.match(text(p), /hello there/);

  // The text box survives state pushes (the keyboard must not be torn down mid-typing).
  input.value = 'on my';
  input.dispatchEvent(new p.w.Event('input', { bubbles: true }));
  p.w.__earshotMenu.setState(withState({ open: { thread: open.thread, messages: open.messages.concat([{ handle: 'm2', body: 'later msg', timestamp: 1001, outgoing: false, read: true }]) } }));
  assert.equal(p.doc.getElementById('earshot-input'), input);
  assert.equal(input.value, 'on my');
  assert.match(text(p), /later msg/);

  input.value = 'on my way';
  input.dispatchEvent(new p.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  const sent = p.last();
  assert.equal(sent.type, 'send');
  assert.equal(sent.thread, 'tel:+15550100');
  assert.equal(sent.text, 'on my way');
  assert.match(sent.cid, /^[A-Za-z0-9_.-]{1,40}$/);
  assert.equal(input.value, '');
  assert.match(text(p), /on my way/);
  assert.match(text(p), /Sending/);

  p.w.__earshotMenu.setState(withState({ open, sends: [{ cid: sent.cid, thread: open.thread, status: 'failed', error: 'Phone busy' }] }));
  assert.match(text(p), /Phone busy/);
  click(p, byAct(p, 'resend', sent.cid));
  const again = p.last();
  assert.equal(again.type, 'send');
  assert.equal(again.text, 'on my way');
  assert.notEqual(again.cid, sent.cid);

  p.w.__earshotMenu.setState(withState({ open, sends: [{ cid: again.cid, thread: open.thread, status: 'sent', error: '' }] }));
  assert.doesNotMatch(text(p), /Sending/);
  assert.doesNotMatch(text(p), /Phone busy/);
});

test('empty text is not sent; Back closes the thread', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(withState({ open: { thread: 'tel:+15550100', messages: [] } }));
  click(p, byAct(p, 'tab', 'messages'));
  const before = p.w.actions.length;
  click(p, byAct(p, 'send'));
  assert.equal(p.w.actions.length, before);
  click(p, byAct(p, 'back'));
  assert.deepEqual(p.last(), { type: 'close_thread' });
});

test('a conversation that cannot be replied to shows why instead of a text box', () => {
  const p = openPopup();
  const s = withState({ open: { thread: 'tel:12345', messages: [] } });
  s.threads = [{ thread: 'tel:12345', name: '12345', preview: '', timestamp: 1, unread: 0, repliable: false, reply_reason: 'No reply route.' }];
  p.w.__earshotMenu.setState(s);
  click(p, byAct(p, 'tab', 'messages'));
  assert.equal(p.doc.getElementById('earshot-input'), null);
  assert.match(text(p), /No reply route\./);
});

test('not set up yet: shows what is missing and the fix, keeps Settings reachable', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(withState({ setup: { kind: 'no_bluetooth', title: 'Bluetooth is not ready for Earshot', text: 'BlueZ is too old.', steps: [{ what: 'Enable experimental mode', command: 'sudo systemctl restart bluetooth' }] } }));
  assert.match(text(p), /Bluetooth is not ready/);
  assert.match(text(p), /sudo systemctl restart bluetooth/);
  assert.equal(p.doc.querySelector('.es-item'), null, 'no empty lists');
  click(p, byAct(p, 'retry'));
  assert.deepEqual(p.last(), { type: 'retry' });
  click(p, byAct(p, 'tab', 'settings'));
  assert.match(text(p), /Who gets calls and alerts/);
});

test('settings send their actions', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  click(p, byAct(p, 'tab', 'settings'));
  assert.match(text(p), /Installed: engine 0\.3\.5/);
  click(p, p.doc.querySelector('[data-act="setting-set"][data-b="phone"]'));
  assert.deepEqual(p.last(), { type: 'set_setting', key: 'priority', value: 'phone' });
  click(p, p.doc.querySelector('[data-act="setting-toggle"][data-a="alerts"]'));
  assert.deepEqual(p.last(), { type: 'set_setting', key: 'alerts', value: false });
  click(p, p.doc.querySelector('[data-act="mute"]'));
  assert.deepEqual(p.last(), { type: 'mute', app_id: 'com.example.chat', muted: true });
  click(p, byAct(p, 'check-updates'));
  assert.deepEqual(p.last(), { type: 'check_updates' });
});

test('update progress is shown and disables the check button', () => {
  const p = openPopup();
  const s = withState({});
  s.update.status = { state: 'updating', message: 'Updating Earshot', time: Math.floor(Date.now() / 1000) };
  p.w.__earshotMenu.setState(s);
  click(p, byAct(p, 'tab', 'settings'));
  const btn = byAct(p, 'check-updates');
  assert.equal(btn.disabled, true);
  assert.match(btn.textContent, /Updating Earshot/);
});

test('tab choice survives in browser storage; unavailable storage does not break it', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  click(p, byAct(p, 'tab', 'alerts'));
  assert.equal(JSON.parse(p.w.localStorage.getItem('earshot.ui')).tab, 'alerts');
  const q = page('popup', POPUP_HTML);
  Object.defineProperty(q.w, 'localStorage', { get() { throw new Error('blocked'); } });
  assert.equal(inject(q), `installed ${VERSION} (popup)`);
  click(q, q.doc.getElementById('earshot-qam-tab'));
  q.w.__earshotMenu.setState(BASE);
  click(q, byAct(q, 'tab', 'alerts'));
  assert.match(text(q), /Hello/);
});

test('queues actions until the binding exists', async () => {
  const p = page('popup', POPUP_HTML);
  const fn = p.w.earshotAction;
  delete p.w.earshotAction;
  inject(p);
  click(p, p.doc.getElementById('earshot-qam-tab'));
  p.w.earshotAction = fn;
  await new Promise((r) => setTimeout(r, 700));
  assert.ok(p.w.actions.some((a) => a.type === 'hello'));
  p.w.__earshotMenu.teardown();
});

test('teardown removes everything it added and restores the tabs', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  p.w.__earshotMenu.teardown();
  assert.equal(p.doc.getElementById('earshot-qam-tab'), null);
  assert.equal(p.doc.getElementById('earshot-overlay'), null);
  assert.equal(p.doc.getElementById('earshot-menu-style'), null);
  assert.equal(p.w.__earshotMenu, undefined);
  assert.equal(p.doc.getElementById('t1').style.width, '');
});

test('coexists with another injected tab in the same strip', () => {
  const p = page('popup', POPUP_HTML);
  const other = p.doc.createElement('div');
  other.id = 'magicpods-qam-tab';
  other.className = 'tab';
  p.doc.getElementById('strip').appendChild(other);
  inject(p);
  assert.ok(p.doc.getElementById('earshot-qam-tab'));
  assert.equal(p.doc.querySelectorAll('#strip > *').length, 4);
  assert.equal(p.doc.getElementById('magicpods-qam-tab'), other);
  click(p, p.doc.getElementById('earshot-qam-tab'));
  assert.ok(p.doc.getElementById('earshot-overlay').classList.contains('es-open'));
  click(p, p.doc.getElementById('t1'));  // a native tab closes our overlay
  assert.ok(!p.doc.getElementById('earshot-overlay').classList.contains('es-open'));
});

test('no popup structure: nothing is added and nothing throws', () => {
  const p = page('popup', '<div>no tabs here</div>');
  assert.equal(inject(p), `installed ${VERSION} (popup)`);
  assert.equal(p.doc.getElementById('earshot-qam-tab'), null);
});

test('bar indicator: hidden until the phone is connected, shows calls and unread', () => {
  const p = page('bar', BAR_HTML);
  assert.equal(inject(p), `installed ${VERSION} (bar)`);
  const el = p.doc.getElementById('earshot-bar-indicator');
  assert.ok(el);
  assert.equal(el.nextElementSibling.getAttribute('aria-label'), 'Headset Battery 80%');
  assert.equal(el.style.display, 'none');
  p.w.__earshotMenu.setState(BASE);
  assert.equal(el.style.display, 'flex');
  assert.equal(el.querySelector('span').textContent, '2');
  assert.equal(el.style.opacity, '1');
  p.w.__earshotMenu.setState(withState({ priority: { mode: 'auto', frame_active: false, worn: false } }));
  assert.equal(el.style.opacity, '0.5');
  p.w.__earshotMenu.setState(withState({ phone: Object.assign({}, BASE.phone, { connected: false }), calls: { items: [], recent: [], error: '', incoming: { source: 'hfp', path: '/c', name: 'x', ringing: true } } }));
  assert.equal(el.style.display, 'flex');
  assert.equal(el.querySelector('span').textContent, 'Call');
  assert.match(el.querySelector('.es-ic').innerHTML, /#59bf40/);
  p.w.__earshotMenu.setState(withState({ setup: { kind: 'no_engine', title: 't', text: '', steps: [] }, phone: Object.assign({}, BASE.phone, { connected: false }) }));
  assert.equal(el.style.display, 'none');
  p.w.__earshotMenu.teardown();
  assert.equal(p.doc.getElementById('earshot-bar-indicator'), null);
});

test('bar click asks the popup to open and falls back to clicking Headset Battery', () => {
  const p = page('bar', BAR_HTML);
  inject(p);
  p.w.__earshotMenu.setState(BASE);
  let opened = 0;
  p.doc.querySelector('[aria-label^="Headset Battery"]').addEventListener('click', () => { opened++; });
  click(p, p.doc.getElementById('earshot-bar-indicator'));
  assert.equal(opened, 1);
});
