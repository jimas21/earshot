# Earshot menu for Steam's VR interface (Steam Frame)

The files in this folder make up the Phone tab on the Steam Frame. For what Earshot does
and how to install it, see the [project README](../README.md).

| File | What it is |
|---|---|
| `earshot-menu.js` | The menu itself. Runs inside Steam's VR bottom bar and quick menu. It has no network access: the helper pushes state into the page and the page hands actions back. |
| `earshot-steamui.py` | A small background service (Python 3, standard library only). Talks to the Earshot engine (`tetherd`) over its local socket, keeps the menu in Steam's pages, raises SteamVR notifications and applies the priority rules. |

The installer (`../install/`) copies both to `~/.local/share/earshot/frame/` and runs the
helper as the user unit `earshot-steamui.service`.

## What the helper does

- **Engine link.** Connects to `$XDG_RUNTIME_DIR/tether/tetherd.sock`, asks for
  `protocol_info`, subscribes, and reconnects with a growing delay (1 s up to 30 s) when
  the engine restarts. Set `EARSHOT_TETHER_SOCKET` to use another path.
- **Menu.** Finds Steam's VR bar and quick-menu pages through Steam's local DevTools
  endpoint (127.0.0.1:8080, the same switch Decky Loader uses), injects `earshot-menu.js`,
  and injects it again if a page reloads. State goes in with `Runtime.evaluate`; actions
  come back through a `Runtime.addBinding` binding named `earshotAction`. Nothing listens
  on any port. Everything it adds is prefixed `earshot-`, so it can sit next to the Tragus
  menu in the same pages.
- **Alerts.** While the headset has priority, new phone notifications, texts and incoming
  calls become SteamVR notifications (`vrcmd --notify`). A burst from one app or one
  conversation is shown once, then summarised ("3 new messages from ..."). Apps can be
  muted in Settings; muted apps still appear under Alerts.
- **Calls.** An incoming call puts an Answer / Decline card on top of the Phone tab and a
  "Call" mark on the bar. Answer and Decline use the call commands when Hands-Free is up
  and the iPhone's notification actions otherwise.
- **Replies.** The Messages tab lists one-to-one conversations. Its text box is a plain
  text input, so Steam's own keyboard handles typing. Each message shows sending, sent or
  the reason it failed.
- **Priority.** Settings has Auto, Always Frame and Always phone. When the phone has
  priority the helper stops raising alerts and asks BlueZ to let go of the Hands-Free link
  (`Device1.DisconnectProfile` through `busctl`), so the phone rings normally; it asks for
  the link back when the headset has priority again. Auto currently follows whether
  SteamVR is running. A better "worn" signal can be added behind `PresenceDetector`.
- **Settings** are kept in `~/.config/earshot/settings.json` (private, written atomically).
- **Updates.** Settings has Check for updates and Update automatically (first check ten
  minutes after start, then every six hours). The helper starts
  `~/.local/share/earshot/earshot-frame-install.py --if-newer` in its own user unit,
  `earshot-update`, and shows the progress that the installer writes to
  `~/.local/state/earshot/update.json`.
- **Logs** are warnings only by default and never contain message, notification or caller
  text. `EARSHOT_LOG=info` or `debug` adds detail, still without text.

## Troubleshooting

```sh
systemctl --user status earshot-steamui --no-pager
journalctl --user -u earshot-steamui -n 30 --no-pager
```

If the Phone tab says the engine is not running, start it with
`systemctl --user start earshot-tetherd`.

## Tests

No headset is needed. From the repository root:

```sh
python3 -m unittest discover -s frame/tests -v      # helper, against fake engine, DevTools and commands
cd frame/tests/js && npm install && npm test         # menu, under jsdom
```

## Security note

Steam's DevTools connection has no password. It only listens on the headset, but any
program running on the headset could use it to control Steam's interface, the same as with
Decky Loader.
