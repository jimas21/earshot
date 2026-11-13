#!/bin/sh
# Remove Earshot's user services and files. Settings stay in ~/.config/earshot unless
# --purge is given. The root setup is separate: earshot-system-setup.sh --undo.
set -u
data="${XDG_DATA_HOME:-$HOME/.local/share}/earshot"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
flag="$HOME/.steam/steam/.cef-enable-remote-debugging"
marker="$data/created-cef-flag"

systemctl --user disable --now earshot-steamui.service earshot-tetherd.service 2>/dev/null
rm -f "$units/earshot-tetherd.service" "$units/earshot-steamui.service"
systemctl --user daemon-reload 2>/dev/null

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
if [ "${1:-}" = "--purge" ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/earshot"
    echo "Earshot and its settings removed."
else
    echo "Earshot removed (settings kept)."
fi
exit 0
