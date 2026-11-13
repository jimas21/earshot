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
                "id": pid, "title": "SteamVR - %s (%d)" % (mark, n),
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
                body = json.dumps([{k: v for k, v in p.items() if k in ("id", "title", "url", "webSocketDebuggerUrl")}
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
