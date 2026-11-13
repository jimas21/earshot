# Earshot

iPhone calls, texts and notifications on the Steam Frame, built into the Frame's own menus.

Earshot is a fork of [Tether](https://github.com/zackb/tether) (MIT), which does the Bluetooth
work: notification mirroring over ANCS, reading and replying to SMS and iMessage over MAP,
contacts over PBAP, and call control over HFP. Earshot adds the Frame side:

- a **Phone** tab in the Steam quick menu (conversations, calls, settings)
- Steam-style pop-ups for incoming notifications and calls, over whatever is running
- typed replies on the Frame's on-screen keyboard
- device priority: the Frame takes over while you wear it, the phone takes back everything when
  you do not
- an installer, a background service and signed updates.

No Mac and no app on the phone are needed. **No password, no root and no system files:** Earshot
installs and runs as your normal user, on the Frame's stock Bluetooth, the way the Tragus earbuds
app does.

Status: building v1. See [frame/PLAN.md](frame/PLAN.md), [frame/SPEC-v1.md](frame/SPEC-v1.md) and
[frame/TESTS.md](frame/TESTS.md).

## Install on the Steam Frame

**Not released yet.** The button does nothing until the first signed release is published; this
is what it will do. There is no password to enter and nothing to type.

1. In a web browser on the headset, open this page and press the button:

   <a href="https://github.com/jimas21/earshot/releases/latest/download/Install-Earshot.desktop"><img src="frame/img/install-button.svg" alt="Install Earshot" width="240" /></a>

2. In Desktop Mode, open the file it downloads, `Install-Earshot.desktop`. If you are asked
   whether to trust it, say yes. A window opens, downloads the engine and the Phone tab, checks
   their signatures, installs them and starts them. Everything goes under your home folder.
3. Restart the headset once, so Steam picks up the Phone tab.
4. Open the quick menu, choose **Phone**, then tap **Pair iPhone**. Earshot makes the headset
   visible; on the iPhone open Settings > Bluetooth and tap the headset, then compare the code
   shown in the Phone tab and tap Confirm. Finally switch on the permissions the tab lists
   (Show Notifications, and Messages and Contacts when the iPhone offers them).

Do not pair in the Frame's own Bluetooth settings: a bond made that way cannot carry
notifications. Pairing from the Phone tab always works from the iPhone's side, which is the one
that produces a bond with notifications.

### What the first install trusts

The button runs `earshot-frame-install.py` straight from the latest GitHub release, and that
first download is **not signature-checked**: the installer carries the signing key that checks
everything after it (the engine, the Phone tab and every update), so it cannot check itself.
Trust at that moment rests on GitHub and on the repository owner's account. If that is not
enough for you, check the installer before it runs:

```sh
curl -LO https://github.com/jimas21/earshot/releases/latest/download/earshot-frame-install.py
sha256sum earshot-frame-install.py        # compare with the release notes and earshot-frame.sha256
python3 -I -c "import re,base64,hashlib;k=re.search(r'^PUBKEY = \"([^\"]+)\"',open('earshot-frame-install.py').read(),re.M).group(1);print(hashlib.sha256(base64.b64decode(k)).hexdigest())"
python3 earshot-frame-install.py          # then run it yourself instead of using the button
```

The third command prints the signing key's SHA-256 fingerprint. It should be:

```
78e1b408f2949b742e06195f89ae0375d4d0b726e0041345f3f37d4ad910e85a
```

Every later download (engine, Phone tab, updates) is verified against that key before anything is
unpacked, and must carry a `VERSION` file that matches its release tag, so an older signed release
cannot be passed off as a newer one.

Earshot's engine runs with `TETHER_NO_TCP=1`: nothing listens on the network (no 5134/tcp, no
mDNS); everything goes over Bluetooth.

## What works without root, and what depends on your iPhone

Earshot uses what an ordinary user can do on stock BlueZ: the headset is made discoverable for a
couple of minutes when you tap **Pair iPhone**, a pairing agent confirms the code, and the iPhone
reconnects by itself afterwards. It cannot change the Bluetooth adapter's class of device or the
kernel's L2CAP settings, so it only reports them. Whether the iPhone offers the Messages and
Contacts switches for the headset is decided on the iPhone; if they do not appear, the Phone tab
says so instead of hiding it.

## Updates and removing it

**Updates:** Earshot checks for updates itself (Phone tab, Settings, Updates). A failed update
keeps the old install.

**Remove:** open
[`Uninstall-Earshot.desktop`](https://github.com/jimas21/earshot/releases/latest/download/Uninstall-Earshot.desktop)
the same way. It removes Earshot's services and files, and **also deletes what the engine kept
about your phone**: the stored messages and contacts, its settings and encryption key, and its
state, all kept in `~/.local/share/earshot/engine-home` (a Tether installed separately keeps its own
directories and is not touched). Earshot's own settings in `~/.config/earshot` stay unless you remove them too (`--uninstall --purge`). To
make the iPhone forget the headset, use Forget This Device on the iPhone, or **Forget this
iPhone** in the Phone tab's Settings.

Earshot lives in your home folder (`~/.local/share/earshot`), runs as two user services
(`earshot-tetherd`, `earshot-steamui`) and never touches Tragus. It shares Steam's
remote-debugging switch (`~/.steam/steam/.cef-enable-remote-debugging`) with it and only
removes that file if Earshot made it and Tragus is not installed.

## Upstream Tether

The original Tether README (desktop app, other Linux machines, building from source) is kept in
[docs/UPSTREAM-README.md](docs/UPSTREAM-README.md). Engine changes are small separate commits so they can be
offered back upstream.

## Branches

- `main` tracks upstream Tether unchanged.
- `frame` is where Earshot is developed.

Earshot is not affiliated with Valve or Apple.
