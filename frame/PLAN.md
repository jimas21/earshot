# Earshot plan

## Goal

While the Steam Frame is on your head, your iPhone's notifications, texts and calls
come to you in the headset, in the Frame's own UI, and you can answer them without
taking it off. When it is off your head, everything stays on the phone as normal.
It must feel built in, and it must stay light.

## Architecture

```
iPhone ── BLE (ANCS) ──────────┐
       └─ Classic BT (MAP, PBAP, HFP) ─┐
                                ▼
                     BlueZ (bluetoothd --experimental, obexd)
                                ▼
                tetherd (headless, stripped build)  ◄── engine, from upstream
                                ▼  UNIX socket ($XDG_RUNTIME_DIR/tether), docs/PROTOCOL.md
                earshot-frame (bridge service)       ◄── new: routing rules, presence
                                ▼  loopback WebSocket
                Steam UI layer (injected via CEF)    ◄── new: Phone tab, pop-ups, keyboard
```

- **Engine:** `tetherd` built headless: no GTK, TUI, camera, extensions, Wi-Fi
  server or clipboard (`TETHER_BUILD_TUI=OFF`, `TETHER_CAMERA=OFF`,
  `TETHER_BUILD_EXTENSIONS=OFF`, GTK off). Its AirPods feature stays off: Tragus owns
  the earbuds on the Frame.
- **Bridge (`earshot-frame`):** a small user service. It talks to `tetherd` over its
  documented socket protocol, applies the priority rules below, and serves the UI
  over a loopback WebSocket, as Tragus does.
- **UI layer:** injected into Steam's own client UI through CEF remote debugging,
  the route Tragus already uses on the Frame (a Quick Access tab plus a top-bar
  indicator). Pop-ups use Steam's own toast component if it can show over a running
  game. First choice: SteamVR's own notifications (`vrcmd --notify`), which the Tragus
  earbuds companion already uses on the Frame. Fallback: a small SteamVR overlay drawn only while a
  notification is showing, styled like Steam's toasts.

## Scope

### v1: see and act

- Notification pop-ups for every app (ANCS), with the app's name, the title and the text.
- Incoming call pop-up: caller, Accept, Decline (ANCS actions, already in the engine).
- Phone tab: recent notifications, recent calls, settings, connection state.
- Typed replies to 1:1 texts and iMessages over MAP, using the Frame keyboard, from
  a pop-up's Reply button or the Phone tab.
- Priority rules (below), using the headset's presence sensor.
- Installer, user services, signed releases, self-update (Tragus's pattern).

### v2: talk

- Call audio in the headset: HFP handed to PipeWire (`docs/BLUETOOTH.md`, "Getting
  the call audio onto the desktop instead").
- AirPods connected to the Frame take the call audio when present.
- Place calls from contacts and recents.

### Later

- Silence the phone while the Frame has priority. Only an Apple Watch can do this
  natively; a Focus-mode route needs testing.
- Group chats: MAP gives no conversation ID or participant list, so this stays
  conservative, as upstream does.

### Not possible (iOS limits)

No attachments, reactions, typing indicators or read receipts. SMS and iMessage
look the same over MAP. Sent history comes only from what Earshot sent itself.

## Priority rules

| State | Behaviour |
|---|---|
| Frame worn | Frame first: pop-ups show, calls ring in the headset |
| Frame worn, AirPods on the Frame | As above; call audio goes to the AirPods (v2) |
| Frame on, not worn | Nothing shows. The call link is released so the phone handles calls. Notifications are kept for the Phone tab |
| Frame asleep or off | Bluetooth links are gone; the phone behaves as if Earshot did not exist |

The phone still rings and buzzes while the Frame has priority (see Later).

## Resource budget (release requirements)

- Idle: under 1% of one CPU core, about 30 MB RAM across `tetherd` and the bridge,
  no GPU.
- Nothing drawn when nothing is shown.
- No measurable change in game frame time.
- No noticeable extra battery drain over an hour of play.

Measured on the headset before each release. Anything over budget is cut or made
optional.

## Upstream policy

- `main` mirrors upstream. `frame` is rebased or merged onto it regularly.
- Engine changes are separate commits with upstream-style messages, offered as pull
  requests once proven on the Frame.
- Everything Frame-specific lives under `frame/`, so upstream merges stay clean.

## Risks, in order

1. **System setup on SteamOS.** The engine needs root-level setup: `bluetoothd
   --experimental` (a systemd drop-in), a Class of Device unit, BlueZ 5.86 or newer,
   and L2CAP ERTM enabled (gaming distros often set `bluetooth.disable_ertm=1` for
   old Xbox controllers). Whether this survives SteamOS updates is unknown.
2. **Pop-ups over a running game.** Unproven for Steam's own toasts. The fallback
   overlay is proven possible on the Frame by an existing commercial app.
3. **Presence sensor.** The Frame has one, but no documented API for a background
   service.
4. **Call audio.** Handing HFP to PipeWire also moves the phone's media audio to the
   headset (A2DP sink is mandatory on that path). Acceptable while worn; must be
   undone when the headset comes off.
5. **One MAP session per iPhone.** Another computer running Phone Link or Tether
   would block Earshot's messages.
6. **Steam updates** can break the CEF injection (shared risk with Tragus).
7. **iOS version.** Notification mirroring needs iOS 26.1 or newer.
