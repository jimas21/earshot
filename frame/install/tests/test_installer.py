"""Offline tests for earshot-frame-install.py (standard library only):
    python3 -m unittest discover -s frame/install/tests -v
"""

import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_INSTALL = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location("ei", os.path.join(_INSTALL, "earshot-frame-install.py"))
ei = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ei)

# RFC 8032, section 7.1, tests 1 and 2.
VECTORS = [
    (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555"
        "fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")


def _compress(pt):
    zi = pow(pt[2], ei._P - 2, ei._P)
    x, y = pt[0] * zi % ei._P, pt[1] * zi % ei._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def sign(seed, msg):
    """RFC 8032 signing, built on the installer's own curve code, so tests can make
    signatures without any third-party package."""
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    pub = _compress(ei._mul(a, ei._G))
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % ei._L
    big_r = _compress(ei._mul(r, ei._G))
    k = int.from_bytes(hashlib.sha512(big_r + pub + msg).digest(), "little") % ei._L
    return pub, big_r + ((r + k * a) % ei._L).to_bytes(32, "little")


class SignatureTests(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for pub, msg, sig in VECTORS:
            pub, msg, sig = bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig)
            self.assertTrue(ei.verify_signature(pub, msg, sig, force_pure=True))
            self.assertTrue(ei.verify_signature(pub, msg, sig))
            self.assertFalse(ei.verify_signature(pub, msg + b"x", sig, force_pure=True))
            for i in (5, 40):
                bad = bytearray(sig)
                bad[i] ^= 1
                self.assertFalse(ei.verify_signature(pub, msg, bytes(bad), force_pure=True))

    def test_wrong_lengths(self):
        pub, msg, sig = (bytes.fromhex(x) for x in VECTORS[0])
        self.assertFalse(ei.verify_signature(pub[:31], msg, sig, force_pure=True))
        self.assertFalse(ei.verify_signature(pub, msg, sig[:63], force_pure=True))

    def test_test_signer_matches_the_rfc(self):
        pub, sig = sign(SEED, b"")
        self.assertEqual(pub.hex(), VECTORS[0][0])
        self.assertEqual(sig.hex(), VECTORS[0][2])

    def _component(self, pub):
        return {"asset": "earshot-engine-{ver}-aarch64.tar.gz", "pubkey": base64.b64encode(pub).decode()}

    def test_check_signature_accepts_and_rejects(self):
        pub, sig = sign(SEED, b"release bytes")
        comp = self._component(pub)
        sig_text = base64.b64encode(sig).decode() + "\n"
        ei.check_signature(comp, b"release bytes", sig_text)  # no exception
        with self.assertRaises(ei.InstallError):
            ei.check_signature(comp, b"release bytes!", sig_text)
        with self.assertRaises(ei.InstallError):
            ei.check_signature(comp, b"release bytes", "not base64 !!")
        other_pub, _ = sign(bytes(32), b"")
        with self.assertRaises(ei.InstallError):
            ei.check_signature(self._component(other_pub), b"release bytes", sig_text)

    def test_unpinned_key_fails_closed(self):
        # The shipped installer has no key until the maintainer pins one: it must refuse.
        for comp in ei.COMPONENTS:
            if not comp["pubkey"]:
                with self.assertRaises(ei.InstallError) as cm:
                    ei.check_signature(comp, b"x", base64.b64encode(b"\0" * 64).decode())
                self.assertIn("no release key", str(cm.exception))
        with self.assertRaises(ei.InstallError):
            ei.check_signature({"asset": "a", "pubkey": ""}, b"x", "AAAA")

    def test_release_key_is_pinned(self):
        # A raw 32-byte ed25519 public key, the same one the release workflow checks against.
        key = base64.b64decode(ei.PUBKEY, validate=True)
        self.assertEqual(len(key), 32)
        with open(os.path.join(_INSTALL, "..", "..", ".github", "workflows", "earshot-arm64.yml"), encoding="utf-8") as f:
            self.assertIn(f"EARSHOT_RELEASE_PUBKEY: '{ei.PUBKEY}'", f.read())
        self.assertTrue(all(c["pubkey"] == ei.PUBKEY for c in ei.COMPONENTS))


class VersionTests(unittest.TestCase):
    def test_version_key_orders_numbers(self):
        self.assertLess(ei.version_key("0.3.5-frame.2"), ei.version_key("0.3.5-frame.10"))
        self.assertLess(ei.version_key("v0.9.0"), ei.version_key("v0.10.0"))
        self.assertEqual(ei.version_key(""), ())
        self.assertEqual(ei.version_key(None), ())

    def test_prereleases_sort_before_the_release(self):
        k = ei.version_key
        self.assertLess(k("1.0.0-rc.1"), k("1.0.0"))
        self.assertLess(k("v1.0.0-rc.1"), k("v1.0.0"))
        self.assertLess(k("1.0.0-rc.1"), k("1.0.0-rc.2"))
        self.assertLess(k("1.0.0-rc.2"), k("1.0.0-rc.10"))
        self.assertLess(k("1.0.0-alpha"), k("1.0.0-alpha.1"))
        self.assertLess(k("1.0.0-alpha.1"), k("1.0.0-beta"))
        self.assertLess(k("0.9.9"), k("1.0.0-rc.1"))
        self.assertEqual(k("1.0"), k("1.0.0"))
        self.assertEqual(k("1.0.0+build5"), k("1.0.0"))
        self.assertLess(k(""), k("0.0.1"))

    def test_updates_follow_prerelease_order(self):
        keys = lambda todo: [c["key"] for c in todo]
        rc = {"engine": "v1.0.0-rc.1", "frame": "v1.0.0-rc.1"}
        self.assertEqual(keys(ei.components_to_update(rc, "v1.0.0", if_newer=True)), ["engine", "frame"])
        final = {"engine": "v1.0.0", "frame": "v1.0.0"}
        self.assertEqual(ei.components_to_update(final, "v1.0.0-rc.2", if_newer=True), [])

    def test_version_of_and_asset_names(self):
        self.assertEqual(ei.version_of("v0.1.0"), "0.1.0")
        self.assertEqual(ei.version_of("0.1.0-frame.1"), "0.1.0-frame.1")
        self.assertEqual(ei.version_of("vanilla"), "vanilla")
        engine, frame = ei.COMPONENTS
        self.assertEqual(ei.asset_name(engine, "v0.1.0"), "earshot-engine-0.1.0-aarch64.tar.gz")
        self.assertEqual(ei.asset_name(frame, "v0.1.0"), "earshot-frame-0.1.0.tar.gz")

    def test_earshot_tag_family(self):
        self.assertEqual(ei.version_of("e0.1.0"), "0.1.0")
        self.assertEqual(ei.version_of("e0.1.0-rc.1"), "0.1.0-rc.1")
        self.assertEqual(ei.version_of("earnest"), "earnest")
        engine, frame = ei.COMPONENTS
        for tag in ("e0.1.0", "v0.1.0"):
            self.assertEqual(ei.asset_name(engine, tag), "earshot-engine-0.1.0-aarch64.tar.gz")
            self.assertEqual(ei.asset_name(frame, tag), "earshot-frame-0.1.0.tar.gz")
        self.assertEqual(ei.version_key("e0.1.0"), ei.version_key("v0.1.0"))
        self.assertGreater(ei.version_key("e0.2.0"), ei.version_key("e0.1.9"))
        self.assertLess(ei.version_key("e0.2.0-rc.1"), ei.version_key("e0.2.0"))
        old = {"engine": "e0.1.0", "frame": "e0.1.0"}
        self.assertEqual(ei.components_to_update(old, "e0.1.0", if_newer=True), [])
        self.assertEqual(len(ei.components_to_update(old, "e0.1.1", if_newer=True)), 2)
        self.assertEqual(ei.components_to_update(old, "e0.0.9", if_newer=True), [])

    def test_components_to_update(self):
        keys = lambda todo: [c["key"] for c in todo]
        self.assertEqual(keys(ei.components_to_update({}, "v1.0.0", if_newer=True)), ["engine", "frame"])
        same = {"engine": "v1.0.0", "frame": "v1.0.0"}
        self.assertEqual(ei.components_to_update(same, "v1.0.0", if_newer=True), [])
        self.assertEqual(keys(ei.components_to_update(same, "v1.0.0", if_newer=False)), ["engine", "frame"])
        newer = {"engine": "v1.0.0", "frame": "v1.1.0"}
        self.assertEqual(keys(ei.components_to_update(newer, "v1.1.0", if_newer=True)), ["engine"])
        # never an older release than the one installed
        self.assertEqual(ei.components_to_update(newer, "v0.9.0", if_newer=True), [])


def make_tar(path, top, files, extra=None):
    with tarfile.open(path, "w:gz") as tar:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)


def tar_data(path):
    with open(path, "rb") as f:
        return f.read()


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_extracts_files_with_modes(self):
        arc = os.path.join(self.tmp, "a.tar.gz")
        make_tar(arc, "earshot-engine", {"tetherd": (b"bin", 0o755), "README": (b"hi", 0o600)})
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        root = ei.safe_extract(tar_data(arc), out, "earshot-engine")
        self.assertEqual(root, os.path.join(out, "earshot-engine"))
        with open(os.path.join(root, "tetherd"), "rb") as f:
            self.assertEqual(f.read(), b"bin")
        if os.name == "posix":
            self.assertTrue(os.access(os.path.join(root, "tetherd"), os.X_OK))
            self.assertFalse(os.access(os.path.join(root, "README"), os.X_OK))

    def _refused(self, info_or_name):
        arc = os.path.join(self.tmp, "bad.tar.gz")
        if isinstance(info_or_name, str):
            make_tar(arc, "earshot-engine", {"ok": (b"x", 0o644)})
            with tarfile.open(arc, "w:gz") as tar:
                info = tarfile.TarInfo(info_or_name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
        else:
            with tarfile.open(arc, "w:gz") as tar:
                tar.addfile(info_or_name)
        out = os.path.join(self.tmp, "o")
        os.makedirs(out, exist_ok=True)
        with self.assertRaises(ei.InstallError):
            ei.safe_extract(tar_data(arc), out, "earshot-engine")
        self.assertEqual(os.listdir(out), [])

    def test_refuses_traversal_absolute_and_other_top(self):
        self._refused("earshot-engine/../evil")
        self._refused("/etc/evil")
        self._refused("other/file")
        self._refused("earshot-engine\\evil")

    def test_refuses_links_and_devices(self):
        link = tarfile.TarInfo("earshot-engine/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        self._refused(link)
        hard = tarfile.TarInfo("earshot-engine/hard")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "earshot-engine/ok"
        self._refused(hard)

    def test_a_version_file_must_match_the_release_tag(self):
        root = os.path.join(self.tmp, "r")
        os.makedirs(root)
        with open(os.path.join(root, "VERSION"), "w") as f:
            f.write("1.2.0\n")
        ei.check_version(root, "v1.2.0", "Earshot engine")
        ei.check_version(root, "1.2.0", "Earshot engine")
        with self.assertRaises(ei.InstallError) as cm:
            ei.check_version(root, "v1.3.0", "Earshot engine")
        self.assertIn("1.2.0", str(cm.exception))
        os.remove(os.path.join(root, "VERSION"))
        with self.assertRaises(ei.InstallError):
            ei.check_version(root, "v1.2.0", "Earshot engine")

    def test_refuses_empty_archive(self):
        arc = os.path.join(self.tmp, "e.tar.gz")
        with tarfile.open(arc, "w:gz"):
            pass
        with self.assertRaises(ei.InstallError):
            ei.safe_extract(tar_data(arc), self.tmp, "earshot-engine")


class SwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dest = os.path.join(self.tmp, "engine")

    def _dir(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        with open(os.path.join(path, "v"), "w") as f:
            f.write(text)
        return path

    def _read(self, path):
        with open(os.path.join(path, "v")) as f:
            return f.read()

    def test_first_install_has_no_old(self):
        old = ei.swap_in(self._dir("new", "1"), self.dest)
        self.assertIsNone(old)
        self.assertEqual(self._read(self.dest), "1")

    def test_swap_keeps_old_until_discarded(self):
        ei.swap_in(self._dir("n1", "1"), self.dest)
        old = ei.swap_in(self._dir("n2", "2"), self.dest)
        self.assertEqual(self._read(self.dest), "2")
        self.assertEqual(self._read(old), "1")
        ei.discard_old(old)
        self.assertFalse(os.path.exists(old))

    def test_roll_back_restores_the_old_install(self):
        ei.swap_in(self._dir("n1", "1"), self.dest)
        old = ei.swap_in(self._dir("n2", "2"), self.dest)
        ei.roll_back(self.dest, old)
        self.assertEqual(self._read(self.dest), "1")
        self.assertFalse(os.path.exists(old))

    def test_roll_back_of_a_first_install_removes_it(self):
        old = ei.swap_in(self._dir("n1", "1"), self.dest)
        ei.roll_back(self.dest, old)
        self.assertFalse(os.path.exists(self.dest))

    def test_failed_move_puts_the_old_one_back(self):
        ei.swap_in(self._dir("n1", "1"), self.dest)
        with self.assertRaises(OSError):
            ei.swap_in(os.path.join(self.tmp, "does-not-exist"), self.dest)
        self.assertEqual(self._read(self.dest), "1")


class FlowTests(unittest.TestCase):
    """install() end to end with the network, the machine type and the shell steps faked."""

    TAG = "v1.2.0"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data", "earshot")
        self.state = os.path.join(self.tmp, "state", "earshot")
        self.pub, _ = sign(SEED, b"")
        pubkey = base64.b64encode(self.pub).decode()
        self.comps = [dict(c, pubkey=pubkey) for c in ei.COMPONENTS]
        self.net = {}
        self.publish(self.TAG)
        patches = [
            mock.patch.object(ei, "COMPONENTS", self.comps),
            mock.patch.object(ei, "DATA_DIR", self.data),
            mock.patch.object(ei, "STATE_DIR", self.state),
            mock.patch.object(ei, "VERSIONS_FILE", os.path.join(self.data, "versions.json")),
            mock.patch.object(ei, "LOCK_FILE", os.path.join(self.state, "install.lock")),
            mock.patch.object(ei, "latest_tag", lambda c: self.TAG),
            mock.patch.object(ei, "fetch", lambda url: self.net[url]),
            mock.patch.object(ei.platform, "machine", lambda: "aarch64"),
            mock.patch.object(ei.shutil, "which", lambda name: "/usr/bin/" + name),
            mock.patch.object(ei, "health_check", lambda d: None),
            mock.patch.object(ei, "restart_services", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.status_path = os.path.join(self.state, "update.json")
        self.install_sh_calls = 0

    def publish(self, tag, version="same"):
        """Put signed tarballs for release `tag` on the fake network. Each carries a VERSION file naming the
        release (or `version`, when a test wants it to disagree; None leaves the file out)."""
        version = ei.version_of(tag) if version == "same" else version
        for c, label in zip(self.comps, ("engine", "frame")):
            arc = os.path.join(self.tmp, f"{label}.tar.gz")
            files = {"payload": (f"{label} {ei.version_of(tag)}".encode(), 0o644), "tether": (b"x", 0o755)}
            if version is not None:
                files["VERSION"] = (version.encode() + b"\n", 0o644)
            make_tar(arc, c["top"], files)
            with open(arc, "rb") as f:
                data = f.read()
            url = f"https://github.com/{c['repo']}/releases/download/{tag}/{ei.asset_name(c, tag)}"
            self.net[url] = data
            self.net[url + ".sig"] = base64.b64encode(sign(SEED, data)[1])

    def _args(self, **kw):
        ns = mock.Mock(download_only=None, if_newer=False)
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def _status(self):
        with open(self.status_path) as f:
            return json.load(f)

    def _run(self, **kw):
        status = ei.Status(self.status_path)
        with mock.patch("sys.stdout", new=io.StringIO()):
            ei.install(self._args(**kw), status)

    def _payload(self, label):
        with open(os.path.join(self.data, label, "payload"), "rb") as f:
            return f.read()

    def test_fresh_install(self):
        with mock.patch.object(ei, "run_install_sh", lambda: None):
            self._run()
        self.assertEqual(self._payload("engine"), b"engine 1.2.0")
        self.assertEqual(self._payload("frame"), b"frame 1.2.0")
        self.assertEqual(ei.read_versions(), {"engine": self.TAG, "frame": self.TAG})
        self.assertEqual(self._status()["state"], "updated")
        self.assertEqual([n for n in os.listdir(self.data) if n.startswith(".stage") or n.endswith(".old")], [])
        # nothing is left on disk but the unpacked, checked files: no archive, no setup script
        self.assertFalse([n for n in os.listdir(os.path.join(self.data, "frame")) if "setup" in n or n.endswith(".gz")])

    def test_release_whose_version_file_names_another_release_is_refused(self):
        self.publish(self.TAG, version="1.1.0")
        with mock.patch.object(ei, "run_install_sh", lambda: None), self.assertRaises(ei.InstallError) as cm:
            self._run()
        self.assertIn("1.1.0", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.data, "engine")))
        self.assertEqual(ei.read_versions(), {})

    def test_release_without_a_version_file_is_refused(self):
        self.publish(self.TAG, version=None)
        with mock.patch.object(ei, "run_install_sh", lambda: None), self.assertRaises(ei.InstallError):
            self._run()
        self.assertFalse(os.path.exists(os.path.join(self.data, "frame")))

    def test_install_never_writes_the_archive_to_disk(self):
        seen = []
        real = ei.safe_extract

        def spy(data, dest, top):
            seen.append(type(data))
            self.assertEqual([n for n in os.listdir(dest) if n.endswith(".gz")], [])
            return real(data, dest, top)

        with mock.patch.object(ei, "safe_extract", spy), mock.patch.object(ei, "run_install_sh", lambda: None):
            self._run()
        self.assertEqual(set(seen), {bytes})

    def test_done_message_asks_for_one_restart_and_no_password(self):
        with mock.patch.object(ei, "run_install_sh", lambda: None), \
                mock.patch("sys.stdout", new=io.StringIO()) as out:
            ei.install(self._args(), ei.Status(self.status_path))
        text = out.getvalue()
        self.assertIn("restart the headset", text)
        self.assertNotIn("password", text.lower())

    def test_failed_update_keeps_the_old_install(self):
        with mock.patch.object(ei, "run_install_sh", lambda: None):
            self._run()
        # a newer release whose install.sh fails
        self.TAG = "v1.3.0"
        self.publish("v1.3.0")
        bad = mock.Mock(side_effect=ei.InstallError("install.sh exited 1"))
        with mock.patch.object(ei, "latest_tag", lambda c: "v1.3.0"), mock.patch.object(ei, "run_install_sh", bad):
            with self.assertRaises(ei.InstallError):
                self._run()
        self.assertEqual(self._payload("engine"), b"engine 1.2.0")
        self.assertEqual(self._payload("frame"), b"frame 1.2.0")
        self.assertEqual(ei.read_versions(), {"engine": "v1.2.0", "frame": "v1.2.0"})
        self.assertEqual([n for n in os.listdir(self.data) if n.endswith(".old")], [])

    def test_unhealthy_engine_is_rolled_back(self):
        with mock.patch.object(ei, "run_install_sh", lambda: None):
            self._run()
        self.TAG = "v1.3.0"
        self.publish("v1.3.0")
        bad = mock.Mock(side_effect=ei.InstallError("the new engine does not start"))
        with mock.patch.object(ei, "latest_tag", lambda c: "v1.3.0"), mock.patch.object(ei, "health_check", bad):
            with self.assertRaises(ei.InstallError):
                self._run()
        self.assertEqual(self._payload("engine"), b"engine 1.2.0")
        self.assertEqual(ei.read_versions()["engine"], "v1.2.0")

    def test_bad_signature_installs_nothing(self):
        for url in self.net:
            if url.endswith(".sig"):
                self.net[url] = base64.b64encode(b"\1" * 64)
        with mock.patch.object(ei, "run_install_sh", lambda: None):
            with self.assertRaises(ei.InstallError):
                self._run()
        self.assertFalse(os.path.exists(os.path.join(self.data, "engine")))
        self.assertEqual(ei.read_versions(), {})

    def test_if_newer_with_nothing_new_changes_nothing(self):
        with mock.patch.object(ei, "run_install_sh", lambda: None):
            self._run()
        before = self._payload("engine")
        called = mock.Mock()
        with mock.patch.object(ei, "run_install_sh", called):
            self._run(if_newer=True)
        called.assert_not_called()
        self.assertEqual(self._payload("engine"), before)
        self.assertEqual(self._status()["state"], "uptodate")

    def test_download_only_unpacks_and_installs_nothing(self):
        out = os.path.join(self.tmp, "dl")
        self._run(download_only=out)
        self.assertTrue(os.path.isfile(os.path.join(out, "earshot-engine", "payload")))
        self.assertFalse(os.path.exists(self.data))

    def test_wrong_machine_is_refused(self):
        with mock.patch.object(ei.platform, "machine", lambda: "x86_64"):
            with self.assertRaises(ei.InstallError):
                self._run()


    # --- what the Phone tab's "Check for updates" shows: always a result, in plain words ---

    def _main(self, *argv):
        with mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()):
            rc = ei.main(["--if-newer", "--status-file", self.status_path] + list(argv))
        return rc, self._status()

    def test_up_to_date_names_the_version(self):
        with mock.patch.object(ei, "run_install_sh", lambda: None):
            self._run()
            rc, st = self._main()
        self.assertEqual((rc, st["state"]), (0, "uptodate"))
        self.assertEqual(st["message"], "Up to date (version v1.2.0)")

    def test_a_newer_release_says_so_before_it_updates(self):
        with mock.patch.object(ei, "run_install_sh", lambda: None):
            self._run()
            self.publish("v1.3.0")
            seen = []
            real = ei.Status.set

            def spy(status, state, message):
                seen.append((state, message))
                real(status, state, message)

            with mock.patch.object(ei, "latest_tag", lambda c: "v1.3.0"), mock.patch.object(ei.Status, "set", spy):
                rc, st = self._main()
        self.assertEqual(rc, 0)
        self.assertIn(("updating", "Update available: v1.3.0 - updating..."), seen)
        self.assertEqual(st["state"], "updated")

    def test_no_release_published_yet_is_a_clear_result_not_an_error(self):
        def none(c):
            raise ei.NoReleases("nothing published")

        with mock.patch.object(ei, "latest_tag", none):
            rc, st = self._main()
        self.assertEqual(rc, 0)
        self.assertEqual((st["state"], st["message"]), ("noreleases", "No releases published yet"))
        with mock.patch.object(ei, "latest_tag", none):          # a first install by hand has nothing to install: that is an error exit
            with mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(ei.main(["--status-file", self.status_path]), 1)
        self.assertEqual(self._status()["state"], "noreleases")

    def test_a_failed_lookup_says_why(self):
        def broken(c):
            raise ei.CheckFailed("could not check x", "Temporary failure in name resolution")

        with mock.patch.object(ei, "latest_tag", broken):
            rc, st = self._main()
        self.assertEqual(rc, 1)
        self.assertEqual((st["state"], st["message"]), ("failed", "Could not check (Temporary failure in name resolution)"))


class LatestTagTests(unittest.TestCase):
    """latest_tag() turns GitHub's answers into a tag, "no releases yet", or a short reason."""

    COMPONENT = {"repo": "example/earshot"}

    def _open(self, result):
        class Resp:
            def __init__(self, url):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def geturl(self):
                return self.url

        def fake(req, timeout=None):
            if isinstance(result, Exception):
                raise result
            return Resp(result)

        return mock.patch.object(ei.urllib.request, "urlopen", fake)

    def test_the_tag_comes_from_the_redirect(self):
        with self._open("https://github.com/example/earshot/releases/tag/v1.2.0"):
            self.assertEqual(ei.latest_tag(self.COMPONENT), "v1.2.0")

    def test_404_means_nothing_is_published_yet(self):
        err = ei.urllib.error.HTTPError("https://github.com/x", 404, "Not Found", {}, io.BytesIO(b""))
        with self._open(err):
            with self.assertRaises(ei.NoReleases):
                ei.latest_tag(self.COMPONENT)

    def test_a_redirect_without_a_tag_means_the_same(self):
        with self._open("https://github.com/example/earshot/releases"):
            with self.assertRaises(ei.NoReleases):
                ei.latest_tag(self.COMPONENT)

    def test_other_http_errors_and_network_errors_carry_a_short_reason(self):
        err = ei.urllib.error.HTTPError("https://github.com/x", 503, "Unavailable", {}, io.BytesIO(b""))
        with self._open(err):
            with self.assertRaises(ei.CheckFailed) as caught:
                ei.latest_tag(self.COMPONENT)
        self.assertEqual(caught.exception.reason, "GitHub answered 503")
        with self._open(ei.urllib.error.URLError("Temporary failure in name resolution")):
            with self.assertRaises(ei.CheckFailed) as caught:
                ei.latest_tag(self.COMPONENT)
        self.assertEqual(caught.exception.reason, "Temporary failure in name resolution")
        with self._open(TimeoutError("timed out")):
            with self.assertRaises(ei.CheckFailed) as caught:
                ei.latest_tag(self.COMPONENT)
        self.assertEqual(caught.exception.reason, "timed out")


class StatusTests(unittest.TestCase):
    def test_status_file_and_final_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "update.json")
            ei.Status(path).set("updating", "Updating...")
            with open(path) as f:
                got = json.load(f)
            self.assertEqual((got["state"], got["message"]), ("updating", "Updating..."))
            self.assertIn("time", got)
            quiet = os.path.join(d, "quiet.json")
            s = ei.Status(quiet, final_only=True)
            s.set("checking", "x")
            self.assertFalse(os.path.exists(quiet))
            s.set("failed", "boom")
            self.assertTrue(os.path.exists(quiet))
            ei.Status(None).set("failed", "no path: no crash")


class UninstallTests(unittest.TestCase):
    """uninstall(): one shell script does the work, and nothing needs a password."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "earshot")
        os.makedirs(os.path.join(self.data, "frame"))
        open(os.path.join(self.data, "frame", "uninstall.sh"), "w").close()
        self.calls = []
        self.rc = 0
        env = {"XDG_DATA_HOME": os.path.join(self.tmp, "xdg-data"), "XDG_CONFIG_HOME": os.path.join(self.tmp, "xdg-config"),
               "XDG_STATE_HOME": os.path.join(self.tmp, "xdg-state")}
        for p in (mock.patch.object(ei, "DATA_DIR", self.data),
                  mock.patch.dict(os.environ, env),
                  mock.patch.object(ei.subprocess, "run", self._run)):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, cmd, **kw):
        self.calls.append(list(cmd))
        return mock.Mock(returncode=self.rc)

    def _uninstall(self, purge=False):
        with mock.patch("sys.stdout", new=io.StringIO()) as out, mock.patch("builtins.input", side_effect=AssertionError("asked")):
            ei.uninstall(mock.Mock(purge=purge))
        return out.getvalue()

    def test_runs_the_script_and_asks_nothing(self):
        self._uninstall()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][1].endswith("uninstall.sh"))
        self.assertNotIn("--purge", self.calls[0])
        for word in ("sudo", "pkexec", "--undo", "--keep-setup"):
            self.assertNotIn(word, " ".join(self.calls[0]))

    def test_purge_is_passed_through(self):
        self._uninstall(purge=True)
        self.assertIn("--purge", self.calls[0])

    def test_a_failed_script_is_an_error(self):
        self.rc = 3
        with self.assertRaises(ei.InstallError):
            self._uninstall()

    def test_without_the_script_what_earshot_left_behind_is_still_removed(self):
        os.remove(os.path.join(self.data, "frame", "uninstall.sh"))
        dirs = ei.tether_data_dirs()
        for d in dirs:
            os.makedirs(d)
            open(os.path.join(d, "x"), "w").close()
        out = self._uninstall()
        self.assertIn("not installed", out)
        self.assertEqual([d for d in dirs if os.path.exists(d)], [])

    def test_a_separately_installed_tether_is_never_touched(self):
        stock = [os.path.join(os.environ["XDG_DATA_HOME"], "tether"), os.path.join(os.environ["XDG_CONFIG_HOME"], "tether"),
                 os.path.join(os.environ["XDG_STATE_HOME"], "tether")]
        for d in stock:
            os.makedirs(d)
            open(os.path.join(d, "x"), "w").close()
        # Earshot installed, and Earshot not installed (a stray run of the uninstaller)
        self._uninstall()
        os.remove(os.path.join(self.data, "frame", "uninstall.sh"))
        self._uninstall()
        self.assertEqual([d for d in stock if os.path.exists(os.path.join(d, "x"))], stock)

    def test_nothing_is_removed_when_earshot_never_ran(self):
        shutil.rmtree(self.data)
        out = self._uninstall()
        self.assertIn("not installed", out)
        self.assertEqual(self.calls, [])

    def test_the_engines_data_dirs_are_earshots_own(self):
        dirs = ei.tether_data_dirs()
        self.assertEqual([os.path.basename(d) for d in dirs], ["data", "config", "state"])
        for d in dirs:
            self.assertEqual(os.path.dirname(d), os.path.join(self.data, "engine-home"))


class CommandLineTests(unittest.TestCase):
    def test_purge_needs_uninstall(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
            ei.main(["--purge"])


if __name__ == "__main__":
    unittest.main()
