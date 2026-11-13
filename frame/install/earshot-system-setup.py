#!/usr/bin/env python3
"""Earshot's one-time Bluetooth system setup for the Steam Frame (SteamOS, root).

Apple's phone profiles need three things the headset does not have by default:

  experimental   bluetoothd runs with --experimental (a systemd drop-in). The phone's
                 notification service (ANCS) needs BlueZ's bearer API, and it has to be
                 on before the phone is paired.
  btclass        the Bluetooth Class of Device is "A/V Hands-Free" and stays that way
                 across bluetoothd restarts. iOS only offers Show Notifications and
                 Sync Contacts to a device that presents this class.
  ertm           L2CAP ERTM is on. Some gaming distros turn it off for an old Xbox
                 controller; obexd then cannot open the message and contact channels.

This script applies only what is missing, lists each change first, and writes
/etc/earshot/manifest.json so --undo reverts exactly what it did and nothing else. It
only creates files named earshot-* and never touches anything another program installed
(Tragus, a Tether package). Only /etc is written: SteamOS keeps / read-only, and /etc is
the part that is writable and kept.

  --check   exit 0 when nothing is missing, 10 when changes are needed, 11 when only
            blockers remain, 1 on error. No root needed.
  --plan    print what would change. No root needed.
  --apply   make the changes (root). Needs --yes: the confirmation is the caller's.
  --undo    revert what the manifest says this script did (root). Needs --yes.

All decisions are in plan(), a pure function of State, so they are tested without root.
Python standard library only.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

MANIFEST_REL = "etc/earshot/manifest.json"
DROPIN_REL = "etc/systemd/system/bluetooth.service.d/90-earshot-experimental.conf"
BTCLASS_UNIT_REL = "etc/systemd/system/earshot-btclass@.service"
ERTM_UNIT_REL = "etc/systemd/system/earshot-ertm.service"
ERTM_PARAM_REL = "sys/module/bluetooth/parameters/disable_ertm"
MIN_BLUEZ = (5, 86)

BTCLASS_UNIT = """[Unit]
# Set the Bluetooth Class of Device to A/V Hands-Free (major 4, minor 8) for Earshot.
# bluetoothd resets the class to its own default each time it starts; without this the
# phone refuses the message and contact channels. Written by earshot-system-setup.sh and
# removed by its --undo. %i is a hint: a controller that re-enumerates comes back under
# the next free index, so the unit looks for the live adapter itself.
Description=Set Bluetooth Class of Device to A/V Hands-Free for Earshot
After=bluetooth.service
PartOf=bluetooth.service

[Service]
Type=oneshot
RemainAfterExit=yes
# btmgmt epolls its stdin before it runs a command and rejects /dev/null, so pipe into it.
ExecStart=/bin/sh -c 'for t in $$(seq 30); do \\
    a=%i; \\
    [ -e /sys/class/bluetooth/$$a ] || a=$$(ls /sys/class/bluetooth 2>/dev/null | head -n1); \\
    if [ -n "$$a" ]; then \\
        echo | btmgmt --index $$a class 4 8 >/dev/null 2>&1; \\
        echo | btmgmt --index $$a info 2>/dev/null | grep -q "class 0x..0408" && exit 0; \\
    fi; \\
    sleep 1; \\
done; exit 1'
TimeoutStartSec=60

[Install]
WantedBy=bluetooth.service
"""

ERTM_UNIT = """[Unit]
# Turn L2CAP ERTM back on for Earshot: obexd (messages, contacts) needs it, and some gaming
# distros switch it off for an old Xbox controller. Written by earshot-system-setup.sh and
# removed by its --undo, which puts the previous value back.
Description=Enable Bluetooth L2CAP ERTM for Earshot
After=bluetooth.service
PartOf=bluetooth.service

[Service]
Type=oneshot
RemainAfterExit=yes
# The parameter exists once the bluetooth module is loaded; wait for it, then write.
ExecStart=/bin/sh -c 'for t in $$(seq 30); do \\
    if [ -w /sys/module/bluetooth/parameters/disable_ertm ]; then \\
        echo N > /sys/module/bluetooth/parameters/disable_ertm && exit 0; \\
    fi; \\
    sleep 1; \\
done; exit 1'

[Install]
WantedBy=bluetooth.service
"""


class SetupError(Exception):
    pass


# --- reading the system ----------------------------------------------------------------


def _read(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _exists(root, rel):
    return os.path.exists(os.path.join(root, rel))


def _listdir(root, rel):
    try:
        return sorted(os.listdir(os.path.join(root, rel)))
    except OSError:
        return []


def effective_execstart(root):
    """The ExecStart bluetooth.service would run: the vendor unit, then /etc override and
    drop-ins in systemd's order (an empty ExecStart= resets the list). None if unknown."""
    sources = []
    for rel in ("usr/lib/systemd/system/bluetooth.service", "lib/systemd/system/bluetooth.service",
                "etc/systemd/system/bluetooth.service"):
        text = _read(root, rel)
        if text is not None:
            sources.append(text)
    dropins = {}
    for d in ("usr/lib/systemd/system/bluetooth.service.d", "etc/systemd/system/bluetooth.service.d"):
        for name in _listdir(root, d):
            if name.endswith(".conf"):
                dropins[name] = _read(root, os.path.join(d, name))  # /etc shadows /usr/lib
    for name in sorted(dropins):
        if dropins[name] is not None:
            sources.append(dropins[name])
    current = None
    for text in sources:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("ExecStart="):
                value = line[len("ExecStart="):].strip()
                current = value or None
    return current


def parse_version(text):
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def inspect(root="/", run=None, which=shutil.which):
    """Everything plan() decides from. `run(cmd) -> (rc, stdout)` is injectable."""
    run = run or _run
    state = {}
    execstart = effective_execstart(root)
    state["execstart"] = execstart
    main_conf = _read(root, "etc/bluetooth/main.conf") or ""
    state["experimental"] = bool(
        (execstart and "--experimental" in execstart.split())
        or re.search(r"^\s*Experimental\s*=\s*true", main_conf, re.M | re.I)
    )
    bluetoothd = None
    for rel in ("usr/lib/bluetooth/bluetoothd", "usr/libexec/bluetooth/bluetoothd"):
        if _exists(root, rel):
            bluetoothd = "/" + rel
            break
    state["bluetoothd"] = bluetoothd
    rc, out = run([bluetoothd, "--version"]) if bluetoothd else (1, "")
    state["bluez_version"] = parse_version(out) if rc == 0 else None
    state["btmgmt"] = bool(which("btmgmt"))
    adapters = [a for a in _listdir(root, "sys/class/bluetooth") if re.fullmatch(r"hci\d+", a)]
    state["adapter"] = adapters[0] if adapters else "hci0"
    state["adapters"] = adapters
    wants = _listdir(root, "etc/systemd/system/bluetooth.service.wants")
    wants += _listdir(root, "usr/lib/systemd/system/bluetooth.service.wants")
    state["btclass_enabled"] = any("btclass@" in w for w in wants)
    state["ertm_fix_enabled"] = "earshot-ertm.service" in wants
    param = _read(root, ERTM_PARAM_REL)
    state["ertm_disabled"] = None if param is None else param.strip().upper().startswith(("Y", "1"))
    return state


# --- deciding what to do (pure) --------------------------------------------------------


def plan(state):
    """Returns (changes, blockers, warnings). Each change is a dict with id, title, detail."""
    changes, blockers, warnings = [], [], []
    version = state.get("bluez_version")
    if version is not None and version < MIN_BLUEZ:
        warnings.append(
            "BlueZ %d.%d is older than %d.%d. Notifications and messages need the newer one, and "
            "this setup cannot update it." % (version + MIN_BLUEZ)
        )
    if not state.get("experimental"):
        if not state.get("execstart") and not state.get("bluetoothd"):
            blockers.append("bluetoothd was not found, so BlueZ's experimental mode cannot be turned on.")
        else:
            changes.append({
                "id": "experimental",
                "title": "Run bluetoothd with --experimental",
                "detail": "Adds /etc/systemd/system/bluetooth.service.d/90-earshot-experimental.conf and "
                          "restarts bluetooth.service (Bluetooth devices reconnect in a few seconds).",
            })
    if not state.get("btclass_enabled"):
        if not state.get("btmgmt"):
            blockers.append("btmgmt was not found (it is part of bluez-utils), so the Bluetooth class "
                            "cannot be set. Install bluez-utils and run this again.")
        else:
            changes.append({
                "id": "btclass",
                "title": "Keep the Bluetooth class at A/V Hands-Free",
                "detail": "Adds the unit /etc/systemd/system/earshot-btclass@.service and enables it for %s, "
                          "so the class survives bluetoothd restarts." % state.get("adapter", "hci0"),
            })
    if state.get("ertm_disabled") and not state.get("ertm_fix_enabled"):
        changes.append({
            "id": "ertm",
            "title": "Turn L2CAP ERTM on",
            "detail": "ERTM is switched off on this system (messages and contacts cannot connect). Adds "
                      "/etc/systemd/system/earshot-ertm.service, which switches it on at every boot, and "
                      "switches it on now. Undo puts the old value back.",
        })
    return changes, blockers, warnings


def render_plan(changes, blockers, warnings):
    lines = []
    if changes:
        lines.append("Earshot will make these changes (root, under /etc only):")
        for i, c in enumerate(changes, 1):
            lines.append("  %d. %s" % (i, c["title"]))
            lines.append("     %s" % c["detail"])
        lines.append("A record of them is kept in /etc/earshot/manifest.json so they can be undone.")
    else:
        lines.append("Nothing to change: Bluetooth is already set up for Earshot.")
    for b in blockers:
        lines.append("Cannot fix here: " + b)
    for w in warnings:
        lines.append("Note: " + w)
    return "\n".join(lines)


def exit_code_for(changes, blockers):
    return 10 if changes else (11 if blockers else 0)


def dropin_text(execstart, bluetoothd):
    """The drop-in body: the unit's own command line plus --experimental."""
    cmd = execstart or bluetoothd
    if not cmd:
        raise SetupError("no bluetoothd command line to extend")
    return "[Service]\nExecStart=\nExecStart=%s --experimental\n" % cmd


# --- applying and undoing ---------------------------------------------------------------


def _run(cmd):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(root, rel, text, record):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    record["files"].append({"path": "/" + rel, "sha256": _sha(text)})


def load_manifest(root):
    text = _read(root, MANIFEST_REL)
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        raise SetupError("/etc/earshot/manifest.json is damaged; nothing was changed")
    return data if isinstance(data, dict) else None


def save_manifest(root, record):
    path = os.path.join(root, MANIFEST_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def apply(root, changes, state, run=None, say=print):
    """Make the planned changes. The manifest is written as each change lands, so an
    interrupted run can still be undone."""
    run = run or _run
    record = load_manifest(root) or {"version": 1, "files": [], "units": [], "restarted_bluetooth": False}
    record["time"] = int(time.time())
    ids = {c["id"] for c in changes}
    need_restart = False
    if "experimental" in ids:
        _write(root, DROPIN_REL, dropin_text(state.get("execstart"), state.get("bluetoothd")), record)
        need_restart = True
        save_manifest(root, record)
        say("  wrote the bluetoothd --experimental drop-in")
    if "btclass" in ids:
        _write(root, BTCLASS_UNIT_REL, BTCLASS_UNIT, record)
        save_manifest(root, record)
        say("  wrote earshot-btclass@.service")
    if "ertm" in ids:
        _write(root, ERTM_UNIT_REL, ERTM_UNIT, record)
        record["ertm_was_disabled"] = True
        save_manifest(root, record)
        say("  wrote earshot-ertm.service")
    if not ids:
        return record
    run(["systemctl", "daemon-reload"])
    new_units = []
    if "btclass" in ids:
        new_units.append("earshot-btclass@%s.service" % state.get("adapter", "hci0"))
    if "ertm" in ids:
        new_units.append("earshot-ertm.service")
    for unit in new_units:
        rc, out = run(["systemctl", "enable", unit])
        if rc != 0:
            raise SetupError("could not enable %s: %s" % (unit, out.strip()))
        record["units"].append(unit)
        save_manifest(root, record)
        say("  enabled " + unit)
    if need_restart:
        record["restarted_bluetooth"] = True
        save_manifest(root, record)
        rc, out = run(["systemctl", "restart", "bluetooth.service"])
        if rc != 0:
            raise SetupError("bluetooth.service did not restart: %s" % out.strip())
        say("  restarted bluetooth.service")
    for unit in new_units:
        run(["systemctl", "restart", unit])
    if "ertm" in ids:
        # the unit above set it; this covers a unit that raced the module loading
        try:
            with open(os.path.join(root, ERTM_PARAM_REL), "w") as f:
                f.write("N")
        except OSError:
            pass
    return record


def undo_plan(record):
    """Plain-language list of what --undo will revert."""
    if not record:
        return ["Nothing to undo: Earshot has not changed this system's Bluetooth setup."]
    lines = ["Earshot will revert these changes:"]
    for f in record.get("files", []):
        lines.append("  remove " + f["path"])
    for u in record.get("units", []):
        lines.append("  disable " + u)
    if record.get("ertm_was_disabled"):
        lines.append("  switch L2CAP ERTM back off (its value before Earshot)")
    if record.get("restarted_bluetooth"):
        lines.append("  restart bluetooth.service")
    return lines


def undo(root, run=None, say=print):
    """Revert exactly what the manifest lists. A file changed since we wrote it is left alone."""
    run = run or _run
    record = load_manifest(root)
    if not record:
        say("Nothing to undo.")
        return []
    left = []
    for unit in reversed(record.get("units", [])):
        run(["systemctl", "disable", "--now", unit])
        say("  disabled " + unit)
    for f in reversed(record.get("files", [])):
        rel = f["path"].lstrip("/")
        path = os.path.join(root, rel)
        text = _read(root, rel)
        if text is None:
            continue
        if _sha(text) != f.get("sha256"):
            left.append(f["path"])
            say("  left %s: it was changed after Earshot wrote it" % f["path"])
            continue
        os.remove(path)
        say("  removed " + f["path"])
        parent = os.path.dirname(path)
        try:
            if os.path.basename(parent) == "bluetooth.service.d":
                os.rmdir(parent)  # only if we left it empty
        except OSError:
            pass
    if record.get("ertm_was_disabled"):
        try:
            with open(os.path.join(root, ERTM_PARAM_REL), "w") as f:
                f.write("Y")
            say("  put L2CAP ERTM back to its old value")
        except OSError:
            pass
    run(["systemctl", "daemon-reload"])
    if record.get("restarted_bluetooth"):
        run(["systemctl", "restart", "bluetooth.service"])
        say("  restarted bluetooth.service")
    try:
        os.remove(os.path.join(root, MANIFEST_REL))
        os.rmdir(os.path.dirname(os.path.join(root, MANIFEST_REL)))
    except OSError:
        pass
    return left


def main(argv=None):
    ap = argparse.ArgumentParser(description="Earshot's Bluetooth system setup for the Steam Frame.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 0 if nothing is missing, 10 if changes are needed")
    mode.add_argument("--plan", action="store_true", help="print what would change")
    mode.add_argument("--apply", action="store_true", help="make the changes (root)")
    mode.add_argument("--undo", action="store_true", help="revert what this script did (root)")
    mode.add_argument("--undo-plan", action="store_true", help="print what --undo would revert")
    ap.add_argument("--yes", action="store_true", help="confirmed by the caller; required with --apply and --undo")
    ap.add_argument("--root", default="/", help="treat this folder as / (tests)")
    args = ap.parse_args(argv)
    root = args.root
    try:
        if args.undo_plan:
            print("\n".join(undo_plan(load_manifest(root))))
            return 0
        if args.undo:
            _need_root(args)
            undo(root)
            print("Done.")
            return 0
        state = inspect(root)
        changes, blockers, warnings = plan(state)
        if args.check or args.plan or not args.apply:
            print(render_plan(changes, blockers, warnings))
            return exit_code_for(changes, blockers)
        _need_root(args)
        if not changes:
            print(render_plan(changes, blockers, warnings))
            return 0
        print(render_plan(changes, blockers, warnings))
        apply(root, changes, state)
        print("Done.")
        return 0
    except SetupError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


def _need_root(args):
    if not args.yes:
        raise SetupError("--yes is required: the confirmation comes from the caller")
    if args.root == "/" and hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SetupError("this needs root; run earshot-system-setup.sh, which asks for your password")


if __name__ == "__main__":
    sys.exit(main())
