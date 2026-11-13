#!/usr/bin/env python3
"""Install, update or remove Earshot on a Steam Frame, for the current user. No root.

Downloads the Earshot engine (tetherd, the Bluetooth daemon) and the Frame part (the
helper service and the Phone tab) from the latest release of
  https://github.com/jimas21/earshot
checks each one's ed25519 signature against the public key pinned below, unpacks it
safely, swaps it in beside the old copy (a failed update keeps the old install) and runs
its own install.sh to set up the user services.

Usually started by Install-Earshot.desktop (Desktop Mode), or by hand:
  python3 earshot-frame-install.py              install or update
  python3 earshot-frame-install.py --uninstall  remove (settings kept)
  python3 earshot-frame-install.py --uninstall --purge   also delete the settings
The Phone tab runs the copy kept in ~/.local/share/earshot/frame/ with --if-newer
--status-file for its "Check for updates" button and automatic updates.

The one root step (BlueZ experimental mode, the Class of Device unit, L2CAP ERTM) is
separate: earshot-system-setup.sh lists what it would change and asks first. This
installer offers to run it, once, when it is run by hand and something is missing.

Python standard library only. Pattern and signature code follow the Tragus installer.
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

# The .desktop launchers replace the excepthook so a failed download prints one plain line.
# Once this file is running, errors are this file's own: show them normally.
sys.excepthook = sys.__excepthook__

USER_AGENT = "earshot-frame-install"
MAX_DOWNLOAD = 64 * 1024 * 1024

REPO = "jimas21/earshot"
# TODO(release): paste the base64 of the raw 32-byte ed25519 PUBLIC key that signs the
# release tarballs. Until it is set, this installer refuses to install anything (it fails
# closed): there is nothing to check a download against. The key pair is made and held by
# the maintainer; never commit the private key. See .github/workflows/earshot-arm64.yml.
PUBKEY = ""

# Assets are named after the release tag with any leading "v" dropped: {ver}.
COMPONENTS = [
    {
        "key": "engine",
        "name": "Earshot engine",
        "repo": REPO,
        "asset": "earshot-engine-{ver}-aarch64.tar.gz",
        "top": "earshot-engine",
        "dest": "engine",
        "pubkey": PUBKEY,
    },
    {
        "key": "frame",
        "name": "Earshot for the Frame",
        "repo": REPO,
        "asset": "earshot-frame-{ver}.tar.gz",
        "top": "earshot-frame",
        "dest": "frame",
        "pubkey": PUBKEY,
    },
]

# --- ed25519 signature check -------------------------------------------------------
# Uses the `cryptography` package when it is installed; otherwise the reference
# verification from RFC 8032, section 6 (pure Python; slow, but it checks one
# signature per download).

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _add(a, b):
    p1 = (a[1] - a[0]) * (b[1] - b[0]) % _P
    p2 = (a[1] + a[0]) * (b[1] + b[0]) % _P
    p3 = 2 * a[3] * b[3] * _D % _P
    p4 = 2 * a[2] * b[2] % _P
    e, f, g, h = p2 - p1, p4 - p3, p4 + p3, p2 + p1
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s, pt):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _verify_pure(pub, msg, sig):
    if len(pub) != 32 or len(sig) != 64:
        return False
    a = _decompress(pub)
    r = _decompress(sig[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % _L
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


def verify_signature(pub, msg, sig, force_pure=False):
    """True when sig is a valid ed25519 signature of msg by the raw 32-byte key pub."""
    if not force_pure:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError:
            pass
        else:
            if len(pub) != 32 or len(sig) != 64:
                return False
            try:
                Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
                return True
            except (InvalidSignature, ValueError):
                return False
    return _verify_pure(pub, msg, sig)

# --- download, check, unpack ---------------------------------------------------------


class InstallError(Exception):
    pass


def version_of(tag):
    """A release tag without a leading v: v0.3.5 -> 0.3.5."""
    return tag[1:] if tag[:1] in ("v", "V") and tag[1:2].isdigit() else tag


def asset_name(component, tag):
    return component["asset"].format(ver=version_of(tag))


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            expected = resp.headers.get("Content-Length")
            data = resp.read(MAX_DOWNLOAD + 1)
    except (OSError, http.client.HTTPException) as e:
        raise InstallError(f"could not download {url}: {e}; check the connection and try again") from None
    if len(data) > MAX_DOWNLOAD:
        raise InstallError(f"{url} is larger than expected; not using it")
    if expected is not None and expected.isdigit() and int(expected) != len(data):
        raise InstallError(f"the download of {url} was cut short; check the connection and try again")
    return data


def check_signature(component, data, sig_text, asset=None):
    """Raise InstallError unless sig_text (base64) is a valid ed25519 signature of data
    by the component's pinned key. Fails closed when no key is pinned."""
    asset = asset or component["asset"]
    if not component.get("pubkey"):
        raise InstallError(
            "this installer has no release key pinned yet, so it cannot check the download; "
            "nothing was installed"
        )
    try:
        sig = base64.b64decode(sig_text.strip(), validate=True)
        pub = base64.b64decode(component["pubkey"], validate=True)
    except (ValueError, TypeError):
        raise InstallError(f"{asset}.sig or the pinned key is not valid base64") from None
    if not verify_signature(pub, data, sig):
        raise InstallError(
            f"the signature of {asset} does not match the pinned key; the download was not used"
        )


def safe_extract(archive_path, dest, top):
    """Unpack only regular files and folders under top/; refuse anything else."""
    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise InstallError("the release archive is empty")
        for m in members:
            name = m.name
            parts = name.split("/")
            if (
                name.startswith("/")
                or "\\" in name
                or ".." in parts
                or parts[0] != top
                or not (m.isfile() or m.isdir())
            ):
                raise InstallError(f"unexpected entry in the release archive: {name!r}")
        for m in members:
            target = os.path.join(dest, *m.name.split("/"))
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tar.extractfile(m)
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
    return os.path.join(dest, top)


# --- paths, versions, status ---------------------------------------------------------

DATA_DIR = os.path.expanduser("~/.local/share/earshot")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "earshot")
VERSIONS_FILE = os.path.join(DATA_DIR, "versions.json")
# Same path the Phone tab's helper (frame/menu/earshot-steamui.py) reads.
DEFAULT_STATUS_FILE = os.path.join(STATE_DIR, "update.json")
LOCK_FILE = os.path.join(STATE_DIR, "install.lock")
SYSTEM_MANIFEST = "/etc/earshot/manifest.json"
_TAG_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_")


def latest_tag(component):
    """The tag of the repository's latest release (GitHub redirects /releases/latest
    to /releases/tag/<tag>)."""
    url = f"https://github.com/{component['repo']}/releases/latest"
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            final = resp.geturl()
    except (OSError, http.client.HTTPException) as e:
        raise InstallError(f"could not check {url}: {e}; check the connection and try again") from None
    marker = "/releases/tag/"
    tag = final.split(marker, 1)[1] if marker in final else ""
    if not tag or len(tag) > 64 or not set(tag) <= _TAG_OK:
        raise InstallError(f"could not find the latest release of {component['repo']}")
    return tag


def read_versions():
    try:
        with open(VERSIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_versions(versions):
    os.makedirs(os.path.dirname(VERSIONS_FILE), exist_ok=True)
    tmp = VERSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(versions, f, indent=1, sort_keys=True)
    os.replace(tmp, VERSIONS_FILE)


def version_key(tag):
    """A sortable key for a release tag, following semantic versioning: the numbers before
    any "-" decide first; at the same numbers a pre-release (1.0.0-rc.1, 0.3.5-frame.7)
    sorts before the release (1.0.0), and its dot-separated parts compare as numbers when
    they are numbers (rc.2 < rc.10). An empty or missing tag is () and sorts lowest."""
    text = (tag or "").strip()
    if not text:
        return ()
    if text[:1] in ("v", "V") and text[1:2].isdigit():
        text = text[1:]
    text = text.split("+", 1)[0]
    core, _, pre = text.partition("-")
    nums = []
    for part in core.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while nums and nums[-1] == 0:
        nums.pop()
    if not pre:
        return (tuple(nums), 1)
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.replace("-", ".").split(".") if p)
    return (tuple(nums), 0, ids)


def components_to_update(installed, tag, if_newer):
    """The components to fetch for release `tag`. With if_newer: only those whose
    installed tag differs, and never an older release than the one installed."""
    if not if_newer:
        return list(COMPONENTS)
    return [c for c in COMPONENTS
            if installed.get(c["key"]) != tag
            and version_key(tag) > version_key(installed.get(c["key"]) or "")]


class Status:
    """Progress for the Phone tab (its helper reads the file): a JSON object with
    state (checking, updating, uptodate, updated, failed), message and time."""

    FINAL = ("uptodate", "updated", "failed")

    def __init__(self, path, final_only=False):
        self.path = path
        self.final_only = final_only  # a run the menu did not start: only its result

    def set(self, state, message):
        if not self.path or (self.final_only and state not in self.FINAL):
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"state": state, "message": message, "time": int(time.time())}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass


# --- swap a directory in, keeping the old one until the update has worked --------------


def swap_in(new_dir, dest):
    """Put new_dir at dest. The old dest is kept as dest + '.old' until discard_old().
    new_dir must be on the same filesystem as dest (the staging folder is made beside it).
    Returns the old path, or None when there was no old install. If the move fails, the
    old install is put back and the error is raised."""
    old = dest + ".old"
    shutil.rmtree(old, ignore_errors=True)
    had_old = os.path.isdir(dest)
    if had_old:
        os.rename(dest, old)
    try:
        os.rename(new_dir, dest)
    except OSError:
        if had_old:
            os.rename(old, dest)
        raise
    return old if had_old else None


def roll_back(dest, old):
    """Undo swap_in: the old install comes back, the new one is removed."""
    shutil.rmtree(dest, ignore_errors=True)
    if old and os.path.isdir(old):
        os.rename(old, dest)


def discard_old(old):
    if old:
        shutil.rmtree(old, ignore_errors=True)


def clean_env():
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(k, None)
    return env


def take_lock():
    """One install at a time (the button and the Phone tab's updater use the same script)."""
    try:
        import fcntl
    except ImportError:  # not Linux (tests, --download-only on a PC)
        return None
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise InstallError("another Earshot install or update is running; try again in a minute") from None
    return f


def download_component(component, workdir, tag):
    asset = asset_name(component, tag)
    base = f"https://github.com/{component['repo']}/releases/download/{tag}/{asset}"
    print(f"Downloading {component['name']} {tag} ...", flush=True)
    data = fetch(base)
    sig_text = fetch(base + ".sig").decode("ascii", "replace")
    check_signature(component, data, sig_text, asset)
    print(f"  signature OK ({len(data):,} bytes, sha256 {hashlib.sha256(data).hexdigest()[:16]}...)")
    archive = os.path.join(workdir, asset)
    with open(archive, "wb") as f:
        f.write(data)
    root = safe_extract(archive, workdir, component["top"])
    os.remove(archive)
    if component["key"] == "frame":
        record_setup_hash(root)
    return root


SETUP_PY = "earshot-system-setup.py"


def record_setup_hash(frame_root):
    """Write the sha256 of the root setup script, as it came out of the signature-checked
    release, beside it. earshot-system-setup.sh checks the script against this before it runs
    anything as root."""
    path = os.path.join(frame_root, SETUP_PY)
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        raise InstallError(f"{SETUP_PY} is missing from the release") from None
    with open(path + ".sha256", "w", encoding="ascii") as f:
        f.write(digest + "\n")


def health_check(engine_dir):
    """The new engine must start: `tether --version` loads the same libraries as tetherd."""
    exe = os.path.join(engine_dir, "tether")
    r = subprocess.run([exe, "--version"], env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        raise InstallError(f"the new engine does not start ({msg[0]}); keeping the old one")


def run_install_sh():
    script = os.path.join(DATA_DIR, "frame", "install.sh")
    if not os.path.isfile(script):
        raise InstallError("install.sh is missing from the release")
    r = subprocess.run(["sh", script], env=clean_env())
    if r.returncode != 0:
        raise InstallError(f"install.sh exited {r.returncode}")


def restart_services():
    """Best effort: bring the (old) services back after a failed update."""
    if shutil.which("systemctl") is None:
        return
    for unit in ("earshot-tetherd.service", "earshot-steamui.service"):
        subprocess.run(["systemctl", "--user", "restart", "--no-block", unit],
                       env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install(args, status):
    if platform.machine() not in ("aarch64", "arm64") and not args.download_only:
        raise InstallError(f"this is for the Steam Frame (ARM64); this computer is {platform.machine()}")
    if not args.download_only and shutil.which("systemctl") is None:
        raise InstallError("systemctl was not found; this installer needs SteamOS")
    lock = None if args.download_only else take_lock()
    try:
        status.set("checking", "Checking for updates...")
        tag = latest_tag(COMPONENTS[0])  # one release holds both parts
        installed = read_versions()
        todo = components_to_update(installed, tag, args.if_newer)
        if not todo:
            print("Earshot is up to date.")
            status.set("uptodate", "Up to date")
            return
        status.set("updating", "Updating...")
        stage_parent = args.download_only or DATA_DIR
        os.makedirs(stage_parent, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=stage_parent) as work:
            roots = [download_component(c, work, tag) for c in todo]
            if args.download_only:
                for c, root in zip(todo, roots):
                    keep = os.path.join(args.download_only, c["top"])
                    shutil.rmtree(keep, ignore_errors=True)
                    shutil.move(root, keep)
                    print(f"  unpacked to {keep}")
                return
            swapped = []
            try:
                for c, root in zip(todo, roots):
                    dest = os.path.join(DATA_DIR, c["dest"])
                    swapped.append((dest, swap_in(root, dest)))
                    if c["key"] == "engine":
                        health_check(dest)
                print("Setting up the services ...", flush=True)
                run_install_sh()
            except BaseException:
                for dest, old in reversed(swapped):
                    roll_back(dest, old)
                restart_services()  # the old install, if install.sh had already stopped it
                raise
            for _, old in swapped:
                discard_old(old)
        for c in todo:
            installed[c["key"]] = tag
        write_versions(installed)
        status.set("updated", f"Updated to {tag}")
    finally:
        if lock:
            lock.close()
    print()
    print("Earshot is installed and running.")
    print("Pair your iPhone in Steam Settings > Bluetooth, then open the Phone tab in the quick menu.")
    if not args.if_newer:
        offer_system_setup()


def system_setup_script():
    return os.path.join(DATA_DIR, "frame", "earshot-system-setup.sh")


def offer_system_setup():
    """If BlueZ is not set up for the iPhone yet, run the explicit, root setup. It lists
    what it will change and asks first; this only starts it when a person is at a terminal."""
    script = system_setup_script()
    if not os.path.isfile(script) or not sys.stdin.isatty():
        return
    r = subprocess.run(["sh", script, "--check"], env=clean_env())
    if r.returncode == 0:
        return  # nothing missing
    print()
    print("Bluetooth on this headset needs a one-time system setup before the iPhone can send")
    print("messages and notifications. It lists each change and asks for your password.")
    subprocess.run(["sh", script], env=clean_env())


def remove_kept_setup():
    """An earlier uninstall kept the setup scripts for the system undo; it is done now."""
    frame = os.path.join(DATA_DIR, "frame")
    if os.path.isdir(frame) and all(n.startswith("earshot-system-setup.") for n in os.listdir(frame)):
        shutil.rmtree(frame, ignore_errors=True)
        try:
            os.rmdir(DATA_DIR)
        except OSError:
            pass


def uninstall(args):
    script = os.path.join(DATA_DIR, "frame", "uninstall.sh")
    undone = not os.path.isfile(SYSTEM_MANIFEST)  # nothing to undo
    if not undone and os.path.isfile(system_setup_script()) and sys.stdin.isatty():
        answer = input("Also undo the Bluetooth system setup Earshot made (needs your password)? [y/N] ")
        if answer.strip().lower() in ("y", "yes"):
            r = subprocess.run(["sh", system_setup_script(), "--undo"], env=clean_env())
            undone = r.returncode == 0 and not os.path.isfile(SYSTEM_MANIFEST)
    if not os.path.isfile(script):
        print("Earshot is not installed.")
        if undone:
            remove_kept_setup()
    else:
        # The setup scripts are the only way to undo the system setup: keep them until it is undone.
        cmd = ["sh", script] + (["--purge"] if args.purge else []) + ([] if undone else ["--keep-setup"])
        r = subprocess.run(cmd, env=clean_env())
        if r.returncode != 0:
            raise InstallError(f"removing Earshot failed (exit {r.returncode})")
    if not undone:
        print("The Bluetooth system setup is still in place, so its scripts were kept in")
        print("  ~/.local/share/earshot/frame/")
        print("To undo it later:  sh ~/.local/share/earshot/frame/earshot-system-setup.sh --undo")
        print("then remove that folder (rm -rf ~/.local/share/earshot), or run this uninstall again and answer y.")
    print("Done.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install, update or remove Earshot on a Steam Frame.")
    ap.add_argument("--uninstall", action="store_true", help="remove Earshot")
    ap.add_argument("--purge", action="store_true",
                    help="with --uninstall: also delete the settings in ~/.config/earshot")
    ap.add_argument("--download-only", metavar="DIR",
                    help="download, check and unpack the releases into DIR without installing")
    ap.add_argument("--if-newer", action="store_true",
                    help="only install when a newer release exists (used by the Phone tab's updater)")
    ap.add_argument("--status-file", metavar="PATH",
                    help="write progress as JSON to PATH (used by the Phone tab's updater)")
    args = ap.parse_args(argv)
    if args.purge and not args.uninstall:
        ap.error("--purge only goes with --uninstall")
    if args.uninstall or args.download_only:
        status = Status(None)
    elif args.status_file:
        status = Status(args.status_file)
    else:
        # Started by hand or from the install button: still tell the Phone tab how it ended,
        # so an earlier failed update stops showing once this run succeeds. Only the final
        # state: a "checking"/"updating" with no running updater would read as stuck.
        status = Status(DEFAULT_STATUS_FILE, final_only=True)

    def on_term(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    try:
        if args.uninstall:
            uninstall(args)
        else:
            install(args, status)
    except (InstallError, OSError, tarfile.TarError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        status.set("failed", f"Update failed: {e}")
        return 1
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        status.set("failed", "Update cancelled")
        return 130
    except Exception as e:  # never leave the Phone tab showing "Updating..."
        print(f"\nerror: {type(e).__name__}: {e}", file=sys.stderr)
        status.set("failed", f"Update failed: {type(e).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
