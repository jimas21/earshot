"""Tests for frame/menu/earshot-steamui.py. Standard library only.

Run from the repository root:  python -m unittest discover -s frame/tests -v
"""

import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import FakeDevTools, FakeSpawn, FakeTether, load_helper, pump, wait_for  # noqa: E402

H = load_helper()

ADDR = "AA:BB:CC:DD:EE:FF"
DEV_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
SECRET = "SECRET-BODY-7431"

BT_STATUS = {"command": "bt_status", "available": True, "device_address": ADDR,
             "capability": {"mode": "full", "reasons": [], "setup": []}}
DEVICES = {"command": "bt_devices", "devices": [
    {"address": ADDR, "name": "Test Phone", "path": DEV_PATH, "connected": True, "classic_connected": True}]}
CONNECTED = {"command": "bt_connection_changed", "device_present": True, "device_paired": True,
             "classic_connected": True, "le_connected": True, "map_open": True, "ancs_ready": True,
             "battery": 80, "link_reason": "", "profile_reason": "",
             "calls": {"available": True, "audio": "", "reason": ""}}
THREADS = {"command": "bt_threads", "threads": [
    {"thread": "tel:+15550100", "name": "Pat Example", "address": "+15550100", "preview": "hello",
     "timestamp": 1000, "unread": 2, "count": 3, "group": False, "repliable": True},
    {"thread": "group:name:friends", "name": "Friends", "address": "", "preview": "x", "timestamp": 900,
     "unread": 0, "count": 1, "group": True, "repliable": False, "reply_reason": "group"},
    {"thread": "tel:12345", "name": "12345", "address": "12345", "preview": "code", "timestamp": 800,
     "unread": 0, "count": 1, "group": False, "repliable": False, "reply_reason": "No reply route."}]}


def notif(uid, app_id="com.example.chat", app="Chat", title="Title", body="Body", category=0, **extra):
    n = {"command": "bt_notification", "uid": uid, "app_id": app_id, "app_name": app, "title": title,
         "subtitle": "", "body": body, "category": category, "timestamp": 1000, "silent": False,
         "positive_action": False, "negative_action": True}
    n.update(extra)
    return n


class Rig:
    def __init__(self, test, worn=True, pages=("popup",), connect_tether=None, **helper_kwargs):
        self.tmp = tempfile.mkdtemp(prefix="earshot-test-")
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.tether = FakeTether()
        self.dev = FakeDevTools(pages)
        self.spawn = FakeSpawn()
        self.worn = [worn]
        test.addCleanup(self.close)
        self.tether.on_subscribe += [CONNECTED]
        self.tether.answers.update({
            "bt_status": [BT_STATUS], "bt_list_devices": [DEVICES], "bt_list_threads": [THREADS],
            "bt_list_notifications": [{"command": "bt_notifications", "notifications": []}],
            "bt_list_calls": [{"command": "bt_calls", "calls": []}],
            "bt_list_call_history": [{"command": "bt_call_history", "calls": []}],
        })
        self.settings_path = os.path.join(self.tmp, "config", "settings.json")
        self.status_file = os.path.join(self.tmp, "state", "update.json")
        self.updater = os.path.join(self.tmp, "earshot-frame-install.py")
        loop = H.Loop()
        kwargs = dict(
            loop=loop, executor=H.Executor(loop, spawn=self.spawn, inline=True),
            connect_tether=connect_tether or self.tether.connect, cdp_base=self.dev.base,
            settings_path=self.settings_path, exists=lambda p: True, scan=lambda names: self.worn[0],
            status_file=self.status_file, versions_file=os.path.join(self.tmp, "versions.json"),
            updater=self.updater, burst_window=0.4, push_delay=0.02, tether_backoff=(0.05, 0.2),
            presence_every=3600, send_timeout=0.5, settle_seconds=0.0, auto_update_first=3600)
        kwargs.update(helper_kwargs)
        self.helper = H.Helper(**kwargs)
        self.helper.shell.POLL_FAST = 0.1
        self.helper.shell.POLL_IDLE = 0.1

    def start(self, wait=True):
        self.helper.start()
        if wait:
            assert pump(self.helper, lambda: self.settled(), 5), "helper did not settle"
            self.barrier()

    def barrier(self):
        """Replies on one connection are ordered: once a fresh bt_status answer is in, every
        answer to the helper's start-up requests has been handled too."""
        self.helper.bt_status = {}
        self.helper.request("bt_status")
        assert pump(self.helper, lambda: self.helper.bt_status, 5), "no barrier reply"

    def settled(self):
        h = self.helper
        return (h.engine_up and h.conn.get("ancs_ready") and h.threads and h.devices
                and self.dev.last_state() is not None and self.dev.last_state()["threads"])

    def pump(self, predicate, timeout=5.0):
        return pump(self.helper, predicate, timeout)

    def settle_for(self, seconds):
        pump(self.helper, lambda: False, seconds)

    def action(self, **action):
        self.dev.call_binding("page1", action)

    def state(self):
        return self.dev.last_state()

    def vrcmd_texts(self):
        return [c[2] for c in self.spawn.of("vrcmd") if c[1] == "--notify"]

    def busctl_methods(self):
        return [c[6] for c in self.spawn.of("busctl")]

    def close(self):
        try:
            self.helper.shutdown()
            self.helper.loop.close()
        finally:
            self.tether.close()
            self.dev.close()


class TetherLinkTests(unittest.TestCase):
    def test_subscribes_and_asks_for_the_state(self):
        rig = Rig(self)
        rig.start()
        names = [m["command"] for m in rig.tether.commands()]
        self.assertEqual(names[:2], ["protocol_info", "subscribe"])
        for wanted in ("bt_status", "bt_connection", "bt_list_devices", "bt_list_threads",
                       "bt_list_notifications", "bt_list_calls", "bt_list_call_history"):
            self.assertIn(wanted, names)

    def test_reconnects_after_the_daemon_restarts(self):
        rig = Rig(self)
        rig.start()
        rig.tether.drop()
        self.assertTrue(rig.pump(lambda: not rig.helper.engine_up, 2))
        self.assertTrue(rig.pump(lambda: rig.tether.accepts >= 2 and rig.helper.engine_up and rig.helper.conn, 5))
        self.assertEqual(len(rig.tether.commands("subscribe")), 2)
        self.assertGreaterEqual(len(rig.tether.commands("bt_list_threads")), 2)

    def test_backoff_while_the_engine_is_absent_then_connects(self):
        attempts = []
        holder = {}

        def connect():
            attempts.append(holder["clock"]())
            if len(attempts) < 4:
                raise FileNotFoundError("no socket")
            return holder["tether"].connect()

        rig = Rig(self, connect_tether=connect)
        holder["tether"], holder["clock"] = rig.tether, rig.helper.loop.clock
        rig.start(wait=False)
        self.assertTrue(rig.pump(lambda: rig.helper.engine_up, 5))
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        self.assertGreaterEqual(gaps[0], 0.04)
        self.assertGreater(gaps[2], gaps[0])  # doubling

    def test_ignores_bare_ok_and_junk_lines(self):
        rig = Rig(self)
        rig.start()
        rig.tether.raw(b"OK\n\nnot json\n[1,2]\n")
        rig.tether.push(notif(50))
        self.assertTrue(rig.pump(lambda: 50 in rig.helper.notifications, 2))

    def test_message_split_across_reads(self):
        rig = Rig(self)
        rig.start()
        data = json.dumps(notif(51)).encode() + b"\n"
        rig.tether.raw(data[:20])
        rig.settle_for(0.05)
        rig.tether.raw(data[20:])
        self.assertTrue(rig.pump(lambda: 51 in rig.helper.notifications, 2))

    def test_socket_path_follows_tethers_runtime_dir(self):
        old = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "EARSHOT_TETHER_SOCKET")}
        try:
            os.environ.pop("EARSHOT_TETHER_SOCKET", None)
            os.environ["XDG_RUNTIME_DIR"] = "/run/user/1000"
            self.assertEqual(H.tether_socket_path(), os.path.join("/run/user/1000", "tether", "tetherd.sock"))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class PriorityTests(unittest.TestCase):
    def test_worn_notifications_toast_and_no_call_release(self):
        rig = Rig(self, worn=True)
        rig.start()
        self.assertEqual(rig.busctl_methods(), [])
        rig.tether.push(notif(1, title="Hi", body="there"))
        self.assertTrue(rig.pump(lambda: rig.vrcmd_texts(), 2))
        self.assertEqual(rig.vrcmd_texts()[0], "Chat: Hi - there")

    def test_a_notification_text_can_never_look_like_a_vrcmd_option(self):
        rig = Rig(self, worn=True)
        rig.start()
        rig.tether.push(notif(2, app="--help", title="x", body="y"))
        self.assertTrue(rig.pump(lambda: rig.vrcmd_texts(), 2))
        text = rig.vrcmd_texts()[0]
        self.assertFalse(text.startswith("-"), repr(text))
        self.assertIn("--help", text)
        for plain in ("Chat: Hi", "", "a-b"):
            self.assertEqual(H.vrcmd_text(plain), plain)
        for dashed in ("-x", "--version", "-"):
            self.assertFalse(H.vrcmd_text(dashed).startswith("-"))

    def test_not_worn_releases_hfp_and_stays_silent_but_records(self):
        rig = Rig(self, worn=True)
        rig.start()
        rig.worn[0] = False
        rig.helper.poll_presence()
        self.assertEqual(rig.busctl_methods(), ["DisconnectProfile"])
        call = rig.spawn.of("busctl")[0]
        self.assertEqual(call[:6], ["busctl", "--system", "call", "org.bluez", DEV_PATH, "org.bluez.Device1"])
        self.assertEqual(call[7:], ["s", "0000111f-0000-1000-8000-00805f9b34fb"])   # the full UUID
        rig.tether.push(notif(2))
        self.assertTrue(rig.pump(lambda: any(n["uid"] == 2 for n in (rig.state() or {}).get("notifications", [])), 2))
        self.assertEqual(rig.vrcmd_texts(), [])
        st = rig.state()
        self.assertFalse(st["priority"]["frame_active"])
        self.assertEqual(st["priority"]["worn"], False)

    def test_worn_again_takes_the_call_link_back(self):
        rig = Rig(self, worn=False)
        rig.start()
        self.assertEqual(rig.busctl_methods(), ["DisconnectProfile"])
        rig.worn[0] = True
        rig.helper.poll_presence()
        # ConnectProfile does nothing on a live link: the restore drops the BR/EDR bearer instead
        self.assertEqual(rig.busctl_methods(), ["DisconnectProfile", "Disconnect"])
        restore = rig.spawn.of("busctl")[1]
        self.assertEqual(restore[4:7], [DEV_PATH, "org.bluez.Bearer.BREDR1", "Disconnect"])
        self.assertEqual(len(restore), 7)
        self.assertNotIn("ConnectProfile", rig.busctl_methods())

    def test_restore_is_pending_until_the_call_link_is_seen_again(self):
        rig = Rig(self, worn=False)
        rig.tether.on_subscribe[-1] = dict(CONNECTED, calls={"available": False, "audio": "", "reason": ""})
        rig.start()
        rig.worn[0] = True
        rig.helper.poll_presence()
        self.assertTrue(rig.pump(lambda: rig.state()["phone"]["calls_restore"] == "pending", 2))
        self.assertIsNone(rig.helper.hfp_released)          # not "restored": nothing proves it yet
        rig.tether.push(dict(CONNECTED, classic_connected=False, calls={"available": False}))
        rig.tether.push(CONNECTED)                           # the phone reconnected with hands-free
        self.assertTrue(rig.pump(lambda: rig.state()["phone"]["calls_restore"] == "", 2))
        self.assertTrue(rig.state()["phone"]["calls"])

    def test_restore_that_never_comes_back_is_reported(self):
        rig = Rig(self, worn=False)
        rig.helper.hfp_verify_seconds = 0.2
        rig.tether.on_subscribe[-1] = dict(CONNECTED, calls={"available": False, "audio": "", "reason": ""})
        rig.start()
        rig.worn[0] = True
        rig.helper.poll_presence()
        self.assertTrue(rig.pump(lambda: rig.state()["phone"]["calls_restore"] == "unverified", 3))
        # a late recovery still clears it
        rig.tether.push(CONNECTED)
        self.assertTrue(rig.pump(lambda: rig.state()["phone"]["calls_restore"] == "", 2))

    def test_failed_restore_is_not_marked_and_is_retried(self):
        rig = Rig(self, worn=False)
        rig.helper.hfp_retry_after = 0.3
        rig.start()
        rig.spawn.results["busctl"] = (1, "", "boom")
        rig.worn[0] = True
        rig.helper.poll_presence()
        self.assertTrue(rig.pump(lambda: rig.busctl_methods().count("Disconnect") == 1, 2))
        self.assertEqual(rig.helper.hfp_restore, None)
        self.assertEqual(rig.helper.hfp_released, DEV_PATH)   # still released: the restore is owed
        self.assertTrue(rig.pump(lambda: rig.busctl_methods().count("Disconnect") == 2, 2))

    def test_release_is_not_repeated(self):
        rig = Rig(self, worn=False)
        rig.start()
        rig.helper.poll_presence()
        rig.helper.poll_presence()
        rig.tether.push(CONNECTED)
        rig.settle_for(0.1)
        self.assertEqual(rig.busctl_methods(), ["DisconnectProfile"])

    def test_new_link_while_not_worn_releases_again(self):
        rig = Rig(self, worn=False)
        rig.start()
        rig.tether.push(dict(CONNECTED, classic_connected=False))
        rig.tether.push(CONNECTED)
        self.assertTrue(rig.pump(lambda: rig.busctl_methods().count("DisconnectProfile") == 2, 2))

    def test_failed_release_is_retried(self):
        rig = Rig(self, worn=False)
        rig.helper.hfp_retry_after = 0.3
        rig.spawn.results["busctl"] = (1, "", "boom")
        rig.start()
        self.assertEqual(rig.busctl_methods(), ["DisconnectProfile"])   # not hammered while blocked
        rig.tether.push(CONNECTED)
        rig.settle_for(0.1)
        self.assertEqual(rig.busctl_methods(), ["DisconnectProfile"])
        self.assertIsNone(rig.helper.hfp_released)
        self.assertTrue(rig.pump(lambda: len(rig.busctl_methods()) == 2, 2))   # the retry timer

    def test_override_always_phone_while_worn(self):
        rig = Rig(self, worn=True)
        rig.start()
        rig.action(type="set_setting", key="priority", value="phone")
        self.assertTrue(rig.pump(lambda: rig.busctl_methods() == ["DisconnectProfile"], 2))
        rig.tether.push(notif(3))
        rig.settle_for(0.2)
        self.assertEqual(rig.vrcmd_texts(), [])
        self.assertFalse(rig.state()["priority"]["frame_active"])

    def test_override_always_frame_while_not_worn(self):
        rig = Rig(self, worn=False)
        rig.start()
        rig.action(type="set_setting", key="priority", value="frame")
        self.assertTrue(rig.pump(lambda: rig.busctl_methods() == ["DisconnectProfile", "Disconnect"], 2))
        rig.tether.push(notif(4))
        self.assertTrue(rig.pump(lambda: rig.vrcmd_texts(), 2))
        self.assertTrue(rig.pump(lambda: rig.state()["priority"]["frame_active"], 2))

    def test_auto_treats_unknown_presence_as_frame(self):
        rig = Rig(self, worn=True)
        h = rig.helper
        h.worn = None
        self.assertTrue(h.frame_active())

    def test_file_detector_and_fallback(self):
        path = os.path.join(tempfile.mkdtemp(), "worn")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        got = []
        det = H.FileDetector(path, ["1", "worn"])
        det.poll(got.append)
        self.assertEqual(got, [None])
        with open(path, "w") as f:
            f.write("1\n")
        det.poll(got.append)
        with open(path, "w") as f:
            f.write("0\n")
        det.poll(got.append)
        self.assertEqual(got, [None, True, False])
        rig = Rig(self, worn=True)
        rig.helper.settings.set("presence_source", "file")
        rig.helper.settings.set("presence_file", os.path.join(rig.tmp, "missing"))
        rig.helper.refresh_detector()
        rig.helper.poll_presence()
        self.assertTrue(rig.helper.worn)  # missing file: falls back to "SteamVR is running"

    def test_steamvr_detector(self):
        out = []
        H.SteamVrRunningDetector(lambda names: "vrserver" in names).poll(out.append)
        H.SteamVrRunningDetector(lambda names: False).poll(out.append)
        self.assertEqual(out, [True, False])


class BurstTests(unittest.TestCase):
    def test_collapser_pure(self):
        c = H.BurstCollapser(window=10)
        self.assertEqual(c.add("k", "a", "messages", "Pat", 0), ["a"])
        self.assertEqual(c.add("k", "b", "messages", "Pat", 1), [])
        self.assertEqual(c.add("k", "c", "messages", "Pat", 2), [])
        self.assertEqual(c.flush(5), [])
        self.assertEqual(c.flush(10), ["2 new messages from Pat"])
        self.assertEqual(c.add("k", "d", "messages", "Pat", 11), [])   # the flush opened a new window
        self.assertEqual(c.flush(19), [])
        self.assertEqual(c.flush(20), ["d"])                            # a lone item is shown as itself
        self.assertEqual(c.flush(40), [])
        self.assertEqual(c.state, {})
        self.assertEqual(c.add("k", "e", "messages", "Pat", 50), ["e"])
        self.assertEqual(c.add("other", "z", "notifications", "App", 50), ["z"])

    def test_burst_becomes_one_summary(self):
        rig = Rig(self)
        rig.start()
        for uid in range(10, 15):
            rig.tether.push(notif(uid, title="T%d" % uid, body="b"))
        self.assertTrue(rig.pump(lambda: len(rig.vrcmd_texts()) >= 1, 2))
        rig.settle_for(0.1)
        self.assertEqual(rig.vrcmd_texts(), ["Chat: T10 - b"])
        self.assertTrue(rig.pump(lambda: len(rig.vrcmd_texts()) == 2, 3))
        self.assertEqual(rig.vrcmd_texts()[1], "4 new notifications from Chat")

    def test_other_apps_are_not_collapsed_together(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push(notif(20, app_id="a.one", app="One"))
        rig.tether.push(notif(21, app_id="b.two", app="Two"))
        self.assertTrue(rig.pump(lambda: len(rig.vrcmd_texts()) == 2, 2))

    def test_message_burst(self):
        rig = Rig(self)
        rig.start()
        for i in range(3):
            rig.tether.push({"command": "bt_message", "handle": "h%d" % i, "thread": "tel:+15550100", "name": "Pat Example",
                             "body": "m%d" % i, "timestamp": 1000 + i, "outgoing": False, "read": False, "folder": "inbox"})
        self.assertTrue(rig.pump(lambda: len(rig.vrcmd_texts()) == 2, 3))
        self.assertEqual(rig.vrcmd_texts()[0], "Pat Example: m0")
        self.assertEqual(rig.vrcmd_texts()[1], "2 new messages from Pat Example")


class FilterTests(unittest.TestCase):
    def test_muted_app_is_silent_but_listed(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push(notif(30, app_id="com.example.noisy", app="Noisy"))
        self.assertTrue(rig.pump(lambda: len(rig.vrcmd_texts()) == 1, 2))
        rig.action(type="mute", app_id="com.example.noisy", muted=True)
        self.assertTrue(rig.pump(lambda: rig.helper.settings.get("muted_apps") == ["com.example.noisy"], 2))
        rig.tether.push(notif(31, app_id="com.example.noisy", app="Noisy"))
        rig.settle_for(0.5)
        self.assertEqual(len(rig.vrcmd_texts()), 1)
        st = rig.state()
        self.assertEqual([n["uid"] for n in st["notifications"]][:2], [31, 30])
        self.assertEqual([a for a in st["apps"] if a["id"] == "com.example.noisy"][0]["muted"], True)
        rig.action(type="mute", app_id="com.example.noisy", muted=False)
        self.assertTrue(rig.pump(lambda: rig.helper.settings.get("muted_apps") == [], 2))

    def test_silent_notification_is_listed_but_does_not_toast(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push(notif(32, silent=True))
        self.assertTrue(rig.pump(lambda: any(n["uid"] == 32 for n in (rig.state() or {}).get("notifications", [])), 2))
        rig.tether.push(notif(33))
        self.assertTrue(rig.pump(lambda: len(rig.vrcmd_texts()) == 1, 2))
        self.assertEqual(len(rig.vrcmd_texts()), 1)     # only the one that was not silent

    def test_alerts_off_and_preview_off(self):
        rig = Rig(self)
        rig.start()
        rig.action(type="set_setting", key="preview", value=False)
        self.assertTrue(rig.pump(lambda: rig.helper.settings.get("preview") is False, 2))
        rig.tether.push(notif(32, title=SECRET, body=SECRET))
        self.assertTrue(rig.pump(lambda: rig.vrcmd_texts(), 2))
        self.assertEqual(rig.vrcmd_texts(), ["Chat: new notification"])
        rig.action(type="set_setting", key="alerts", value=False)
        self.assertTrue(rig.pump(lambda: rig.helper.settings.get("alerts") is False, 2))
        rig.tether.push(notif(33, app_id="x.y", app="Other"))
        rig.settle_for(0.2)
        self.assertEqual(len(rig.vrcmd_texts()), 1)

    def test_messages_app_notifications_never_toast(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push(notif(34, app_id="com.apple.MobileSMS", app="Messages"))
        rig.settle_for(0.2)
        self.assertEqual(rig.vrcmd_texts(), [])
        self.assertIn(34, rig.helper.notifications)

    def test_backlog_is_not_toasted(self):
        rig = Rig(self, settle_seconds=60.0)
        rig.start()
        rig.tether.push(notif(40))                    # arrives while ANCS is settling
        rig.tether.push(notif(41, pre_existing=True))
        rig.settle_for(0.2)
        self.assertEqual(rig.vrcmd_texts(), [])
        rig.helper.ancs_ready_at = None               # settled
        rig.tether.push(notif(42))
        self.assertTrue(rig.pump(lambda: rig.vrcmd_texts(), 2))

    def test_listed_notifications_are_known_not_new(self):
        rig = Rig(self)
        rig.tether.answers["bt_list_notifications"] = [{"command": "bt_notifications", "notifications": [
            {k: v for k, v in notif(60).items() if k != "command"}]}]
        rig.start()
        rig.tether.push(notif(60))
        rig.settle_for(0.2)
        self.assertEqual(rig.vrcmd_texts(), [])

    def test_clear_all_hides_locally_dismiss_acts_on_phone(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push(notif(70))
        self.assertTrue(rig.pump(lambda: 70 in rig.helper.notifications, 2))
        rig.action(type="notif_action", uid=70, action="negative")
        self.assertTrue(rig.pump(lambda: rig.tether.commands("bt_notification_action"), 2))
        self.assertEqual(rig.tether.commands("bt_notification_action")[0],
                         {"command": "bt_notification_action", "uid": 70, "action": "negative"})
        rig.action(type="notif_action", uid=70, action="positive")   # the notification offers no positive action
        rig.action(type="clear_all")
        self.assertTrue(rig.pump(lambda: rig.state() and not rig.state()["notifications"], 2))
        self.assertEqual(len(rig.tether.commands("bt_notification_action")), 1)
        rig.tether.push({"command": "bt_notification_removed", "uid": 70})
        self.assertTrue(rig.pump(lambda: 70 not in rig.helper.notifications, 2))


class ReplyTests(unittest.TestCase):
    def send(self, rig, thread="tel:+15550100", text="on my way", cid="c1"):
        rig.action(type="send", thread=thread, text=text, cid=cid)

    def test_success(self):
        rig = Rig(self)
        rig.start()
        self.send(rig)
        self.assertTrue(rig.pump(lambda: rig.tether.commands("bt_send_message"), 2))
        cmd = rig.tether.commands("bt_send_message")[0]
        self.assertEqual((cmd["thread"], cmd["body"], cmd["operation_id"]), ("tel:+15550100", "on my way", "earshot-c1"))
        self.assertTrue(rig.pump(lambda: [s["status"] for s in rig.state()["sends"]] == ["sending"], 2))
        rig.tether.push({"command": "bt_send_result", "thread": "tel:+15550100", "success": True, "operation_id": "earshot-c1"})
        self.assertTrue(rig.pump(lambda: [s["status"] for s in rig.state()["sends"]] == ["sent"], 2))

    def test_failure_carries_the_reason(self):
        rig = Rig(self)
        rig.start()
        self.send(rig)
        self.assertTrue(rig.pump(lambda: rig.tether.commands("bt_send_message"), 2))
        rig.tether.push({"command": "bt_send_result", "thread": "tel:+15550100", "success": False,
                         "message": "Phone busy", "operation_id": "earshot-c1"})
        self.assertTrue(rig.pump(lambda: [s["status"] for s in rig.state()["sends"]] == ["failed"], 2))
        self.assertEqual(rig.state()["sends"][0]["error"], "Phone busy")

    def test_no_answer_times_out_as_failed(self):
        rig = Rig(self, send_timeout=0.2)
        rig.start()
        self.send(rig)
        self.assertTrue(rig.pump(lambda: [s["status"] for s in rig.state()["sends"]] == ["failed"], 3))

    def test_refuses_unknown_empty_or_unrepliable(self):
        rig = Rig(self)
        rig.start()
        self.send(rig, thread="tel:+19999999", cid="c2")
        self.send(rig, text="   ", cid="c3")
        self.send(rig, text="x" * 3000, cid="c4")
        rig.action(type="send", thread="tel:+15550100", text="hi", cid="bad cid!")
        self.send(rig, thread="tel:12345", cid="c5")          # known thread, no reply route
        rig.settle_for(0.3)
        self.assertEqual(rig.tether.commands("bt_send_message"), [])
        sends = rig.state()["sends"]
        self.assertEqual([(s["cid"], s["status"]) for s in sends], [("c5", "failed")])
        self.assertEqual(sends[0]["error"], "No reply route.")

    def test_duplicate_cid_is_sent_once(self):
        rig = Rig(self)
        rig.start()
        self.send(rig)
        self.send(rig)
        rig.settle_for(0.3)
        self.assertEqual(len(rig.tether.commands("bt_send_message")), 1)

    def test_group_threads_are_left_out_and_open_requests_messages(self):
        rig = Rig(self)
        rig.start()
        self.assertEqual([t["thread"] for t in rig.state()["threads"]], ["tel:+15550100", "tel:12345"])
        rig.action(type="open_thread", thread="tel:+15550100")
        self.assertTrue(rig.pump(lambda: rig.tether.commands("bt_list_messages"), 2))
        rig.tether.push({"command": "bt_messages", "thread": "tel:+15550100", "messages": [
            {"handle": "m1", "body": "hello", "timestamp": 1, "outgoing": False, "read": False, "folder": "inbox"}]})
        self.assertTrue(rig.pump(lambda: rig.state()["open"] and rig.state()["open"]["messages"], 2))
        self.assertTrue(rig.pump(lambda: rig.tether.commands("bt_mark_read"), 2))
        self.assertEqual(rig.tether.commands("bt_mark_read")[0]["handles"], ["m1"])
        rig.action(type="open_thread", thread="group:name:friends")   # not offered in v1
        rig.settle_for(0.1)
        self.assertEqual(rig.state()["open"]["thread"], "tel:+15550100")
        rig.tether.push({"command": "bt_message", "handle": "m2", "thread": "tel:+15550100", "body": "again",
                         "timestamp": 2, "outgoing": True, "read": True, "folder": "sent"})
        self.assertTrue(rig.pump(lambda: len(rig.state()["open"]["messages"]) == 2, 2))


class CallTests(unittest.TestCase):
    RINGING = {"command": "bt_calls", "calls": [
        {"path": DEV_PATH + "/telephony0/call1", "number": "+15550100", "name": "Pat Example", "state": "incoming",
         "ringing": True, "outgoing": False, "connected": False, "withheld": False}]}

    def test_incoming_call_card_and_toast_once(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push(self.RINGING)
        self.assertTrue(rig.pump(lambda: rig.state()["calls"]["incoming"], 2))
        self.assertEqual(rig.state()["calls"]["incoming"]["source"], "hfp")
        self.assertEqual(rig.vrcmd_texts(), ["Incoming call: Pat Example"])
        rig.tether.push(self.RINGING)                       # a repeat of the same call
        rig.settle_for(0.2)
        self.assertEqual(len(rig.vrcmd_texts()), 1)
        rig.tether.push({"command": "bt_calls", "calls": []})
        self.assertTrue(rig.pump(lambda: rig.state()["calls"]["incoming"] is None, 2))

    def test_automatic_update_waits_for_the_call_to_end(self):
        rig = Rig(self)
        with open(rig.updater, "w") as f:
            f.write("# fake installer")
        rig.start()
        rig.tether.push(self.RINGING)
        self.assertTrue(rig.pump(lambda: rig.state()["calls"]["incoming"], 2))
        h = rig.helper
        h.next_auto_update = h.loop.clock() - 1
        h.tick_update()
        rig.settle_for(0.1)
        self.assertEqual(rig.spawn.of("systemd-run"), [])
        self.assertLess(h.next_auto_update - h.loop.clock(), 10 * 60)    # soon again, not in 6 hours
        rig.tether.push({"command": "bt_calls", "calls": []})
        self.assertTrue(rig.pump(lambda: rig.state()["calls"]["incoming"] is None, 2))
        h.next_auto_update = h.loop.clock() - 1
        h.tick_update()
        self.assertTrue(rig.pump(lambda: rig.spawn.of("systemd-run"), 2))

    def test_no_call_toast_when_the_phone_has_priority(self):
        rig = Rig(self, worn=False)
        rig.start()
        rig.tether.push(self.RINGING)
        self.assertTrue(rig.pump(lambda: rig.state()["calls"]["incoming"], 2))
        self.assertEqual(rig.vrcmd_texts(), [])

    def test_answer_and_decline_use_call_commands(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push(self.RINGING)
        self.assertTrue(rig.pump(lambda: rig.state()["calls"]["incoming"], 2))
        path = rig.state()["calls"]["incoming"]["path"]
        rig.action(type="call", op="answer", path=path)
        rig.action(type="call", op="decline", path=path)
        rig.action(type="call", op="decline", path="/not/a/call")
        self.assertTrue(rig.pump(lambda: len(rig.tether.commands("bt_call_action")) == 2, 2))
        rig.settle_for(0.1)
        self.assertEqual(rig.tether.commands("bt_call_action"), [
            {"command": "bt_call_action", "action": "answer", "path": path},
            {"command": "bt_call_action", "action": "hangup", "path": path}])

    def test_failed_call_command_is_reported(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push(self.RINGING)
        self.assertTrue(rig.pump(lambda: rig.state()["calls"]["incoming"], 2))
        rig.tether.push({"command": "bt_call_result", "action": "answer", "success": False, "message": "Not allowed"})
        self.assertTrue(rig.pump(lambda: rig.state()["calls"]["error"] == "Not allowed", 2))

    def test_without_hands_free_falls_back_to_ancs_actions(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push(dict(CONNECTED, calls={"available": False}))
        rig.tether.push(notif(80, app_id="com.apple.mobilephone", app="Phone", title="Pat Example",
                              category=1, positive_action=True, negative_action=True))
        self.assertTrue(rig.pump(lambda: (rig.state()["calls"]["incoming"] or {}).get("source") == "ancs", 2))
        self.assertEqual(rig.state()["calls"]["incoming"]["name"], "Pat Example")
        self.assertEqual(rig.vrcmd_texts(), [])  # ringing is announced from the call list, not as an app notification
        rig.action(type="call", op="answer", uid=80)
        rig.action(type="call", op="decline", uid=80)
        self.assertTrue(rig.pump(lambda: len(rig.tether.commands("bt_notification_action")) == 2, 2))
        self.assertEqual([c["action"] for c in rig.tether.commands("bt_notification_action")], ["positive", "negative"])

    def test_history_is_trimmed_into_state(self):
        rig = Rig(self)
        rig.start()
        rig.tether.push({"command": "bt_call_history", "calls": [
            {"name": "Pat Example", "number": "+15550100", "type": "missed", "timestamp": 5}] * 30})
        self.assertTrue(rig.pump(lambda: len(rig.state()["calls"]["recent"]) == 15, 2))


class SettingsTests(unittest.TestCase):
    def test_persist_and_reload(self):
        rig = Rig(self)
        rig.start()
        rig.action(type="set_setting", key="priority", value="phone")
        rig.action(type="set_setting", key="auto_update", value=False)
        rig.action(type="mute", app_id="a.b", muted=True)
        self.assertTrue(rig.pump(lambda: os.path.exists(rig.settings_path) and rig.helper.settings.get("muted_apps") == ["a.b"], 2))
        again = H.Settings(rig.settings_path)
        self.assertEqual((again.get("priority"), again.get("auto_update"), again.get("muted_apps")), ("phone", False, ["a.b"]))
        self.assertEqual(os.listdir(os.path.dirname(rig.settings_path)), ["settings.json"])  # no temp leftovers

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_file_is_private(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        s = H.Settings(os.path.join(d, "cfg", "settings.json"))
        s.set("alerts", False)
        self.assertEqual(os.stat(s.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(s.path)).st_mode & 0o777, 0o700)

    def test_rejects_bad_values_and_survives_corruption(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "settings.json")
        s = H.Settings(path)
        for key, value in (("priority", "sometimes"), ("alerts", "yes"), ("muted_apps", [1]), ("nope", True), ("muted_apps", "x")):
            self.assertFalse(s.set(key, value))
        self.assertFalse(os.path.exists(path))
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(H.Settings(path).get("priority"), "auto")
        with open(path, "w") as f:
            json.dump({"priority": "bogus", "alerts": False, "extra": 1}, f)
        loaded = H.Settings(path)
        self.assertEqual((loaded.get("priority"), loaded.get("alerts")), ("auto", False))

    def test_state_carries_settings(self):
        rig = Rig(self)
        rig.start()
        self.assertEqual(rig.state()["settings"], {"priority": "auto", "alerts": True, "preview": True, "auto_update": True})


class ShellTests(unittest.TestCase):
    def test_injects_binds_and_pushes_state(self):
        rig = Rig(self, pages=("popup", "bar"))
        rig.start()
        self.assertTrue(rig.pump(lambda: all(rig.dev.last_state(p) for p in ("page1", "page2")), 3))
        for pid in ("page1", "page2"):
            page = rig.dev.pages[pid]
            self.assertEqual(page["bindings"], ["earshotAction"])
            self.assertEqual(len(page["scripts"]), 1)
            self.assertNotIn("__VERSION__", page["scripts"][0])
            self.assertNotIn("__BINDING__", page["scripts"][0])
        # The fake engine's device list can land after the first state push: wait for it.
        self.assertTrue(rig.pump(lambda: ((rig.state() or {}).get("phone") or {}).get("name") == "Test Phone", 3))
        st = rig.state()
        self.assertEqual(st["phone"]["name"], "Test Phone")
        self.assertEqual(st["phone"]["battery"], 80)
        self.assertTrue(st["phone"]["connected"])
        self.assertIsNone(st["setup"])

    def test_reinjects_after_a_page_reload(self):
        rig = Rig(self)
        rig.start()
        before = len(rig.dev.pages["page1"]["states"])
        rig.dev.reload("page1")
        self.assertTrue(rig.pump(lambda: len(rig.dev.pages["page1"]["scripts"]) == 2, 3))
        # counted before the reload: the state can be pushed to the fresh page before this line runs
        self.assertTrue(rig.pump(lambda: len(rig.dev.pages["page1"]["states"]) > before, 3))

    def test_only_pushes_when_something_changed(self):
        rig = Rig(self)
        rig.start()
        rig.settle_for(0.5)
        count = len(rig.dev.pages["page1"]["states"])
        rig.settle_for(0.6)
        self.assertEqual(len(rig.dev.pages["page1"]["states"]), count)

    def test_survives_the_devtools_endpoint_going_away(self):
        rig = Rig(self)
        rig.start()
        rig.dev.up = False
        self.assertTrue(rig.pump(lambda: rig.helper.shell.endpoint_ok is False, 3))
        self.assertEqual(rig.helper.shell.pages, {})
        rig.dev.up = True
        rig.tether.push(notif(90, title="after"))
        self.assertTrue(rig.pump(lambda: rig.dev.last_state() and any(n["uid"] == 90 for n in rig.dev.last_state()["notifications"]), 4))

    def test_only_real_bar_pages_are_injected(self):
        good = {"type": "page", "webSocketDebuggerUrl": "ws://x", "url": "https://steamloopback.host/r/?a=1&vrOverlayKey=valve.steam.gamepadui.bar"}
        self.assertTrue(H.is_bar_page(good))
        self.assertTrue(H.is_bar_page(dict(good, url="http://localhost/?vrOverlayKey=valve.steam.gamepadui.barpopup.3")))
        # the text elsewhere in the address, a frame or worker, a missing socket or a wrong type: not ours
        self.assertFalse(H.is_bar_page(dict(good, url="http://localhost/vrOverlayKey=valve.steam.gamepadui.bar")))
        self.assertFalse(H.is_bar_page(dict(good, url="http://localhost/?x=vrOverlayKey%3Dvalve.steam.gamepadui.bar")))
        self.assertFalse(H.is_bar_page(dict(good, url="http://localhost/?vrOverlayKey=other.valve.steam.gamepadui.bar")))
        self.assertFalse(H.is_bar_page(dict(good, type="iframe")))
        self.assertFalse(H.is_bar_page(dict(good, type="service_worker")))
        self.assertFalse(H.is_bar_page({k: v for k, v in good.items() if k != "type"}))
        self.assertFalse(H.is_bar_page({k: v for k, v in good.items() if k != "webSocketDebuggerUrl"}))
        for junk in (None, [], "x", {"type": "page", "url": 5, "webSocketDebuggerUrl": "ws://x"}):
            self.assertFalse(H.is_bar_page(junk))

    def test_ignores_other_pages_and_bad_payloads(self):
        rig = Rig(self)
        rig.dev.pages["other"] = {"id": "other", "title": "x", "url": "http://localhost/other", "sock": None,
                                  "webSocketDebuggerUrl": "ws://127.0.0.1:%d/devtools/page/other" % rig.dev.port}
        rig.start()
        self.assertEqual(list(rig.helper.shell.pages), ["page1"])
        for bad in ("not json", "[1]", json.dumps({"type": "nope"}), json.dumps({"type": "send"}), "x" * 20000):
            rig.dev.call_binding("page1", bad)
        rig.settle_for(0.2)
        self.assertEqual(rig.tether.commands("bt_send_message"), [])

    def test_teardown_removes_the_menu(self):
        rig = Rig(self)
        rig.start()
        rig.helper.shutdown()
        self.assertTrue(wait_for(lambda: rig.dev.pages["page1"]["teardowns"] == 1, 2))

    def test_bad_ws_url_is_refused(self):
        with self.assertRaises(ValueError):
            H.WsClient("ws://example.com:80/devtools/page/x")


class SetupAndUpdateTests(unittest.TestCase):
    def test_not_set_up_states(self):
        def refuse():
            raise FileNotFoundError

        rig = Rig(self, connect_tether=refuse)
        rig.start(wait=False)
        self.assertTrue(rig.pump(lambda: rig.dev.last_state() is not None, 3))
        self.assertEqual(rig.state()["setup"]["kind"], "no_engine")
        h = rig.helper
        h.engine_up = True
        h.bt_status = {"available": False, "device_address": "", "capability": {
            "reasons": ["BlueZ was not started with --experimental."], "setup": []}}
        s = h.view_state()["setup"]
        self.assertEqual(s["kind"], "no_bluetooth")
        self.assertNotIn("steps", s)
        self.assertNotIn("sudo", json.dumps(s))
        self.assertFalse(h.view_state()["bluetooth"]["ready"])
        h.bt_status = dict(BT_STATUS, device_address="")
        self.assertEqual(h.view_state()["setup"]["kind"], "no_phone")
        h.bt_status = BT_STATUS
        self.assertIsNone(h.view_state()["setup"])
        bt = h.view_state()["bluetooth"]
        self.assertTrue(bt["ready"])
        self.assertEqual(bt["text"], "Bluetooth is ready.")

    def test_engine_notes_are_status_never_steps(self):
        rig = Rig(self)
        h = rig.helper
        h.engine_up = True
        h.bt_status = dict(BT_STATUS, capability={
            "mode": "full", "powered": True, "adapter_present": True, "setup": [{"what": "x", "command": "sudo x"}],
            "reasons": ["This adapter does not present itself as A/V Hands-Free."]})
        view = h.view_state()
        self.assertIsNone(view["setup"])
        self.assertTrue(view["bluetooth"]["ready"])
        self.assertEqual(view["bluetooth"]["notes"], ["This adapter does not present itself as A/V Hands-Free."])
        self.assertNotIn("sudo", json.dumps(view))

    def test_update_runs_the_installer_in_its_own_unit(self):
        rig = Rig(self)
        with open(rig.updater, "w") as f:
            f.write("# fake installer")
        rig.start()
        rig.action(type="check_updates")
        self.assertTrue(rig.pump(lambda: rig.spawn.of("systemd-run"), 2))
        run = rig.spawn.of("systemd-run")[0]
        self.assertIn("--unit=earshot-update", run)
        self.assertIn("--user", run)
        self.assertEqual(run[-2:], [rig.updater, "--if-newer"])
        self.assertEqual(rig.spawn.of("systemctl")[0][:4], ["systemctl", "--user", "reset-failed", "earshot-update"])
        self.assertTrue(rig.pump(lambda: (rig.state()["update"]["status"] or {}).get("state") == "checking", 2))
        # the installer reports progress in the shared status file
        with open(rig.status_file, "w") as f:
            json.dump({"state": "updating", "message": "Updating Earshot to 0.2.0", "time": 1}, f)
        rig.helper.read_update_status()
        self.assertTrue(rig.pump(lambda: rig.state()["update"]["status"]["message"] == "Updating Earshot to 0.2.0", 2))

    def test_update_without_installer_says_so(self):
        rig = Rig(self)
        rig.start()
        rig.action(type="check_updates")
        self.assertTrue(rig.pump(lambda: (rig.state()["update"]["status"] or {}).get("state") == "failed", 2))
        self.assertEqual(rig.spawn.of("systemd-run"), [])

    def test_versions_file_is_passed_on(self):
        rig = Rig(self)
        with open(os.path.join(rig.tmp, "versions.json"), "w") as f:
            json.dump({"engine": "0.3.5", "frame": "0.1.0"}, f)
        rig.start()
        self.assertEqual(rig.state()["update"]["versions"], {"engine": "0.3.5", "frame": "0.1.0"})


class ExecutorTests(unittest.TestCase):
    def test_runs_commands_off_the_loop_and_reports_back(self):
        loop = H.Loop()
        self.addCleanup(loop.close)
        ex = H.Executor(loop)
        self.addCleanup(ex.close)
        results = []
        ex.run([sys.executable, "-c", "print('hi')"], lambda rc, out, err: results.append((rc, out.strip())))
        ex.run(["definitely-not-a-command-earshot"], lambda rc, out, err: results.append((rc, out)))
        end = __import__("time").monotonic() + 15
        while len(results) < 2 and __import__("time").monotonic() < end:
            loop.run_once(0.1)
        self.assertEqual(sorted(results, key=lambda r: r[0]), [(0, "hi"), (127, "")])


class LogTests(unittest.TestCase):
    def test_logs_never_contain_message_text(self):
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Grab(level=logging.DEBUG)
        logger = logging.getLogger("earshot")
        old = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old)
        rig = Rig(self)
        rig.start()
        rig.tether.push(notif(100, title=SECRET, body=SECRET))
        rig.tether.push({"command": "bt_message", "handle": "z", "thread": "tel:+15550100", "name": SECRET, "body": SECRET,
                         "timestamp": 1, "outgoing": False, "read": False, "folder": "inbox"})
        rig.tether.push({"command": "bt_calls", "calls": [{"path": "/c", "name": SECRET, "number": SECRET, "ringing": True}]})
        rig.tether.raw(b'{"command":"bt_notification","uid":"bad"}\n')   # provokes a handler problem path
        rig.action(type="send", thread="tel:+15550100", text=SECRET, cid="c9")
        rig.tether.drop()
        rig.settle_for(0.5)
        self.assertTrue(records)
        self.assertFalse([r for r in records if SECRET in r or "+15550100" in r or ADDR in r], records)

    def test_default_level_is_warning(self):
        old = os.environ.pop("EARSHOT_LOG", None)
        try:
            root = logging.getLogger()
            saved = root.handlers[:]
            root.handlers = []
            H.setup_logging()
            self.assertEqual(root.level, logging.WARNING)
        finally:
            root.handlers = saved
            if old is not None:
                os.environ["EARSHOT_LOG"] = old


if __name__ == "__main__":
    unittest.main()
