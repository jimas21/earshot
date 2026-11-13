"""Sanity checks for the installer's launchers, unit files and shell scripts, and a run of
install.sh and uninstall.sh against a fake home with a stub systemctl (Unix only):
    python3 -m unittest discover -s frame/install/tests -v
"""

import configparser
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL = os.path.dirname(_HERE)
SH = shutil.which("sh")


def read(name):
    with open(os.path.join(INSTALL, name), encoding="utf-8", newline="") as f:
        return f.read()


class DesktopFileTests(unittest.TestCase):
    BOOTSTRAP = "https://github.com/jimas21/earshot/releases/latest/download/earshot-frame-install.py"

    def parse(self, name):
        cp = configparser.RawConfigParser(strict=True)
        cp.optionxform = str
        cp.read_string(read(name))
        return cp["Desktop Entry"]

    def test_entries(self):
        for name, title, flag in (("Install-Earshot.desktop", "Install Earshot", None),
                                  ("Uninstall-Earshot.desktop", "Uninstall Earshot", "--uninstall")):
            entry = self.parse(name)
            self.assertEqual(entry["Type"], "Application")
            self.assertEqual(entry["Name"], title)
            self.assertEqual(entry["Terminal"], "false")
            for key in ("Comment", "Icon", "Exec"):
                self.assertTrue(entry[key].strip(), key)
            exec_line = entry["Exec"]
            self.assertTrue(exec_line.startswith('konsole --hold -e python3 -I -c "'), name)
            self.assertIn(self.BOOTSTRAP, exec_line)
            self.assertEqual(exec_line.endswith("--uninstall"), flag is not None)
            self.assertNotIn("/tmp", exec_line)
            self.assertNotIn("sudo", exec_line)
            self.assertEqual(exec_line.count('"'), 2)  # the -c program is one quoted argument

    def test_bootstrap_url_names_the_installer_asset(self):
        self.assertIn("earshot-frame-install.py", self.BOOTSTRAP)
        self.assertTrue(os.path.isfile(os.path.join(INSTALL, "earshot-frame-install.py")))

    def test_files_use_unix_line_endings_and_no_personal_data(self):
        for name in os.listdir(INSTALL):
            path = os.path.join(INSTALL, name)
            if not os.path.isfile(path):
                continue
            text = read(name)
            self.assertNotIn("\r", text, name)
            for needle in ("/home/deck", "10.0.0.", "192.168."):
                self.assertNotIn(needle, text, name)
            # nothing that names a person or a machine: a home path, an email address that is not a placeholder
            self.assertIsNone(re.search(r"/home/(?!deck\b|user\b)\w+", text), name)
            self.assertIsNone(re.search(r"[A-Za-z]:\\Users\\", text), name)
            for address in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
                self.assertTrue(address.endswith(("@example.com", "@users.noreply.github.com")) or
                                address.startswith("noreply@"), f"{name}: {address}")

    def test_the_product_never_asks_for_root(self):
        """No installer, script, unit or document may use or offer root: no password, no system files."""
        words = ("pkexec", "sudo", "kdialog", "/etc/earshot", "system-setup", "system_setup")
        names = [n for n in os.listdir(INSTALL) if os.path.isfile(os.path.join(INSTALL, n))]
        self.assertFalse([n for n in names if "system-setup" in n], "the root setup step must not exist")
        for name in names:
            text = read(name).lower()
            for word in words:
                self.assertNotIn(word, text, f"{name} mentions {word}")
            # saying it needs none is fine; asking for one is not
            self.assertIsNone(re.search(r"(ask|prompt|enter|type|supply)[^.\n]{0,30}password", text), name)


class UnitFileTests(unittest.TestCase):
    def sections(self, name):
        text = read(name)
        out, current = {}, None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = out.setdefault(line[1:-1], [])
            elif current is not None:
                current.append(line)
        return out

    def test_tetherd_unit(self):
        s = self.sections("earshot-tetherd.service")
        self.assertIn("ExecStart=%h/.local/share/earshot/engine/tetherd", s["Service"])
        self.assertIn("WantedBy=default.target", s["Install"])
        self.assertIn("Environment=TETHER_LOG_STDERR=1", s["Service"])
        self.assertIn("Environment=TETHER_NO_TCP=1", s["Service"])   # Bluetooth only: nothing listens on the network
        self.assertIn("Environment=TETHER_NO_ROOT=1", s["Service"])  # stock BlueZ, normal user
        self.assertIn("Environment=TETHER_RETENTION=none", s["Service"])  # no message or contact copies on disk
        # Earshot's engine keeps its own directories, not the ones a separately installed Tether uses
        for name, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state")):
            self.assertIn("Environment=%s=%%h/.local/share/earshot/engine-home/%s" % (name, sub), s["Service"])
        self.assertIn("NoNewPrivileges=yes", s["Service"])
        self.assertFalse([l for l in s["Service"] if l.startswith("User=")])  # runs as the user manager's user

    def test_steamui_unit(self):
        s = self.sections("earshot-steamui.service")
        self.assertIn("ExecStart=/usr/bin/python3 %h/.local/share/earshot/frame/earshot-steamui.py", s["Service"])
        self.assertIn("After=earshot-tetherd.service", s["Unit"])
        self.assertIn("WantedBy=default.target", s["Install"])

    def test_every_unit_has_the_three_sections(self):
        for name in ("earshot-tetherd.service", "earshot-steamui.service"):
            self.assertEqual(sorted(self.sections(name)), ["Install", "Service", "Unit"])

    def test_nothing_touches_tragus(self):
        for name in os.listdir(INSTALL):
            path = os.path.join(INSTALL, name)
            if os.path.isfile(path) and not name.endswith("uninstall.sh") and name != "install.sh":
                text = read(name).lower()
                self.assertNotIn("magicpodscore.service", text, name)


class ScriptSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(SH, "no sh")
    def test_sh_syntax(self):
        for name in ("install.sh", "uninstall.sh"):
            r = subprocess.run([SH, "-n", os.path.join(INSTALL, name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, name + r.stderr)

    def test_python_files_compile(self):
        for name in ("earshot-frame-install.py",):
            with open(os.path.join(INSTALL, name), encoding="utf-8") as f:
                compile(f.read(), name, "exec")


@unittest.skipUnless(SH and os.name == "posix", "needs a Unix sh")
class ScriptRunTests(unittest.TestCase):
    """install.sh and uninstall.sh in a fake HOME with a systemctl that always says active."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.bin = os.path.join(self.home, "stubbin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "systemctl")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "$@" >> "$HOME/systemctl.log"\n'
                    '[ "$2" = is-active ] && echo active\nexit 0\n')
        os.chmod(stub, 0o755)
        self.data = os.path.join(self.home, ".local/share/earshot")
        os.makedirs(os.path.join(self.data, "engine"))
        os.makedirs(os.path.join(self.data, "frame"))
        for rel in ("engine/tetherd", "engine/tether"):
            with open(os.path.join(self.data, rel), "w") as f:
                f.write("#!/bin/sh\n")
        for name in ("earshot-tetherd.service", "earshot-steamui.service", "install.sh", "uninstall.sh"):
            shutil.copy(os.path.join(INSTALL, name), os.path.join(self.data, "frame", name))
        self.flag = os.path.join(self.home, ".steam/steam/.cef-enable-remote-debugging")

    def sh(self, script, *args):
        env = {"HOME": self.home, "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        return subprocess.run([SH, os.path.join(self.data, "frame", script), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def log(self):
        with open(os.path.join(self.home, "systemctl.log")) as f:
            return f.read()

    def test_install_creates_flag_and_marker_and_units(self):
        os.makedirs(os.path.dirname(self.flag))
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.flag))
        self.assertTrue(os.path.exists(os.path.join(self.data, "created-cef-flag")))
        for name in ("earshot-tetherd.service", "earshot-steamui.service"):
            self.assertTrue(os.path.isfile(os.path.join(self.home, ".config/systemd/user", name)))
        self.assertIn("--user enable earshot-tetherd.service earshot-steamui.service", self.log())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.home, ".config/earshot")).st_mode), 0o700)

    def test_existing_flag_is_not_ours(self):
        os.makedirs(os.path.dirname(self.flag))
        open(self.flag, "w").close()
        self.assertEqual(self.sh("install.sh").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.data, "created-cef-flag")))
        # uninstall must leave a file we did not create
        self.assertEqual(self.sh("uninstall.sh").returncode, 0)
        self.assertTrue(os.path.exists(self.flag))

    def test_uninstall_removes_the_flag_it_created(self):
        os.makedirs(os.path.dirname(self.flag))
        self.sh("install.sh")
        self.assertEqual(self.sh("uninstall.sh").returncode, 0)
        self.assertFalse(os.path.exists(self.flag))
        self.assertFalse(os.path.exists(self.data))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config/systemd/user/earshot-tetherd.service")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".config/earshot")))  # settings kept

    def test_uninstall_leaves_the_flag_while_tragus_is_installed(self):
        os.makedirs(os.path.dirname(self.flag))
        self.sh("install.sh")
        os.makedirs(os.path.join(self.home, ".local/share/magicpodscore"))
        r = self.sh("uninstall.sh")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(self.flag))
        self.assertIn("Tragus", r.stdout)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".local/share/magicpodscore")))  # never touched

    def test_purge_deletes_the_settings(self):
        os.makedirs(os.path.dirname(self.flag))
        self.sh("install.sh")
        self.sh("uninstall.sh", "--purge")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config/earshot")))

    def test_install_without_steam_still_works_and_says_so(self):
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.flag))
        self.assertIn(".steam/steam", r.stderr)

    def test_install_fails_when_the_engine_is_missing(self):
        os.remove(os.path.join(self.data, "engine/tetherd"))
        r = self.sh("install.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("missing", r.stderr)

    def test_uninstall_removes_the_engines_own_data_but_not_other_programs(self):
        home = os.path.join(self.home, ".local/share/earshot/engine-home")
        engine = [os.path.join(home, "data/messages.ndjson.enc"),
                  os.path.join(home, "config/store.key"),
                  os.path.join(home, "config/bluetooth.json"),
                  os.path.join(home, "state/x")]
        # a Tether installed separately, and another program
        stock = [os.path.join(self.home, ".local/share/tether/messages.ndjson.enc"),
                 os.path.join(self.home, ".config/tether/store.key"),
                 os.path.join(self.home, ".local/state/tether/x")]
        other = os.path.join(self.home, ".config/tragus/keep")
        for path in engine + stock + [other]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        self.sh("install.sh")
        self.assertEqual(self.sh("uninstall.sh").returncode, 0)  # without --purge: still removed
        for path in engine:
            self.assertFalse(os.path.exists(path), path)
        self.assertFalse(os.path.exists(home))
        for path in stock + [other]:
            self.assertTrue(os.path.exists(path), path)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".config/earshot")))  # Earshot settings kept

    def test_the_uninstall_text_says_what_it_removes(self):
        text = read("uninstall.sh")
        self.assertIn("engine-home", text)
        # the stock Tether directories are named only to say they are left alone, never deleted
        self.assertNotIn('}/tether"', text)

    def test_uninstall_runs_the_helpers_teardown_first(self):
        usr_python = "/usr/bin/python3"
        if not os.path.exists(usr_python):
            self.skipTest("no /usr/bin/python3")
        marker = os.path.join(self.home, "teardown-ran")
        with open(os.path.join(self.data, "frame", "earshot-steamui.py"), "w") as f:
            f.write("import sys\nopen(%r, 'w').write(' '.join(sys.argv[1:]))\n" % marker)
        self.assertEqual(self.sh("uninstall.sh").returncode, 0)
        with open(marker) as f:
            self.assertEqual(f.read(), "--teardown")
        self.assertFalse(os.path.exists(self.data))


class DocsAndCiTests(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(INSTALL))

    def text(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_readme_states_the_unsigned_first_download_and_how_to_check_it(self):
        readme = self.text("README.md")
        self.assertIn("not signature-checked", readme)
        self.assertIn("sha256sum earshot-frame-install.py", readme)
        self.assertIn("fingerprint", readme)
        self.assertIn("78e1b408f2949b742e06195f89ae0375d4d0b726e0041345f3f37d4ad910e85a", readme)

    def test_readme_describes_the_final_no_password_flow(self):
        readme = self.text("README.md")
        self.assertIn("Install-Earshot.desktop", readme)
        self.assertIn("Pair iPhone", readme)
        self.assertIn("restart the headset", readme.lower())
        self.assertIn("no password", readme.lower())
        self.assertNotIn("pkexec", readme)
        self.assertNotIn("system setup", readme.lower())

    def test_upstream_readme_moved_and_the_frame_readme_points_to_the_front_page(self):
        self.assertTrue(os.path.isfile(os.path.join(self.ROOT, "docs", "UPSTREAM-README.md")))
        pointer = self.text("frame", "README.md")
        self.assertLess(len(pointer.splitlines()), 15)
        self.assertIn("../README.md", pointer)

    def test_readme_images_exist(self):
        readme = self.text("README.md")
        for path in re.findall(r"!\[[^\]]*\]\(([^)#?]+)\)", readme) + re.findall(r'<img[^>]+src="([^"]+)"', readme):
            if path.startswith("http"):
                continue
            self.assertTrue(os.path.isfile(os.path.join(self.ROOT, path)), path)

    def test_release_signing_is_pinned_and_gated(self):
        wf = self.text(".github", "workflows", "earshot-arm64.yml")
        self.assertIn("environment: release", wf)
        self.assertIn("--require-hashes", wf)
        self.assertIn("--only-binary=:all:", wf)
        self.assertIn("sha256sum -c", wf)
        # the secret appears in the signing step only
        self.assertEqual(wf.count("secrets.EARSHOT_RELEASE_KEY"), 1)
        reqs = self.text(".github", "release-sign-requirements.txt")
        for name in ("cryptography==", "cffi==", "pycparser=="):
            self.assertIn(name, reqs)
        self.assertGreaterEqual(reqs.count("--hash=sha256:"), 3)

    def test_both_tarballs_carry_the_version_the_installer_checks(self):
        wf = self.text(".github", "workflows", "earshot-arm64.yml")
        self.assertEqual(wf.count('> "${d}/VERSION"'), 2)

    def test_ci_publishes_the_installer_checksum_and_names_no_person(self):
        wf = self.text(".github", "workflows", "earshot-arm64.yml")
        self.assertIn("SHA-256 of earshot-frame-install.py", wf)
        self.assertIn("earshot-frame.sha256", wf)
        self.assertNotIn("TODO(Jim)", wf)


if __name__ == "__main__":
    unittest.main()
