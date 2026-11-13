#!/usr/bin/env python3
"""Earshot helper: iPhone calls, texts and notifications in Steam's VR menus.

Bridges Tether's local socket (docs/PROTOCOL.md) and Steam's own UI:

  tetherd --(UNIX socket, JSON lines)--> this helper --(DevTools, 127.0.0.1:8080)--> earshot-menu.js
                                              |  state goes into the pages with Runtime.evaluate,
                                              |  actions come back through a Runtime binding
                                              +--> vrcmd --notify (SteamVR notifications)

Nothing listens on any port. One thread runs a selector loop, so an idle helper
sleeps until the next timer (a few seconds apart) or a socket event. External
commands (vrcmd, busctl, systemd-run) run on one worker thread.

Logs are warning level by default and never contain message, notification or
caller text. Set EARSHOT_LOG=info or debug for more detail (still no text).

Python 3 standard library only.
"""

import base64
import collections
import hashlib
import heapq
import itertools
import json
import logging
import os
import queue
import re
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("earshot")

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL_ROOT = os.path.expanduser("~/.local/share/earshot")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "earshot")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "earshot")

CDP_BASE = "http://127.0.0.1:8080"
PAGE_MARKS = ("vrOverlayKey=valve.steam.gamepadui.bar",)  # the bar and barpopup.* pages, as Tragus finds them
MENU_FILE = os.path.join(HERE, "earshot-menu.js")
BINDING = "earshotAction"
VRCMD = "/opt/steamvr/bin/linuxarm64/vrcmd"
HFP_AG_UUID = "0000111f"  # what the iPhone advertises; BlueZ's hands-free unit connects to this
ANCS_CATEGORY_INCOMING_CALL = 1
MAP_APP_ID = "com.apple.MobileSMS"  # Tether leaves ANCS "Messages" alone: MAP carries those

UPDATER = os.path.join(INSTALL_ROOT, "earshot-frame-install.py")
VERSIONS_FILE = os.path.join(INSTALL_ROOT, "versions.json")
UPDATE_STATUS_FILE = os.path.join(STATE_DIR, "update.json")
UPDATE_UNIT = "earshot-update"
AUTO_UPDATE_FIRST = 10 * 60
AUTO_UPDATE_EVERY = 6 * 60 * 60

MAX_NOTIFICATIONS = 100
MAX_THREADS = 50
MAX_MESSAGES = 100
MAX_TEXT_SEND = 2000
MAX_LINE = 8 * 1024 * 1024
OP_ID = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def trim(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# Settings: ~/.config/earshot/settings.json, 0600, written atomically
# ---------------------------------------------------------------------------

SETTING_SPECS = {
    "priority": lambda v: v in ("auto", "frame", "phone"),
    "alerts": lambda v: isinstance(v, bool),          # SteamVR notifications at all
    "preview": lambda v: isinstance(v, bool),         # include text in them
    "auto_update": lambda v: isinstance(v, bool),
    "muted_apps": lambda v: isinstance(v, list) and len(v) <= 500 and all(isinstance(x, str) and 0 < len(x) <= 200 for x in v),
    "presence_source": lambda v: v in ("steamvr", "file"),
    "presence_file": lambda v: isinstance(v, str) and len(v) <= 300,
    "presence_worn_values": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v) and len(v) <= 10,
}
SETTING_DEFAULTS = {
    "priority": "auto",
    "alerts": True,
    "preview": True,
    "auto_update": True,
    "muted_apps": [],
    "presence_source": "steamvr",
    "presence_file": "",
    "presence_worn_values": ["1", "worn", "true"],
}


class Settings:
    def __init__(self, path):
        self.path = path
        self.values = json.loads(json.dumps(SETTING_DEFAULTS))
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.loads(f.read(256 * 1024))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            log.warning("settings unreadable (%s); using defaults", type(error).__name__)
            return
        if not isinstance(data, dict):
            return
        for key, ok in SETTING_SPECS.items():
            if key in data and ok(data[key]):
                self.values[key] = data[key]

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        """Validate and store one setting. Returns True when it changed."""
        ok = SETTING_SPECS.get(key)
        if ok is None or not ok(value):
            return False
        if self.values[key] == value:
            return False
        self.values[key] = json.loads(json.dumps(value))
        self.save()
        return True

    def save(self):
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".settings.", dir=directory)  # mkstemp creates it 0600
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.values, f, indent=1, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as error:
            log.warning("could not save settings (%s)", type(error).__name__)


# ---------------------------------------------------------------------------
# Event loop with timers (single thread)
# ---------------------------------------------------------------------------

class Loop:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.sel = selectors.DefaultSelector()
        self.timers = []
        self.seq = itertools.count()
        self.cancelled = set()
        self.stopping = False
        self.wake_r, self.wake_w = socket.socketpair()
        self.wake_r.setblocking(False)
        self.wake_w.setblocking(False)
        self.wake_handlers = []
        self.sel.register(self.wake_r, selectors.EVENT_READ, ("wake", None))

    def wake(self):
        try:
            self.wake_w.send(b"x")
        except OSError:
            pass

    def add_reader(self, sock, callback):
        try:
            self.sel.register(sock, selectors.EVENT_READ, ("read", callback))
        except KeyError:
            self.sel.modify(sock, selectors.EVENT_READ, ("read", callback))

    def remove_reader(self, sock):
        try:
            self.sel.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass

    def call_later(self, delay, fn):
        handle = next(self.seq)
        heapq.heappush(self.timers, (self.clock() + max(0.0, delay), handle, fn))
        return handle

    def cancel(self, handle):
        if handle is not None:
            self.cancelled.add(handle)

    def next_timeout(self, limit):
        while self.timers and self.timers[0][1] in self.cancelled:
            self.cancelled.discard(heapq.heappop(self.timers)[1])
        if not self.timers:
            return limit
        return max(0.0, min(limit, self.timers[0][0] - self.clock()))

    def run_once(self, max_wait=30.0):
        for key, _ in self.sel.select(self.next_timeout(max_wait)):
            kind, callback = key.data
            if kind == "wake":
                try:
                    self.wake_r.recv(4096)
                except OSError:
                    pass
                for fn in list(self.wake_handlers):
                    self.guard(fn)
            else:
                self.guard(callback)
        now = self.clock()
        while self.timers and self.timers[0][0] <= now:
            _, handle, fn = heapq.heappop(self.timers)
            if handle in self.cancelled:
                self.cancelled.discard(handle)
                continue
            self.guard(fn)

    @staticmethod
    def guard(fn):
        try:
            fn()
        except Exception as error:  # one bad handler must not stop the helper
            log.warning("handler failed: %s", type(error).__name__)
            log.debug("handler traceback", exc_info=True)

    def run(self):
        while not self.stopping:
            self.run_once()

    def stop(self):
        self.stopping = True
        self.wake()

    def close(self):
        for s in (self.wake_r, self.wake_w):
            try:
                s.close()
            except OSError:
                pass
        self.sel.close()


# ---------------------------------------------------------------------------
# External commands, off the main loop
# ---------------------------------------------------------------------------

def default_spawn(argv, timeout):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except (OSError, subprocess.SubprocessError) as error:
        return 127, "", type(error).__name__


class Executor:
    """Runs argv lists on one worker thread; on_done(rc, out, err) runs on the loop thread.
    inline=True runs everything on the caller (tests)."""

    def __init__(self, loop, spawn=default_spawn, inline=False):
        self.loop = loop
        self.spawn = spawn
        self.inline = inline
        self.jobs = queue.Queue()
        self.done = collections.deque()
        self.thread = None
        loop.wake_handlers.append(self.drain)

    def run(self, argv, on_done=None, timeout=20):
        if self.inline:
            result = self.spawn(argv, timeout)
            if on_done:
                on_done(*result)
            return
        if self.thread is None:
            self.thread = threading.Thread(target=self.work, name="earshot-exec", daemon=True)
            self.thread.start()
        self.jobs.put((argv, on_done, timeout))

    def work(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            argv, on_done, timeout = job
            self.done.append((on_done, self.spawn(argv, timeout)))
            self.loop.wake()

    def drain(self):
        while self.done:
            on_done, result = self.done.popleft()
            if on_done:
                on_done(*result)

    def close(self):
        if self.thread:
            self.jobs.put(None)


# ---------------------------------------------------------------------------
# Tether socket client
# ---------------------------------------------------------------------------

def tether_socket_path():
    override = os.environ.get("EARSHOT_TETHER_SOCKET")
    if override:
        return override
    base = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    # Tether's get_runtime_dir(): <runtime dir>/tether, the socket is tetherd.sock inside it.
    return os.path.join(base, "tether", "tetherd.sock")


def connect_unix(path=None):
    path = path or tether_socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect(path)
    except OSError:
        s.close()
        raise
    return s


class TetherLink:
    """Newline-delimited JSON over a stream socket, with reconnect and backoff."""

    def __init__(self, loop, connect, on_message, on_state, backoff=(1.0, 30.0)):
        self.loop = loop
        self.connect = connect
        self.on_message = on_message
        self.on_state = on_state          # on_state(connected: bool)
        self.backoff_start, self.backoff_max = backoff
        self.delay = self.backoff_start
        self.sock = None
        self.buf = b""
        self.timer = None
        self.connected_at = 0.0
        self.stopped = False

    @property
    def connected(self):
        return self.sock is not None

    def start(self):
        self.stopped = False
        self.attempt()

    def attempt(self):
        self.timer = None
        if self.stopped or self.sock is not None:
            return
        try:
            sock = self.connect()
        except (OSError, ConnectionError) as error:
            log.info("tether: not reachable (%s); retrying in %.0fs", type(error).__name__, self.delay)
            self.schedule()
            return
        sock.settimeout(2.0)
        self.sock = sock
        self.buf = b""
        self.connected_at = self.loop.clock()
        self.loop.add_reader(sock, self.readable)
        log.info("tether: connected")
        self.send({"command": "protocol_info"})
        self.send({"command": "subscribe"})
        self.on_state(True)

    def schedule(self):
        if self.stopped:
            return
        self.timer = self.loop.call_later(self.delay, self.attempt)
        self.delay = min(self.backoff_max, self.delay * 2)

    def readable(self):
        sock = self.sock
        if sock is None:
            return
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self.drop("closed")
            return
        self.buf += chunk
        if len(self.buf) > MAX_LINE and b"\n" not in self.buf:
            self.drop("unframed stream")
            return
        while True:
            pos = self.buf.find(b"\n")
            if pos < 0:
                break
            line, self.buf = self.buf[:pos], self.buf[pos + 1:]
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue  # the daemon answers some commands with a bare "OK"
            if isinstance(msg, dict):
                self.on_message(msg)

    def send(self, msg):
        if self.sock is None:
            return False
        try:
            self.sock.sendall(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
            return True
        except OSError:
            self.drop("write failed")
            return False

    def drop(self, why):
        sock, self.sock = self.sock, None
        if sock is not None:
            self.loop.remove_reader(sock)
            try:
                sock.close()
            except OSError:
                pass
        was_long = self.loop.clock() - self.connected_at > 10
        log.warning("tether: connection lost (%s)", why)
        self.delay = self.backoff_start if was_long else min(self.backoff_max, self.delay)
        self.on_state(False)
        self.schedule()

    def reconnect_now(self):
        if self.sock is None and not self.stopped:
            self.loop.cancel(self.timer)
            self.delay = self.backoff_start
            self.attempt()

    def stop(self):
        self.stopped = True
        self.loop.cancel(self.timer)
        sock, self.sock = self.sock, None
        if sock is not None:
            self.loop.remove_reader(sock)
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# DevTools (minimal RFC 6455 client, as in Tragus's helper)
# ---------------------------------------------------------------------------

class WsClient:
    def __init__(self, ws_url, timeout=4.0):
        url = urllib.parse.urlparse(ws_url)
        if url.hostname not in ("127.0.0.1", "localhost") or not url.port:
            raise ValueError("refusing a non-local DevTools URL")
        self.sock = socket.create_connection((url.hostname, url.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (f"GET {url.path} HTTP/1.1\r\nHost: {url.hostname}:{url.port}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk or len(head) > 16384:
                raise ConnectionError("DevTools handshake failed")
            head += chunk
        head, self.buffer = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            raise ConnectionError("DevTools refused the connection")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if expected not in head:
            raise ConnectionError("DevTools handshake: bad accept key")
        self.fragments = []

    def fileno(self):
        return self.sock.fileno()

    def send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self.send_frame(0x1, text.encode("utf-8"))

    def read_messages(self):
        """Call when the socket is readable. Returns complete text messages."""
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("DevTools connection closed")
        self.buffer += chunk
        if len(self.buffer) > 32 * 1024 * 1024:
            raise ConnectionError("DevTools sent too much")
        out = []
        while True:
            frame = self.parse()
            if frame is None:
                break
            opcode, fin, payload = frame
            if opcode == 0x9:
                self.send_frame(0xA, payload)
            elif opcode == 0x8:
                raise ConnectionError("DevTools connection closed")
            elif opcode in (0x0, 0x1):
                self.fragments.append(payload)
                if fin:
                    out.append(b"".join(self.fragments).decode("utf-8", "replace"))
                    self.fragments = []
        return out

    def parse(self):
        buf = self.buffer
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        n, offset = b1 & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            n, offset = struct.unpack("!H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, offset = struct.unpack("!Q", buf[2:10])[0], 10
        if b1 & 0x80:
            offset += 4
        if len(buf) < offset + n:
            return None
        payload = buf[offset:offset + n]
        if b1 & 0x80:
            mask = buf[offset - 4:offset]
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.buffer = buf[offset + n:]
        return b0 & 0x0F, bool(b0 & 0x80), payload

    def close(self):
        try:
            self.send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# Loopback only: never route these requests through a proxy from the environment.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get_json(url, timeout=3):
    with OPENER.open(url, timeout=timeout) as response:
        return json.load(response)


class Page:
    """One Steam page we hold a DevTools connection to."""

    def __init__(self, target, ws):
        self.id = target.get("id")
        self.title = str(target.get("title") or "?")[:60]
        self.ws = ws
        self.next_id = itertools.count(1)
        self.pending = {}
        self.binding_added = False
        self.ready = False          # the menu is in the page and answered
        self.last_sent = None       # last state JSON pushed
        self.checking = False
        self.checking_since = 0.0

    def call(self, method, params=None, cb=None):
        msg_id = next(self.next_id)
        if cb:
            self.pending[msg_id] = cb
        self.ws.send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))

    def evaluate(self, expression, cb=None):
        self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, cb)


def eval_value(result, error):
    """Unpack a Runtime.evaluate reply: (ok, value)."""
    if error or not isinstance(result, dict) or "exceptionDetails" in result:
        return False, None
    return True, (result.get("result") or {}).get("value")


class Shell:
    """Keeps the menu injected into Steam's VR pages and moves state/actions across."""

    POLL_FAST = 3.0
    POLL_IDLE = 10.0

    def __init__(self, loop, version, script, on_action, on_ready,
                 cdp_base=CDP_BASE, http_get=http_get_json, ws_factory=WsClient):
        self.loop = loop
        self.version = version
        self.script = script
        self.on_action = on_action
        self.on_ready = on_ready      # on_ready(page): push full state
        self.cdp_base = cdp_base
        self.http_get = http_get
        self.ws_factory = ws_factory
        self.pages = {}
        self.timer = None
        self.endpoint_ok = None
        self.stopped = False

    def start(self):
        self.poll()

    def poll(self):
        self.timer = None
        if self.stopped:
            return
        busy = False
        try:
            targets = self.http_get(self.cdp_base + "/json/list")
            if not isinstance(targets, list):
                raise ValueError("unexpected target list")
            if self.endpoint_ok is not True:
                log.info("steam: DevTools endpoint reachable")
            self.endpoint_ok = True
        except Exception as error:  # Steam restarting, endpoint missing, odd replies
            if self.endpoint_ok is not False:
                log.info("steam: waiting for the DevTools endpoint (%s)", type(error).__name__)
            self.endpoint_ok = False
            self.drop_all()
            targets = []
        wanted = {}
        for t in targets:
            if (isinstance(t, dict) and isinstance(t.get("url"), str) and t.get("webSocketDebuggerUrl")
                    and any(m in t["url"] for m in PAGE_MARKS)):
                wanted[t.get("id")] = t
        for pid in [p for p in self.pages if p not in wanted]:
            self.drop(pid)
        for pid, target in wanted.items():
            page = self.pages.get(pid)
            if page is None:
                page = self.open(target)
                busy = True
            if page is not None:
                self.sync(page)
                busy = busy or not page.ready
        self.timer = self.loop.call_later(self.POLL_FAST if busy or not self.pages else self.POLL_IDLE, self.poll)

    def open(self, target):
        try:
            ws = self.ws_factory(target["webSocketDebuggerUrl"])
        except Exception as error:
            log.warning("steam: could not open a page (%s)", type(error).__name__)
            return None
        page = Page(target, ws)
        self.pages[page.id] = page
        self.loop.add_reader(ws.sock, lambda p=page: self.readable(p))
        return page

    def drop(self, pid):
        page = self.pages.pop(pid, None)
        if page is not None:
            self.loop.remove_reader(page.ws.sock)
            page.ws.close()

    def drop_all(self):
        for pid in list(self.pages):
            self.drop(pid)

    def readable(self, page):
        try:
            messages = page.ws.read_messages()
        except (OSError, ConnectionError) as error:
            log.info("steam: page connection closed (%s)", type(error).__name__)
            self.drop(page.id)
            return
        for raw in messages:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.handle(page, msg)

    def handle(self, page, msg):
        if "id" in msg:
            cb = page.pending.pop(msg["id"], None)
            if cb:
                cb(msg.get("result"), msg.get("error"))
        elif msg.get("method") == "Runtime.bindingCalled":
            params = msg.get("params") or {}
            if params.get("name") == BINDING and isinstance(params.get("payload"), str):
                self.on_action(page, params["payload"])

    def sync(self, page):
        """Make sure the binding exists and the current menu runs in this page."""
        now = self.loop.clock()
        if page.checking and now - page.checking_since < 15.0:
            return
        page.checking = True
        page.checking_since = now
        try:
            if not page.binding_added:
                page.call("Runtime.addBinding", {"name": BINDING}, lambda r, e: None)
                page.binding_added = True
            page.evaluate("window.__earshotMenu ? window.__earshotMenu.version : null",
                          lambda r, e, p=page: self.checked(p, r, e))
        except OSError:
            page.checking = False
            self.drop(page.id)

    def checked(self, page, result, error):
        ok, value = eval_value(result, error)
        if ok and value == self.version:
            page.checking = False
            if not page.ready:
                page.ready = True
                self.on_ready(page)
            return
        page.ready = False
        page.last_sent = None
        page.evaluate(self.script, lambda r, e, p=page: self.injected(p, r, e))

    def injected(self, page, result, error):
        page.checking = False
        ok, value = eval_value(result, error)
        if not ok:
            log.warning("steam: menu did not start in a page")
            return
        if isinstance(value, str) and value.startswith("installed") or value == "already " + self.version:
            log.info("steam: %s", value)
            page.ready = True
            page.last_sent = None
            self.on_ready(page)

    def push(self, page, state_json):
        if not page.ready or page.last_sent == state_json:
            return
        page.last_sent = state_json
        try:
            page.evaluate("window.__earshotMenu&&window.__earshotMenu.setState(%s)" % state_json,
                          lambda r, e, p=page: self.pushed(p, r, e))
        except OSError:
            self.drop(page.id)

    def pushed(self, page, result, error):
        ok, _ = eval_value(result, error)
        if not ok:
            page.ready = False  # the page reloaded or the menu is gone: re-inject on the next check
            page.last_sent = None

    def teardown(self):
        self.stopped = True
        self.loop.cancel(self.timer)
        for page in list(self.pages.values()):
            try:
                page.evaluate("window.__earshotMenu&&window.__earshotMenu.teardown()")
            except OSError:
                pass
        self.drop_all()


# ---------------------------------------------------------------------------
# Notification rate limiting
# ---------------------------------------------------------------------------

class BurstCollapser:
    """First item of a quiet period is shown at once. Further items from the same key
    inside `window` seconds are held and shown as one line when the window closes:
    'N new messages from Name'. Pure: callers pass the time."""

    def __init__(self, window=8.0):
        self.window = window
        self.state = {}  # key -> {"start": t, "pending": [text], "noun": str, "who": str}

    def add(self, key, text, noun, who, now):
        st = self.state.get(key)
        if st is None or now - st["start"] >= self.window:
            self.state[key] = {"start": now, "pending": [], "noun": noun, "who": who}
            return [text]
        st["pending"].append(text)
        return []

    def next_due(self):
        return min((s["start"] + self.window for s in self.state.values()), default=None)

    def flush(self, now):
        out = []
        for key in list(self.state):
            st = self.state[key]
            if now < st["start"] + self.window:
                continue
            pending = st["pending"]
            if not pending:
                del self.state[key]
            elif len(pending) == 1:
                out.append(pending[0])
                st["start"], st["pending"] = now, []
            else:
                out.append("%d new %s from %s" % (len(pending), st["noun"], st["who"]))
                st["start"], st["pending"] = now, []
        return out


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

def scan_processes(names, proc="/proc"):
    try:
        entries = os.listdir(proc)
    except OSError:
        return False
    wanted = set(names)
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc, entry, "comm"), "r", encoding="utf-8", errors="replace") as f:
                if f.read(64).strip() in wanted:
                    return True
        except OSError:
            continue
    return False


class PresenceDetector:
    """Answers 'is the headset on a head?'. poll(done) calls done(True|False|None);
    None means 'cannot tell'. Add a detector here once a signal is proven on the headset."""
    name = "base"

    def poll(self, done):
        done(None)


class SteamVrRunningDetector(PresenceDetector):
    """Stand-in until a real worn sensor is proven: the headset counts as worn while SteamVR runs."""
    name = "steamvr"

    def __init__(self, scan=scan_processes):
        self.scan = scan

    def poll(self, done):
        done(bool(self.scan(("vrserver", "vrcompositor"))))


class FileDetector(PresenceDetector):
    """Reads a small sysfs/input-style file; its trimmed text in worn_values means worn."""
    name = "file"

    def __init__(self, path, worn_values):
        self.path = path
        self.worn_values = set(worn_values)

    def poll(self, done):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                text = f.read(64).strip().lower()
        except OSError:
            done(None)
            return
        done(text in self.worn_values)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

class Helper:
    def __init__(self, loop=None, settings=None, connect_tether=connect_unix, executor=None,
                 menu_file=MENU_FILE, cdp_base=CDP_BASE, http_get=http_get_json, ws_factory=WsClient,
                 vrcmd=VRCMD, updater=UPDATER, status_file=UPDATE_STATUS_FILE, versions_file=VERSIONS_FILE,
                 settings_path=None, scan=scan_processes, burst_window=8.0, push_delay=0.15,
                 tether_backoff=(1.0, 30.0), presence_every=10.0, send_timeout=45.0,
                 settle_seconds=5.0, auto_update_first=AUTO_UPDATE_FIRST, clock=None, exists=os.path.exists):
        self.loop = loop or Loop(clock or time.monotonic)
        self.settings = settings or Settings(settings_path or os.path.join(CONFIG_DIR, "settings.json"))
        self.exec = executor or Executor(self.loop)
        self.vrcmd = vrcmd
        self.exists = exists
        self.updater = updater
        self.status_file = status_file
        self.versions_file = versions_file
        self.push_delay = push_delay
        self.presence_every = presence_every
        self.send_timeout = send_timeout
        self.settle_seconds = settle_seconds
        self.scan = scan
        self.auto_update_first = auto_update_first
        self.hfp_retry_after = 30.0

        # what the engine told us
        self.engine_up = False
        self.caps = []
        self.bt_status = {}
        self.conn = {}
        self.devices = []
        self.notifications = collections.OrderedDict()   # uid -> dict, oldest first
        self.cleared = set()
        self.known_apps = {}                             # app_id -> name
        self.threads = []
        self.messages = {}                               # thread -> list
        self.open_thread = None
        self.marked_read = set()
        self.calls = []
        self.call_history = []
        self.call_error = ""
        self.announced_calls = set()
        self.sends = collections.OrderedDict()           # op id -> {thread, status, error, at}
        self.seen_uids = set()
        self.ancs_ready_at = None
        self.map_open_at = None

        # presence and priority
        self.worn = None
        self.detector = None
        self.hfp_released = None
        self.hfp_busy = False
        self.hfp_retry = None
        self.hfp_block = None     # (until, wanted method) after a failed attempt
        self.last_classic = False
        self.presence_timer = None

        # notifications out
        self.collapser = BurstCollapser(burst_window)
        self.collapse_timer = None
        self.vrcmd_missing_logged = False

        # updates
        self.update_status = None
        self.update_mtime = None
        self.update_timer = None
        self.started_wall = time.time()
        self.next_auto_update = None

        self.push_timer = None
        self.op_counter = itertools.count(1)
        self.stopping = False

        self.version, self.script = self.load_menu(menu_file)
        self.link = TetherLink(self.loop, connect_tether, self.on_tether, self.on_tether_state, tether_backoff)
        self.shell = Shell(self.loop, self.version, self.script, self.on_action, self.on_page_ready,
                           cdp_base=cdp_base, http_get=http_get, ws_factory=ws_factory)

    # ----- lifecycle -----

    @staticmethod
    def load_menu(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
        except OSError as error:
            log.warning("menu file missing (%s)", type(error).__name__)
            source = "'menu file missing'"
        version = hashlib.sha256(source.encode()).hexdigest()[:12]
        return version, source.replace("__VERSION__", version).replace("__BINDING__", BINDING)

    def start(self):
        self.refresh_detector()
        self.poll_presence()
        self.link.start()
        self.shell.start()
        self.read_update_status(force=True)
        self.update_timer = self.loop.call_later(10.0, self.tick_update)
        self.next_auto_update = self.loop.clock() + self.auto_update_first

    def run(self):
        self.start()
        try:
            self.loop.run()
        finally:
            self.shutdown()
            self.loop.close()

    def shutdown(self):
        if self.stopping:
            return
        self.stopping = True
        self.link.stop()
        self.shell.teardown()
        self.exec.close()

    # ----- Tether side -----

    def on_tether_state(self, up):
        self.engine_up = up
        if up:
            self.ancs_ready_at = None
            self.map_open_at = None
        else:
            self.conn = {}
            self.bt_status = {}
            self.calls = []
            self.hfp_released = None
            self.last_classic = False
        self.dirty()

    def request(self, command, **fields):
        fields["command"] = command
        return self.link.send(fields)

    def on_tether(self, msg):
        handler = getattr(self, "t_" + str(msg.get("command", "")), None)
        if handler:
            handler(msg)

    def t_protocol_info(self, msg):
        caps = msg.get("capabilities")
        self.caps = [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else []
        # Everything below is a read; ask for each once per connection.
        for command in ("bt_status", "bt_connection", "bt_list_devices", "bt_list_threads",
                        "bt_list_notifications", "bt_list_calls", "bt_list_call_history"):
            self.request(command)
        self.dirty()

    def t_bt_status(self, msg):
        self.bt_status = msg
        self.dirty()

    def t_bt_devices(self, msg):
        devices = msg.get("devices")
        self.devices = [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []
        self.enforce_hfp()
        self.dirty()

    def t_bt_connection_changed(self, msg):
        before = self.conn
        self.conn = msg
        classic = bool(msg.get("classic_connected"))
        if classic and not self.last_classic:
            self.hfp_released = None            # a new link: whatever we released is back
            self.request("bt_list_devices")
        self.last_classic = classic
        if msg.get("map_open") and not before.get("map_open"):
            self.map_open_at = self.loop.clock()
            self.request("bt_list_threads")
        if msg.get("ancs_ready") and not before.get("ancs_ready"):
            self.ancs_ready_at = self.loop.clock()
            self.request("bt_list_notifications")
        calls = msg.get("calls")
        if isinstance(calls, dict) and calls.get("available") and not (before.get("calls") or {}).get("available"):
            self.request("bt_list_calls")
            self.request("bt_list_call_history")
        self.enforce_hfp()
        self.dirty()

    def t_bt_threads(self, msg):
        threads = msg.get("threads")
        self.threads = [t for t in threads if isinstance(t, dict)] if isinstance(threads, list) else []
        self.dirty()

    def t_bt_messages(self, msg):
        thread = msg.get("thread")
        messages = msg.get("messages")
        if isinstance(thread, str) and isinstance(messages, list):
            self.messages[thread] = [m for m in messages if isinstance(m, dict)][-MAX_MESSAGES:]
            if thread == self.open_thread:
                self.mark_read(thread)
            self.dirty()

    def t_bt_message(self, msg):
        thread = msg.get("thread")
        if not isinstance(thread, str):
            return
        cached = self.messages.get(thread)
        if cached is not None and not any(m.get("handle") == msg.get("handle") for m in cached):
            cached.append({k: msg.get(k) for k in ("handle", "body", "timestamp", "outgoing", "read", "folder", "name")})
            del cached[:-MAX_MESSAGES]
        self.request("bt_list_threads")
        if thread == self.open_thread and not msg.get("outgoing"):
            self.mark_read(thread, [msg])
        settling = (self.map_open_at is not None
                    and self.loop.clock() - self.map_open_at < self.settle_seconds)
        if not msg.get("outgoing") and not msg.get("read", False) and not settling:
            self.toast_message(msg)
        self.dirty()

    def t_bt_send_result(self, msg):
        op = msg.get("operation_id")
        send = self.sends.get(op)
        if send is None:
            return
        if msg.get("success"):
            send["status"], send["error"] = "sent", ""
        else:
            send["status"], send["error"] = "failed", trim(msg.get("message") or "The phone did not accept it.", 160)
        self.loop.cancel(send.pop("timer", None))
        self.dirty()

    def t_bt_message_read(self, msg):
        self.request("bt_list_threads")

    def t_bt_notifications(self, msg):
        items = msg.get("notifications")
        if not isinstance(items, list):
            return
        fresh = collections.OrderedDict()
        for n in sorted((n for n in items if isinstance(n, dict) and isinstance(n.get("uid"), int)),
                        key=lambda n: n.get("timestamp") or 0):
            fresh[n["uid"]] = n
            self.seen_uids.add(n["uid"])
            self.note_app(n)
        self.notifications = collections.OrderedDict(list(fresh.items())[-MAX_NOTIFICATIONS:])
        self.dirty()

    def t_bt_notification(self, msg):
        uid = msg.get("uid")
        if not isinstance(uid, int):
            return
        is_new = uid not in self.seen_uids
        self.seen_uids.add(uid)
        self.note_app(msg)
        self.notifications[uid] = msg
        self.notifications.move_to_end(uid)
        while len(self.notifications) > MAX_NOTIFICATIONS:
            self.notifications.popitem(last=False)
        settling = (self.ancs_ready_at is not None
                    and self.loop.clock() - self.ancs_ready_at < self.settle_seconds)
        if is_new and not settling and not msg.get("pre_existing"):
            self.toast_notification(msg)
        self.dirty()

    def t_bt_notification_removed(self, msg):
        self.notifications.pop(msg.get("uid"), None)
        self.dirty()

    def t_bt_notification_action_result(self, msg):
        if not msg.get("success"):
            self.call_error = "The iPhone did not take that."
            self.dirty()

    def t_bt_calls(self, msg):
        calls = msg.get("calls")
        self.calls = [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []
        live = {c.get("path") for c in self.calls}
        self.announced_calls &= live
        for call in self.calls:
            if call.get("ringing") and call.get("path") not in self.announced_calls:
                self.announced_calls.add(call.get("path"))
                self.toast_call(call.get("name") or call.get("number") or "")
        if not any(c.get("ringing") for c in self.calls):
            self.call_error = ""
        self.dirty()

    def t_bt_call_history(self, msg):
        calls = msg.get("calls")
        self.call_history = [c for c in calls if isinstance(c, dict)][:15] if isinstance(calls, list) else []
        self.dirty()

    def t_bt_call_result(self, msg):
        if msg.get("action") in ("answer", "hangup") and not msg.get("success"):
            self.call_error = trim(msg.get("message") or "The call could not be changed.", 160)
            self.dirty()

    def t_error(self, msg):
        log.info("tether: error reply")

    def note_app(self, n):
        app_id = n.get("app_id")
        if isinstance(app_id, str) and app_id:
            self.known_apps[app_id] = n.get("app_name") or self.known_apps.get(app_id) or app_id

    def mark_read(self, thread, messages=None):
        pending = []
        for m in (messages if messages is not None else self.messages.get(thread, [])):
            h = m.get("handle")
            if h and not m.get("outgoing") and not m.get("read", True) and h not in self.marked_read:
                pending.append(h)
        if pending and self.request("bt_mark_read", handles=pending, read=True):
            self.marked_read.update(pending)

    # ----- presence and priority -----

    def refresh_detector(self):
        source = self.settings.get("presence_source")
        if source == "file" and self.settings.get("presence_file"):
            self.detector = FileDetector(self.settings.get("presence_file"), self.settings.get("presence_worn_values"))
        else:
            self.detector = SteamVrRunningDetector(self.scan)
        self.fallback_detector = SteamVrRunningDetector(self.scan)

    def poll_presence(self):
        self.presence_timer = None

        def done(value):
            if value is None:  # cannot tell: fall back to "SteamVR is running"
                self.fallback_detector.poll(lambda v: self.set_worn(bool(v)))
            else:
                self.set_worn(value)

        try:
            self.detector.poll(done)
        finally:
            if not self.stopping:
                self.presence_timer = self.loop.call_later(self.presence_every, self.poll_presence)

    def set_worn(self, worn):
        if worn != self.worn:
            log.info("presence: %s", "worn" if worn else "not worn")
            self.worn = worn
            self.enforce_hfp()
            self.dirty()

    def frame_active(self):
        mode = self.settings.get("priority")
        if mode == "frame":
            return True
        if mode == "phone":
            return False
        return self.worn is not False  # unknown counts as worn: Auto = Frame while SteamVR runs

    def phone_device(self):
        address = self.bt_status.get("device_address")
        for d in self.devices:
            if address and str(d.get("address", "")).lower() == str(address).lower() and d.get("path"):
                return d
        return None

    def enforce_hfp(self):
        """Not worn: let go of the call link so the phone takes calls. Worn again: take it back."""
        dev = self.phone_device()
        if dev is None or self.hfp_busy:
            return
        path = dev["path"]
        if not self.frame_active():
            method = "DisconnectProfile"
            if self.hfp_released == path or not dev.get("connected", dev.get("classic_connected", True)):
                return
        elif self.hfp_released == path:
            method = "ConnectProfile"
        else:
            return
        block = self.hfp_block
        if block and block[1] == method and self.loop.clock() < block[0]:
            return  # it just failed; the retry timer tries again
        self.hfp_call(method, path, released=path if method == "DisconnectProfile" else None)

    def hfp_call(self, method, path, released):
        self.hfp_busy = True
        self.loop.cancel(self.hfp_retry)

        def done(rc, out, err):
            self.hfp_busy = False
            if rc == 0:
                self.hfp_released = released
                self.hfp_block = None
                log.info("hfp: %s ok", method)
            else:
                log.warning("hfp: %s failed (rc %s)", method, rc)
                self.hfp_block = (self.loop.clock() + self.hfp_retry_after, method)
                self.hfp_retry = self.loop.call_later(self.hfp_retry_after, self.enforce_hfp)

        self.exec.run(["busctl", "--system", "call", "org.bluez", path, "org.bluez.Device1", method, "s", HFP_AG_UUID],
                      done, timeout=15)

    # ----- SteamVR notifications -----

    def can_toast(self, app_id=None):
        return (self.settings.get("alerts") and self.frame_active()
                and not (app_id and app_id in self.settings.get("muted_apps")))

    def toast_text(self, who, text):
        if not self.settings.get("preview"):
            return trim(who, 60)
        return trim("%s: %s" % (who, text) if text else who, 160)

    def toast_notification(self, n):
        app_id = n.get("app_id") or ""
        if app_id == MAP_APP_ID or n.get("category") == ANCS_CATEGORY_INCOMING_CALL or not self.can_toast(app_id):
            return  # messages come over MAP; calls get their own, unthrottled toast
        app = trim(n.get("app_name") or app_id or "iPhone", 40)
        detail = " - ".join(x for x in (n.get("title"), n.get("body") or n.get("subtitle")) if x)
        text = self.toast_text(app, detail) if self.settings.get("preview") else app + ": new notification"
        self.emit_collapsed("app:" + app_id, text, "notifications", app)

    def toast_message(self, m):
        if not self.can_toast(MAP_APP_ID):
            return
        who = m.get("name") or self.thread_name(m.get("thread")) or "New message"
        text = self.toast_text(who, m.get("body")) if self.settings.get("preview") else "New message from " + trim(who, 40)
        self.emit_collapsed("msg:" + str(m.get("thread")), text, "messages", trim(who, 40))

    def thread_name(self, key):
        for t in self.threads:
            if t.get("thread") == key:
                return t.get("name") or t.get("address")
        return ""

    def toast_call(self, who):
        if not self.can_toast():
            return
        self.send_vrcmd("Incoming call: %s" % trim(who, 60) if who and self.settings.get("preview") else "Incoming call")

    def emit_collapsed(self, key, text, noun, who):
        for t in self.collapser.add(key, text, noun, who, self.loop.clock()):
            self.send_vrcmd(t)
        self.arm_collapse_timer()

    def arm_collapse_timer(self):
        self.loop.cancel(self.collapse_timer)
        self.collapse_timer = None
        due = self.collapser.next_due()
        if due is not None:
            self.collapse_timer = self.loop.call_later(max(0.05, due - self.loop.clock()) + 0.01, self.flush_collapsed)

    def flush_collapsed(self):
        self.collapse_timer = None
        for t in self.collapser.flush(self.loop.clock()):
            if self.frame_active():
                self.send_vrcmd(t)
        self.arm_collapse_timer()

    def send_vrcmd(self, text):
        if not self.frame_active():
            return
        if not self.exists(self.vrcmd):
            if not self.vrcmd_missing_logged:
                log.warning("vrcmd not found; SteamVR notifications are off")
                self.vrcmd_missing_logged = True
            return
        self.exec.run([self.vrcmd, "--notify", text], None, timeout=20)

    # ----- actions from the menu -----

    def on_page_ready(self, page):
        self.push_to(page)

    def on_action(self, page, payload):
        if len(payload) > 16 * 1024:
            return
        try:
            action = json.loads(payload)
        except ValueError:
            return
        if not isinstance(action, dict):
            return
        handler = getattr(self, "a_" + str(action.get("type", "")), None)
        if handler:
            handler(action)
            self.dirty()

    def a_hello(self, a):
        pass  # the state push that follows is the answer

    def a_retry(self, a):
        self.link.reconnect_now()

    def a_open_thread(self, a):
        thread = a.get("thread")
        if not isinstance(thread, str) or not any(t.get("thread") == thread and not t.get("group") for t in self.threads):
            return
        self.open_thread = thread
        self.request("bt_list_messages", thread=thread)

    def a_close_thread(self, a):
        self.open_thread = None

    def a_send(self, a):
        thread, text, cid = a.get("thread"), a.get("text"), a.get("cid")
        if not (isinstance(thread, str) and isinstance(text, str) and isinstance(cid, str) and OP_ID.match(cid)):
            return
        text = text.strip()
        info = next((t for t in self.threads if t.get("thread") == thread), None)
        if not text or len(text) > MAX_TEXT_SEND or info is None:
            return
        op = "earshot-" + cid
        if op in self.sends:
            return
        send = {"thread": thread, "status": "sending", "error": "", "at": time.time(), "cid": cid}
        if not info.get("repliable", True):
            send["status"], send["error"] = "failed", trim(info.get("reply_reason") or "This conversation cannot be replied to.", 160)
        elif not self.request("bt_send_message", thread=thread, body=text, operation_id=op):
            send["status"], send["error"] = "failed", "Earshot cannot reach the phone service."
        else:
            send["timer"] = self.loop.call_later(self.send_timeout, lambda: self.send_timed_out(op))
        self.sends[op] = send
        while len(self.sends) > 20:
            self.sends.popitem(last=False)

    def send_timed_out(self, op):
        send = self.sends.get(op)
        if send and send["status"] == "sending":
            send["status"], send["error"] = "failed", "No answer from the phone. It may still have been sent."
            send.pop("timer", None)
            self.dirty()

    def a_call(self, a):
        op, path, uid = a.get("op"), a.get("path"), a.get("uid")
        if op not in ("answer", "decline"):
            return
        self.call_error = ""
        hfp_up = isinstance(self.conn.get("calls"), dict) and self.conn["calls"].get("available")
        known = isinstance(path, str) and any(c.get("path") == path for c in self.calls)
        if hfp_up and known:
            self.request("bt_call_action", action="answer" if op == "answer" else "hangup", path=path)
            return
        n = self.notifications.get(uid) if isinstance(uid, int) else None
        if n is not None and n.get("category") == ANCS_CATEGORY_INCOMING_CALL:
            key = "positive_action" if op == "answer" else "negative_action"
            if n.get(key, False):
                self.request("bt_notification_action", uid=uid, action="positive" if op == "answer" else "negative")
                return
        self.call_error = "Calls are not available right now."

    def a_notif_action(self, a):
        uid, action = a.get("uid"), a.get("action")
        n = self.notifications.get(uid) if isinstance(uid, int) else None
        if n is None or action not in ("positive", "negative") or not n.get(action + "_action", False):
            return
        self.request("bt_notification_action", uid=uid, action=action)

    def a_clear_all(self, a):
        self.cleared.update(self.notifications)

    def a_set_setting(self, a):
        key = a.get("key")
        if key in ("priority", "alerts", "preview", "auto_update") and self.settings.set(key, a.get("value")):
            if key == "priority":
                self.enforce_hfp()
            if key == "auto_update":
                self.next_auto_update = self.loop.clock() + self.auto_update_first

    def a_mute(self, a):
        app_id, muted = a.get("app_id"), a.get("muted")
        if not (isinstance(app_id, str) and app_id and isinstance(muted, bool)):
            return
        current = list(self.settings.get("muted_apps"))
        if muted and app_id not in current:
            current.append(app_id)
        elif not muted and app_id in current:
            current.remove(app_id)
        else:
            return
        self.settings.set("muted_apps", current)

    def a_check_updates(self, a):
        self.start_update()

    # ----- updates -----

    def read_update_status(self, force=False):
        try:
            st = os.stat(self.status_file)
            mtime = (st.st_mtime_ns, st.st_size)
        except OSError:
            if self.update_status is not None:
                self.update_status = None
                self.dirty()
            return
        if mtime == self.update_mtime and not force:
            return
        self.update_mtime = mtime
        try:
            with open(self.status_file, "r", encoding="utf-8") as f:
                data = json.loads(f.read(4096))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.update_status = {"state": str(data.get("state", ""))[:20], "message": trim(data.get("message", ""), 200),
                                  "time": data.get("time") if isinstance(data.get("time"), int) else 0}
            self.dirty()

    def installed_versions(self):
        try:
            with open(self.versions_file, "r", encoding="utf-8") as f:
                data = json.loads(f.read(8192))
        except (OSError, ValueError):
            return {}
        return {str(k)[:30]: str(v)[:30] for k, v in data.items()} if isinstance(data, dict) else {}

    def tick_update(self):
        self.update_timer = None
        if self.stopping:
            return
        self.read_update_status()
        now = self.loop.clock()
        if self.settings.get("auto_update") and self.next_auto_update is not None and now >= self.next_auto_update:
            self.next_auto_update = now + AUTO_UPDATE_EVERY
            log.info("automatic update check")
            self.start_update()
        busy = bool(self.update_status and self.update_status.get("state") in ("checking", "updating"))
        self.update_timer = self.loop.call_later(3.0 if busy else 30.0, self.tick_update)

    def write_update_status(self, state, message):
        try:
            os.makedirs(os.path.dirname(self.status_file), exist_ok=True)
            tmp = self.status_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"state": state, "message": message, "time": int(time.time())}, f)
            os.replace(tmp, self.status_file)
        except OSError as error:
            log.warning("update status not written (%s)", type(error).__name__)
        self.read_update_status(force=True)

    def start_update(self):
        """Run the signed installer with --if-newer in its own user unit (it restarts this service)."""
        if not os.path.isfile(self.updater):
            self.write_update_status("failed", "Updates need a newer Earshot: install it once more from the install button.")
            return
        if self.update_status and self.update_status.get("state") in ("checking", "updating") \
                and time.time() - self.update_status.get("time", 0) < 600:
            return
        python = shutil.which("python3") or "/usr/bin/python3"
        self.write_update_status("checking", "Checking for updates...")

        def started(rc, out, err):
            if rc != 0:
                self.write_update_status("failed", "Update failed: " + (trim(err, 160) or "systemd-run exited %s" % rc))

        def run():
            self.exec.run(["systemd-run", "--user", "--quiet", "--collect", "--unit=" + UPDATE_UNIT,
                           "--property=UnsetEnvironment=LD_LIBRARY_PATH LD_PRELOAD",
                           python, "-I", self.updater, "--if-newer"], started, timeout=20)

        self.exec.run(["systemctl", "--user", "reset-failed", UPDATE_UNIT], lambda *_: run(), timeout=10)

    # ----- state for the menu -----

    def setup_state(self):
        """What the menu should say when things are not working yet."""
        if not self.engine_up:
            return {"kind": "no_engine", "title": "The Earshot engine is not running",
                    "text": "Start it from a terminal in Desktop Mode, or restart the headset.",
                    "steps": [{"what": "Start the engine", "command": "systemctl --user start earshot-tetherd"}]}
        cap = self.bt_status.get("capability") if isinstance(self.bt_status.get("capability"), dict) else None
        if self.bt_status and (not self.bt_status.get("available") or cap is None):
            steps = [{"what": s.get("what", ""), "command": s.get("command", "")}
                     for s in (cap or {}).get("setup", []) if isinstance(s, dict)][:6]
            reasons = [trim(r, 160) for r in (cap or {}).get("reasons", []) if isinstance(r, str)][:4]
            return {"kind": "no_bluetooth", "title": "Bluetooth is not ready for Earshot",
                    "text": " ".join(reasons) or "The Bluetooth service is not available.", "steps": steps}
        if self.bt_status and not self.bt_status.get("device_address"):
            return {"kind": "no_phone", "title": "No iPhone is paired yet",
                    "text": "Pair your iPhone with Earshot first. The Earshot README has the steps.", "steps": []}
        cap_steps = [{"what": s.get("what", ""), "command": s.get("command", "")}
                     for s in (cap or {}).get("setup", []) if isinstance(s, dict)][:6]
        if cap_steps and not self.conn.get("classic_connected") and not self.conn.get("le_connected"):
            return {"kind": "setup_needed", "title": "Bluetooth setup is incomplete",
                    "text": trim(self.conn.get("link_reason") or "", 200), "steps": cap_steps}
        return None

    def phone_view(self):
        address = str(self.bt_status.get("device_address") or "").lower()
        name = "iPhone"
        for d in self.devices:
            if address and str(d.get("address", "")).lower() == address and d.get("name"):
                name = trim(d["name"], 60)
        battery = self.conn.get("battery")
        calls = self.conn.get("calls") if isinstance(self.conn.get("calls"), dict) else {}
        return {
            "name": name,
            "connected": bool(self.conn.get("classic_connected") or self.conn.get("le_connected") or self.conn.get("ancs_ready")),
            "battery": battery if isinstance(battery, int) else None,
            "messages": bool(self.conn.get("map_open")),
            "notifications": bool(self.conn.get("ancs_ready")),
            "calls": bool(calls.get("available")),
            "reason": trim(self.conn.get("link_reason") or "", 200),
            "profile_reason": trim(self.conn.get("profile_reason") or "", 200),
            "ancs_reason": trim(self.conn.get("ancs_reason") or "", 200),
        }

    def calls_view(self):
        items = [{"path": c.get("path"), "name": trim(c.get("name") or "", 60), "number": trim(c.get("number") or "", 30),
                  "state": c.get("state") or "", "ringing": bool(c.get("ringing")), "outgoing": bool(c.get("outgoing")),
                  "connected": bool(c.get("connected"))} for c in self.calls][:5]
        incoming = next((i for i in items if i["ringing"]), None)
        if incoming:
            incoming = dict(incoming, source="hfp")
        else:  # no hands-free link: Tether still mirrors the call as an ANCS notification
            for uid, n in reversed(self.notifications.items()):
                if n.get("category") == ANCS_CATEGORY_INCOMING_CALL and (n.get("positive_action") or n.get("negative_action")):
                    incoming = {"source": "ancs", "uid": uid, "name": trim(n.get("title") or n.get("app_name") or "", 60),
                                "number": "", "state": "incoming", "ringing": True,
                                "can_answer": bool(n.get("positive_action")), "can_decline": bool(n.get("negative_action"))}
                    break
        recent = [{"name": trim(c.get("name") or "", 60), "number": trim(c.get("number") or "", 30),
                   "type": c.get("type") or "", "timestamp": c.get("timestamp") or 0} for c in self.call_history]
        return {"items": items, "incoming": incoming, "recent": recent, "error": self.call_error}

    def apps_view(self):
        muted = set(self.settings.get("muted_apps"))
        apps = {i: n for i, n in self.known_apps.items() if i != MAP_APP_ID}
        for i in muted:
            apps.setdefault(i, i)
        return [{"id": i, "name": trim(n, 50), "muted": i in muted} for i, n in sorted(apps.items(), key=lambda kv: kv[1].lower())][:100]

    def view_state(self):
        shown = [n for uid, n in self.notifications.items() if uid not in self.cleared][::-1]
        notifs = [{"uid": n["uid"], "app_id": n.get("app_id") or "", "app": trim(n.get("app_name") or n.get("app_id") or "", 50),
                   "title": trim(n.get("title"), 160), "text": trim(n.get("body") or n.get("subtitle"), 240),
                   "timestamp": n.get("timestamp") or 0, "category": n.get("category") or 0,
                   "can_dismiss": bool(n.get("negative_action")), "can_accept": bool(n.get("positive_action"))}
                  for n in shown][:MAX_NOTIFICATIONS]
        threads = [{"thread": t.get("thread"), "name": trim(t.get("name") or t.get("address") or "", 60),
                    "preview": trim(t.get("preview"), 100), "timestamp": t.get("timestamp") or 0,
                    "unread": t.get("unread") or 0, "repliable": bool(t.get("repliable", True)),
                    "reply_reason": trim(t.get("reply_reason") or "", 160)}
                   for t in self.threads if not t.get("group")][:MAX_THREADS]
        opened = None
        if self.open_thread:
            opened = {"thread": self.open_thread,
                      "messages": [{"handle": m.get("handle"), "body": trim(m.get("body"), 1000), "timestamp": m.get("timestamp") or 0,
                                    "outgoing": bool(m.get("outgoing")), "read": bool(m.get("read", True))}
                                   for m in self.messages.get(self.open_thread, [])][-MAX_MESSAGES:]}
        sends = [{"cid": s["cid"], "thread": s["thread"], "status": s["status"], "error": s["error"]}
                 for s in self.sends.values()]
        status = self.update_status
        return {
            "v": 1,
            "setup": self.setup_state(),
            "phone": self.phone_view(),
            "priority": {"mode": self.settings.get("priority"), "frame_active": self.frame_active(), "worn": self.worn},
            "calls": self.calls_view(),
            "notifications": notifs,
            "threads": threads,
            "open": opened,
            "sends": sends,
            "apps": self.apps_view(),
            "settings": {k: self.settings.get(k) for k in ("priority", "alerts", "preview", "auto_update")},
            "update": {"status": status, "versions": self.installed_versions()},
            "unread": sum(int(t["unread"] or 0) for t in threads),
        }

    def state_json(self):
        return json.dumps(self.view_state(), separators=(",", ":"), ensure_ascii=True)

    def dirty(self):
        if self.push_timer is None and not self.stopping:
            self.push_timer = self.loop.call_later(self.push_delay, self.push_all)

    def push_all(self):
        self.push_timer = None
        if not self.shell.pages:
            return
        text = self.state_json()
        for page in list(self.shell.pages.values()):
            self.shell.push(page, text)

    def push_to(self, page):
        self.shell.push(page, self.state_json())


# ---------------------------------------------------------------------------

def setup_logging():
    level = {"debug": logging.DEBUG, "info": logging.INFO}.get(os.environ.get("EARSHOT_LOG", "").lower(), logging.WARNING)
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)


def teardown_pages(cdp_base=CDP_BASE):
    """Remove the menu from Steam's pages (uninstall.sh runs this with --teardown)."""
    try:
        targets = http_get_json(cdp_base + "/json/list")
    except Exception:
        return
    for t in targets if isinstance(targets, list) else []:
        if not (isinstance(t, dict) and isinstance(t.get("url"), str) and t.get("webSocketDebuggerUrl")
                and any(m in t["url"] for m in PAGE_MARKS)):
            continue
        try:
            ws = WsClient(t["webSocketDebuggerUrl"])
            try:
                ws.send_text(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": "window.__earshotMenu&&window.__earshotMenu.teardown()"}}))
            finally:
                ws.close()
        except Exception:
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    setup_logging()
    if argv == ["--teardown"]:
        teardown_pages()
        return 0
    helper = Helper()

    def stop(signum, frame):
        helper.loop.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    helper.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
