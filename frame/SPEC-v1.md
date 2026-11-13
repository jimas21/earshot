# Earshot v1 spec

Built and tested without a headset first (fakes for Tether, Steam and SteamVR), then
proven on the Frame with [TESTS.md](TESTS.md). Patterns follow the Tragus earbuds
companion for the Frame (menu injection, SteamVR notifications, installer, signing,
self-update) so both behave the same way on the headset.

## Parts

| Part | Path | What it is |
|---|---|---|
| Engine | `src/` (upstream) | `tetherd`, built headless for aarch64: `TETHER_BUILD_TUI=OFF`, `TETHER_CAMERA=OFF`, `TETHER_BUILD_EXTENSIONS=OFF`, no GTK, no tests. Non-system libraries bundled under `lib/` with an `$ORIGIN/lib` RUNPATH. |
| Helper | `frame/menu/earshot-steamui.py` | Python 3, standard library only, one systemd user service. Bridges Tether and Steam's UI, applies the priority rules, raises SteamVR notifications. |
| Menu | `frame/menu/earshot-menu.js` | The Phone tab, injected into Steam's VR quick menu. |
| Installer | `frame/install/` | `Install-Earshot.desktop`, `install.sh`, `uninstall.sh`, `earshot-frame-install.py` (signed download, versions, self-update). |
| CI | `.github/workflows/earshot-arm64.yml` | Builds the engine and packages the release tarballs on an ARM64 runner. Publishing a release is a separate manual step. |

## Data flow

```
tetherd ──UNIX socket (docs/PROTOCOL.md)──► earshot-steamui.py ──DevTools (127.0.0.1:8080)──► earshot-menu.js
                                                  │  Runtime.evaluate: state pushed into the page
                                                  │  Runtime.addBinding: actions from the page back to the helper
                                                  └─► vrcmd --notify (SteamVR notifications)
```

No new listening port. The helper already holds Steam's DevTools connection to inject
the menu, so state and actions travel over it. Settings live in
`~/.config/earshot/settings.json` (mode 0600).

## Helper

- Connects to `$XDG_RUNTIME_DIR/tether` (fallback `/run/user/<uid>/tether`), sends
  `protocol_info`, subscribes to the Bluetooth broadcasts, and reconnects with backoff
  when `tetherd` restarts.
- Keeps a small in-memory model: connection state, notifications (latest 100),
  conversations from MAP, call state and recent calls, contacts for names.
- Injects `earshot-menu.js` into Steam's VR pages the way Tragus does (find the pages,
  re-inject after Steam reloads, remove on stop). Element IDs and storage keys are
  prefixed `earshot-` so it coexists with Tragus in the same pages.
- **Notifications:** when worn, each new phone notification raises a SteamVR
  notification (`/opt/steamvr/bin/linuxarm64/vrcmd --notify`): app name, title and
  text, trimmed. Rate limit: bursts are collapsed ("3 new messages from …").
  Per-app mute list in settings.
- **Calls:** an incoming call raises a SteamVR notification and puts an Answer/Decline
  card at the top of the Phone tab and on its bar indicator. Answer and Decline use the
  ANCS positive/negative actions (`bt_notification_action`), or the call commands when
  HFP is up.
- **Replies:** the page sends `{to, text}`; the helper sends it through Tether's MAP
  send command and reports success or the error back to the page.
- **Presence:** a pluggable detector with a manual override in settings
  (Auto / Always Frame / Always phone). Candidates to implement behind one interface,
  each tested against a fake: SteamVR user-interaction / standby events read through
  `vrcmd`, and any sysfs or input source found on the headset. Until one is proven,
  Auto behaves like Always Frame while SteamVR is running.
- **Priority rules** (see PLAN.md): when not worn, no SteamVR notifications, calls left
  to the phone (the HFP profile is disconnected through BlueZ's `Device1.DisconnectProfile`
  via `busctl`, and reconnected when worn again); notifications are still recorded.
- Logs at warning level by default; no message text in logs.

## Menu (Phone tab)

Frame style, matching Steam's own quick-menu components (reuse the Tragus menu's
element-finding and styling approach):

- **Header:** phone name, connected / not connected, battery if Tether reports it.
- **Calls:** an active or incoming call card at the top; recent calls below.
- **Notifications:** newest first; actions shown only when the notification has them;
  Clear all.
- **Messages:** conversations (1:1) newest first; opening one shows recent messages and a
  text field. Focusing the field must bring up the Frame's keyboard (Steam's own text
  input, not a custom keyboard). Send shows sending / sent / failed.
- **Settings:** priority mode, per-app mute, notification preview on/off, setup status
  (from `tether --bt-setup` / `--bt-status`), updates.
- **Pair iPhone** (first thing shown with no phone paired, and in Settings): make the headset
  visible and wait for the iPhone, show the code with Confirm / Cancel, explain each failure in
  plain words with one fix button, then an "Almost done" checklist of the iPhone's switches.
  Scan-and-pick is the fallback. All state is in the helper. No terminal, no root.
- **Not set up yet:** if the engine or system setup is missing, the tab shows what is
  missing and how to fix it instead of empty lists.

## Installer

- `Install-Earshot.desktop`: the one-click entry from the README, as with Tragus and
  Laterna. Runs `install.sh` from the latest signed release.
- `install.sh`, no root: copies the engine and helper to `~/.local/share/earshot/`,
  installs and starts the user units (`earshot-tetherd.service`,
  `earshot-steamui.service`), creates `~/.steam/steam/.cef-enable-remote-debugging` if
  missing (and records that it did).
- **System setup (root, separate, explicit):** BlueZ's experimental mode drop-in, the
  Class of Device unit, and the L2CAP ERTM fix if needed. The installer lists exactly
  what it will change, asks for the password once through a graphical prompt in
  Desktop Mode, and writes a manifest so `uninstall.sh` can undo each change. Skipped if
  already in place. It never touches anything Tragus installed.
- Signed releases: ed25519, public key pinned in the installer, as Tragus does.
  Self-update every 6 hours, on by default, with a setting to turn it off.

## Tests (no headset)

- Helper: unit tests with a fake `tetherd` socket server that replays real protocol
  messages, a fake DevTools endpoint, and a fake `vrcmd` that records calls.
  Cover reconnects, the priority rules, burst collapsing, replies, and calls.
- Menu: runs under jsdom with a stub of Steam's page structure and the binding.
- Installer: unit tests in the style of Tragus's installer tests (signature check,
  version comparison, manifest and undo, no-root path).
- CI builds the engine for aarch64 and checks `ldd` shows nothing missing against a
  SteamOS-like image where possible.

## Out of scope for v1

Call audio in the headset, AirPods routing, placing calls, group chats, silencing the
phone. See PLAN.md.
