# On-headset tests

In order: each one decides whether the next is worth running. Run over SSH on the
Frame. Record results at the bottom with the date and SteamOS version.

## 1. System readiness (read-only, about 5 minutes)

```bash
cat /etc/os-release | head -5
pacman -Q bluez bluez-utils bluez-obex pipewire wireplumber 2>&1
bluetoothctl --version
systemctl cat bluetooth | grep -E 'ExecStart|experimental'
cat /sys/module/bluetooth/parameters/disable_ertm
btmgmt info | head -20                       # look for le, secure-conn, advertising
busctl --system introspect org.bluez /org/bluez/hci0 | grep -c AdvertisementMonitor
ls /etc/systemd/system/bluetooth.service.d/ 2>&1
steamos-readonly status 2>&1
sudo -n true && echo "sudo: passwordless" || echo "sudo: needs password or not allowed"
```

Pass: BlueZ 5.86 or newer and `bluez-obex` present (or installable), sudo available,
a controller with LE, advertising and Secure Connections.
Blocker: BlueZ older than 5.86 with no way to update it.

## 2. Engine on the Frame (upstream build, no Earshot code)

Use upstream's `tether-<version>-aarch64.AppImage` (needs glibc 2.38 or newer).

1. `./tether*.AppImage --bt-setup` and apply what it prints (the experimental drop-in,
   the Class of Device unit, ERTM if needed). Note every change made under `/etc`.
2. Start the daemon headless: `dbus-run-session ./tether*.AppImage tetherd` (see
   `docs/HEADLESS.md`), or install the user unit with `tether service`.
3. Pair: `tether --bt-pair <iphone-address>`, confirm the code on both sides. (This is
   the upstream engine on its own. The Phone tab route is tested in 3b.)
4. On the iPhone, under the Frame's Bluetooth entry: Show Notifications, Sync Contacts,
   Phone Favorites, Phone Recents on.
5. Check `tether --bt-status` and `tether --bt-connection`.
6. Receive a text and a notification from another app. Send a reply with the CLI.
7. Ring the phone from another phone; decline it from the Frame.
8. Reboot the Frame and check the setup survived. After the next SteamOS update, check again.

9. **Must verify (Earshot helper, hands-free hand-back).** With the helper running, take
   the headset off (or set Priority to "Always phone") and ring the phone from another phone:
   the phone must ring normally and `busctl --system introspect org.bluez <device path>`
   must show no `telephony0` object. Put the headset on again (or set "Always Frame"). The
   helper restores hands-free by dropping the BR/EDR bearer
   (`org.bluez.Bearer.BREDR1 Disconnect`), because `ConnectProfile` returns success and does
   nothing. Check that Phone tab, Settings shows "Calls: reconnecting..." and then "connected"
   within about 45 s, that `telephony0` is back and that a call rings in the headset. If it
   shows "The call link did not come back", the restore does not work on this stack: record
   it in Results, then fix before release (the helper never reports a restore as done on its
   own).

Pass: steps 5 to 9 work. Also note idle CPU and memory of `tetherd` (`top -p`).

## 3. Pop-up over a running game

While an OpenXR game is running, from SSH:

```bash
/opt/steamvr/bin/linuxarm64/vrcmd --notify "Earshot test: does this show over the game?"
```

Check that it shows in the headset without opening the dashboard, and how long a text it
shows before cutting off.

Pass: it shows. Fail: try a Steam toast raised from the SharedJSContext (the Tragus CEF
route); if neither shows, plan the SteamVR overlay fallback.

## 3b. Earshot package (no release yet)

Before any release exists, install the CI build by hand: download the
`earshot-engine` and `earshot-frame` artifacts from the latest green run on `frame`,
copy both tarballs to the headset, unpack the frame tarball and run its `install.sh`
with the engine tarball beside it (see `frame/install/install.sh` for the expected
layout). Stop the upstream Tether test daemon from test 2 first. Then:

1. `systemctl --user status earshot-tetherd earshot-steamui --no-pager`: both running.
2. `ss -ltn | grep 5134`: nothing (the engine runs with `TETHER_NO_TCP=1`).
3. Restart the headset once (for Steam's DevTools switch). The **Phone** tab appears in the
   quick menu next to Tragus's tab, and the phone's name and connection show in it.
4. **Pair from the Phone tab, with no terminal.** Remove any earlier pairing first (Settings, Forget
   this iPhone, and Forget This Device on the iPhone). Phone tab: **Pair iPhone**. On the iPhone open
   Settings > Bluetooth and tap the headset under Other Devices; both show a 6-digit code; tap Pair on
   the iPhone and Confirm in the tab. Pass: the "Almost done" list appears, then Show Notifications /
   Sync Contacts tick as you turn them on, and `tether --bt-status` shows a dual (BR/EDR + LE) bond.
   Also try: let the 2 minutes run out and tap **Make visible again**; **Pair a different way**
   (search and pick); a failed attempt (leave the iPhone's old entry in place) shows the Forget
   This Device instruction and Try again then works; close the quick menu mid-pairing and reopen it.
   If the engine has no `bt_pair_listen`, the tab falls back to Search by itself.
5. A text arrives: a SteamVR notification shows, the message appears under Messages.
6. Reply from the Phone tab: focusing the text field brings up the Frame keyboard; the
   reply arrives on the other phone; the tab shows "sent".
7. A call: notification plus the Answer/Decline card; Decline ends it on the phone.
8. Settings: switch Priority to "Always phone": no notifications in the headset, calls
   stay on the phone. Back to Auto.
9. `top`: idle CPU and memory of `tetherd` and `earshot-steamui.py` against the budget in
   PLAN.md.
10. `Uninstall-Earshot.desktop` (or `uninstall.sh`): the tab goes, the system setup offers
   its undo, nothing of Tragus is touched.

## 4. Presence sensor

Find a signal a user service can read for "worn" and "asleep". Candidates: SteamVR
events (`VREvent_TrackedDeviceUserInteractionStarted/Ended`,
`VREvent_EnterStandbyMode/LeaveStandbyMode`), a sysfs or input device, logind, or
whatever the "headset off" pause in other Frame apps already uses.

Pass: a reliable worn/not-worn signal within about a second.

## 5. Call audio (v2)

Follow `docs/BLUETOOTH.md`, "Getting the call audio onto the desktop instead", with
the Frame's PipeWire and WirePlumber versions. Then try routing that call audio
on to AirPods connected to the Frame.

## Results

| Date | SteamOS | Test | Result | Notes |
|---|---|---|---|---|
