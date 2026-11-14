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
  assert.match(t, /Notifications here/);
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

// Keys of the on-screen keyboard. The conversation never uses a text input: Steam's keyboard would take the focus
// away from the quick menu, which closes, and the reply is lost.
const keyBtn = (p, k) => p.doc.querySelector(`[data-act="key"][data-a="${k}"]`);
const tap = (p, k) => { const b = keyBtn(p, k); assert.ok(b, 'no key ' + k); click(p, b); };
const typeText = (p, s) => { for (const ch of s) tap(p, ch === ' ' ? 'space' : ch); };
const draftText = (p) => p.doc.getElementById('earshot-draft').textContent;
const THREAD = 'tel:+15550100';
const OPEN = { thread: THREAD, messages: [{ handle: 'm1', body: 'hello there', timestamp: 1000, outgoing: false, read: true }] };
const draftActions = (p) => p.w.actions.filter((a) => a.type === 'draft');
const withReplies = (patch) => withState(Object.assign({ quick_replies: ['On my way', "Can't talk now, I'll call you back", 'OK', 'Yes', 'No'] }, patch));

function openConversation(open = OPEN, patch = {}) {
  const p = openPopup();
  p.w.__earshotMenu.setState(withReplies());
  click(p, byAct(p, 'tab', 'messages'));
  click(p, byAct(p, 'open-thread', THREAD));
  p.w.__earshotMenu.setState(withReplies(Object.assign({ open }, patch)));
  return p;
}

// jsdom has no layout: give every element a scroll height we control and remember what scrollTop was set to.
function fakeScroll(w) {
  const tops = new WeakMap();
  // 100 px per message bubble on top of a 900 px base: the height follows the content, as it does for real
  Object.defineProperty(w.HTMLElement.prototype, 'scrollHeight', { configurable: true, get() { return 900 + 100 * this.querySelectorAll('.es-bub').length; } });
  Object.defineProperty(w.HTMLElement.prototype, 'scrollTop', { configurable: true, get() { return tops.get(this) || 0; }, set(v) { tops.set(this, v); } });
}

test('the Phone tab pages are Calls, Messages, Notifications, Settings, and nothing says Alerts any more', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  const tabs = () => [...p.doc.querySelectorAll('#earshot-tabs button')];
  assert.deepEqual(tabs().map((b) => b.getAttribute('data-a')), ['calls', 'messages', 'alerts', 'settings']);   // ids are unchanged: saved choices still load
  assert.deepEqual(tabs().map((b) => b.textContent.replace(/\d+$/, '')), ['Calls', 'Messages', 'Notifications', 'Settings']);
  assert.match(p.doc.getElementById('earshot-head').textContent, /Notifications here/);
  for (const k of ['calls', 'messages', 'alerts', 'settings']) {
    click(p, byAct(p, 'tab', k));
    assert.doesNotMatch(text(p), /alert/i, k);
  }
  p.w.__earshotMenu.setState(withState({ priority: { mode: 'phone', frame_active: false, worn: false } }));
  assert.match(p.doc.getElementById('earshot-head').textContent, /Notifications on phone/);
  assert.doesNotMatch(text(p), /alert/i);
});

test('messages: list, open a thread, type on the keyboard, send, fail, retry, sent', () => {
  const p = openConversation();
  assert.equal(p.doc.querySelector('#earshot-overlay input, #earshot-overlay textarea, #earshot-overlay [contenteditable]'), null);
  assert.match(text(p), /hello there/);
  assert.equal(byAct(p, 'send').disabled, true);          // nothing to send yet
  typeText(p, 'on my');
  assert.equal(draftText(p), 'On my');                     // the sentence starts with a capital
  assert.equal(draftActions(p).pop().text, 'On my');       // the helper keeps the draft too
  // state pushes do not disturb typing: the same nodes, the same text
  const conv = p.doc.getElementById('earshot-conv'), kb = p.doc.getElementById('earshot-kb');
  p.w.__earshotMenu.setState(withReplies({ open: { thread: THREAD, messages: OPEN.messages.concat([{ handle: 'm2', body: 'later msg', timestamp: 1001, outgoing: false, read: true }]) } }));
  assert.equal(p.doc.getElementById('earshot-conv'), conv);
  assert.equal(p.doc.getElementById('earshot-kb'), kb);
  assert.equal(draftText(p), 'On my');
  assert.match(text(p), /later msg/);
  typeText(p, ' way');
  assert.equal(draftText(p), 'On my way');
  click(p, byAct(p, 'send'));
  const sent = p.last();
  assert.equal(sent.type, 'send');
  assert.equal(sent.thread, THREAD);
  assert.equal(sent.text, 'On my way');
  assert.match(sent.cid, /^[A-Za-z0-9_.-]{1,40}$/);
  assert.deepEqual(draftActions(p).pop(), { type: 'draft', thread: THREAD, text: '' });   // the helper forgets the draft
  assert.equal(byAct(p, 'send').disabled, true);
  assert.match(text(p), /On my way/);
  assert.match(text(p), /Sending/);

  p.w.__earshotMenu.setState(withReplies({ open: OPEN, sends: [{ cid: sent.cid, thread: THREAD, status: 'failed', error: 'Phone busy' }] }));
  assert.match(text(p), /Not sent: Phone busy/);
  assert.ok(p.doc.querySelector('.es-bub.es-fail'));
  click(p, byAct(p, 'resend', sent.cid));
  const again = p.last();
  assert.equal(again.type, 'send');
  assert.equal(again.text, 'On my way');
  assert.notEqual(again.cid, sent.cid);

  p.w.__earshotMenu.setState(withReplies({ open: OPEN, sends: [{ cid: again.cid, thread: THREAD, status: 'sent', error: '' }] }));
  assert.doesNotMatch(text(p), /Sending/);
  assert.doesNotMatch(text(p), /Phone busy/);
  assert.ok(p.doc.querySelector('.es-bub.es-sentok'));
  assert.match(p.doc.querySelector('.es-bub.es-sentok .es-meta').textContent, /^Sent/);
  // once the conversation itself shows the message, the local copy goes away
  p.w.__earshotMenu.setState(withReplies({ open: { thread: THREAD, messages: OPEN.messages.concat([{ handle: 'm3', body: 'On my way', timestamp: 1002, outgoing: true, read: true }]) }, sends: [{ cid: again.cid, thread: THREAD, status: 'sent', error: '' }] }));
  assert.equal(p.doc.querySelector('.es-bub.es-sentok'), null);
  assert.equal([...p.doc.querySelectorAll('.es-bub')].filter((b) => /On my way/.test(b.textContent)).length, 1);
});

test('typing never gives focus to anything: no input, no text area, the focus stays where it was', () => {
  const p = openConversation();
  const before = p.doc.activeElement;
  typeText(p, 'hi');
  assert.equal(p.doc.querySelectorAll('input, textarea, [contenteditable]').length, 0);
  assert.ok(!/^(INPUT|TEXTAREA)$/.test(p.doc.activeElement.tagName));
  assert.equal(p.doc.activeElement, before);
  assert.equal(draftText(p), 'Hi');
});

test('keyboard: capitals at the start of a sentence, Shift once and twice, numbers page, Del, space', () => {
  const p = openConversation();
  typeText(p, 'hi');
  assert.equal(draftText(p), 'Hi');
  assert.equal(keyBtn(p, 'q').textContent, 'q');           // lower case again after the first letter
  tap(p, 'shift'); tap(p, 'x');
  assert.equal(draftText(p), 'HiX');
  tap(p, 'y');
  assert.equal(draftText(p), 'HiXy');
  tap(p, 'shift'); tap(p, 'shift');                         // twice: caps lock
  typeText(p, 'ab');
  assert.equal(draftText(p), 'HiXyAB');
  tap(p, 'shift');                                          // off again
  tap(p, 'c');
  assert.equal(draftText(p), 'HiXyABc');
  tap(p, 'mode');
  assert.ok(keyBtn(p, '1') && !keyBtn(p, 'q'));
  tap(p, '1'); tap(p, '!');
  assert.equal(draftText(p), 'HiXyABc1!');
  tap(p, 'mode');
  assert.ok(keyBtn(p, 'q'));
  tap(p, 'back'); tap(p, 'back');
  assert.equal(draftText(p), 'HiXyABc');
  tap(p, 'back');
  typeText(p, '. a');                                       // after ". " the next letter is a capital
  assert.equal(draftText(p), 'HiXyAB. A');
  for (let i = 0; i < 20; i++) tap(p, 'back');
  assert.equal(p.doc.querySelector('.es-hint').textContent, 'Tap the keys to write a reply');   // an empty draft shows the hint
  assert.equal(byAct(p, 'send').disabled, true);
});

test('quick replies: one tap sends, the draft is left alone', () => {
  const p = openConversation();
  typeText(p, 'wait');
  const buttons = [...p.doc.querySelectorAll('#earshot-quick [data-act="quick"]')];
  assert.deepEqual(buttons.map((b) => b.textContent), ['On my way', "Can't talk now, I'll call you back", 'OK', 'Yes', 'No']);
  click(p, buttons[1]);
  const sent = p.last();
  assert.equal(sent.type, 'send');
  assert.equal(sent.thread, THREAD);
  assert.equal(sent.text, "Can't talk now, I'll call you back");
  assert.equal(draftText(p), 'Wait');
  assert.match(text(p), /Sending/);
  click(p, buttons[4]);
  assert.equal(p.last().text, 'No');
  // an index that does not exist sends nothing
  const n = p.w.actions.length;
  p.doc.getElementById('earshot-quick').insertAdjacentHTML('beforeend', '<button data-act="quick" data-a="99" id="ghost">x</button>');
  click(p, p.doc.getElementById('ghost'));
  assert.equal(p.w.actions.length, n);
});

test('the draft survives a re-render, the tab being left, and the menu closing and reopening', () => {
  const p = openConversation();
  typeText(p, 'on my');
  p.w.__earshotMenu.setState(withReplies({ open: OPEN }));             // a state push
  assert.equal(draftText(p), 'On my');
  click(p, byAct(p, 'tab', 'settings'));                                 // leaving the conversation tab closes it...
  assert.deepEqual(p.w.actions.filter((a) => a.type === 'close_thread').length, 1);
  p.w.__earshotMenu.setState(withReplies({ open: null }));
  click(p, byAct(p, 'tab', 'messages'));
  click(p, byAct(p, 'open-thread', THREAD));
  p.w.__earshotMenu.setState(withReplies({ open: OPEN }));
  assert.equal(draftText(p), 'On my');                                  // ...and the draft is still there
  click(p, p.doc.getElementById('t1'));                                   // the menu closes (another Steam tab)
  click(p, p.doc.getElementById('earshot-qam-tab'));                      // and opens again
  assert.equal(draftText(p), 'On my');
  // the popup page itself goes away when the menu closes: a new page starts from what the helper kept
  const q = openPopup();
  q.w.__earshotMenu.setState(withReplies({ open: Object.assign({ draft: 'On my' }, OPEN) }));
  click(q, byAct(q, 'tab', 'messages'));
  assert.equal(draftText(q), 'On my');
  tap(q, 'space'); tap(q, 'w');
  assert.equal(draftText(q), 'On my w');
  assert.equal(draftActions(q).pop().text, 'On my w');
});

test('a conversation opens at the newest message and keeps following new ones, unless the person scrolled up', () => {
  const p = openPopup();
  fakeScroll(p.w);
  p.w.__earshotMenu.setState(withReplies());
  click(p, byAct(p, 'tab', 'messages'));
  click(p, byAct(p, 'open-thread', THREAD));
  p.w.__earshotMenu.setState(withReplies({ open: OPEN }));
  const box = () => p.doc.getElementById('earshot-msgs');
  assert.equal(box().scrollTop, 1000);                                   // the bottom
  p.w.__earshotMenu.setState(withReplies({ open: { thread: THREAD, messages: OPEN.messages.concat([{ handle: 'm2', body: 'newest', timestamp: 1001, outgoing: false, read: true }]) } }));
  assert.equal(box().scrollTop, 1100);                                   // follows a new message
  box().scrollTop = 100;                                                  // the person reads older messages
  p.w.__earshotMenu.setState(withReplies({ open: { thread: THREAD, messages: OPEN.messages.concat([{ handle: 'm2', body: 'newest', timestamp: 1001, outgoing: false, read: true }, { handle: 'm3', body: 'even newer', timestamp: 1002, outgoing: false, read: true }]) } }));
  assert.equal(box().scrollTop, 100);
  // reopening the same conversation from the list lands at the bottom again
  click(p, byAct(p, 'back'));
  click(p, byAct(p, 'open-thread', THREAD));
  p.w.__earshotMenu.setState(withReplies({ open: OPEN }));
  assert.equal(box().scrollTop, 1000);
});

test('conversation view: the messages scroll, the keyboard stays in view, the header gets out of the way', () => {
  const p = openConversation();
  const overlay = p.doc.getElementById('earshot-overlay');
  assert.ok(overlay.classList.contains('es-convmode'));
  assert.equal(p.doc.getElementById('earshot-head').textContent, '');
  const conv = p.doc.getElementById('earshot-conv');
  const order = [...conv.children].map((c) => c.id || c.className);
  assert.deepEqual(order.slice(-2), ['earshot-msgs', 'es-reply']);       // messages above, quick replies + draft + keys last
  assert.deepEqual([...conv.querySelector('.es-reply').children].map((c) => c.id), ['earshot-quick', 'earshot-draftrow', 'earshot-kb']);
  click(p, byAct(p, 'back'));
  assert.ok(!overlay.classList.contains('es-convmode'));
  assert.match(p.doc.getElementById('earshot-head').textContent, /Test Phone/);
});

test('a conversation that cannot be replied to has no keyboard and no quick replies', () => {
  const p = openPopup();
  const s = withReplies({ open: { thread: 'tel:12345', messages: [] } });
  s.threads = [{ thread: 'tel:12345', name: '12345', preview: '', timestamp: 1, unread: 0, repliable: false, reply_reason: 'No reply route.' }];
  p.w.__earshotMenu.setState(s);
  click(p, byAct(p, 'tab', 'messages'));
  assert.equal(p.doc.getElementById('earshot-kb'), null);
  assert.equal(p.doc.getElementById('earshot-quick'), null);
  assert.match(text(p), /No reply route\./);
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

test('not set up yet: says what is wrong in plain words, never a command, keeps Settings reachable', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(withState({ setup: { kind: 'no_bluetooth', title: 'Bluetooth is not ready for Earshot', text: 'Bluetooth is turned off in the headset settings.' } }));
  assert.match(text(p), /Bluetooth is not ready/);
  assert.match(text(p), /turned off in the headset settings/);
  assert.equal(p.doc.querySelector('code'), null, 'no commands to run');
  assert.doesNotMatch(text(p), /sudo|terminal/i);
  assert.equal(p.doc.querySelector('.es-item'), null, 'no empty lists');
  click(p, byAct(p, 'retry'));
  assert.deepEqual(p.last(), { type: 'retry' });
  click(p, byAct(p, 'tab', 'settings'));
  assert.match(text(p), /Show iPhone notifications in the headset/);
});

test('settings show the Earshot and Tether versions when the helper sends them', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(Object.assign({}, BASE, { update: Object.assign({}, BASE.update, { about: 'Earshot 0.1.0 · Tether 0.3.5' }) }));
  click(p, byAct(p, 'tab', 'settings'));
  assert.match(text(p), /Earshot 0\.1\.0 · Tether 0\.3\.5/);
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

test('settings say when the call link is being restored or did not come back', () => {
  const p = openPopup();
  const s = withState({});
  s.phone = Object.assign({}, s.phone, { calls: false, calls_restore: 'pending' });
  p.w.__earshotMenu.setState(s);
  click(p, byAct(p, 'tab', 'settings'));
  assert.match(text(p), /Calls: reconnecting/);
  s.phone = Object.assign({}, s.phone, { calls_restore: 'unverified' });
  p.w.__earshotMenu.setState(s);
  assert.match(text(p), /did not come back/);
  s.phone = Object.assign({}, s.phone, { calls: true, calls_restore: '' });
  p.w.__earshotMenu.setState(s);
  assert.match(text(p), /Calls: connected/);
  assert.doesNotMatch(text(p), /did not come back/);
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

// ---------- unread alerts ----------
const tabBadge = (p) => p.doc.querySelector('#earshot-qam-tab .es-tabbadge');
const newAlert = (uid, app = 'Chat', title = 'Ping') => ({ uid, app_id: 'com.example.' + app.toLowerCase(), app, title, text: '', timestamp: 2000 + uid, category: 0, can_dismiss: true, can_accept: false });

test('a new alert puts a count on the Phone tab button, and opening Notifications clears it', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);                       // first state is the baseline: what the phone already had is not new
  assert.equal(tabBadge(p), null);
  p.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8), newAlert(9)] }));
  assert.equal(tabBadge(p).textContent, '2');
  assert.match(p.doc.getElementById('earshot-qam-tab').getAttribute('aria-label'), /2 new notifications/);
  assert.match(byAct(p, 'tab', 'alerts').textContent, /2/);   // the Notifications tab inside the menu counts them too
  click(p, byAct(p, 'tab', 'alerts'));
  assert.equal(tabBadge(p), null);
  assert.equal(p.doc.getElementById('earshot-qam-tab').getAttribute('aria-label'), 'Phone');
  assert.doesNotMatch(byAct(p, 'tab', 'alerts').textContent, /\d/);
});

test('the Phone tab count also shows while the quick menu is on another of Steam\'s tabs', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  click(p, p.doc.getElementById('t1'));                    // leave the Phone tab: the overlay closes
  assert.ok(!p.doc.getElementById('earshot-overlay').classList.contains('es-open'));
  p.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8)] }));
  assert.equal(tabBadge(p).textContent, '1');
  assert.match(p.doc.getElementById('earshot-qam-tab').getAttribute('aria-label'), /1 new notification\b/);
});

test('alerts list: new ones are highlighted while the tab stays open, and are not new on the next visit', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  p.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8, 'Mail', 'Receipt')] }));
  click(p, byAct(p, 'tab', 'alerts'));
  const items = () => [...p.doc.querySelectorAll('#earshot-body .es-item')];
  assert.equal(items().length, 2);
  assert.deepEqual(items().map((i) => i.classList.contains('es-new')), [false, true]);
  assert.match(items()[1].textContent, /New/);
  assert.doesNotMatch(items()[0].textContent, /New/);
  // a state push while the person is looking keeps the highlight, and one more arrival joins it
  p.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8, 'Mail', 'Receipt'), newAlert(9)] }));
  assert.deepEqual(items().map((i) => i.classList.contains('es-new')), [false, true, true]);
  assert.equal(tabBadge(p), null);
  // leave and come back: all of it has been seen
  click(p, byAct(p, 'tab', 'calls'));
  click(p, byAct(p, 'tab', 'alerts'));
  assert.deepEqual(items().map((i) => i.classList.contains('es-new')), [false, false, false]);
});

test('alerts that arrive while another tab is open stay unread until Notifications is opened', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  click(p, byAct(p, 'tab', 'messages'));
  p.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8)] }));
  assert.equal(tabBadge(p).textContent, '1');
  click(p, byAct(p, 'tab', 'alerts'));
  assert.equal(tabBadge(p), null);
});

test('what was seen survives a reload; the very first state is not announced as new', () => {
  const first = openPopup();
  first.w.__earshotMenu.setState(BASE);
  first.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8)] }));
  assert.equal(tabBadge(first).textContent, '1');
  click(first, byAct(first, 'tab', 'alerts'));
  const stored = first.w.localStorage.getItem('earshot.seen');
  assert.ok(stored);
  // a new page of the same browser profile (same storage) with the same alerts: nothing is unread
  const again = page('popup', POPUP_HTML);
  again.w.localStorage.setItem('earshot.seen', stored);
  inject(again);
  click(again, again.doc.getElementById('earshot-qam-tab'));
  again.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8)] }));
  assert.equal(tabBadge(again), null);
  // ...but an alert that came in meanwhile is
  again.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8), newAlert(10)] }));
  assert.equal(tabBadge(again).textContent, '1');
});

test('unavailable browser storage does not break the unread count', () => {
  const p = openPopup();
  Object.defineProperty(p.w, 'localStorage', { get() { throw new Error('denied'); } });
  p.w.__earshotMenu.setState(BASE);
  p.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8)] }));
  assert.equal(tabBadge(p).textContent, '1');
  click(p, byAct(p, 'tab', 'alerts'));
  assert.equal(tabBadge(p), null);
});

test('teardown removes the Phone tab count with the tab', () => {
  const p = openPopup();
  p.w.__earshotMenu.setState(BASE);
  p.w.__earshotMenu.setState(withState({ notifications: [BASE.notifications[0], newAlert(8)] }));
  p.w.__earshotMenu.teardown();
  assert.equal(p.doc.querySelector('.es-tabbadge'), null);
});

// ---------- the Phone tab stays last in the strip ----------
const stripIds = (p) => [...p.doc.getElementById('strip').children].map((c) => c.id);
const addTab = (p, id) => { const d = p.doc.createElement('div'); d.id = id; p.doc.getElementById('strip').appendChild(d); return d; };
// Records every tab the strip is asked to move (appendChild, insertBefore, ...) by id.
function watchStrip(p) {
  const strip = p.doc.getElementById('strip');
  const moved = [];
  for (const m of ['appendChild', 'insertBefore', 'removeChild', 'append', 'prepend', 'replaceChild']) {
    const orig = strip[m].bind(strip);
    strip[m] = (...a) => { moved.push(a[0] && a[0].id); return orig(...a); };
  }
  return moved;
}

test('the Phone tab is last when there is no other add-on, and stays put', async () => {
  const p = page('popup', POPUP_HTML);
  inject(p);
  assert.deepEqual(stripIds(p), ['t1', 't2', 'earshot-qam-tab']);
  const moved = watchStrip(p);
  for (let i = 0; i < 3; i++) p.w.__earshotMenu.ensure();
  await tick();
  assert.deepEqual(stripIds(p), ['t1', 't2', 'earshot-qam-tab']);
  assert.deepEqual(moved, []);                                              // nothing was reordered
});

test('another tab appended after ours: ours goes back to the end at once, theirs is never moved', async () => {
  const p = page('popup', POPUP_HTML);
  inject(p);
  const moved = watchStrip(p);
  const airpods = addTab(p, 'magicpods-tab');                               // Tragus's tab, added after ours
  await Promise.resolve();                                                  // the strip's own observer: a microtask, before any paint
  assert.deepEqual(stripIds(p), ['t1', 't2', 'magicpods-tab', 'earshot-qam-tab']);
  assert.equal(p.doc.getElementById('strip').children[2], airpods);
  addTab(p, 'someone-elses-tab');                                           // any other add-on, whatever its id
  await Promise.resolve();
  assert.deepEqual(stripIds(p), ['t1', 't2', 'magicpods-tab', 'someone-elses-tab', 'earshot-qam-tab']);
  assert.deepEqual(moved.filter((id) => id !== 'earshot-qam-tab'), ['magicpods-tab', 'someone-elses-tab']);   // only the appends the others made themselves
});

test('the injection pass also puts it last, and a correct order is stable on every open', async () => {
  const p = page('popup', POPUP_HTML);
  inject(p);
  addTab(p, 'magicpods-tab');
  p.w.__earshotMenu.ensure();                                               // even before the observer has run
  assert.deepEqual(stripIds(p), ['t1', 't2', 'magicpods-tab', 'earshot-qam-tab']);
  await tick();
  const moved = watchStrip(p);
  for (let i = 0; i < 5; i++) { p.w.__earshotMenu.ensure(); await tick(); }
  assert.deepEqual(stripIds(p), ['t1', 't2', 'magicpods-tab', 'earshot-qam-tab']);
  assert.deepEqual(moved, []);
  // Steam re-renders the strip: both add-ons are re-added, Tragus's last, ours has to follow
  const strip = p.doc.getElementById('strip');
  const ours = p.doc.getElementById('earshot-qam-tab'), theirs = p.doc.getElementById('magicpods-tab');
  ours.remove(); theirs.remove();
  strip.appendChild(ours); strip.appendChild(theirs);
  await tick();
  assert.deepEqual(stripIds(p), ['t1', 't2', 'magicpods-tab', 'earshot-qam-tab']);
});

test('moving our tab does not set the observer off again', async () => {
  const p = page('popup', POPUP_HTML);
  inject(p);
  const moved = watchStrip(p);
  addTab(p, 'magicpods-tab');
  await tick(); await tick();
  assert.deepEqual(moved.filter((id) => id === 'earshot-qam-tab').length, 1);   // exactly one correction, no ping-pong
  assert.deepEqual(stripIds(p), ['t1', 't2', 'magicpods-tab', 'earshot-qam-tab']);
});

test('teardown stops watching the strip', async () => {
  const p = page('popup', POPUP_HTML);
  inject(p);
  p.w.__earshotMenu.teardown();
  addTab(p, 'magicpods-tab');
  await tick();
  assert.deepEqual(stripIds(p), ['t1', 't2', 'magicpods-tab']);
});

// ---------- settings: one plain priority choice, a steady app list, an update check that always answers ----------
const settingsText = (p) => p.doc.getElementById('earshot-body').textContent;
const openSettings = (state) => {
  const p = openPopup();
  p.w.__earshotMenu.setState(state || BASE);
  click(p, byAct(p, 'tab', 'settings'));
  return p;
};
const choices = (p) => [...p.doc.querySelectorAll('[data-act="setting-set"][data-a="priority"]')];

test('priority: "While I\'m wearing it" or "Never", one short line, no talk of calls or modes', () => {
  const p = openSettings();
  assert.match(settingsText(p), /Show iPhone notifications in the headset/);
  assert.deepEqual(choices(p).map((b) => b.textContent), ["While I'm wearing it", 'Never']);
  assert.deepEqual(choices(p).map((b) => b.classList.contains('es-on')), [true, false]);     // the default
  assert.match(settingsText(p), /When you take the headset off, everything stays on your phone as usual\./);
  assert.doesNotMatch(settingsText(p), /Always|Right now|SteamVR|Auto follows|call link|rings/);
  click(p, choices(p)[1]);
  assert.deepEqual(p.last(), { type: 'set_setting', key: 'priority', value: 'phone' });
  click(p, choices(p)[0]);
  assert.deepEqual(p.last(), { type: 'set_setting', key: 'priority', value: 'auto' });
});

test('priority: what old settings files held is shown as what it means now', () => {
  for (const [old, wearing] of [['frame', true], ['auto', true], ['phone', false]]) {
    const s = withState({});
    s.settings.priority = old;
    const p = openSettings(s);
    assert.deepEqual(choices(p).map((b) => b.classList.contains('es-on')), [wearing, !wearing], old);
  }
});

test('mute list: alphabetical by display name, the same however the helper sends them, muted state shown', () => {
  const s = withState({});
  s.apps = [
    { id: 'z.last', name: 'Zeta', muted: false }, { id: 'a.mail', name: 'alpha mail', muted: true },
    { id: 'm.chat', name: 'Chat', muted: false }, { id: 'b.same', name: 'Chat', muted: false },
  ];
  const p = openSettings(s);
  const rows = [...p.doc.querySelectorAll('[data-act="mute"]')];
  assert.deepEqual(rows.map((b) => b.getAttribute('data-a')), ['a.mail', 'b.same', 'm.chat', 'z.last']);
  assert.deepEqual(rows.map((b) => b.classList.contains('es-on')), [true, false, false, false]);
  click(p, rows[0]);
  assert.deepEqual(p.last(), { type: 'mute', app_id: 'a.mail', muted: false });
});

function updatePage(status) {
  const s = withState({});
  s.update.status = status;
  return openSettings(s);
}
const resultLine = (p) => (p.doc.querySelector('.es-result') || {}).textContent;
const nowS = () => Math.floor(Date.now() / 1000);

test('update check: every outcome is a plain line on the page', () => {
  assert.equal(resultLine(updatePage({ state: 'uptodate', message: 'Up to date (version v1.2.0)', time: nowS() })).startsWith('Up to date (version v1.2.0)'), true);
  assert.match(resultLine(updatePage({ state: 'noreleases', message: 'No releases published yet', time: nowS() })), /^No releases published yet/);
  assert.match(resultLine(updatePage({ state: 'failed', message: 'Could not check (timed out)', time: nowS() })), /^Could not check \(timed out\)/);
  assert.match(resultLine(updatePage({ state: 'updated', message: 'Updated to v1.3.0', time: nowS() })), /^Updated to v1\.3\.0/);
  // a state with no message still says something
  assert.match(resultLine(updatePage({ state: 'noreleases', message: '', time: nowS() })), /No releases published yet/);
  assert.match(resultLine(updatePage({ state: 'failed', message: '', time: nowS() })), /Could not check/);
  // while it works the button carries the words, and the update that is being installed is named
  const p = updatePage({ state: 'updating', message: 'Update available: v1.3.0 - updating...', time: nowS() });
  assert.equal(byAct(p, 'check-updates').disabled, true);
  assert.match(byAct(p, 'check-updates').textContent, /Update available: v1\.3\.0 - updating/);
  // one that never finished is not left looking busy forever
  const stuck = updatePage({ state: 'checking', message: 'Checking for updates...', time: nowS() - 400 });
  assert.equal(byAct(stuck, 'check-updates').disabled, false);
  assert.match(resultLine(stuck), /did not finish/);
});

test('update check: the tap is acknowledged at once, then the answer replaces it; no answer is said too', () => {
  const p = updatePage({ state: 'noreleases', message: 'No releases published yet', time: nowS() - 100 });
  const realNow = p.w.Date.now;
  click(p, byAct(p, 'check-updates'));
  assert.deepEqual(p.last(), { type: 'check_updates' });
  assert.equal(byAct(p, 'check-updates').disabled, true);                         // not "nothing happened"
  assert.match(byAct(p, 'check-updates').textContent, /Checking for updates/);
  assert.equal(resultLine(p), undefined);                                        // the old result is not shown as if it were new
  // the helper answers
  const s = withState({}); s.update.status = { state: 'noreleases', message: 'No releases published yet', time: nowS() + 1 };
  p.w.__earshotMenu.setState(s);
  assert.equal(byAct(p, 'check-updates').disabled, false);
  assert.match(resultLine(p), /^No releases published yet/);
  // a second tap that nothing answers
  click(p, byAct(p, 'check-updates'));
  p.w.Date.now = () => realNow.call(p.w.Date) + 20000;
  const t = withState({}); t.update.status = Object.assign({}, s.update.status, { time: nowS() - 100 });
  p.w.__earshotMenu.setState(t);
  assert.match(resultLine(p), /No answer from the Earshot service/);
  assert.equal(byAct(p, 'check-updates').disabled, false);
});
