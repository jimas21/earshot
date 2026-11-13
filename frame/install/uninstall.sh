#!/bin/sh
# Remove Earshot's user services and files. Earshot's own settings stay in ~/.config/earshot
# unless --purge is given. What the engine learned about the phone is always removed: its message
# journal and contact cache, its settings and encryption key, and its state all live under
# ~/.local/share/earshot/engine-home, which belongs to Earshot alone. A Tether installed separately keeps
# its own ~/.local/share/tether, ~/.config/tether and ~/.local/state/tether, and they are never touched.
# No root is needed.
set -u
purge=""
for arg in "$@"; do
    case "$arg" in
        --purge) purge=1 ;;
    esac
done
data="${XDG_DATA_HOME:-$HOME/.local/share}/earshot"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
flag="$HOME/.steam/steam/.cef-enable-remote-debugging"
marker="$data/created-cef-flag"

systemctl --user disable --now earshot-steamui.service earshot-tetherd.service 2>/dev/null
rm -f "$units/earshot-tetherd.service" "$units/earshot-steamui.service"
systemctl --user daemon-reload 2>/dev/null

# Take the Phone tab out of Steam's open pages (the stopped helper can no longer do it).
if [ -f "$data/frame/earshot-steamui.py" ]; then
    if command -v timeout >/dev/null 2>&1; then
        timeout 20 /usr/bin/python3 -I "$data/frame/earshot-steamui.py" --teardown >/dev/null 2>&1
    else
        /usr/bin/python3 -I "$data/frame/earshot-steamui.py" --teardown >/dev/null 2>&1
    fi
fi

# Steam's DevTools flag: ours to remove only if we created it, and never while Tragus
# (which injects its menu through the same file) is installed.
tragus=""
for p in "$HOME/.local/share/magicpods-app" "$HOME/.local/share/magicpodscore" \
         "$units/magicpodscore.service" "$units/magicpods-steamui.service"; do
    [ -e "$p" ] && tragus=1
done
if [ -f "$marker" ]; then
    if [ -z "$tragus" ]; then
        rm -f "$flag"
    else
        echo "Left $flag in place: Tragus uses it."
    fi
fi

rm -rf "$data"
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/earshot"
# The engine's own data (messages, contacts, its settings and key): the service file puts it at a fixed place
# under $HOME, which is not $data when XDG_DATA_HOME is set to somewhere else.
rm -rf "$HOME/.local/share/earshot/engine-home"
if [ -n "$purge" ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/earshot"
    echo "Earshot and its settings removed."
else
    echo "Earshot removed (settings kept)."
fi
exit 0
