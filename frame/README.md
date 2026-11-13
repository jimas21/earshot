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

## Branches

- `main` tracks upstream Tether unchanged.
- `frame` is where Earshot is developed. Engine fixes are kept as small, separate
  commits so they can be offered back upstream.

Earshot is not affiliated with Valve or Apple.
