#!/bin/sh
# Set up Earshot's user services (no root). Run by earshot-frame-install.py after it has
# put the engine in ~/.local/share/earshot/engine and this folder in .../frame, and safe
# to run again by hand from there:  sh ~/.local/share/earshot/frame/install.sh
# Settings in ~/.config/earshot are never overwritten.
set -eu
data="${XDG_DATA_HOME:-$HOME/.local/share}/earshot"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
src="$data/frame"
flag="$HOME/.steam/steam/.cef-enable-remote-debugging"
marker="$data/created-cef-flag"

for f in "$data/engine/tetherd" "$data/engine/tether" "$src/earshot-tetherd.service" "$src/earshot-steamui.service"; do
    [ -f "$f" ] || { echo "error: $f is missing" >&2; exit 1; }
done
chmod 755 "$data/engine/tetherd" "$data/engine/tether"

mkdir -p "$units" "${XDG_CONFIG_HOME:-$HOME/.config}/earshot"
chmod 700 "${XDG_CONFIG_HOME:-$HOME/.config}/earshot"
cp -f "$src/earshot-tetherd.service" "$src/earshot-steamui.service" "$units/"

# The Phone tab reaches Steam's UI through its DevTools endpoint, which Steam only opens
# when this file exists. Tragus needs it too: record that we made it, so uninstall removes
# it only if we did and Tragus is not installed.
if [ -d "$HOME/.steam/steam" ]; then
    if [ ! -e "$flag" ]; then
        : > "$flag"
        : > "$marker"
        echo "Created $flag (Steam needs one restart to pick it up)."
    fi
else
    echo "note: ~/.steam/steam was not found; start Steam once, then run this again." >&2
fi

# The headset must never act as the phone's Bluetooth headset or speaker: an iPhone that sees an audio sink or a
# Hands-Free unit routes its microphone there, and this headset's Bluetooth chip cannot carry that audio. Keep
# only the roles earbuds and headphones paired to the headset use. A user-level WirePlumber setting.
wpdir="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d"
wpconf="$wpdir/51-earshot-no-phone-audio.conf"
wpnew="$(printf '%s\n' \
    "# Written by Earshot; removed when Earshot is uninstalled. The headset never acts as a phone's" \
    "# Bluetooth headset or speaker. Earbuds and headphones paired to the headset are unaffected." \
    "monitor.bluez.properties = {" \
    "  bluez5.roles = [ a2dp_source bap_source hfp_ag ]" \
    "}")"
if [ ! -f "$wpconf" ] || [ "$(cat "$wpconf")" != "$wpnew" ]; then
    mkdir -p "$wpdir"
    printf '%s\n' "$wpnew" > "$wpconf"
    systemctl --user try-restart wireplumber.service 2>/dev/null || true
fi

systemctl --user daemon-reload
systemctl --user enable earshot-tetherd.service earshot-steamui.service
systemctl --user reset-failed earshot-tetherd.service earshot-steamui.service 2>/dev/null || true
systemctl --user restart --no-block earshot-tetherd.service earshot-steamui.service

# The engine waits up to 30 s for Bluetooth before it starts; wait for it. Right after a
# --no-block restart it can still read "inactive", so only "active" or "failed" end the wait.
state=unknown
for _ in $(seq 1 45); do
    state="$(systemctl --user is-active earshot-tetherd.service 2>/dev/null || true)"
    case "$state" in active|failed) break ;; esac
    sleep 1
done
if [ "$state" = active ]; then
    echo "Earshot engine running."
else
    echo "error: earshot-tetherd.service did not start; see: journalctl --user -u earshot-tetherd" >&2
    exit 1
fi
