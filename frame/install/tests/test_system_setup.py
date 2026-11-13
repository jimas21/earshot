"""Tests for earshot-system-setup.py: the decisions, the manifest and the undo, all
against a fake root folder and a fake systemctl, so no root and no BlueZ are needed:
    python3 -m unittest discover -s frame/install/tests -v
"""

import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "ess", os.path.join(os.path.dirname(_HERE), "earshot-system-setup.py"))
ess = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ess)

BLUETOOTH_SERVICE = """[Unit]
Description=Bluetooth service

[Service]
Type=dbus
BusName=org.bluez
ExecStart=/usr/lib/bluetooth/bluetoothd
"""


class FakeRun:
    """Records commands; answers `bluetoothd --version`."""

    def __init__(self, version="5.86", fail=()):
        self.calls = []
        self.version = version
        self.fail = fail

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if cmd[-1] == "--version":
            return 0, "%s\n" % self.version
        if any(f in cmd for f in self.fail):
            return 1, "failed"
        return 0, ""

    def verbs(self):
        return [" ".join(c[:3]) for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def put(self, rel, text="", mode=None):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def read(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read()

    def exists(self, rel):
        return os.path.exists(os.path.join(self.root, rel))

    def stock_steamos(self, ertm="Y"):
        """A SteamOS-like root: stock bluetooth.service, hci0, and the ERTM switch."""
        self.put("usr/lib/systemd/system/bluetooth.service", BLUETOOTH_SERVICE)
        self.put("usr/lib/bluetooth/bluetoothd", "")
        self.put("sys/class/bluetooth/hci0/x", "")
        if ertm:
            self.put(ess.ERTM_PARAM_REL, ertm + "\n")

    def inspect(self, run=None, btmgmt=True):
        return ess.inspect(self.root, run=run or FakeRun(), which=lambda n: "/usr/bin/" + n if btmgmt else None)


class ExecStartTests(Base):
    def test_vendor_unit_only(self):
        self.stock_steamos()
        self.assertEqual(ess.effective_execstart(self.root), "/usr/lib/bluetooth/bluetoothd")

    def test_dropin_resets_and_replaces(self):
        self.stock_steamos()
        self.put("etc/systemd/system/bluetooth.service.d/a.conf",
                 "[Service]\nExecStart=\nExecStart=/usr/lib/bluetooth/bluetoothd -d\n")
        self.assertEqual(ess.effective_execstart(self.root), "/usr/lib/bluetooth/bluetoothd -d")

    def test_empty_reset_without_replacement_is_unknown(self):
        self.stock_steamos()
        self.put("etc/systemd/system/bluetooth.service.d/a.conf", "[Service]\nExecStart=\n")
        self.assertIsNone(ess.effective_execstart(self.root))

    def test_later_dropin_wins(self):
        self.stock_steamos()
        self.put("etc/systemd/system/bluetooth.service.d/10-a.conf", "[Service]\nExecStart=\nExecStart=/a\n")
        self.put("etc/systemd/system/bluetooth.service.d/20-b.conf", "[Service]\nExecStart=\nExecStart=/b\n")
        self.assertEqual(ess.effective_execstart(self.root), "/b")

    def test_dropin_text_keeps_the_existing_flags(self):
        text = ess.dropin_text("/usr/lib/bluetooth/bluetoothd -n", "/usr/lib/bluetooth/bluetoothd")
        self.assertEqual(text, "[Service]\nExecStart=\nExecStart=/usr/lib/bluetooth/bluetoothd -n --experimental\n")
        with self.assertRaises(ess.SetupError):
            ess.dropin_text(None, None)


class PlanTests(Base):
    def test_stock_steamos_needs_all_three(self):
        self.stock_steamos()
        changes, blockers, warnings = ess.plan(self.inspect())
        self.assertEqual([c["id"] for c in changes], ["experimental", "btclass", "ertm"])
        self.assertEqual((blockers, warnings), ([], []))
        self.assertEqual(ess.exit_code_for(changes, blockers), 10)

    def test_everything_in_place_means_nothing_to_do(self):
        self.stock_steamos(ertm="N")
        self.put("etc/systemd/system/bluetooth.service.d/x.conf",
                 "[Service]\nExecStart=\nExecStart=/usr/lib/bluetooth/bluetoothd --experimental\n")
        self.put("etc/systemd/system/bluetooth.service.wants/earshot-btclass@hci0.service", "")
        changes, blockers, _ = ess.plan(self.inspect())
        self.assertEqual((changes, blockers), ([], []))
        self.assertEqual(ess.exit_code_for(changes, blockers), 0)
        self.assertIn("Nothing to change", ess.render_plan(changes, blockers, []))

    def test_someone_elses_class_unit_counts(self):
        self.stock_steamos(ertm="N")
        self.put("etc/systemd/system/bluetooth.service.wants/tether-btclass@hci0.service", "")
        ids = [c["id"] for c in ess.plan(self.inspect())[0]]
        self.assertNotIn("btclass", ids)

    def test_main_conf_experimental_counts(self):
        self.stock_steamos(ertm="N")
        self.put("etc/bluetooth/main.conf", "[General]\nExperimental = true\n")
        ids = [c["id"] for c in ess.plan(self.inspect())[0]]
        self.assertNotIn("experimental", ids)

    def test_ertm_already_on_or_absent_is_left_alone(self):
        self.stock_steamos(ertm="N")
        self.assertNotIn("ertm", [c["id"] for c in ess.plan(self.inspect())[0]])
        shutil.rmtree(os.path.join(self.root, "sys/module"))
        self.assertNotIn("ertm", [c["id"] for c in ess.plan(self.inspect())[0]])

    def test_ertm_fix_already_enabled_is_not_repeated(self):
        self.stock_steamos(ertm="Y")
        self.put("etc/systemd/system/bluetooth.service.wants/earshot-ertm.service", "")
        self.assertNotIn("ertm", [c["id"] for c in ess.plan(self.inspect())[0]])

    def test_missing_btmgmt_blocks_only_the_class_change(self):
        self.stock_steamos(ertm="N")
        changes, blockers, _ = ess.plan(self.inspect(btmgmt=False))
        self.assertEqual([c["id"] for c in changes], ["experimental"])
        self.assertEqual(len(blockers), 1)
        self.assertIn("btmgmt", blockers[0])

    def test_only_blockers_exit_code(self):
        self.stock_steamos(ertm="N")
        self.put("etc/systemd/system/bluetooth.service.d/x.conf",
                 "[Service]\nExecStart=\nExecStart=/usr/lib/bluetooth/bluetoothd --experimental\n")
        changes, blockers, _ = ess.plan(self.inspect(btmgmt=False))
        self.assertEqual(ess.exit_code_for(changes, blockers), 11)

    def test_old_bluez_warns_but_does_not_block(self):
        self.stock_steamos()
        changes, blockers, warnings = ess.plan(self.inspect(run=FakeRun(version="5.72")))
        self.assertEqual(blockers, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("5.72", warnings[0])
        self.assertTrue(changes)

    def test_new_enough_bluez_has_no_warning(self):
        self.stock_steamos()
        self.assertEqual(ess.plan(self.inspect(run=FakeRun(version="5.87")))[2], [])

    def test_no_bluez_at_all_is_a_blocker(self):
        _, blockers, _ = ess.plan(self.inspect())
        self.assertTrue(any("bluetoothd" in b for b in blockers))

    def test_plan_text_lists_each_change(self):
        self.stock_steamos()
        text = ess.render_plan(*ess.plan(self.inspect()))
        for needle in ("--experimental", "A/V Hands-Free", "ERTM", "manifest.json"):
            self.assertIn(needle, text)


class ApplyUndoTests(Base):
    def apply_all(self, run=None, state=None):
        self.stock_steamos()
        state = state or self.inspect()
        changes, _, _ = ess.plan(state)
        run = run or FakeRun()
        ess.apply(self.root, changes, state, run=run, say=lambda *_: None)
        return run

    def test_apply_writes_files_enables_units_and_restarts(self):
        run = self.apply_all()
        self.assertIn("[Service]\nExecStart=\nExecStart=/usr/lib/bluetooth/bluetoothd --experimental\n",
                      self.read(ess.DROPIN_REL))
        self.assertIn("btmgmt", self.read(ess.BTCLASS_UNIT_REL))
        self.assertIn("disable_ertm", self.read(ess.ERTM_UNIT_REL))
        self.assertEqual(self.read(ess.ERTM_PARAM_REL), "N")
        verbs = run.verbs()
        self.assertIn("systemctl enable earshot-btclass@hci0.service", verbs)
        self.assertIn("systemctl enable earshot-ertm.service", verbs)
        self.assertIn("systemctl restart bluetooth.service", verbs)
        self.assertLess(verbs.index("systemctl daemon-reload"), verbs.index("systemctl enable earshot-ertm.service"))
        manifest = json.loads(self.read(ess.MANIFEST_REL))
        self.assertEqual(sorted(f["path"] for f in manifest["files"]),
                         sorted(["/" + ess.DROPIN_REL, "/" + ess.BTCLASS_UNIT_REL, "/" + ess.ERTM_UNIT_REL]))
        self.assertEqual(manifest["units"], ["earshot-btclass@hci0.service", "earshot-ertm.service"])
        self.assertTrue(manifest["restarted_bluetooth"])
        self.assertTrue(manifest["ertm_was_disabled"])

    def test_after_apply_the_plan_is_empty(self):
        self.apply_all()
        # the enabled units show up as wants links on a real system; mimic what systemctl enable does
        self.put("etc/systemd/system/bluetooth.service.wants/earshot-btclass@hci0.service", "")
        self.put("etc/systemd/system/bluetooth.service.wants/earshot-ertm.service", "")
        changes, blockers, _ = ess.plan(self.inspect())
        self.assertEqual((changes, blockers), ([], []))

    def test_undo_reverts_exactly_what_apply_did(self):
        self.apply_all()
        other = self.put("etc/systemd/system/other.service", "someone else's")
        run = FakeRun()
        left = ess.undo(self.root, run=run, say=lambda *_: None)
        self.assertEqual(left, [])
        for rel in (ess.DROPIN_REL, ess.BTCLASS_UNIT_REL, ess.ERTM_UNIT_REL, ess.MANIFEST_REL):
            self.assertFalse(self.exists(rel), rel)
        self.assertTrue(os.path.exists(other))
        self.assertEqual(self.read(ess.ERTM_PARAM_REL), "Y")
        verbs = run.verbs()
        self.assertIn("systemctl disable --now", [v[:len("systemctl disable --now")] for v in verbs])
        self.assertIn("systemctl restart bluetooth.service", verbs)
        self.assertFalse(self.exists("etc/earshot"))

    def test_undo_leaves_a_file_changed_since(self):
        self.apply_all()
        with open(os.path.join(self.root, ess.BTCLASS_UNIT_REL), "a") as f:
            f.write("# edited by hand\n")
        left = ess.undo(self.root, run=FakeRun(), say=lambda *_: None)
        self.assertEqual(left, ["/" + ess.BTCLASS_UNIT_REL])
        self.assertTrue(self.exists(ess.BTCLASS_UNIT_REL))
        self.assertFalse(self.exists(ess.DROPIN_REL))

    def test_undo_without_a_manifest_does_nothing(self):
        run = FakeRun()
        self.assertEqual(ess.undo(self.root, run=run, say=lambda *_: None), [])
        self.assertEqual(run.calls, [])

    def test_undo_does_not_restart_bluetooth_when_apply_did_not(self):
        self.stock_steamos(ertm="N")
        self.put("etc/systemd/system/bluetooth.service.d/x.conf",
                 "[Service]\nExecStart=\nExecStart=/usr/lib/bluetooth/bluetoothd --experimental\n")
        state = self.inspect()
        changes, _, _ = ess.plan(state)
        self.assertEqual([c["id"] for c in changes], ["btclass"])
        ess.apply(self.root, changes, state, run=FakeRun(), say=lambda *_: None)
        run = FakeRun()
        ess.undo(self.root, run=run, say=lambda *_: None)
        self.assertNotIn("systemctl restart bluetooth.service", run.verbs())
        self.assertNotIn("ertm", " ".join(run.verbs()))
        self.assertEqual(self.read(ess.ERTM_PARAM_REL).strip(), "N")  # untouched: we never changed it

    def test_failed_enable_stops_with_an_error_and_keeps_a_manifest(self):
        self.stock_steamos()
        state = self.inspect()
        changes, _, _ = ess.plan(state)
        with self.assertRaises(ess.SetupError):
            ess.apply(self.root, changes, state, run=FakeRun(fail=("earshot-ertm.service",)), say=lambda *_: None)
        manifest = json.loads(self.read(ess.MANIFEST_REL))
        self.assertIn("earshot-btclass@hci0.service", manifest["units"])
        self.assertEqual(len(manifest["files"]), 3)  # everything written so far can be undone
        ess.undo(self.root, run=FakeRun(), say=lambda *_: None)
        self.assertFalse(self.exists(ess.ERTM_UNIT_REL))

    def test_nothing_to_apply_writes_nothing(self):
        state = {"experimental": True, "btclass_enabled": True, "btmgmt": True, "adapter": "hci0",
                 "ertm_disabled": False, "ertm_fix_enabled": False}
        run = FakeRun()
        ess.apply(self.root, [], state, run=run, say=lambda *_: None)
        self.assertEqual(run.calls, [])
        self.assertFalse(self.exists(ess.MANIFEST_REL))

    def test_only_earshot_named_files_are_ever_written(self):
        self.apply_all()
        written = []
        for dirpath, _, names in os.walk(os.path.join(self.root, "etc")):
            written += [os.path.join(dirpath, n) for n in names]
        self.assertTrue(written)
        for path in written:
            self.assertTrue("earshot" in path, path)

    def test_damaged_manifest_is_refused(self):
        self.put(ess.MANIFEST_REL, "{not json")
        with self.assertRaises(ess.SetupError):
            ess.load_manifest(self.root)

    def test_undo_plan_lists_the_changes(self):
        self.apply_all()
        text = "\n".join(ess.undo_plan(ess.load_manifest(self.root)))
        for needle in ("90-earshot-experimental.conf", "earshot-btclass@hci0.service", "ERTM", "restart"):
            self.assertIn(needle, text)
        self.assertIn("Nothing to undo", "\n".join(ess.undo_plan(None)))


class CommandLineTests(Base):
    def test_check_exit_codes_and_no_root_needed(self):
        self.stock_steamos()
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(ess.main(["--check", "--root", self.root]), 10)
            self.assertEqual(ess.main(["--plan", "--root", self.root]), 10)

    def test_apply_needs_yes(self):
        self.stock_steamos()
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(ess.main(["--apply", "--root", self.root]), 1)
        self.assertFalse(self.exists(ess.DROPIN_REL))

    def test_apply_as_a_normal_user_is_refused(self):
        if not hasattr(os, "geteuid") or os.geteuid() == 0:
            self.skipTest("needs a normal user on a Unix system")
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(ess.main(["--apply", "--yes"]), 1)

    def test_unit_text_is_systemd_safe(self):
        for text in (ess.BTCLASS_UNIT, ess.ERTM_UNIT):
            self.assertIn("[Install]", text)
            self.assertIn("WantedBy=bluetooth.service", text)
            self.assertNotIn("$a", text.replace("$$a", ""))  # a bare $a would be eaten by systemd
        self.assertIn("\\\n", ess.BTCLASS_UNIT)  # continuation lines kept


if __name__ == "__main__":
    unittest.main()
