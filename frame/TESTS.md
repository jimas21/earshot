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
3. Pair: `tether --bt-pair <iphone-address>`, confirm the code on both sides.
4. On the iPhone, under the Frame's Bluetooth entry: Show Notifications, Sync Contacts,
   Phone Favorites, Phone Recents on.
5. Check `tether --bt-status` and `tether --bt-connection`.
6. Receive a text and a notification from another app. Send a reply with the CLI.
7. Ring the phone from another phone; decline it from the Frame.
8. Reboot the Frame and check the setup survived. After the next SteamOS update, check again.

Pass: steps 5 to 8 work. Also note idle CPU and memory of `tetherd` (`top -p`).

## 3. Pop-up over a running game

Using the Tragus CEF route, raise a Steam toast from the SharedJSContext while an
OpenXR game is running. Check whether it shows in the headset without opening the
dashboard.

Pass: it shows. Fail: plan the SteamVR overlay fallback.

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
