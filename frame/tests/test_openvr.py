"""Tests for the OpenVR pop-up path in frame/menu/earshot-steamui.py (OpenVrNotifier, Popups).
Standard library only. Run from the repository root:  python -m unittest discover -s frame/tests -v

FakeOpenVR builds real ctypes function tables, so the notifier's struct layout and call signatures are
exercised; what only the headset can show is whether SteamVR accepts them.
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import FakeOpenVR, load_helper, wait_for  # noqa: E402

H = load_helper()
LIB = "/fake/libopenvr_api.so"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def notifier(vr, clock=None, **kwargs):
    return H.OpenVrNotifier(paths=("/missing/libopenvr_api.so", LIB), loader=vr.loader, clock=clock or Clock(), **kwargs)


class OpenVrNotifierTests(unittest.TestCase):
    def test_shows_a_transient_pop_up_from_our_own_overlay(self):
        vr = FakeOpenVR()
        n = notifier(vr)
        self.assertTrue(n.notify("Chat: Hi - there"))
        self.assertEqual(vr.inits, [3])                                           # VRApplication_Background
        self.assertEqual(vr.created, [("earshot.notifications", "Earshot")])
        self.assertEqual(vr.notifications, [(FakeOpenVR.OVERLAY_HANDLE, 0, 0, "Chat: Hi - there", 100, None)])

    def test_one_session_and_one_overlay_serve_every_pop_up(self):
        vr = FakeOpenVR()
        n = notifier(vr)
        n.notify("a")
        n.notify("b")
        self.assertEqual(vr.texts(), ["a", "b"])
        self.assertEqual((len(vr.inits), len(vr.created), vr.shutdowns), (1, 1, 0))

    def test_text_is_utf8_and_a_nul_cannot_cut_it_short(self):
        vr = FakeOpenVR()
        notifier(vr).notify("Café\0 – x")
        self.assertEqual(vr.texts(), ["Café – x"])

    def test_uses_the_newest_overlay_interface_the_runtime_knows(self):
        vr = FakeOpenVR(overlay_versions=("IVROverlay_028", "IVROverlay_027"))
        notifier(vr).notify("x")
        self.assertEqual([a for a in vr.asked if "Overlay" in a], ["FnTable:IVROverlay_028"])
        vr = FakeOpenVR(overlay_versions=("IVROverlay_026",))
        self.assertTrue(notifier(vr).notify("x"))
        self.assertEqual([a for a in vr.asked if "Overlay" in a],
                         ["FnTable:IVROverlay_028", "FnTable:IVROverlay_027", "FnTable:IVROverlay_026"])
        self.assertIn("FnTable:IVRNotifications_002", vr.asked)

    def test_no_known_overlay_interface_is_a_failure_and_the_session_is_released(self):
        vr = FakeOpenVR(overlay_versions=())
        with self.assertRaises(H.OpenVrError):
            notifier(vr).notify("x")
        self.assertEqual(vr.shutdowns, 1)

    def test_an_overlay_that_already_exists_is_found_not_recreated(self):
        vr = FakeOpenVR(key_in_use=True)
        self.assertTrue(notifier(vr).notify("x"))
        self.assertEqual(vr.found, ["earshot.notifications"])
        self.assertEqual(vr.notifications[0][0], FakeOpenVR.OVERLAY_HANDLE)

    def test_missing_library_raises_so_the_caller_can_fall_back(self):
        n = H.OpenVrNotifier(paths=("/missing/a.so", "/missing/b.so"), loader=FakeOpenVR().loader)
        with self.assertRaises(H.OpenVrError):
            n.notify("x")

    def test_steamvr_not_running_raises_and_the_next_pop_up_tries_again(self):
        vr = FakeOpenVR(init_error=121)       # VRInitError_Init_NoServerForBackgroundApp
        n = notifier(vr)
        with self.assertRaises(H.OpenVrError):
            n.notify("x")
        self.assertEqual(vr.shutdowns, 0)     # nothing was opened
        vr.init_error = 0                     # SteamVR started
        self.assertTrue(n.notify("y"))
        self.assertEqual(vr.texts(), ["y"])
        self.assertEqual(len(vr.inits), 2)

    def test_a_dead_session_is_shut_down_and_re_initialised_on_the_same_pop_up(self):
        vr = FakeOpenVR()
        n = notifier(vr)
        n.notify("before")
        vr.fail_notifications = 1             # SteamVR restarted: the old connection is stale
        self.assertTrue(n.notify("after"))
        self.assertEqual(vr.texts(), ["before", "after"])
        self.assertEqual((len(vr.inits), vr.shutdowns, len(vr.created)), (2, 1, 2))

    def test_a_fresh_session_that_fails_too_raises_and_leaves_nothing_open(self):
        vr = FakeOpenVR()
        n = notifier(vr)
        n.notify("before")
        vr.fail_notifications = 2
        with self.assertRaises(H.OpenVrError):
            n.notify("lost")
        self.assertEqual((len(vr.inits), vr.shutdowns), (2, 2))
        self.assertTrue(n.notify("later"))    # the next pop-up starts over
        self.assertEqual(len(vr.inits), 3)

    def test_the_session_is_released_when_idle_and_on_close(self):
        vr = FakeOpenVR()
        clock = Clock()
        n = notifier(vr, clock, idle_close=30.0)
        n.notify("x")
        clock.now += 10
        n.idle_tick()
        self.assertEqual(vr.shutdowns, 0)
        clock.now += 25
        n.idle_tick()
        self.assertEqual(vr.shutdowns, 1)
        n.notify("y")
        self.assertEqual(len(vr.inits), 2)
        n.close()
        self.assertEqual(vr.shutdowns, 2)
        n.close()                              # nothing open: no second shutdown
        self.assertEqual(vr.shutdowns, 2)


class PopupsTests(unittest.TestCase):
    def setUp(self):
        self.loop = H.Loop()
        self.addCleanup(self.loop.close)

    def test_failure_on_the_worker_thread_reaches_the_fallback_on_the_loop_thread(self):
        failed = []
        main = threading.get_ident()

        class Backend:
            def notify(self, text):
                raise RuntimeError("down")

            def idle_tick(self):
                pass

            def close(self):
                pass

        p = H.Popups(self.loop, Backend(), lambda t: failed.append((t, threading.get_ident())))
        self.addCleanup(p.close)
        p.send("hello")
        self.assertTrue(wait_for(lambda: self._pump(lambda: failed)))
        self.assertEqual(failed, [("hello", main)])

    def test_a_worker_stuck_in_a_call_is_bypassed(self):
        release = threading.Event()
        failed = []
        clock = Clock()

        class Backend:
            def notify(self, text):
                release.wait(5)
                return True

            def idle_tick(self):
                pass

            def close(self):
                pass

        p = H.Popups(self.loop, Backend(), failed.append, stuck_after=15.0, clock=clock)
        self.addCleanup(p.close)
        self.addCleanup(release.set)
        p.send("first")
        self.assertTrue(wait_for(lambda: p.busy_since is not None))
        clock.now += 16
        p.send("second")                      # the worker has been in one call for 16 s
        self.assertEqual(failed, ["second"])

    def test_idle_worker_lets_the_backend_release_its_session(self):
        ticks = []

        class Backend:
            def notify(self, text):
                return True

            def idle_tick(self):
                ticks.append(1)

            def close(self):
                pass

        p = H.Popups(self.loop, Backend(), lambda t: None, idle_poll=0.02)
        self.addCleanup(p.close)
        p.send("x")
        self.assertTrue(wait_for(lambda: ticks))

    def _pump(self, predicate):
        self.loop.run_once(0.02)
        return bool(predicate())


if __name__ == "__main__":
    unittest.main()
