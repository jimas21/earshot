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
