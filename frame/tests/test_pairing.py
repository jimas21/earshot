"""Tests for the Pair iPhone flow in frame/menu/earshot-steamui.py, against a fake daemon that
replays the real pairing traffic (see PairingFake in fakes.py). Standard library only.

Run from the repository root:  python -m unittest discover -s frame/tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import PairingFake, load_helper  # noqa: E402
from test_helper import BT_STATUS, Rig  # noqa: E402

H = load_helper()
P = PairingFake
PHONE2 = "AA:AA:AA:AA:AA:01"

NO_PHONE = dict(BT_STATUS, device_address="", adapters=[{"path": "/org/bluez/hci0", "name": "Steam Frame"}],
                capability={"mode": "full", "reasons": [], "setup": [], "adapter_present": True,
                            "powered": True, "adapter_id": "hci0"})


def pair_rig(test, outcome="success", listen=True, supports=True, status=None, **kw):
    """A rig with no phone paired and a fake daemon that plays `outcome`.
    supports: the daemon announces bt_pair_listen in protocol_info."""
    rig = Rig(test)
    rig.tether.answers["bt_status"] = [status or NO_PHONE]
    if supports:
        rig.tether.on_subscribe[0] = dict(rig.tether.on_subscribe[0],
                                          capabilities=rig.tether.on_subscribe[0]["capabilities"] + ["bluetooth.pair_listen"])
    fake = P(rig.tether, outcome, listen, **kw)
    rig.start()
    pairing = rig.helper.pairing
    pairing.BUSY_DELAY = 0.05
    pairing.LISTEN_DETECT = 0.3
    pairing.SCAN_STALL = 0.5
    return rig, fake


def pair_view(rig):
    s = rig.state()
    return s and s.get("pair")


def wait_phase(test, rig, phase, timeout=4.0):
    ok = rig.pump(lambda: (pair_view(rig) or {}).get("phase") == phase, timeout)
    test.assertTrue(ok, "wanted phase %r, state is %r" % (phase, pair_view(rig)))
    return pair_view(rig)


def op_of(rig):
    return rig.helper.pairing.s["op"]


def to_confirm(test, rig, fake):
    rig.action(type="pair_start")
    wait_phase(test, rig, "listen")
    test.assertTrue(rig.pump(lambda: fake.listening, 2))
    fake.phone_taps()
    return wait_phase(test, rig, "confirm")


class FlowStartTests(unittest.TestCase):
    def test_no_phone_offers_the_pair_button(self):
        rig, _ = pair_rig(self)
        # the status the setup message is built from arrives a moment after the first state push
        self.assertTrue(rig.pump(lambda: ((rig.state() or {}).get("setup") or {}).get("kind") == "no_phone", 3))
        setup = rig.state()["setup"]
        self.assertEqual(setup["kind"], "no_phone")
        self.assertEqual(setup["action"], {"act": "pair-start", "label": "Pair iPhone"})
        self.assertNotIn("README", setup["text"])
        self.assertIsNone(pair_view(rig))

    def test_blocked_when_bluetooth_is_off_and_nothing_starts(self):
        off = dict(NO_PHONE, capability=dict(NO_PHONE["capability"], powered=False))
        rig, fake = pair_rig(self, status=off)
        rig.action(type="pair_start")
        v = wait_phase(self, rig, "blocked")
        self.assertIn("turned off", v["reasons"][0])
        rig.action(type="pair_search")
        rig.action(type="pair_pick", address=P.PHONE_ADDR)
        rig.settle_for(0.2)
        self.assertEqual(fake.log, [])
        self.assertEqual(rig.tether.commands("bt_scan"), [])
        # fixed, the blocked screen turns into the real flow by itself
        rig.tether.push(NO_PHONE)
        wait_phase(self, rig, "listen")

    def test_blocked_when_the_service_is_missing(self):
        rig, fake = pair_rig(self, status=dict(NO_PHONE, available=False))
        rig.action(type="pair_start")
        v = wait_phase(self, rig, "blocked")
        self.assertIn("not available", v["reasons"][0])
        self.assertEqual(fake.log, [])

    def test_root_only_setup_does_not_block_pairing(self):
        # stock BlueZ without the class or experimental mode must still be able to pair
        status = dict(NO_PHONE, capability=dict(NO_PHONE["capability"], class_ok=False, bearer_api="absent",
                                                setup=[{"what": "x", "command": "sudo x"}]))
        rig, fake = pair_rig(self, status=status)
        rig.action(type="pair_start")
        wait_phase(self, rig, "listen")

    def test_unknown_actions_and_garbage_are_ignored(self):
        rig, fake = pair_rig(self)
        for a in ({"type": "pair_confirm", "op": "x", "accept": True}, {"type": "pair_pick", "address": 5},
                  {"type": "pair_retry"}, {"type": "pair_done"}, {"type": "forget_do"}, {"type": "pair_nope"}):
            rig.action(**a)
        rig.settle_for(0.2)
        self.assertIsNone(pair_view(rig))
        self.assertEqual(fake.log, [])


class ListenRouteTests(unittest.TestCase):
    def test_listen_then_confirm_then_checklist(self):
        rig, fake = pair_rig(self)
        rig.action(type="pair_start")
        v = wait_phase(self, rig, "listen")
        self.assertEqual(v["headset"], "Steam Frame")
        self.assertEqual(v["seconds"], 120)
        self.assertGreater(v["ends_at"], __import__("time").time() + 100)
        cmd = rig.tether.commands("bt_pair_listen")[0]
        self.assertEqual((cmd["seconds"], cmd["operation_id"].startswith("earshot-listen-")), (120, True))
        self.assertTrue(rig.pump(lambda: fake.listening, 2))
        fake.phone_taps()
        v = wait_phase(self, rig, "confirm")
        self.assertEqual(v["code"], "123456")
        self.assertEqual(v["op"], cmd["operation_id"])
        # it asked for attention in the headset and did not answer by itself
        self.assertTrue(rig.pump(lambda: any("pairing code" in t for t in rig.vrcmd_texts()), 2))
        rig.settle_for(0.2)
        self.assertEqual(fake.commands("confirm"), [])
        rig.action(type="pair_confirm", op="someone-elses", accept=True)
        rig.action(type="pair_confirm", op=v["op"], accept="yes")
        rig.settle_for(0.2)
        self.assertEqual(fake.commands("confirm"), [])
        rig.action(type="pair_confirm", op=v["op"], accept=True)
        v = wait_phase(self, rig, "almost")
        self.assertEqual(fake.commands("confirm"), [("confirm", cmd["operation_id"], True)])
        self.assertFalse(v["weak"])
        states = {i["id"]: i["state"] for i in v["items"]}
        self.assertEqual(states["notifications"], "on")        # CONNECTED reports ancs_ready and map_open
        self.assertEqual(states["messages"], "on")
        self.assertEqual(states["contacts"], "waiting")
        self.assertEqual(states["favorites"], "unknown")
        self.assertTrue(v["ready"])
        rig.action(type="pair_done")
        self.assertTrue(rig.pump(lambda: pair_view(rig) is None, 2))

    def test_progress_text_is_plain_words(self):
        rig, fake = pair_rig(self)
        rig.action(type="pair_start")
        wait_phase(self, rig, "listen")
        rig.pump(lambda: fake.listening, 2)
        fake.event("bt_pair_progress", fake.listening, step="connecting", detail="Pat's iPhone")
        v = wait_phase(self, rig, "pairing")
        self.assertNotIn("step", v["progress"].lower())
        for step in ("settling", "soliciting"):
            fake.event("bt_pair_progress", fake.listening, step=step, detail="raw detail with 0x43 and step ids")
        rig.settle_for(0.2)
        v = pair_view(rig)
        self.assertNotIn("0x43", v["progress"])
        self.assertNotIn("soliciting", v["progress"])
        fake.event("bt_pair_progress", fake.listening, step="warning", detail="Adapter class is not A/V")
        rig.settle_for(0.2)
        self.assertNotIn("class", pair_view(rig)["progress"].lower())

    def test_events_for_another_operation_are_ignored(self):
        rig, fake = pair_rig(self)
        rig.action(type="pair_start")
        wait_phase(self, rig, "listen")
        for ev in ({"command": "bt_pair_progress", "step": "connecting", "detail": "x", "operation_id": "cli-1"},
                   {"command": "bt_pair_confirm_request", "code": "999999", "operation_id": "cli-1"},
                   {"command": "bt_pair_confirm_request", "code": "999999"},
                   {"command": "bt_pair_result", "success": True, "dual_bond": True, "operation_id": "cli-1"}):
            rig.tether.push(ev)
        rig.settle_for(0.3)
        self.assertEqual(pair_view(rig)["phase"], "listen")

    def test_cancel_at_the_code_declines_and_closes(self):
        rig, fake = pair_rig(self)
        to_confirm(self, rig, fake)
        rig.action(type="pair_close")
        self.assertTrue(rig.pump(lambda: fake.commands("confirm"), 2))
        self.assertEqual(fake.commands("confirm")[0][2], False)
        self.assertTrue(rig.pump(lambda: pair_view(rig) is None, 2))

    def test_visible_time_runs_out_then_make_visible_again(self):
        rig, fake = pair_rig(self)
        rig.action(type="pair_start")
        wait_phase(self, rig, "listen")
        rig.pump(lambda: fake.listening, 2)
        first = fake.listening
        rig.action(type="pair_listen_again")        # still visible: nothing happens
        rig.settle_for(0.2)
        self.assertEqual(len(fake.commands("listen")), 1)
        fake.listen_expires()
        wait_phase(self, rig, "listen")
        self.assertTrue(rig.pump(lambda: pair_view(rig)["expired"], 2))
        rig.action(type="pair_listen_again")
        self.assertTrue(rig.pump(lambda: len(fake.commands("listen")) == 2, 2))
        self.assertNotEqual(fake.commands("listen")[1][1], first)
        self.assertTrue(rig.pump(lambda: pair_view(rig)["expired"] is False, 2))

    def test_only_one_pairing_at_a_time(self):
        rig, fake = pair_rig(self)
        to_confirm(self, rig, fake)
        rig.action(type="pair_start")
        rig.action(type="pair_other")
        rig.action(type="pair_search")
        rig.action(type="pair_pick", address=P.PHONE_ADDR)
        rig.settle_for(0.3)
        self.assertEqual(pair_view(rig)["phase"], "confirm")
        self.assertEqual(len(fake.commands("listen")), 1)
        self.assertEqual(fake.commands("pair"), [])

    def test_state_survives_the_page_reloading_and_the_tab_closing(self):
        rig, fake = pair_rig(self)
        to_confirm(self, rig, fake)
        rig.dev.reload("page1")          # Steam reloaded its page: the menu is re-injected and gets the state again
        before = len(rig.dev.pages["page1"]["states"])
        self.assertTrue(rig.pump(lambda: len(rig.dev.pages["page1"]["states"]) > before, 4))
        v = pair_view(rig)
        self.assertEqual((v["phase"], v["code"]), ("confirm", "123456"))

    def test_engine_restart_during_pairing_fails_cleanly(self):
        rig, fake = pair_rig(self)
        to_confirm(self, rig, fake)
        rig.tether.drop()
        v = wait_phase(self, rig, "failed")
        self.assertEqual(v["kind"], "engine_lost")


class ResultTests(unittest.TestCase):
    def finish(self, outcome, **kw):
        rig, fake = pair_rig(self, outcome, **kw)
        rig.action(type="pair_start")
        wait_phase(self, rig, "listen")
        rig.pump(lambda: fake.listening, 2)
        fake.phone_taps()
        return rig, fake

    def accept(self, rig):
        v = wait_phase(self, rig, "confirm")
        rig.action(type="pair_confirm", op=v["op"], accept=True)

    def test_success_without_the_le_half_explains_and_offers_try_again(self):
        rig, fake = self.finish("single")
        self.accept(rig)
        wait_phase(self, rig, "weak")
        rig.action(type="pair_retry")          # unpair our side, then listen again with the default strategy
        self.assertTrue(rig.pump(lambda: fake.commands("unpair") and len(fake.commands("listen")) == 2, 3))
        self.assertEqual(fake.commands("unpair")[0][2], P.PHONE_ADDR)
        self.assertEqual(fake.commands("pair"), [])
        self.assertTrue(all(c[2] in (120,) for c in fake.commands("listen")))
        wait_phase(self, rig, "listen")

    def test_keep_without_notifications(self):
        rig, fake = self.finish("single")
        self.accept(rig)
        wait_phase(self, rig, "weak")
        rig.action(type="pair_keep")
        v = wait_phase(self, rig, "almost")
        self.assertTrue(v["weak"])
        notif = next(i for i in v["items"] if i["id"] == "notifications")
        self.assertFalse(notif["essential"])

    def test_checklist_follows_the_connection(self):
        rig, fake = self.finish("success")
        self.accept(rig)
        wait_phase(self, rig, "almost")
        h = rig.helper
        h.conn = dict(h.conn, ancs_ready=False, map_open=False, map_error="forbidden", pbap_open=False,
                      pbap_error="forbidden")
        h.dirty()
        self.assertTrue(rig.pump(lambda: not pair_view(rig)["ready"], 2))
        items = {i["id"]: i for i in pair_view(rig)["items"]}
        self.assertEqual(items["messages"]["state"], "off")
        self.assertEqual(items["contacts"]["state"], "off")
        self.assertEqual(items["notifications"]["state"], "waiting")
        h.conn = dict(h.conn, ancs_ready=True, map_open=True, map_error="none")
        h.dirty()
        self.assertTrue(rig.pump(lambda: pair_view(rig)["ready"], 2))

    def test_checklist_without_message_access_lists_only_switches_that_exist(self):
        # No obexd, or an iPhone that never offers Message Access: its Messages/Contacts/Favorites/Recents
        # switches are not on the phone, so the list must not send anyone looking for them.
        rig, fake = self.finish("success")
        self.accept(rig)
        wait_phase(self, rig, "almost")
        h = rig.helper
        h.conn = dict(h.conn, ancs_ready=True, map_open=False, map_error="no_daemon", pbap_open=False,
                      pbap_error="no_daemon")
        h.dirty()
        self.assertTrue(rig.pump(lambda: [i["id"] for i in pair_view(rig)["items"]] == ["notifications", "messages"], 2))
        items = {i["id"]: i for i in pair_view(rig)["items"]}
        self.assertEqual(items["notifications"]["label"], "Share System Notifications")
        self.assertFalse(items["messages"]["essential"])
        self.assertNotIn("Sync Contacts", items["messages"]["note"])
        self.assertTrue(pair_view(rig)["ready"])

    def test_stale_bond_clears_our_side_and_says_what_to_do_on_the_iphone(self):
        for outcome in ("stale", "key_missing"):
            with self.subTest(outcome):
                rig, fake = self.finish(outcome)
                v = wait_phase(self, rig, "failed")
                self.assertEqual(v["kind"], "stale")
                self.assertIn("Forget This Device", v["text"])
                self.assertIn("My Devices", v["text"])
                self.assertEqual(v["button"], "Try again")
                self.assertTrue(rig.pump(lambda: fake.commands("unpair"), 2))
                self.assertEqual(fake.commands("unpair")[0][2], P.PHONE_ADDR)
                rig.action(type="pair_retry")
                self.assertTrue(rig.pump(lambda: len(fake.commands("listen")) == 2, 2))
                self.assertEqual(len(fake.commands("unpair")), 1)       # not repeated
                rig.close()
                rig.helper.pairing.clear()

    def test_each_failure_has_plain_words_and_one_button(self):
        cases = {"refused": ("declined_phone", "The iPhone said no"),
                 "timeout": ("timeout", "did not finish pairing in time"),
                 "agent": ("agent", "busy with another program")}
        for outcome, (kind, words) in cases.items():
            with self.subTest(outcome):
                rig, fake = self.finish(outcome)
                v = wait_phase(self, rig, "failed")
                self.assertEqual(v["kind"], kind)
                self.assertIn(words, v["title"] + v["text"])
                for raw in ("0x", "br-connection", "org.bluez", "BlueZ", "ANCS", "operation"):
                    self.assertNotIn(raw, v["title"] + v["text"])
                self.assertEqual(v["button"], "Try again")
                rig.action(type="pair_retry")
                self.assertTrue(rig.pump(lambda: len(fake.commands("listen")) == 2, 2))
                rig.close()
                rig.helper.pairing.clear()

    def test_code_not_confirmed_here_maps_to_a_retry(self):
        rig, fake = pair_rig(self)
        v = to_confirm(self, rig, fake)
        rig.action(type="pair_confirm", op=v["op"], accept=False)       # a decline that is not a Close
        v = wait_phase(self, rig, "failed")
        self.assertEqual(v["kind"], "declined_here")
        self.assertIn("Confirm here", v["text"])

    def test_busy_waits_and_retries_by_itself(self):
        rig, fake = self.finish("busy_then_success", busy=2)
        v = wait_phase(self, rig, "pairing")
        self.assertIn("Another pairing", v["progress"])
        for n in (2, 3):             # each retry is a fresh listen window; the phone taps again each time
            self.assertTrue(rig.pump(lambda: len(fake.commands("listen")) == n, 3))
            rig.pump(lambda: pair_view(rig)["phase"] == "listen", 2)
            fake.phone_taps()
        v = wait_phase(self, rig, "confirm")
        rig.action(type="pair_confirm", op=v["op"], accept=True)
        wait_phase(self, rig, "almost")

    def test_busy_forever_ends_in_a_try_again_screen(self):
        rig, fake = self.finish("busy_then_success", busy=99)
        rig.helper.pairing.BUSY_TRIES = 2
        for _ in range(3):
            rig.pump(lambda: False, 0.15)
            if fake.listening and pair_view(rig) and pair_view(rig)["phase"] == "listen":
                fake.phone_taps()
        v = wait_phase(self, rig, "failed", 6)
        self.assertEqual(v["kind"], "busy")


class FallbackTests(unittest.TestCase):
    def test_silent_daemon_falls_back_to_search(self):
        rig, fake = pair_rig(self, listen=False, supports=False)
        rig.action(type="pair_start")
        wait_phase(self, rig, "listen")
        v = wait_phase(self, rig, "search")
        self.assertTrue(v["fallback"])
        self.assertFalse(v["can_listen"])
        self.assertEqual(v["devices"], [])
        self.assertFalse(v["searched"])
        self.assertEqual(rig.tether.commands("bt_scan"), [])      # waits for the user's Search tap

    def test_error_reply_falls_back_at_once(self):
        rig, fake = pair_rig(self, listen="error", supports=False)
        rig.action(type="pair_start")
        v = wait_phase(self, rig, "search", 2)
        self.assertTrue(v["fallback"])

    def test_announced_support_does_not_fall_back(self):
        rig, fake = pair_rig(self, listen=False, supports=True)
        rig.action(type="pair_start")
        rig.settle_for(0.6)
        self.assertEqual(pair_view(rig)["phase"], "listen")

    def test_second_attempt_skips_the_listen_probe(self):
        rig, fake = pair_rig(self, listen=False, supports=False)
        rig.action(type="pair_start")
        wait_phase(self, rig, "search")
        rig.action(type="pair_close")
        rig.pump(lambda: pair_view(rig) is None, 2)
        rig.action(type="pair_start")
        self.assertEqual(wait_phase(self, rig, "search")["fallback"], True)
        self.assertEqual(len(fake.commands("listen")), 1)

    def test_search_lists_iphones_first_and_pairs_the_one_tapped(self):
        devices = [P.phone("Speaker", "00:00:00:00:00:09", iphone=False, apple_nearby=False),
                   P.phone("Pat's iPhone"),
                   P.phone("", "00:00:00:00:00:0A", iphone=False, apple_nearby=False),   # no name: hidden
                   P.phone("Buds", "00:00:00:00:00:0B", airpods=True, iphone=False, apple_nearby=False)]
        rig, fake = pair_rig(self, listen=False, supports=False, devices=devices)
        rig.action(type="pair_start")
        wait_phase(self, rig, "search")
        rig.action(type="pair_search")
        v = wait_phase(self, rig, "search")
        self.assertTrue(rig.pump(lambda: pair_view(rig)["devices"] and not pair_view(rig)["searching"], 3))
        names = [d["name"] for d in pair_view(rig)["devices"]]
        self.assertEqual(names, ["Pat's iPhone", "Speaker"])
        # an address that was not listed is refused, whatever the page says
        rig.action(type="pair_pick", address="DE:AD:BE:EF:00:01")
        rig.action(type="pair_pick", address="not an address")
        rig.settle_for(0.2)
        self.assertEqual(fake.commands("pair"), [])
        rig.action(type="pair_pick", address=P.PHONE_ADDR.lower())
        v = wait_phase(self, rig, "confirm")
        pair = fake.commands("pair")[0]
        self.assertEqual((pair[2], pair[1].startswith("earshot-pair-")), (P.PHONE_ADDR, True))
        rig.action(type="pair_confirm", op=v["op"], accept=True)
        wait_phase(self, rig, "almost")

    def test_search_repeats_by_itself_then_gives_up_with_a_hint(self):
        rig, fake = pair_rig(self, listen=False, supports=False, scans=[[], [], []])
        rig.action(type="pair_start")
        wait_phase(self, rig, "search")
        rig.action(type="pair_search")
        wait_phase(self, rig, "search")
        self.assertTrue(rig.pump(lambda: pair_view(rig)["message"], 4))
        self.assertIn("Settings > Bluetooth", pair_view(rig)["message"])
        self.assertEqual(len(fake.commands("scan")), 3)

    def test_search_stops_repeating_once_something_shows(self):
        rig, fake = pair_rig(self, listen=False, supports=False, scans=[[], [P.phone()]])
        rig.action(type="pair_start")
        wait_phase(self, rig, "search")
        rig.action(type="pair_search")
        self.assertTrue(rig.pump(lambda: pair_view(rig)["devices"] and not pair_view(rig)["searching"], 3))
        rig.settle_for(0.2)
        self.assertEqual(len(fake.commands("scan")), 2)

    def test_not_found_goes_back_to_searching(self):
        rig, fake = pair_rig(self, "not_found", listen=False, supports=False)
        rig.action(type="pair_start")
        wait_phase(self, rig, "search")
        rig.action(type="pair_search")
        rig.pump(lambda: pair_view(rig)["devices"], 3)
        rig.action(type="pair_pick", address=P.PHONE_ADDR)
        v = wait_phase(self, rig, "failed")
        self.assertEqual((v["kind"], v["button"]), ("not_found", "Search again"))
        rig.action(type="pair_retry")
        self.assertTrue(rig.pump(lambda: len(fake.commands("scan")) >= 2, 3))

    def test_a_phone_already_paired_in_steam_is_redone_not_hidden(self):
        # earlier installs told people to pair in Steam's own settings: that bond has no LE half
        old = P.phone("Pat's iPhone", paired=True, bonded=True)
        rig, fake = pair_rig(self, listen=False, supports=False, devices=[old])
        rig.action(type="pair_start")
        wait_phase(self, rig, "search")
        rig.action(type="pair_search")
        rig.pump(lambda: pair_view(rig)["devices"], 3)
        self.assertTrue(pair_view(rig)["devices"][0]["redo"])
        rig.action(type="pair_pick", address=P.PHONE_ADDR)
        self.assertTrue(rig.pump(lambda: fake.commands("pair"), 3))
        self.assertEqual([c[0] for c in fake.log if c[0] in ("unpair", "pair")], ["unpair", "pair"])

    def test_names_from_the_phone_are_trimmed(self):
        rig, fake = pair_rig(self, listen=False, supports=False,
                             devices=[P.phone("<img src=x onerror=1>" + "x" * 100)])
        rig.action(type="pair_start")
        wait_phase(self, rig, "search")
        rig.action(type="pair_search")
        rig.pump(lambda: pair_view(rig)["devices"], 3)
        self.assertLessEqual(len(pair_view(rig)["devices"][0]["name"]), 40)


class ForgetTests(unittest.TestCase):
    def paired_rig(self):
        rig = Rig(self)
        fake = P(rig.tether, devices=[P.phone("Test Phone", "AA:BB:CC:DD:EE:FF", paired=True, bonded=True)])
        rig.start()
        return rig, fake

    def test_forget_asks_first_then_clears_our_side(self):
        rig, fake = self.paired_rig()
        self.assertTrue(rig.pump(lambda: rig.state()["phone"]["paired"], 3))
        rig.action(type="forget_do")             # no question asked yet: ignored
        rig.settle_for(0.2)
        self.assertEqual(fake.commands("unpair"), [])
        rig.action(type="forget_ask")
        wait_phase(self, rig, "forget")
        rig.action(type="forget_do")
        wait_phase(self, rig, "forgotten")
        self.assertEqual(fake.commands("unpair")[0][2], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(fake.commands("set_device")[0][2], "")
        rig.action(type="pair_done")
        self.assertTrue(rig.pump(lambda: pair_view(rig) is None, 2))
        self.assertTrue(rig.pump(lambda: not rig.state()["phone"]["paired"], 2))

    def test_keeping_the_phone_closes_the_question(self):
        rig, fake = self.paired_rig()
        rig.action(type="forget_ask")
        wait_phase(self, rig, "forget")
        rig.action(type="pair_close")
        self.assertTrue(rig.pump(lambda: pair_view(rig) is None, 2))
        self.assertEqual(fake.commands("unpair"), [])

    def test_pair_a_different_iphone_starts_from_settings(self):
        rig, fake = self.paired_rig()
        rig.helper.bt_status = dict(rig.helper.bt_status, capability=dict(NO_PHONE["capability"]))
        rig.action(type="pair_start")
        wait_phase(self, rig, "listen")


class ClassifyTests(unittest.TestCase):
    def test_mapping(self):
        c = H.classify_pair_failure
        self.assertEqual(c("busy", "Another pairing attempt is already in progress."), "busy")
        self.assertEqual(c("not_found", "x"), "not_found")
        self.assertEqual(c("timeout", "Pairing failed: br-connection-key-missing"), "stale")
        self.assertEqual(c("timeout", "Pairing failed: org.bluez.Error.AuthenticationFailed"), "stale")
        self.assertEqual(c("timeout", "The iPhone refused the connection before pairing started: x"), "stale")
        self.assertEqual(c("rejected", "Pairing was not confirmed on this computer."), "declined_here")
        self.assertEqual(c("rejected", "The iPhone declined the pairing request."), "declined_phone")
        self.assertEqual(c("timeout", "Pairing did not complete. Confirm the prompt on the iPhone."), "timeout")
        self.assertEqual(c("timeout", "Pairing failed: org.bluez.Error.AuthenticationRejected"), "declined_phone")
        self.assertEqual(c("error", "Could not register a Bluetooth pairing agent."), "agent")
        self.assertEqual(c("error", "Bluetooth is unavailable."), "unavailable")
        self.assertEqual(c("error", "something new"), "other")
        for kind in H.FAILURES:
            title, text, button = H.FAILURES[kind]
            self.assertTrue(title and text and button)


if __name__ == "__main__":
    unittest.main()
