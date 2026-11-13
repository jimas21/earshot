"""Sanity checks for the installer's launchers, unit files and shell scripts, and a run of
install.sh and uninstall.sh against a fake home with a stub systemctl (Unix only):
    python3 -m unittest discover -s frame/install/tests -v
"""

import configparser
import os
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
            for needle in ("/home/deck", "10.0.0.", "immortal"):
                self.assertNotIn(needle, text, name)


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
        for name in ("install.sh", "uninstall.sh", "earshot-system-setup.sh"):
            r = subprocess.run([SH, "-n", os.path.join(INSTALL, name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, name + r.stderr)

    def test_python_files_compile(self):
        for name in ("earshot-frame-install.py", "earshot-system-setup.py"):
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


if __name__ == "__main__":
    unittest.main()
