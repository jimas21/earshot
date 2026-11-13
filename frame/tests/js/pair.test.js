'use strict';
// Tests for the Pair iPhone screens in frame/menu/earshot-menu.js (jsdom). The helper owns the pairing state;
// these tests push S.pair states in and check what is drawn and which actions go back.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'menu', 'earshot-menu.js'), 'utf8');
const SCRIPT = SOURCE.replaceAll('__VERSION__', 'pairtest0001').replaceAll('__BINDING__', 'earshotAction');
const POPUP_HTML = `<div id="root"><div id="stripwrap"><div id="strip">
  <div id="t1" class="tab">Battery Info</div><div id="t2" class="tab sel">Quick Settings</div></div></div>
  <div id="content"><div role="tabpanel" aria-labelledby="t1">native</div></div></div>`;

const windows = [];
test.after(() => { for (const w of windows) { try { w.close(); } catch (e) { } } });

function openPopup() {
  const dom = new JSDOM(`<!doctype html><title>SteamVR - valve.steam.gamepadui.barpopup.1 (1)</title><body>${POPUP_HTML}</body>`,
    { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window;
  w.actions = [];
  w.earshotAction = (text) => w.actions.push(JSON.parse(text));
  w.BroadcastChannel = class { postMessage() { } close() { } };
  windows.push(w);
  w.eval(SCRIPT);
  w.document.getElementById('earshot-qam-tab').dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
  const doc = w.document;
  return {
    w, doc, last: () => w.actions[w.actions.length - 1],
    state: (patch) => w.__earshotMenu.setState(Object.assign({}, BASE, patch)),
    pair: (pair, patch) => w.__earshotMenu.setState(Object.assign({}, BASE, { pair }, patch)),
    click: (el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true })),
    act: (act, a) => doc.querySelector(`[data-act="${act}"]${a != null ? `[data-a="${a}"]` : ''}`),
    text: () => doc.getElementById('earshot-body').textContent,
  };
}

const BASE = {
  v: 1, setup: null, pair: null,
  phone: { name: 'Test Phone', paired: true, connected: true, battery: 80, messages: true, notifications: true, calls: true, reason: '', profile_reason: '', ancs_reason: '' },
  priority: { mode: 'auto', frame_active: true, worn: true },
  calls: { items: [], incoming: null, recent: [], error: '' },
  notifications: [], threads: [], open: null, sends: [], apps: [],
  settings: { priority: 'auto', alerts: true, preview: true, auto_update: true },
  update: { status: null, versions: {} }, unread: 0,
};
const ADDR = '11:22:33:44:55:66';
const soon = (s) => Math.floor(Date.now() / 1000) + s;

test('no phone: the setup card has a Pair iPhone button instead of a README pointer', () => {
  const p = openPopup();
  p.state({ setup: { kind: 'no_phone', title: 'No iPhone is paired yet', text: 'Pair your iPhone with Earshot. It takes about a minute.', steps: [], action: { act: 'pair-start', label: 'Pair iPhone' } } });
  assert.match(p.text(), /No iPhone is paired yet/);
  assert.doesNotMatch(p.text(), /README/);
  p.click(p.act('pair-start'));
  assert.deepEqual(p.last(), { type: 'pair_start' });
});

test('a setup action with a odd name is not rendered as a button', () => {
  const p = openPopup();
  p.state({ setup: { kind: 'no_phone', title: 't', text: 'x', steps: [], action: { act: '"><img src=x>', label: 'Pair iPhone' } } });
  assert.equal(p.doc.querySelector('img'), null);
  assert.ok(p.act('retry'));
});

test('settings: pair a different iPhone and forget this iPhone', () => {
  const p = openPopup();
  p.state({});
  p.click(p.act('tab', 'settings'));
  const start = p.act('pair-start');
  assert.equal(start.textContent, 'Pair a different iPhone');
  p.click(start);
  assert.deepEqual(p.last(), { type: 'pair_start' });
  p.click(p.act('forget-ask'));
  assert.deepEqual(p.last(), { type: 'forget_ask' });
  p.state({ phone: Object.assign({}, BASE.phone, { paired: false }) });
  assert.equal(p.act('pair-start').textContent, 'Pair iPhone');
  assert.equal(p.act('forget-ask'), null);
});

test('blocked: says why first, offers Check again and Close, nothing to start', () => {
  const p = openPopup();
  p.pair({ phase: 'blocked', headset: 'Frame', reasons: ['Bluetooth is turned off. <b>x</b>'] });
  assert.match(p.text(), /cannot pair yet/);
  assert.match(p.text(), /turned off/);
  assert.equal(p.doc.querySelector('.es-pair b:not(.es-x)') && p.doc.querySelector('.es-pstep b'), null);   // text, not markup
  assert.equal(p.act('pair-search'), null);
  p.click(p.act('pair-start'));
  assert.deepEqual(p.last(), { type: 'pair_start' });
  p.click(p.act('pair-close'));
  assert.deepEqual(p.last(), { type: 'pair_close' });
});

test('listen: tells the user what to tap on the iPhone, shows the countdown, hides the tabs', () => {
  const p = openPopup();
  p.click(p.act('tab', 'settings'));
  p.pair({ phase: 'listen', headset: 'Steam <i>Frame</i>', ends_at: soon(100), expired: false, seconds: 120 });
  const t = p.text();
  assert.match(t, /Settings > Bluetooth/);
  assert.match(t, /Other Devices/);
  assert.match(t, /Steam <i>Frame<\/i>/);                   // escaped: shown as text
  assert.equal(p.doc.querySelector('.es-pair i'), null);
  assert.match(p.doc.getElementById('earshot-count').textContent, /^1:(3|4)\d$/);
  assert.equal(p.doc.getElementById('earshot-tabs').children.length, 0);
  assert.equal(p.doc.getElementById('earshot-visible-again').disabled, true);
  assert.ok(p.act('pair-other'));
  p.click(p.act('pair-other'));
  assert.deepEqual(p.last(), { type: 'pair_other' });
  p.click(p.act('pair-close'));
  assert.deepEqual(p.last(), { type: 'pair_close' });
});

test('listen: Make visible again only works once the time is up', () => {
  const p = openPopup();
  p.pair({ phase: 'listen', headset: 'Frame', ends_at: soon(100), expired: false });
  const n = p.w.actions.length;
  p.click(p.doc.getElementById('earshot-visible-again'));
  assert.equal(p.w.actions.length, n);
  p.pair({ phase: 'listen', headset: 'Frame', ends_at: soon(-5), expired: true });
  assert.match(p.text(), /not visible any more/);
  assert.equal(p.doc.getElementById('earshot-count').textContent, '0:00');
  p.click(p.doc.getElementById('earshot-visible-again'));
  assert.deepEqual(p.last(), { type: 'pair_listen_again' });
});

test('listen: the countdown ticks and enables the button by itself', async () => {
  const p = openPopup();
  p.pair({ phase: 'listen', headset: 'Frame', ends_at: Date.now() / 1000 + 1.4, expired: false });
  assert.equal(p.doc.getElementById('earshot-visible-again').disabled, true);
  await new Promise((r) => setTimeout(r, 2300));
  assert.equal(p.doc.getElementById('earshot-count').textContent, '0:00');
  assert.equal(p.doc.getElementById('earshot-visible-again').disabled, false);
});

test('search: one line, a big Search button, then iPhones first as tap targets', () => {
  const p = openPopup();
  p.pair({ phase: 'search', headset: 'Frame', searching: false, searched: false, message: '', devices: [], fallback: true, can_listen: false });
  assert.match(p.text(), /Settings > Bluetooth and keep that screen open/);
  assert.match(p.text(), /search for it instead/);
  assert.equal(p.act('pair-listen'), null);
  p.click(p.act('pair-search'));
  assert.deepEqual(p.last(), { type: 'pair_search' });
  p.pair({ phase: 'search', headset: 'Frame', searching: true, searched: true, message: '', fallback: false, can_listen: true,
    devices: [{ address: ADDR, name: 'Pat <b>iPhone</b>', iphone: true, redo: false }, { address: '00:00:00:00:00:09', name: 'Speaker', iphone: false, redo: false },
      { address: '00:00:00:00:00:0A', name: 'Old phone', iphone: true, redo: true }] });
  assert.match(p.text(), /Searching…/);
  const rows = [...p.doc.querySelectorAll('.es-pdev')];
  assert.equal(rows.length, 3);
  assert.equal(rows[0].querySelector('span').textContent, 'Pat <b>iPhone</b>');
  assert.equal(p.doc.querySelector('.es-pdev b'), null);
  assert.match(rows[2].textContent, /Pair again/);
  assert.equal(p.act('pair-search'), null);              // no Search again while a search is running
  p.click(rows[0]);
  assert.deepEqual(p.last(), { type: 'pair_pick', address: ADDR });
  p.click(p.act('pair-listen'));
  assert.deepEqual(p.last(), { type: 'pair_listen' });
});

test('search: nothing found shows the hint and Search again', () => {
  const p = openPopup();
  p.pair({ phase: 'search', headset: 'Frame', searching: false, searched: true, devices: [], message: 'Nothing found yet.', can_listen: false });
  assert.match(p.text(), /Nothing found yet/);
  p.click(p.act('pair-search'));
  assert.deepEqual(p.last(), { type: 'pair_search' });
  assert.equal(p.act('pair-search').textContent, 'Search again');
});

test('pairing: plain progress text, no buttons until the helper allows closing', () => {
  const p = openPopup();
  p.pair({ phase: 'pairing', headset: 'Frame', name: 'Pat', progress: 'Connecting to Pat…', can_close: false });
  assert.match(p.text(), /Pairing with Pat/);
  assert.match(p.text(), /Connecting to Pat/);
  assert.equal(p.doc.querySelectorAll('button').length, 0);
  p.pair({ phase: 'pairing', headset: 'Frame', name: '', progress: '<u>x</u>', can_close: true });
  assert.equal(p.doc.querySelector('u'), null);
  p.click(p.act('pair-close'));
  assert.deepEqual(p.last(), { type: 'pair_close' });
});

test('confirm: the code is large, Confirm sends the same operation id, Cancel closes', () => {
  const p = openPopup();
  p.pair({ phase: 'confirm', headset: 'Frame', name: 'Pat', code: '123456', op: 'earshot-listen-1-ab12' });
  const code = p.doc.querySelector('.es-code');
  assert.equal(code.textContent, '123 456');
  assert.match(p.text(), /Check the iPhone shows the same code, then tap Pair on the iPhone and Confirm here/);
  assert.equal(p.w.actions.filter((a) => a.type === 'pair_confirm').length, 0);   // never by itself
  p.click(p.act('pair-confirm'));
  assert.deepEqual(p.last(), { type: 'pair_confirm', op: 'earshot-listen-1-ab12', accept: true });
  p.click(p.act('pair-close'));
  assert.deepEqual(p.last(), { type: 'pair_close' });
  assert.equal(p.act('pair-close').textContent, 'Cancel');
});

test('failed: a plain fix and one main button, with the failure text escaped', () => {
  const p = openPopup();
  p.pair({ phase: 'failed', headset: 'Frame', kind: 'stale', title: 'The iPhone remembers an old pairing', button: 'Try again',
    text: 'On your iPhone, open the entry for this headset under My Devices and tap Forget This Device. <em>x</em>' });
  assert.match(p.text(), /Forget This Device/);
  assert.equal(p.doc.querySelector('.es-pair em'), null);
  p.click(p.act('pair-retry'));
  assert.deepEqual(p.last(), { type: 'pair_retry' });
  p.pair({ phase: 'failed', kind: 'not_found', title: 'Earshot cannot see your iPhone', text: 'x', button: 'Search again' });
  assert.equal(p.act('pair-retry').textContent, 'Search again');
});

test('weak bond: explains, offers Try again or keeping it', () => {
  const p = openPopup();
  p.pair({ phase: 'weak', headset: 'Frame', name: 'Pat' });
  assert.match(p.text(), /notifications will not work/);
  assert.match(p.text(), /Forget This Device/);
  p.click(p.act('pair-retry'));
  assert.deepEqual(p.last(), { type: 'pair_retry' });
  p.click(p.act('pair-keep'));
  assert.deepEqual(p.last(), { type: 'pair_keep' });
});

test('almost done: a checklist that ticks itself, Done only when the essentials are on', () => {
  const p = openPopup();
  const items = (n, m) => [
    { id: 'notifications', label: 'Show Notifications', state: n, note: n === 'on' ? 'Working.' : 'It can take a minute to appear.', essential: true },
    { id: 'messages', label: 'Text messages', state: m, note: m === 'off' ? 'Still off on the iPhone.' : 'Working.', essential: true },
    { id: 'contacts', label: 'Sync Contacts', state: 'waiting', note: 'Waiting for the iPhone…', essential: false },
    { id: 'favorites', label: 'Phone Favorites', state: 'unknown', note: 'Turn it on. Earshot cannot check this one.', essential: false },
    { id: 'recents', label: 'Phone Recents', state: 'unknown', note: 'Turn it on. Earshot cannot check this one.', essential: false }];
  p.pair({ phase: 'almost', headset: 'Frame', connected: true, weak: false, ready: false, items: items('waiting', 'off') });
  assert.match(p.text(), /Almost done/);
  assert.match(p.text(), /Settings > Bluetooth/);
  assert.equal(p.doc.querySelectorAll('.es-chk').length, 5);
  assert.equal(p.doc.querySelectorAll('.es-chk.es-on').length, 0);
  assert.equal(p.doc.querySelectorAll('.es-chk.es-off').length, 1);
  assert.equal(p.act('pair-done').textContent, 'I will finish later');
  p.pair({ phase: 'almost', headset: 'Frame', connected: true, weak: false, ready: true, items: items('on', 'on') });
  assert.equal(p.doc.querySelectorAll('.es-chk.es-on').length, 2);
  assert.equal(p.act('pair-done').textContent, 'Done');
  p.click(p.act('pair-done'));
  assert.deepEqual(p.last(), { type: 'pair_done' });
  p.pair({ phase: 'almost', headset: 'Frame', connected: false, reason: 'Waiting for the phone.', ready: false, items: items('waiting', 'waiting') });
  assert.match(p.text(), /Waiting for the iPhone to connect/);
});

test('forget: asks first, then explains the iPhone side', () => {
  const p = openPopup();
  p.pair({ phase: 'forget', headset: 'Frame' });
  assert.match(p.text(), /Forget this iPhone\?/);
  p.click(p.act('forget-do'));
  assert.deepEqual(p.last(), { type: 'forget_do' });
  p.click(p.act('pair-close'));
  assert.deepEqual(p.last(), { type: 'pair_close' });
  p.pair({ phase: 'forgetting', headset: 'Frame' });
  assert.match(p.text(), /Forgetting/);
  p.pair({ phase: 'forgotten', headset: 'Frame' });
  assert.match(p.text(), /Forget This Device/);
  p.click(p.act('pair-start'));
  assert.deepEqual(p.last(), { type: 'pair_start' });
  p.click(p.act('pair-done'));
  assert.deepEqual(p.last(), { type: 'pair_done' });
});

test('the screen follows the helper: closing and reopening the tab shows the same step', () => {
  const p = openPopup();
  p.pair({ phase: 'confirm', headset: 'Frame', name: 'Pat', code: '654321', op: 'o1' });
  p.click(p.doc.getElementById('t1'));                       // switch to a native tab: the overlay closes
  assert.ok(!p.doc.getElementById('earshot-overlay').classList.contains('es-open'));
  p.click(p.doc.getElementById('earshot-qam-tab'));          // and back
  assert.match(p.doc.querySelector('.es-code').textContent, /654 321/);
  p.pair(null);
  assert.equal(p.doc.querySelector('.es-pair'), null);
  assert.ok(p.doc.getElementById('earshot-tabs').children.length > 0);
});
