#!/usr/bin/env python3
"""Earshot helper: iPhone calls, texts and notifications in Steam's VR menus.

Bridges Tether's local socket (docs/PROTOCOL.md) and Steam's own UI:

  tetherd --(UNIX socket, JSON lines)--> this helper --(DevTools, 127.0.0.1:8080)--> earshot-menu.js
                                              |  state goes into the pages with Runtime.evaluate,
                                              |  actions come back through a Runtime binding
                                              +--> SteamVR notifications (OpenVR, vrcmd --notify as the fallback)

Nothing listens on any port. One thread runs a selector loop, so an idle helper
sleeps until the next timer (a few seconds apart) or a socket event. External
commands (vrcmd, busctl, systemd-run) run on one worker thread; OpenVR pop-ups on another.

Logs are warning level by default and never contain message, notification or
caller text. Set EARSHOT_LOG=info or debug for more detail (still no text).

Python 3 standard library only.
"""

import base64
import collections
import ctypes
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
# The bar and barpopup.* pages, as Tragus finds them: a real page (not a frame or worker) whose
# vrOverlayKey query value starts with this. Anything else that merely mentions it is not ours to inject into.
PAGE_KEY_PREFIX = "valve.steam.gamepadui.bar"


def is_bar_page(target):
    if not (isinstance(target, dict) and target.get("type") == "page" and target.get("webSocketDebuggerUrl")
            and isinstance(target.get("url"), str)):
        return False
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(target["url"]).query)
    except ValueError:
        return False
    return any(v.startswith(PAGE_KEY_PREFIX) for v in query.get("vrOverlayKey", []))


def vrcmd_text(text):
    """What goes after vrcmd --notify. A text that begins with a dash would be read as another option, so
    it is always preceded by a letter-free marker that cannot be: the first character is never "-"."""
    text = str(text)
    return "\u200b" + text if text.startswith("-") else text
MENU_FILE = os.path.join(HERE, "earshot-menu.js")
BINDING = "earshotAction"
VRCMD = "/opt/steamvr/bin/linuxarm64/vrcmd"
HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"  # what the iPhone advertises; BlueZ matches the full UUID
BREDR_BEARER = "org.bluez.Bearer.BREDR1"
HFP_VERIFY_SECONDS = 45.0   # how long to wait for the call link to come back after a restore
AUTO_UPDATE_DEFER = 5 * 60  # try an automatic update again this soon after a call ends
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
# Pop-ups from Earshot's own SteamVR notification source (OpenVR), vrcmd as the fallback
# ---------------------------------------------------------------------------
#
# vrcmd --notify creates its own overlay ("testing notifications"), and SteamVR titles every pop-up with the
# overlay's name. Calling the flat OpenVR API ourselves lets the pop-up say "Earshot". Only what is needed is
# declared: the first entries of two function tables (openvr_capi.h, Valve's openvr repository). The first two
# entries of VR_IVROverlay_FnTable (FindOverlay, CreateOverlay) are the same in every version from
# IVROverlay_018 to _028, and VR_IVRNotifications_FnTable (CreateNotification, RemoveNotification) is the same
# in IVRNotifications_002, the only version since 1.0.

OPENVR_LIBS = ("/opt/steamvr/bin/linuxarm64/libopenvr_api.so", "/opt/steamvr/bin/linux64/libopenvr_api.so")
OVERLAY_VERSIONS = tuple("IVROverlay_%03d" % n for n in range(28, 18, -1))   # newest first
NOTIFICATIONS_VERSIONS = ("IVRNotifications_002",)
OVERLAY_KEY = "earshot.notifications"
OVERLAY_NAME = "Earshot"
VR_APPLICATION_BACKGROUND = 3          # EVRApplicationType: never starts SteamVR, fails if it is not running
VR_NOTIFICATION_TRANSIENT = 0          # EVRNotificationType: hides itself after the time the person set in SteamVR
VR_NOTIFICATION_STYLE_APPLICATION = 100
VR_OVERLAY_KEY_IN_USE = 17             # EVROverlayError


class _OverlayTable(ctypes.Structure):
    _fields_ = [
        ("FindOverlay", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint64))),
        ("CreateOverlay", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p,
                                           ctypes.POINTER(ctypes.c_uint64))),
    ]


class _NotificationsTable(ctypes.Structure):
    _fields_ = [
        ("CreateNotification", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int,
                                                ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_uint32))),
        ("RemoveNotification", ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint32)),
    ]


class OpenVrError(Exception):
    """Why a pop-up could not go through OpenVR. The message never contains notification text."""


class OpenVrNotifier:
    """Shows pop-ups through libopenvr_api.so. notify() blocks, so it runs on a worker thread (see Popups).

    One background-application session is kept between pop-ups and dropped after a failure (SteamVR may have
    restarted) or after idle_close seconds without one, so this process never holds SteamVR's shutdown up
    while it is not polling for events. The next pop-up opens a fresh session."""

    def __init__(self, paths=OPENVR_LIBS, loader=None, clock=time.monotonic, idle_close=30.0):
        self.paths = tuple(paths)
        self.loader = loader or ctypes.CDLL
        self.clock = clock
        self.idle_close = idle_close
        self.lock = threading.Lock()
        self.lib = None
        self.inited = False
        self.overlay = None
        self.notifications = None
        self.handle = None
        self.last_used = 0.0
        self.reported = None   # the last failure reason that was logged, so a steady failure logs once

    # ----- the library -----

    def load(self):
        if self.lib is not None:
            return self.lib
        for path in self.paths:
            try:
                lib = self.loader(path)
                self.bind(lib, "VR_InitInternal", ctypes.c_void_p, [ctypes.POINTER(ctypes.c_int), ctypes.c_int])
                self.bind(lib, "VR_ShutdownInternal", None, [])
                self.bind(lib, "VR_GetGenericInterface", ctypes.c_void_p,
                          [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)])
            except (OSError, AttributeError):
                continue
            self.lib = lib
            return lib
        raise OpenVrError("library not found")

    @staticmethod
    def bind(lib, name, restype, argtypes):
        fn = getattr(lib, name)
        try:
            fn.restype = restype
            fn.argtypes = argtypes
        except (AttributeError, TypeError):
            pass   # a stand-in that is not a C function pointer (tests)

    # ----- a session -----

    def interface(self, versions, table):
        for version in versions:
            error = ctypes.c_int(0)
            address = self.lib.VR_GetGenericInterface(("FnTable:" + version).encode("ascii"), ctypes.byref(error))
            if address and error.value == 0:
                log.debug("openvr interface %s", version)
                return ctypes.cast(address, ctypes.POINTER(table)).contents
        raise OpenVrError("no usable %s" % versions[0].split("_")[0])

    def open(self):
        self.load()
        error = ctypes.c_int(0)
        self.lib.VR_InitInternal(ctypes.byref(error), VR_APPLICATION_BACKGROUND)
        if error.value != 0:
            raise OpenVrError("init error %d" % error.value)   # SteamVR not running is code 121
        self.inited = True
        self.overlay = self.interface(OVERLAY_VERSIONS, _OverlayTable)
        self.notifications = self.interface(NOTIFICATIONS_VERSIONS, _NotificationsTable)
        handle = ctypes.c_uint64(0)
        code = self.overlay.CreateOverlay(OVERLAY_KEY.encode(), OVERLAY_NAME.encode(), ctypes.byref(handle))
        if code == VR_OVERLAY_KEY_IN_USE:
            code = self.overlay.FindOverlay(OVERLAY_KEY.encode(), ctypes.byref(handle))
        if code != 0:
            raise OpenVrError("overlay error %d" % code)
        self.handle = handle.value

    def drop(self):
        if self.inited:
            try:
                self.lib.VR_ShutdownInternal()
            except Exception as error:
                log.debug("openvr shutdown failed: %s", type(error).__name__)
        self.inited = False
        self.overlay = self.notifications = self.handle = None

    def post(self, text):
        data = str(text).replace("\0", "").encode("utf-8", "replace")
        notification_id = ctypes.c_uint32(0)
        code = self.notifications.CreateNotification(
            self.handle, 0, VR_NOTIFICATION_TRANSIENT, data, VR_NOTIFICATION_STYLE_APPLICATION, None,
            ctypes.byref(notification_id))
        if code != 0:
            raise OpenVrError("notification error %d" % code)

    # ----- what Popups calls -----

    def notify(self, text):
        """Shows one pop-up and returns True, or raises. A session that has gone bad is dropped and replaced once."""
        with self.lock:
            for _ in (0, 1):
                reused = self.handle is not None
                try:
                    if not reused:
                        self.open()
                    self.post(text)
                except Exception as error:
                    self.drop()
                    if reused:
                        continue            # probably a restarted SteamVR: one fresh session, then give up
                    self.report(error)
                    raise
                self.last_used = self.clock()
                if self.reported is not None:
                    log.warning("SteamVR notifications are back (OpenVR)")
                    self.reported = None
                return True

    def report(self, error):
        reason = str(error) if isinstance(error, OpenVrError) else type(error).__name__
        if reason != self.reported:
            self.reported = reason
            log.warning("OpenVR pop-ups unavailable (%s); using vrcmd", reason)

    def idle_tick(self):
        if self.lock.acquire(blocking=False):
            try:
                if self.handle is not None and self.clock() - self.last_used >= self.idle_close:
                    self.drop()
            finally:
                self.lock.release()

    def close(self):
        if self.lock.acquire(timeout=2.0):
            try:
                self.drop()
            finally:
                self.lock.release()


class Popups:
    """Sends each pop-up through backend.notify(text) on a worker thread of its own (an OpenVR call can block).
    When that raises or returns false, failed(text) runs on the loop thread: the vrcmd fallback. A worker that is
    stuck in a call for stuck_after seconds is bypassed, so pop-ups never stop. inline=True runs on the caller (tests)."""

    def __init__(self, loop, backend, failed, inline=False, stuck_after=15.0, idle_poll=5.0, clock=time.monotonic):
        self.loop = loop
        self.backend = backend
        self.failed = failed
        self.inline = inline
        self.stuck_after = stuck_after
        self.idle_poll = idle_poll
        self.clock = clock
        self.jobs = queue.Queue()
        self.redo = collections.deque()
        self.thread = None
        self.busy_since = None
        self.closed = False
        loop.wake_handlers.append(self.drain)

    def attempt(self, text):
        try:
            return bool(self.backend.notify(text))
        except Exception as error:
            log.debug("pop-up backend failed: %s", type(error).__name__)
            return False

    def send(self, text):
        if self.closed:
            return
        if self.inline:
            if not self.attempt(text):
                self.failed(text)
            return
        busy = self.busy_since
        if busy is not None and self.clock() - busy > self.stuck_after:
            self.failed(text)
            return
        if self.thread is None:
            self.thread = threading.Thread(target=self.work, name="earshot-popup", daemon=True)
            self.thread.start()
        self.jobs.put(text)

    def work(self):
        while True:
            try:
                text = self.jobs.get(timeout=self.idle_poll)
            except queue.Empty:
                try:
                    self.backend.idle_tick()
                except Exception as error:
                    log.debug("pop-up idle failed: %s", type(error).__name__)
                continue
            if text is None:
                return
            self.busy_since = self.clock()
            ok = self.attempt(text)
            self.busy_since = None
            if not ok:
                self.redo.append(text)
                self.loop.wake()

    def drain(self):
        while self.redo:
            text = self.redo.popleft()
            if not self.closed:
                self.failed(text)

    def close(self):
        self.closed = True
        if self.thread:
            self.jobs.put(None)
            self.thread.join(2.0)
        try:
            self.backend.close()
        except Exception as error:
            log.debug("pop-up backend close failed: %s", type(error).__name__)


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
            if is_bar_page(t):
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
# Pairing: the whole "Pair iPhone" flow lives here, in the helper, so it survives the
# menu closing or Steam reloading its pages. The page only draws what view() returns and
# sends small actions back; every action is checked here.
#
# Primary route (no root, stock BlueZ): the headset becomes visible and waits, and the
# user taps it on the iPhone (bt_pair_listen). A phone-initiated bond is what carries the
# LE half that notifications need. Secondary route: scan and pick (bt_scan + bt_pair).
#
# Command contract assumed of the daemon for the primary route:
#   -> {"command":"bt_pair_listen","operation_id":ID,"seconds":120}
#      seconds 0 means "stop being visible" (optional; a daemon may ignore it)
#   <- bt_pair_progress / bt_pair_confirm_request / bt_pair_result, all carrying ID
# ---------------------------------------------------------------------------

MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

PAIR_WORDS = {
    "discovering": "Looking for your iPhone…",
    "rediscovering": "Looking for your iPhone again…",
    "already_paired": "Already paired. Checking the connection…",
    "connecting": "Connecting to {name}…",
    "pairing": "Asking {name} to pair…",
    "confirm": "Waiting for you to check the code…",
    "confirmed": "Code confirmed. Waiting for the iPhone…",
    "declined": "The code was not confirmed.",
    "unanswered": "The code was not confirmed.",
    "retrying": "The iPhone did not answer. Trying once more…",
    "error": "That try did not work. Checking what is next…",
    "paired": "Paired. Setting things up…",
    "settling": "Setting things up…",
    "soliciting": "Almost there. Asking the iPhone to show its switches…",
}

FAILURES = {
    "stale": ("The iPhone remembers an old pairing",
              "On your iPhone, open the entry for this headset under My Devices and tap Forget This Device. "
              "If you see it twice, forget both. Then tap Try again. Earshot has already cleared its own side.",
              "Try again"),
    "declined_here": ("The code was not confirmed",
                      "Tap Try again. When the code appears, check it matches the iPhone, tap Pair on the iPhone, "
                      "then tap Confirm here.", "Try again"),
    "declined_phone": ("The iPhone said no",
                       "Tap Try again, then tap Pair on the iPhone when it asks.", "Try again"),
    "timeout": ("The iPhone did not finish pairing in time",
                "Keep the iPhone unlocked on Settings > Bluetooth, then tap Try again.", "Try again"),
    "not_found": ("Earshot cannot see your iPhone",
                  "Unlock it and keep Settings > Bluetooth open, then search again.", "Search again"),
    "busy": ("Another pairing is still finishing", "Wait a moment, then tap Try again.", "Try again"),
    "agent": ("Bluetooth is busy with another program",
              "Turn Bluetooth off and on in the headset settings, then tap Try again.", "Try again"),
    "unavailable": ("Bluetooth is not available",
                    "Check that Bluetooth is on in the headset settings, then tap Try again.", "Try again"),
    "engine_lost": ("Earshot lost its link to Bluetooth", "Wait a few seconds, then tap Try again.", "Try again"),
    "stalled": ("No answer from Bluetooth", "Tap Try again.", "Try again"),
    "other": ("Pairing did not work this time",
              "Tap Try again. If it keeps failing, restart the headset.", "Try again"),
}


def classify_pair_failure(status, message):
    """Map the engine's status and message onto one of FAILURES. The raw text is never shown."""
    text = str(message or "").lower()
    status = str(status or "")
    if status == "busy":
        return "busy"
    if status == "not_found":
        return "not_found"
    if ("key-missing" in text or "key missing" in text or "authenticationfailed" in text
            or "refused the connection before pairing started" in text):
        return "stale"
    if status == "rejected":
        return "declined_here" if "not confirmed on this computer" in text else "declined_phone"
    if "authenticationrejected" in text or "authenticationcanceled" in text:
        return "declined_phone"
    if status == "timeout":
        return "timeout"
    if "register a bluetooth pairing agent" in text:
        return "agent"
    if "unavailable" in text:
        return "unavailable"
    return "other"


class Pairing:
    LISTEN_SECONDS = 120
    SCAN_TRIES = 3            # automatic searches before asking the user to search again
    SCAN_STALL = 45.0         # no scan result for this long: stop saying "Searching"
    LISTEN_DETECT = 8.0       # a daemon that knows bt_pair_listen reacts within this long
    BUSY_TRIES = 6
    BUSY_DELAY = 5.0
    STALL = 240.0             # a pairing with no event this long is given up on
    GIVE_UP_AFTER = 90.0      # "Close" appears on a pairing screen after this long
    UNPAIR_WAIT = 15.0
    MAX_DEVICES = 12

    def __init__(self, helper):
        self.h = helper
        self.s = None
        self.listen_ok = None     # None unknown, True the daemon has bt_pair_listen, False it has not
        self.counter = itertools.count(1)
        self.timers = {}

    # ----- plumbing -----

    def clock(self):
        return self.h.loop.clock()

    def new_op(self, kind):
        return "earshot-%s-%d-%s" % (kind, next(self.counter), os.urandom(3).hex())

    def timer(self, name, delay, fn):
        self.cancel_timer(name)
        self.timers[name] = self.h.loop.call_later(delay, lambda: self.fire(name, fn))

    def cancel_timer(self, name):
        self.h.loop.cancel(self.timers.pop(name, None))

    def fire(self, name, fn):
        self.timers.pop(name, None)
        fn()
        self.h.dirty()

    def clear(self):
        for name in list(self.timers):
            self.cancel_timer(name)
        self.s = None

    def engine_reset(self, up):
        """The engine connection came or went. A new connection forgets what it supported."""
        self.listen_ok = None
        if up:
            return
        s = self.s
        if s and s["phase"] in ("pairing", "confirm", "listen", "search", "forgetting"):
            if s["phase"] == "forgetting":
                s["mode"] = "forget"
            self.fail("engine_lost")

    def on_protocol_info(self):
        if "bluetooth.pair_listen" in self.h.caps:
            self.listen_ok = True

    def headset_name(self):
        adapters = self.h.bt_status.get("adapters")
        cap = self.h.bt_status.get("capability") if isinstance(self.h.bt_status.get("capability"), dict) else {}
        wanted = str(cap.get("adapter_id") or "")
        chosen = None
        for a in adapters if isinstance(adapters, list) else []:
            if isinstance(a, dict) and a.get("name"):
                if chosen is None or (wanted and str(a.get("path", "")).endswith("/" + wanted)):
                    chosen = a
        return trim(chosen["name"], 40) if chosen else "this headset"

    def blockers(self):
        """Why pairing cannot start at all. The root-only extras (class, experimental mode) are not needed."""
        h = self.h
        if not h.engine_up:
            return ["The Earshot engine is not running. Wait a moment, or restart the headset."]
        if not h.bt_status:
            return ["Checking Bluetooth…"]
        cap = h.bt_status.get("capability") if isinstance(h.bt_status.get("capability"), dict) else None
        if not h.bt_status.get("available") or cap is None:
            return ["The Bluetooth service is not available."]
        if not cap.get("adapter_present", True):
            return ["No Bluetooth adapter was found."]
        if cap.get("powered") is False:
            return ["Bluetooth is turned off. Turn it on in the headset settings."]
        return []

    def recheck(self):
        """Bluetooth became ready while the setup screen was up."""
        s = self.s
        if s and s["phase"] == "blocked" and not self.blockers():
            self.begin_primary()

    # ----- entering and leaving -----

    def start(self):
        if self.s and self.s["phase"] in ("pairing", "confirm", "forgetting", "listen", "search"):
            return
        self.clear()
        self.s = {"phase": "blocked", "last": self.clock(), "mode": "listen", "busy_tries": 0}
        if not self.blockers():
            self.begin_primary()

    def begin_primary(self):
        if self.listen_ok is False:
            self.to_search(fallback=True)
        else:
            self.start_listen()

    def to_search(self, fallback=False):
        for name in ("detect", "listen_end", "stall"):
            self.cancel_timer(name)
        self.s.update(phase="search", mode="pick", searching=False, searched=False, tries=0, message="",
                      fallback=fallback, op=None, cancelled=False)

    def keep(self, **extra):
        self.s.update(extra)
        self.s["last"] = self.clock()

    def close(self):
        s = self.s
        if not s:
            return
        if s["phase"] == "confirm":
            self.confirm(s.get("op"), False)
            s["cancelled"] = True
            return
        if s["phase"] == "pairing" and self.clock() - s["last"] < self.GIVE_UP_AFTER:
            return
        if s["phase"] == "forgetting":
            return
        self.stop_listening()
        self.clear()

    def stop_listening(self):
        s = self.s
        if s and s["phase"] == "listen" and s.get("op") and not s.get("expired"):
            self.h.request("bt_pair_listen", operation_id=s["op"], seconds=0)

    def done(self):
        if self.s and self.s["phase"] in ("almost", "forgotten"):
            self.clear()

    # ----- primary route: be visible, the phone taps us -----

    def start_listen(self):
        s = self.s
        op = self.new_op("listen")
        s.update(phase="listen", mode="listen", op=op, expired=False, heard=False, cancelled=False, sent=False,
                 ends_at=time.time() + self.LISTEN_SECONDS, progress="", name="")
        self.keep()
        if not self.h.request("bt_pair_listen", operation_id=op, seconds=self.LISTEN_SECONDS):
            self.fail("engine_lost")
            return
        if self.listen_ok is not True:
            self.timer("detect", self.LISTEN_DETECT, self.listen_unsupported)
        self.timer("listen_end", self.LISTEN_SECONDS + 5, self.listen_expired)

    def listen_unsupported(self):
        s = self.s
        if s and s["phase"] == "listen" and not s.get("heard"):
            self.listen_ok = False
            self.to_search(fallback=True)

    def listen_expired(self):
        s = self.s
        if s and s["phase"] == "listen":
            s["expired"] = True

    def listen_again(self):
        s = self.s
        if s and s["phase"] == "listen" and (s.get("expired") or time.time() >= s.get("ends_at", 0)):
            self.start_listen()

    def use_listen(self):
        """From the search screen back to waiting for the iPhone."""
        s = self.s
        if s and s["phase"] == "search" and self.listen_ok is not False and not self.blockers():
            self.cancel_timer("scan")
            self.start_listen()

    def use_search(self):
        """'Pair a different way'."""
        s = self.s
        if s and s["phase"] == "listen":
            self.stop_listening()
            self.to_search()

    # ----- secondary route: search, pick -----

    def search(self):
        s = self.s
        if not s or s["phase"] != "search" or self.blockers():
            return
        s.update(searching=True, searched=True, tries=1, message="")
        self.send_scan()

    def send_scan(self):
        if not self.h.request("bt_scan"):
            self.fail("engine_lost")
            return
        self.timer("scan", self.SCAN_STALL, self.scan_stalled)

    def scan_stalled(self):
        s = self.s
        if s and s["phase"] == "search":
            s["searching"] = False

    def on_scan_result(self, msg):
        s = self.s
        if not s or s["phase"] != "search" or not s.get("searching"):
            return
        self.cancel_timer("scan")
        if not msg.get("success"):
            if "already" in str(msg.get("message", "")).lower() and s["tries"] < 6:
                s["tries"] += 1
                self.timer("scan", 5.0, self.send_scan)
                return
            s.update(searching=False, message="Bluetooth could not search just now. Tap Search again.")
            return
        if not self.candidates() and s["tries"] < self.SCAN_TRIES:
            s["tries"] += 1
            self.send_scan()
            return
        s["searching"] = False
        if not self.candidates():
            s["message"] = ("Nothing found yet. Make sure the iPhone is showing Settings > Bluetooth, "
                            "then search again.")

    def candidates(self):
        out = []
        for d in self.h.devices:
            addr = str(d.get("address") or "")
            if not MAC.match(addr) or d.get("airpods"):
                continue
            phone = bool(d.get("apple_nearby") or d.get("iphone"))
            paired = bool(d.get("paired") or d.get("bonded"))
            if (paired and not phone) or (not paired and not phone and not d.get("name")):
                continue
            out.append({"address": addr.upper(),
                        "name": trim(d.get("name"), 40) or ("iPhone" if phone else "Unnamed device"),
                        "iphone": phone, "redo": paired})
        out.sort(key=lambda c: (not c["iphone"], c["redo"], c["name"].lower()))
        phones = [c for c in out if c["iphone"]]
        return (phones + [c for c in out if not c["iphone"]][:6])[:self.MAX_DEVICES]

    def pick(self, address):
        s = self.s
        if not s or s["phase"] != "search" or not isinstance(address, str) or not MAC.match(address):
            return
        chosen = next((c for c in self.candidates() if c["address"] == address.upper()), None)
        if chosen is None:
            return
        self.cancel_timer("scan")
        s["searching"] = False
        self.begin_pick(chosen["address"], chosen["name"], unpair_first=chosen["redo"])

    def begin_pick(self, address, name, unpair_first=False):
        s = self.s
        s.update(phase="pairing", mode="pick", address=address, name=name, progress="Connecting to %s…" % name,
                 code="", cancelled=False, sent=False, after_unpair=False, op=self.new_op("pair"))
        self.keep()
        self.timer("stall", self.STALL, lambda: self.fail("stalled"))
        if unpair_first and not s.get("unpairing"):
            s["unpairing"] = self.new_op("unpair")
            self.h.request("bt_unpair", address=address, operation_id=s["unpairing"])
        if s.get("unpairing"):
            s.update(after_unpair=True, progress="Clearing the old pairing first…")
            self.timer("unpair_wait", self.UNPAIR_WAIT, self.send_pair)
        else:
            self.send_pair()

    def send_pair(self):
        s = self.s
        if not s or s["phase"] != "pairing" or s.get("sent") or s["mode"] != "pick":
            return
        s.update(sent=True, after_unpair=False)
        self.cancel_timer("unpair_wait")
        if not self.h.request("bt_pair", address=s["address"], operation_id=s["op"]):
            self.fail("engine_lost")

    # ----- engine events -----

    def mine(self, msg):
        s = self.s
        return bool(s and s.get("op") and msg.get("operation_id") == s["op"])

    def heard(self):
        self.cancel_timer("detect")
        self.listen_ok = True
        self.s["heard"] = True

    def on_progress(self, msg):
        if not self.mine(msg):
            return
        s = self.s
        self.heard()
        step, detail = str(msg.get("step") or ""), msg.get("detail")
        if s["phase"] == "listen":
            if step not in ("connecting", "pairing", "confirm", "confirmed", "paired", "settling", "soliciting",
                            "already_paired"):
                self.keep()          # "listening" and anything else: still waiting for the phone
                return
            if step == "connecting" and isinstance(detail, str) and detail:
                s["name"] = trim(detail, 40)
            s.update(phase="pairing", progress="Your iPhone is connecting…")
            self.timer("stall", self.STALL, lambda: self.fail("stalled"))
        if s["phase"] != "pairing":
            return
        template = PAIR_WORDS.get(step)
        if step == "retrying" and isinstance(detail, str) and "directly" in detail:
            template = "Trying a different way. Notifications may not work with this one."
        if template:
            s["progress"] = template.replace("{name}", s.get("name") or "your iPhone")
        self.keep()

    def on_confirm_request(self, msg):
        s = self.s
        if not self.mine(msg) or s["phase"] not in ("pairing", "listen"):
            return
        self.heard()
        digits = re.sub(r"[^0-9]", "", str(msg.get("code") or ""))[:8]
        s.update(phase="confirm", code=digits or trim(msg.get("code"), 12), progress="")
        self.keep()
        self.timer("stall", self.STALL, lambda: self.fail("stalled"))
        # The code itself goes in the pop-up: it is what the person has to compare with the iPhone.
        self.h.notify_direct("iPhone pairing code %s - confirm it in the Phone tab" % s["code"])

    def confirm(self, op, accept):
        s = self.s
        if not s or s["phase"] != "confirm" or not isinstance(accept, bool) or op != s.get("op"):
            return
        self.h.request("bt_pair_confirm", operation_id=s["op"], accept=accept)
        s.update(phase="pairing", progress="Code confirmed. Waiting for the iPhone…" if accept else "Cancelling…")
        self.keep()

    def on_result(self, msg):
        s = self.s
        if not self.mine(msg):
            return
        self.cancel_timer("stall")
        self.heard()
        status, message = str(msg.get("status") or ""), msg.get("message")
        address = msg.get("address")
        if isinstance(address, str) and MAC.match(address):
            s["address"] = address.upper()
        if msg.get("success"):
            self.cancel_timer("listen_end")
            self.h.request("bt_list_devices")
            s.update(phase="almost" if msg.get("dual_bond") else "weak", weak=not msg.get("dual_bond"))
            return
        if s["phase"] == "listen" and status in ("timeout", "cancelled"):
            s["expired"] = True      # nobody tapped the headset in time: not a failure
            return
        if s.get("cancelled"):
            self.back_after_cancel()
            return
        kind = classify_pair_failure(status, message)
        if kind == "busy" and s["busy_tries"] < self.BUSY_TRIES:
            s["busy_tries"] += 1
            s.update(phase="pairing", progress="Another pairing is still finishing. Waiting a moment…")
            self.timer("busy", self.BUSY_DELAY, self.retry_after_busy)
            return
        self.fail(kind)

    def retry_after_busy(self):
        s = self.s
        if not s or s["phase"] != "pairing":
            return
        if s["mode"] == "listen":
            self.start_listen()
        else:
            self.begin_pick(s["address"], s.get("name") or "your iPhone")

    def back_after_cancel(self):
        s = self.s
        if s["mode"] == "listen":
            self.clear()
        else:
            s.update(phase="search", searching=False, op=None, cancelled=False)

    def fail(self, kind):
        s = self.s
        if not s:
            return
        for name in ("stall", "detect", "busy", "listen_end", "scan", "unpair_wait"):
            self.cancel_timer(name)
        s.update(phase="failed", kind=kind)
        self.keep()
        if kind == "stale" and s.get("address") and not s.get("unpairing"):
            # Clear our side now, so Try again starts clean once the iPhone has forgotten too.
            s["unpairing"] = self.new_op("unpair")
            self.h.request("bt_unpair", address=s["address"], operation_id=s["unpairing"])

    def on_unpair_result(self, msg):
        s = self.s
        op = msg.get("operation_id")
        if not s or not op or op != s.get("unpairing"):
            return
        s["unpairing"] = None
        self.h.request("bt_list_devices")
        if s["phase"] == "forgetting":
            self.cancel_timer("unpair_wait")
            if msg.get("success") or msg.get("status") == "not_found":
                self.h.request("bt_set_device", address="")
                s["phase"] = "forgotten"
            else:
                s.update(phase="failed", kind="other", mode="forget")
        elif s["phase"] == "pairing" and s.get("after_unpair"):
            self.send_pair()

    # ----- actions on result screens -----

    def retry(self):
        s = self.s
        if not s:
            return
        if s["phase"] == "failed":
            if s.get("mode") == "forget":
                self.forget_ask()
                return
            s["busy_tries"] = 0
            if s.get("kind") == "not_found" or (s["mode"] == "pick" and not s.get("address")):
                self.to_search()
                self.search()
            elif s["mode"] == "listen":
                self.start_listen()
            else:
                self.begin_pick(s["address"], s.get("name") or "your iPhone")
        elif s["phase"] == "weak":
            s["busy_tries"] = 0
            address = s.get("address")
            if address and s["mode"] == "pick":
                self.begin_pick(address, s.get("name") or "your iPhone", unpair_first=True)
            else:
                if address and not s.get("unpairing"):
                    s["unpairing"] = self.new_op("unpair")
                    self.h.request("bt_unpair", address=address, operation_id=s["unpairing"])
                self.start_listen()

    def keep_weak(self):
        if self.s and self.s["phase"] == "weak":
            self.s["phase"] = "almost"

    # ----- forget -----

    def forget_ask(self):
        if self.s and self.s["phase"] in ("pairing", "confirm", "forgetting"):
            return
        if not self.h.phone_paired():
            return
        self.clear()
        self.s = {"phase": "forget", "last": self.clock(), "mode": "forget", "busy_tries": 0}

    def forget_do(self):
        s = self.s
        address = self.h.bt_status.get("device_address")
        if not s or s["phase"] != "forget" or not isinstance(address, str) or not MAC.match(address):
            return
        s.update(phase="forgetting", address=address.upper(), unpairing=self.new_op("unpair"))
        if not self.h.request("bt_unpair", address=address, operation_id=s["unpairing"]):
            s.update(phase="failed", kind="engine_lost")
            return
        self.timer("unpair_wait", 20.0, lambda: s.update(phase="failed", kind="stalled", mode="forget"))

    # ----- what the page draws -----

    def map_on_offer(self):
        """Message Access can work here: it is open, or the iPhone refused it (so the switch exists on the phone).
        Missing obexd, a headset the iPhone does not treat as a car kit, or no answer at all mean it cannot."""
        c = self.h.conn
        return bool(c.get("map_open")) or c.get("map_error") in ("forbidden", "busy")

    def checklist(self):
        c, weak = self.h.conn, bool(self.s.get("weak"))

        def perm(is_open, error, waiting="Waiting for the iPhone…"):
            if is_open:
                return "on", "Working."
            if error == "forbidden":
                return "off", "Still off on the iPhone."
            if error == "busy":
                return "off", "Another device is using this. Turn it off there first."
            return "waiting", waiting

        n_state, n_note = perm(c.get("ancs_ready"), "",
                               "On the iPhone: Settings > Bluetooth > (i) next to %s > Share System Notifications."
                               % self.headset_name())
        if weak and n_state != "on":
            n_state, n_note = "off", "This pairing cannot carry notifications."
        notifications = {"id": "notifications", "label": "Share System Notifications", "state": n_state,
                         "note": n_note, "essential": not weak}
        # The iPhone only shows its Messages, Contacts, Favorites and Recents switches to a device it treats as a
        # car kit. When Message Access is not on offer at all, listing those switches sends people looking for
        # settings that are not there: say plainly that replies are not available instead.
        if not self.map_on_offer():
            return [notifications,
                    {"id": "messages", "label": "Replying to texts", "state": "off",
                     "note": "Not available on this headset yet. Texts still show up as notifications.",
                     "essential": False}]
        m_state, m_note = perm(c.get("map_open"), c.get("map_error"), "Needs Show Notifications and Sync Contacts.")
        p_state, p_note = perm(c.get("pbap_open"), c.get("pbap_error"))
        recents = bool(self.h.call_history)
        return [
            notifications,
            {"id": "messages", "label": "Text messages", "state": m_state, "note": m_note, "essential": True},
            {"id": "contacts", "label": "Sync Contacts", "state": p_state, "note": p_note, "essential": False},
            {"id": "favorites", "label": "Phone Favorites", "state": "unknown",
             "note": "Turn it on. Earshot cannot check this one.", "essential": False},
            {"id": "recents", "label": "Phone Recents", "state": "on" if recents else "unknown",
             "note": "Working." if recents else "Turn it on. Earshot cannot check this one.", "essential": False},
        ]

    def view(self):
        s = self.s
        if not s:
            return None
        phase = s["phase"]
        v = {"phase": phase, "headset": self.headset_name()}
        if phase == "blocked":
            v["reasons"] = self.blockers()
        elif phase == "listen":
            v.update(ends_at=int(s["ends_at"]), expired=bool(s.get("expired")), seconds=self.LISTEN_SECONDS)
        elif phase == "search":
            v.update(searching=bool(s["searching"]), searched=bool(s["searched"]), message=s.get("message", ""),
                     devices=self.candidates() if s["searched"] else [], fallback=bool(s.get("fallback")),
                     can_listen=self.listen_ok is not False)
        elif phase == "pairing":
            v.update(name=s.get("name") or "", progress=s.get("progress", ""),
                     can_close=self.clock() - s["last"] >= self.GIVE_UP_AFTER)
        elif phase == "confirm":
            v.update(name=s.get("name") or "", code=s.get("code", ""), op=s["op"])
        elif phase == "failed":
            title, text, button = FAILURES.get(s.get("kind"), FAILURES["other"])
            v.update(kind=s.get("kind"), title=title, text=text, button=button, forget=s.get("mode") == "forget")
        elif phase == "weak":
            v["name"] = s.get("name") or ""
        elif phase == "almost":
            items = self.checklist()
            v.update(items=items, weak=bool(s.get("weak")),
                     ready=all(i["state"] == "on" for i in items if i["essential"]),
                     connected=bool(self.h.conn.get("classic_connected") or self.h.conn.get("le_connected")),
                     reason=trim(self.h.conn.get("link_reason") or "", 160))
        return v


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

class Helper:
    def __init__(self, loop=None, settings=None, connect_tether=connect_unix, executor=None,
                 menu_file=MENU_FILE, cdp_base=CDP_BASE, http_get=http_get_json, ws_factory=WsClient,
                 vrcmd=VRCMD, notifier=None, updater=UPDATER, status_file=UPDATE_STATUS_FILE, versions_file=VERSIONS_FILE,
                 settings_path=None, scan=scan_processes, burst_window=8.0, push_delay=0.15,
                 tether_backoff=(1.0, 30.0), presence_every=10.0, send_timeout=45.0,
                 settle_seconds=5.0, auto_update_first=AUTO_UPDATE_FIRST, clock=None, exists=os.path.exists):
        self.loop = loop or Loop(clock or time.monotonic)
        self.settings = settings or Settings(settings_path or os.path.join(CONFIG_DIR, "settings.json"))
        self.exec = executor or Executor(self.loop)
        self.vrcmd = vrcmd
        self.popups = Popups(self.loop, notifier if notifier is not None else OpenVrNotifier(),
                             self.vrcmd_fallback, inline=getattr(self.exec, "inline", False))
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
        self.hfp_verify_seconds = HFP_VERIFY_SECONDS

        # what the engine told us
        self.engine_up = False
        self.caps = []
        self.bt_status = {}
        self.conn = {}
        self.devices = []
        self.devices_seen = False
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
        self.restore_needs_drop = False
        self.hfp_restore = None   # None, "pending" (asked the phone to reconnect), "unverified" (it did not come back)
        self.hfp_verify = None
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
        self.pairing = Pairing(self)
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
        self.popups.close()
        self.exec.close()

    # ----- Tether side -----

    def on_tether_state(self, up):
        self.engine_up = up
        self.pairing.engine_reset(up)
        if up:
            self.ancs_ready_at = None
            self.map_open_at = None
        else:
            self.conn = {}
            self.bt_status = {}
            self.calls = []
            self.hfp_released = None
            self.set_restore(None)
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
        self.pairing.on_protocol_info()
        # Everything below is a read; ask for each once per connection.
        for command in ("bt_status", "bt_connection", "bt_list_devices", "bt_list_threads",
                        "bt_list_notifications", "bt_list_calls", "bt_list_call_history"):
            self.request(command)
        self.dirty()

    def t_bt_status(self, msg):
        self.bt_status = msg
        self.pairing.recheck()
        self.dirty()

    def t_bt_devices(self, msg):
        devices = msg.get("devices")
        self.devices = [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []
        self.devices_seen = True
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
        if not classic:
            self.restore_needs_drop = False   # the bearer did drop: what comes next is a new link
        if isinstance(calls, dict) and calls.get("available") and self.hfp_restore and not self.restore_needs_drop:
            log.info("hfp: the call link is back")
            self.set_restore(None)
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
        if is_new and not settling and not msg.get("pre_existing") and not msg.get("silent"):
            self.toast_notification(msg)
            log.info("notification %s: popup %s", uid, "shown" if self.can_toast(msg.get("app_id")) else
                     "held (alerts off, headset not active, or app muted)")
        else:
            log.info("notification %s: no popup (%s)", uid, "seen before" if not is_new else
                     "settling" if settling else "already on the phone" if msg.get("pre_existing") else "silent")
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
        if "detect" in self.pairing.timers:   # the daemon answered bt_pair_listen with an error: it does not know it
            self.pairing.listen_unsupported()
            self.dirty()

    # pairing events: the Pairing class owns what they mean
    def t_bt_scan_result(self, msg):
        self.pairing.on_scan_result(msg)
        self.dirty()

    def t_bt_pair_progress(self, msg):
        self.pairing.on_progress(msg)
        self.dirty()

    def t_bt_pair_confirm_request(self, msg):
        self.pairing.on_confirm_request(msg)
        self.dirty()

    def t_bt_pair_result(self, msg):
        self.pairing.on_result(msg)
        self.dirty()

    def t_bt_unpair_result(self, msg):
        self.pairing.on_unpair_result(msg)
        self.dirty()

    def notify_direct(self, text):
        """A SteamVR notification that does not depend on the alert settings (it asks for a person's attention)."""
        self.popups.send(text)

    def phone_paired(self):
        address = str(self.bt_status.get("device_address") or "").lower()
        if not address:
            return False
        for d in self.devices:
            if str(d.get("address", "")).lower() == address:
                return bool(d.get("paired", True) or d.get("bonded", False))
        return not self.devices_seen

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
            method = "RestoreLink"
        else:
            return
        block = self.hfp_block
        if block and block[1] == method and self.loop.clock() < block[0]:
            return  # it just failed; the retry timer tries again
        self.hfp_call(method, path)

    def set_restore(self, state):
        """None, "pending" or "unverified"; pending ends when the call link is seen again."""
        self.loop.cancel(self.hfp_verify)
        self.hfp_verify = None
        if state != self.hfp_restore:
            self.hfp_restore = state
            self.dirty()

    def restore_unverified(self):
        self.hfp_verify = None
        if self.hfp_restore == "pending":
            log.warning("hfp: the call link did not come back after the restore")
            self.set_restore("unverified")

    def hfp_call(self, method, path):
        """Let go of the call link (DisconnectProfile), or take it back. BlueZ does not
        reconnect hands-free on an existing link: ConnectProfile returns success and does
        nothing (docs/BLUETOOTH.md, "Hands-free after a phone-initiated reconnect"). What
        works is dropping the BR/EDR bearer, so the phone reconnects with hands-free. A
        restore is therefore only "pending" until the call link is seen again."""
        self.hfp_busy = True
        self.loop.cancel(self.hfp_retry)
        restore = method == "RestoreLink"

        def done(rc, out, err):
            self.hfp_busy = False
            if rc == 0:
                self.hfp_block = None
                if restore:
                    self.hfp_released = None
                    self.set_restore("pending")
                    # An "available" seen before the bearer drops is the old link, not the restore.
                    calls = self.conn.get("calls")
                    self.restore_needs_drop = bool(isinstance(calls, dict) and calls.get("available"))
                    self.hfp_verify = self.loop.call_later(self.hfp_verify_seconds, self.restore_unverified)
                else:
                    self.hfp_released = path
                    self.set_restore(None)
                log.info("hfp: %s ok", method)
            else:
                log.warning("hfp: %s failed (rc %s)", method, rc)
                self.hfp_block = (self.loop.clock() + self.hfp_retry_after, method)
                self.hfp_retry = self.loop.call_later(self.hfp_retry_after, self.enforce_hfp)

        if restore:
            cmd = ["busctl", "--system", "call", "org.bluez", path, BREDR_BEARER, "Disconnect"]
        else:
            cmd = ["busctl", "--system", "call", "org.bluez", path, "org.bluez.Device1", method, "s", HFP_AG_UUID]
        self.exec.run(cmd, done, timeout=15)

    # ----- SteamVR notifications -----

    def map_open(self):
        return bool(self.conn.get("map_open"))

    def can_toast(self, app_id=None):
        return (self.settings.get("alerts") and self.frame_active()
                and not (app_id and app_id in self.settings.get("muted_apps")))

    def toast_text(self, who, text):
        if not self.settings.get("preview"):
            return trim(who, 60)
        return trim("%s: %s" % (who, text) if text else who, 160)

    def toast_notification(self, n):
        app_id = n.get("app_id") or ""
        # Texts come over MAP when it is open, with the sender and a reply button; without MAP (no obexd, or the
        # iPhone never offered Message Access) the notification is the only way a text reaches the headset.
        if app_id == MAP_APP_ID and self.map_open():
            return
        if n.get("category") == ANCS_CATEGORY_INCOMING_CALL or not self.can_toast(app_id):
            return  # calls get their own, unthrottled toast
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
        self.send_popup("Incoming call: %s" % trim(who, 60) if who and self.settings.get("preview") else "Incoming call")

    def emit_collapsed(self, key, text, noun, who):
        for t in self.collapser.add(key, text, noun, who, self.loop.clock()):
            self.send_popup(t)
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
                self.send_popup(t)
        self.arm_collapse_timer()

    def send_popup(self, text):
        if self.frame_active():
            self.popups.send(text)

    def vrcmd_fallback(self, text):
        """Runs on the loop thread when the OpenVR path could not show a pop-up."""
        if not self.exists(self.vrcmd):
            if not self.vrcmd_missing_logged:
                log.warning("vrcmd not found; SteamVR notifications are off")
                self.vrcmd_missing_logged = True
            return
        self.exec.run([self.vrcmd, "--notify", vrcmd_text(text)], None, timeout=20)

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

    # ----- pairing actions (the page cannot do anything the Pairing class does not allow) -----

    def a_pair_start(self, a):
        self.pairing.start()

    def a_pair_listen_again(self, a):
        self.pairing.listen_again()

    def a_pair_listen(self, a):
        self.pairing.use_listen()

    def a_pair_other(self, a):
        self.pairing.use_search()

    def a_pair_search(self, a):
        self.pairing.search()

    def a_pair_pick(self, a):
        self.pairing.pick(a.get("address"))

    def a_pair_confirm(self, a):
        self.pairing.confirm(a.get("op"), a.get("accept"))

    def a_pair_retry(self, a):
        self.pairing.retry()

    def a_pair_keep(self, a):
        self.pairing.keep_weak()

    def a_pair_close(self, a):
        self.pairing.close()

    def a_pair_done(self, a):
        self.pairing.done()

    def a_forget_ask(self, a):
        self.pairing.forget_ask()

    def a_forget_do(self, a):
        self.pairing.forget_do()

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

    def call_in_progress(self):
        """A call is active, held or ringing (hands-free), or the phone shows an incoming call."""
        if self.calls:
            return True
        return any(n.get("category") == ANCS_CATEGORY_INCOMING_CALL for n in self.notifications.values())

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
            if self.call_in_progress():
                # The update restarts this service and the engine: never in the middle of a call.
                self.next_auto_update = now + AUTO_UPDATE_DEFER
                log.info("automatic update check postponed: a call is active")
            else:
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
        """What the menu should say when things are not working yet. Plain words: nothing here asks the
        user to run a command, and nothing needs root."""
        if not self.engine_up:
            return {"kind": "no_engine", "title": "Earshot is starting",
                    "text": "The Earshot service is not running yet. Give it a few seconds. If this stays, "
                            "restart the headset."}
        cap = self.bt_status.get("capability") if isinstance(self.bt_status.get("capability"), dict) else None
        if self.bt_status and (not self.bt_status.get("available") or cap is None):
            reasons = [trim(r, 160) for r in (cap or {}).get("reasons", []) if isinstance(r, str)][:4]
            return {"kind": "no_bluetooth", "title": "Bluetooth is not ready for Earshot",
                    "text": " ".join(reasons) or "The Bluetooth service is not available."}
        if self.bt_status and not self.phone_paired():
            return {"kind": "no_phone", "title": "No iPhone is paired yet",
                    "text": "Pair your iPhone with Earshot. It takes about a minute.",
                    "action": {"act": "pair-start", "label": "Pair iPhone"}}
        return None

    def bluetooth_view(self):
        """Read-only status for Settings: is Bluetooth ready, and if not, what is wrong. The engine's own
        notes (such as the adapter class or L2CAP ERTM) are shown as information, never as steps."""
        if not self.engine_up:
            return {"ready": False, "text": "The Earshot service is not running yet.", "notes": []}
        cap = self.bt_status.get("capability") if isinstance(self.bt_status.get("capability"), dict) else None
        if not self.bt_status or cap is None or not self.bt_status.get("available"):
            return {"ready": False, "text": "Bluetooth is not available.", "notes": []}
        if not cap.get("adapter_present", True):
            return {"ready": False, "text": "No Bluetooth adapter was found.", "notes": []}
        if cap.get("powered") is False:
            return {"ready": False, "text": "Bluetooth is turned off in the headset settings.", "notes": []}
        notes = [trim(r, 220) for r in cap.get("reasons", []) if isinstance(r, str)][:4]
        return {"ready": True, "text": "Bluetooth is ready.", "notes": notes}

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
            "paired": self.phone_paired(),
            "connected": bool(self.conn.get("classic_connected") or self.conn.get("le_connected") or self.conn.get("ancs_ready")),
            "battery": battery if isinstance(battery, int) else None,
            "messages": bool(self.conn.get("map_open")),
            "notifications": bool(self.conn.get("ancs_ready")),
            "calls": bool(calls.get("available")),
            "calls_restore": self.hfp_restore or "",
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
            "bluetooth": self.bluetooth_view(),
            "pair": self.pairing.view(),
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
        if not is_bar_page(t):
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
