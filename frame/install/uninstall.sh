#!/bin/sh
# Remove Earshot's user services and files. Settings stay in ~/.config/earshot unless
# --purge is given. The root setup is separate: earshot-system-setup.sh --undo.
# --keep-setup leaves earshot-system-setup.* in place (the installer passes it while that
# setup has not been undone, because those scripts are the only way to undo it).
set -u
purge=""
keep=""
for arg in "$@"; do
    case "$arg" in
        --purge) purge=1 ;;
        --keep-setup) keep=1 ;;
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

if [ -n "$keep" ]; then
    for item in "$data"/* "$data"/.[!.]*; do
        [ -e "$item" ] || continue
        [ "$item" = "$data/frame" ] || rm -rf "$item"
    done
    for item in "$data/frame"/* "$data/frame"/.[!.]*; do
        [ -e "$item" ] || continue
        case "${item##*/}" in earshot-system-setup.*) ;; *) rm -rf "$item" ;; esac
    done
    echo "Kept the Bluetooth setup scripts in $data/frame (undo with earshot-system-setup.sh --undo)."
else
    rm -rf "$data"
fi
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/earshot"
if [ -n "$purge" ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/earshot"
    echo "Earshot and its settings removed."
else
    echo "Earshot removed (settings kept)."
fi
exit 0
