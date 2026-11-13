# Earshot

iPhone calls, texts and notifications on the Steam Frame, built into the Frame's
own menus.

Earshot is a fork of [Tether](https://github.com/zackb/tether) (MIT), which does the
Bluetooth work: notification mirroring over ANCS, reading and replying to SMS and
iMessage over MAP, contacts over PBAP, and call control over HFP. Earshot adds the
Frame side:

- a **Phone** tab in the Steam Quick Access menu (conversations, calls, settings)
- Steam-style pop-ups for incoming notifications and calls, over whatever is running
- typed replies on the Frame's on-screen keyboard
- device priority: the Frame takes over while you wear it, the phone takes back
  everything when you don't
- an installer, a background service and signed updates.

No Mac and no app on the phone are needed for messages, notifications or calls.

Status: building v1. See [PLAN.md](PLAN.md), [SPEC-v1.md](SPEC-v1.md) and [TESTS.md](TESTS.md).

## Install on the Steam Frame

**Not released yet.** The button below does nothing until the first signed release is
published; this is what it will do.

1. Pair your iPhone in the Frame's Bluetooth settings.
2. Open a web browser on the headset, go to this page and press the button:

   <a href="https://github.com/jimas21/earshot/releases/latest/download/Install-Earshot.desktop"><img src="img/install-button.svg" alt="Install Earshot" width="240" /></a>

3. Open the file it downloads (`Install-Earshot.desktop`) in Desktop Mode. If you are asked
   whether to trust it, say yes. A window opens, downloads the engine and the Phone tab,
   checks their signatures and installs them. No password is needed for this part.
4. If Bluetooth is not set up for the iPhone yet, the same window then lists the three
   system changes it needs (BlueZ experimental mode, the Bluetooth class, L2CAP ERTM), asks
   you to confirm, and asks for your password once. It changes files under `/etc` only, and
   only what is missing. Your account needs a password (`passwd` in a terminal sets one).
5. Restart the headset once if it says so, then pair the iPhone and open the **Phone** tab
   in the quick menu.

### What the first install trusts

The button runs `earshot-frame-install.py` straight from the latest GitHub release, and
that first download is **not signature-checked**: the installer carries the signing key
that checks everything after it (the engine, the Phone tab and every update), so it cannot
check itself. Trust at that moment rests on GitHub and on the repository owner's account.
If that is not enough for you, check the installer before it runs:

```sh
curl -LO https://github.com/jimas21/earshot/releases/latest/download/earshot-frame-install.py
sha256sum earshot-frame-install.py        # compare with the release notes and earshot-frame.sha256
python3 -I -c "import re,base64,hashlib;k=re.search(r'^PUBKEY = \"([^\"]+)\"',open('earshot-frame-install.py').read(),re.M).group(1);print(hashlib.sha256(base64.b64decode(k)).hexdigest())"
python3 earshot-frame-install.py          # then run it yourself instead of using the button
```

The third command prints the signing key's SHA-256 fingerprint; it should match
`(published here with the first release)`. Every later download (engine, Phone tab, updates) is verified against that key before
anything is unpacked.

**The one root step** (BlueZ experimental mode, the Class of Device unit, L2CAP ERTM) runs
a copy of the setup script made by root in a root-only folder, only after its SHA-256
matches the value the installer recorded when it checked the release signature, using
`/usr/bin/python3`. A program already running as your user could still replace both the
script and that record, or sit in front of the password prompt; Earshot protects against a
damaged or swapped file, not against a compromised account.

Earshot's engine runs with `TETHER_NO_TCP=1`: nothing listens on the network (no
5134/tcp, no mDNS); everything goes over Bluetooth.

**Updates:** Earshot checks for updates itself (Phone tab, Settings, Updates). A failed
update keeps the old install.
**Remove:** open
[`Uninstall-Earshot.desktop`](https://github.com/jimas21/earshot/releases/latest/download/Uninstall-Earshot.desktop)
the same way. It offers to undo the system setup too, using the record it kept in
`/etc/earshot/manifest.json`; your settings stay unless you ask for `--purge`.

It lives in your home folder (`~/.local/share/earshot`), runs as two user services
(`earshot-tetherd`, `earshot-steamui`) and never touches Tragus. It shares Steam's
remote-debugging switch (`~/.steam/steam/.cef-enable-remote-debugging`) with it and only
removes that file if Earshot made it and Tragus is not installed.

## Branches

- `main` tracks upstream Tether unchanged.
- `frame` is where Earshot is developed. Engine fixes are kept as small, separate
  commits so they can be offered back upstream.

Earshot is not affiliated with Valve or Apple.
