#!/bin/sh
# Earshot's one-time Bluetooth system setup for the Steam Frame (the only part that needs
# root). Lists every change, asks, then asks for your password through a graphical prompt
# in Desktop Mode (a terminal prompt over SSH). Applies only what is missing.
#   earshot-system-setup.sh             list, ask, apply
#   earshot-system-setup.sh --check     exit 0 when nothing is missing (no root)
#   earshot-system-setup.sh --plan      print what would change (no root)
#   earshot-system-setup.sh --undo      list, ask, revert exactly what was applied
# The logic is in earshot-system-setup.py (tested without root). Needs the account to have
# a password: set one with passwd in a terminal if the headset never asked for one.
set -u
dir="$(cd "$(dirname "$0")" && pwd)"
py="$dir/earshot-system-setup.py"
python="$(command -v python3 || echo /usr/bin/python3)"
mode="${1:-apply}"

case "$mode" in
    --check) exec "$python" -I "$py" --check ;;
    --plan)  exec "$python" -I "$py" --plan ;;
esac

gui=""
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v kdialog >/dev/null 2>&1; then gui=1; fi

if [ "$mode" = "--undo" ]; then
    text="$("$python" -I "$py" --undo-plan)"
else
    text="$("$python" -I "$py" --plan)"
    "$python" -I "$py" --check >/dev/null 2>&1
    case $? in
        0) echo "$text"; exit 0 ;;
        10) ;;
        *) echo "$text"; exit 1 ;;
    esac
fi

echo "$text"
echo
if [ -n "$gui" ]; then
    kdialog --title "Earshot" --yesno "$text

Continue? You will be asked for your password." || { echo "Cancelled."; exit 1; }
else
    printf 'Continue? [y/N] '
    read -r answer || answer=n
    case "$answer" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 1 ;; esac
fi

action="--apply"
[ "$mode" = "--undo" ] && action="--undo"

run_root() {
    if [ "$(id -u)" = 0 ]; then
        "$python" -I "$py" "$action" --yes
        return $?
    fi
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v pkexec >/dev/null 2>&1; then
        pkexec "$python" -I "$py" "$action" --yes
        rc=$?
        # 126: the prompt was dismissed or refused; 127: no polkit agent could ask.
        [ "$rc" = 127 ] || return "$rc"
    fi
    if [ -n "$gui" ] && command -v sudo >/dev/null 2>&1; then
        pw="$(kdialog --title "Earshot" --password "Password for $(id -un), to change the Bluetooth setup")" || return 126
        printf '%s\n' "$pw" | sudo -S -p '' "$python" -I "$py" "$action" --yes
        rc=$?
        pw=""
        return "$rc"
    fi
    sudo "$python" -I "$py" "$action" --yes
}

run_root
rc=$?
if [ "$rc" = 0 ]; then
    echo "Bluetooth setup finished."
else
    echo "The Bluetooth setup did not finish (exit $rc). Nothing else was changed." >&2
fi
exit "$rc"
