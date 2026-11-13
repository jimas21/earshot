"""Fakes for the helper tests: a tetherd socket, a DevTools endpoint, and a command recorder.

Everything runs on loopback TCP, so the tests also run on Windows (no AF_UNIX needed).
The helper takes its transports as parameters; these fakes speak the real wire formats.
"""

import base64
import hashlib
import importlib.util
import json
import os
import re
import socket
import socketserver
import struct
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MENU_DIR = os.path.join(HERE, "..", "menu")


def load_helper():
    path = os.path.join(MENU_DIR, "earshot-steamui.py")
    spec = importlib.util.spec_from_file_location("earshot_steamui", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for(predicate, timeout=5.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def pump(helper, predicate, timeout=5.0):
    """Run the helper's loop until predicate() or timeout. Returns predicate()."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        helper.loop.run_once(0.02)
    return predicate()


# ---------------------------------------------------------------------------

class FakeTether:
    """A tetherd stand-in. Replays protocol messages like the real daemon after `subscribe`
    and answers the read commands from `answers` (command -> list of replies)."""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.address = self.server.getsockname()
        self.lock = threading.Lock()
        self.conn = None
        self.received = []          # every command received, parsed
        self.accepts = 0
        self.answers = {}
        self.handlers = {}          # command -> callable(msg, conn): scripted behaviour (PairingFake)
        self.on_subscribe = [
            {"command": "protocol_info", "version": 1,
             "capabilities": ["bluetooth.connection", "messages", "notifications", "calls"]},
            {"command": "state_snapshot", "paired_devices": []},
        ]
        self.closed = False
        threading.Thread(target=self.accept_loop, daemon=True).start()

    def connect(self):
        return socket.create_connection(self.address, timeout=2)

    def accept_loop(self):
        while not self.closed:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with self.lock:
                self.conn = conn
                self.accepts += 1
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    self.received.append(msg)
                cmd = msg.get("command")
                if cmd == "subscribe":
                    for m in self.on_subscribe:
                        self.push(m, conn)
                for reply in self.answers.get(cmd, []):
                    self.push(reply, conn)
                handler = self.handlers.get(cmd)
                if handler:
                    handler(msg, conn)

    def push(self, msg, conn=None):
        conn = conn or self.conn
        try:
            conn.sendall(json.dumps(msg).encode() + b"\n")
        except (OSError, AttributeError):
            pass

    def raw(self, data):
        self.conn.sendall(data)

    def drop(self):
        with self.lock:
            conn, self.conn = self.conn, None
        if conn:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def commands(self, name=None):
        with self.lock:
            return [m for m in self.received if name is None or m.get("command") == name]

    def close(self):
        self.closed = True
        self.drop()
        self.server.close()


# ---------------------------------------------------------------------------

class FakeDevTools:
    """Steam's DevTools endpoint: /json/list over HTTP and a websocket per page."""

    def __init__(self, titles=("popup",)):
        self.pages = {}
        self.lock = threading.Lock()
        self.up = True
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                outer.handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        for i, kind in enumerate(titles):
            self.add_page(kind, i + 1)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def add_page(self, kind, n):
        pid = "page%d" % n
        mark = "valve.steam.gamepadui.barpopup.%d" % n if kind == "popup" else "valve.steam.gamepadui.bar"
        with self.lock:
            self.pages[pid] = {
                "id": pid, "type": "page", "title": "SteamVR - %s (%d)" % (mark, n),
                "url": "http://localhost/?vrOverlayKey=%s" % mark,
                "webSocketDebuggerUrl": "ws://127.0.0.1:%d/devtools/page/%s" % (self.port, pid),
                "version": None, "sock": None, "bindings": [], "scripts": [], "states": [], "teardowns": 0,
            }
        return pid

    def reload(self, pid):
        with self.lock:
            self.pages[pid]["version"] = None

    def handle(self, sock):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                return
            head += chunk
        line = head.split(b"\r\n", 1)[0].decode()
        path = line.split(" ")[1]
        if not self.up:
            sock.close()
            return
        if path == "/json/list":
            with self.lock:
                body = json.dumps([{k: v for k, v in p.items() if k in ("id", "type", "title", "url", "webSocketDebuggerUrl")}
                                   for p in self.pages.values()]).encode()
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            sock.close()
            return
        m = re.match(r"/devtools/page/(\w+)", path)
        if not m or m.group(1) not in self.pages:
            sock.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return
        key = re.search(rb"Sec-WebSocket-Key: (\S+)", head, re.I).group(1).decode()
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        page = self.pages[m.group(1)]
        with self.lock:
            page["sock"] = sock
        self.ws_loop(sock, page)

    def ws_loop(self, sock, page):
        buf = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while True:
                frame = self.parse(buf)
                if frame is None:
                    break
                opcode, payload, buf = frame
                if opcode == 0x8:
                    return
                if opcode == 0x1:
                    self.on_message(sock, page, json.loads(payload.decode()))

    @staticmethod
    def parse(buf):
        if len(buf) < 2:
            return None
        n, off = buf[1] & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            n, off = struct.unpack("!H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, off = struct.unpack("!Q", buf[2:10])[0], 10
        mask = buf[off:off + 4]
        off += 4
        if len(buf) < off + n:
            return None
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[off:off + n]))
        return buf[0] & 0x0F, payload, buf[off + n:]

    @staticmethod
    def send(sock, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        if n < 126:
            head = bytes([0x81, n])
        elif n < 65536:
            head = bytes([0x81, 126]) + struct.pack("!H", n)
        else:
            head = bytes([0x81, 127]) + struct.pack("!Q", n)
        try:
            sock.sendall(head + data)
        except OSError:
            pass

    def on_message(self, sock, page, msg):
        method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        result = {}
        with self.lock:
            if method == "Runtime.addBinding":
                page["bindings"].append(params.get("name"))
            elif method == "Runtime.evaluate":
                expr = params.get("expression", "")
                if "window.__earshotMenu ? window.__earshotMenu.version" in expr:
                    if page["version"]:
                        result = {"result": {"type": "string", "value": page["version"]}}
                    else:
                        result = {"result": {"type": "object", "subtype": "null", "value": None}}
                elif "window.__earshotMenu&&window.__earshotMenu.setState(" in expr:
                    body = expr.split("setState(", 1)[1].rsplit(")", 1)[0]
                    if page["version"] is None:
                        result = {"exceptionDetails": {"text": "menu gone"}}
                    else:
                        page["states"].append(json.loads(body))
                        result = {"result": {"type": "undefined"}}
                elif "window.__earshotMenu&&window.__earshotMenu.teardown()" in expr:
                    page["teardowns"] += 1
                else:
                    page["scripts"].append(expr)
                    v = re.search(r"const VERSION = '([0-9a-f]+)'", expr)
                    page["version"] = v.group(1) if v else "x"
                    result = {"result": {"type": "string", "value": "installed %s (popup)" % page["version"]}}
        if mid is not None:
            self.send(sock, {"id": mid, "result": result})

    def call_binding(self, pid, payload):
        sock = self.pages[pid]["sock"]
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.send(sock, {"method": "Runtime.bindingCalled",
                         "params": {"name": "earshotAction", "payload": text, "executionContextId": 1}})

    def last_state(self, pid="page1"):
        with self.lock:
            states = self.pages[pid]["states"]
            return states[-1] if states else None

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self.pages.values():
            if p["sock"]:
                try:
                    p["sock"].close()
                except OSError:
                    pass


# ---------------------------------------------------------------------------

class FakeSpawn:
    """Stands in for subprocess: records every command (vrcmd, busctl, systemd-run...) and returns scripted results."""

    def __init__(self):
        self.calls = []
        self.results = {}   # argv[0] basename -> (rc, out, err), or a callable(argv)

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        r = self.results.get(os.path.basename(argv[0]), (0, "", ""))
        return r(argv) if callable(r) else r

    def of(self, name):
        return [c for c in self.calls if os.path.basename(c[0]) == name]


# ---------------------------------------------------------------------------

class PairingFake:
    """Replays the daemon's pairing traffic (docs/PROTOCOL.md section 6; src/core/src/net.cpp
    run_bt_scan / run_bt_pair / ask_client_to_confirm / run_bt_unpair) for each outcome.

    outcome: success | single (bonded without the LE half) | stale | key_missing | refused | declined_here |
             timeout | not_found | agent | busy_then_success
    listen:  True answers bt_pair_listen (the phone-initiated route); False leaves it unanswered, like a
             daemon that does not know the command; "error" answers it with {"command":"error"}.
    """

    CODE = "123456"
    PHONE_ADDR = "11:22:33:44:55:66"

    @staticmethod
    def phone(name="Pat's iPhone", address=PHONE_ADDR, **extra):
        d = {"address": address, "name": name, "path": "/org/bluez/hci0/dev_" + address.replace(":", "_"),
             "paired": False, "bonded": False, "connected": False, "iphone": True, "apple_nearby": True,
             "airpods": False}
        d.update(extra)
        return d

    def __init__(self, tether, outcome="success", listen=True, devices=None, scans=None, busy=2):
        self.t = tether
        self.outcome = outcome
        self.listen = listen
        self.devices = list(devices if devices is not None else [self.phone()])
        self.scans = list(scans) if scans is not None else None   # per scan: the device list it finds
        self.busy_left = busy
        self.pending = None       # (op, address) waiting for bt_pair_confirm
        self.listening = None     # op of the open listen window
        self.log = []
        tether.handlers.update({
            "bt_scan": self.on_scan, "bt_pair": self.on_pair, "bt_pair_listen": self.on_listen,
            "bt_pair_confirm": self.on_confirm, "bt_unpair": self.on_unpair, "bt_set_device": self.on_set_device,
        })
        tether.answers["bt_list_devices"] = [self.devices_msg()]

    def devices_msg(self):
        return {"command": "bt_devices", "devices": self.devices}

    def event(self, command, op, **fields):
        msg = {"command": command}
        msg.update(fields)
        if op:
            msg["operation_id"] = op
        self.t.push(msg)

    # --- scan ---
    def on_scan(self, msg, conn):
        self.log.append(("scan", None, None))
        if self.scans is not None:
            self.devices = list(self.scans.pop(0) if self.scans else [])
            self.t.answers["bt_list_devices"] = [self.devices_msg()]
        self.t.push(self.devices_msg())
        self.event("bt_scan_result", None, success=True, message="Bluetooth scan finished.")

    # --- listen (phone-initiated) ---
    def on_listen(self, msg, conn):
        op = msg.get("operation_id")
        self.log.append(("listen", op, msg.get("seconds")))
        if msg.get("seconds") == 0:
            return
        if self.listen == "error":
            self.t.push({"command": "error", "message": "unknown_command"})
            return
        if not self.listen:
            return
        self.listening = op
        self.event("bt_pair_progress", op, step="listening", detail="120")

    def phone_taps(self, address=PHONE_ADDR):
        """The user taps the headset under Other Devices on the iPhone."""
        op = self.listening
        self.event("bt_pair_progress", op, step="connecting", detail="Pat's iPhone")
        self.run_outcome(op, address)

    def listen_expires(self):
        self.event("bt_pair_result", self.listening, success=False, status="timeout", dual_bond=False,
                   message="No iPhone paired in time.", address="")

    # --- explicit pair (scan and pick) ---
    def on_pair(self, msg, conn):
        op, address = msg.get("operation_id"), msg.get("address")
        self.log.append(("pair", op, address))
        self.event("bt_pair_progress", op, step="connecting", detail="iPhone")
        self.run_outcome(op, address)

    def run_outcome(self, op, address):
        o = self.outcome

        def fail(status, message):
            self.event("bt_pair_result", op, success=False, status=status, message=message, address=address,
                       dual_bond=False)

        if o == "busy_then_success" and self.busy_left > 0:
            self.busy_left -= 1
            return fail("busy", "Another pairing attempt is already in progress.")
        if o == "not_found":
            return fail("not_found", "Device %s is not visible to BlueZ. Unlock the iPhone..." % address)
        if o == "agent":
            return fail("error", "Could not register a Bluetooth pairing agent.")
        if o == "stale":
            self.event("bt_pair_progress", op, step="error", detail="org.bluez.Error.ConnectionAttemptFailed")
            return fail("timeout", "The iPhone refused the connection before pairing started: Connection refused. "
                                   "Delete every entry for this computer on the iPhone...")
        if o == "key_missing":
            self.event("bt_pair_progress", op, step="error", detail="br-connection-key-missing")
            return fail("timeout", "Pairing failed: org.bluez.Error.Failed br-connection-key-missing")
        if o == "timeout":
            self.event("bt_pair_progress", op, step="pairing", detail="iPhone")
            return fail("timeout", "Pairing did not complete. Confirm the prompt on the iPhone.")
        if o == "refused":
            self.event("bt_pair_progress", op, step="pairing", detail="iPhone")
            return fail("rejected", "The iPhone declined the pairing request.")
        self.event("bt_pair_progress", op, step="confirm", detail=self.CODE)
        self.event("bt_pair_confirm_request", op, code=self.CODE)
        self.pending = (op, address)

    def on_confirm(self, msg, conn):
        self.log.append(("confirm", msg.get("operation_id"), msg.get("accept")))
        if not self.pending or msg.get("operation_id") != self.pending[0]:
            return
        op, address = self.pending
        self.pending = None
        if not msg.get("accept"):
            self.event("bt_pair_progress", op, step="declined", detail="client")
            return self.event("bt_pair_result", op, success=False, status="rejected", address=address,
                              dual_bond=False, message="Pairing was not confirmed on this computer.")
        self.event("bt_pair_progress", op, step="confirmed", detail="client")
        self.event("bt_pair_progress", op, step="paired", detail="iPhone")
        self.event("bt_pair_progress", op, step="settling", detail="waiting for the Classic link to settle")
        self.event("bt_pair_progress", op, step="soliciting", detail="Open Settings > Bluetooth > (i)...")
        for d in self.devices:
            if d["address"] == address:
                d.update(paired=True, bonded=True)
        self.t.answers["bt_list_devices"] = [self.devices_msg()]
        dual = self.outcome != "single"
        self.event("bt_pair_result", op, success=True, status="paired", address=address, dual_bond=dual,
                   auth_strategy_used="connect-first",
                   message="Paired with iPhone." + ("" if dual else " The bond covers BR/EDR only."))

    # --- unpair, set device ---
    def on_unpair(self, msg, conn):
        self.log.append(("unpair", msg.get("operation_id"), msg.get("address")))
        self.devices = [d for d in self.devices if d["address"] != msg.get("address")]
        self.t.answers["bt_list_devices"] = [self.devices_msg()]
        self.event("bt_unpair_result", msg.get("operation_id"), success=True, status="unpaired",
                   message="Removed the bond.", address=msg.get("address"))

    def on_set_device(self, msg, conn):
        self.log.append(("set_device", None, msg.get("address")))

    def commands(self, kind):
        return [e for e in self.log if e[0] == kind]
