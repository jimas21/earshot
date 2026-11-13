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
#
# The root step never runs the .py where it lies (that folder belongs to the user): the
# elevated side copies it into a root-only folder, checks the copy against the sha256 the
# installer recorded when it verified the release signature, and runs only the copy, with
# the system's /usr/bin/python3. A program already running as this user could still replace
# both the file and its recorded hash, or sit in front of the password prompt; this guards
# against a stale or damaged copy and against a swap between the check and the run, not
# against that. See frame/README.md.
set -u
dir="$(cd "$(dirname "$0")" && pwd)"
py="$dir/earshot-system-setup.py"
sumfile="$py.sha256"
python=/usr/bin/python3
mode="${1:-apply}"

# Runs as root: $1 = the script, $2 = its expected sha256, $3 = --apply or --undo.
root_script='set -eu
tmp=$(/usr/bin/mktemp -d)
trap "/usr/bin/rm -rf \"$tmp\"" EXIT
/usr/bin/cp -- "$1" "$tmp/setup.py"
if ! printf "%s  %s\n" "$2" "$tmp/setup.py" | /usr/bin/sha256sum -c --status - 2>/dev/null; then
    echo "The Earshot setup script does not match the installed release; not running it as root." >&2
    echo "Run the Earshot install again to repair it." >&2
    exit 3
fi
/usr/bin/python3 -I "$tmp/setup.py" "$3" --yes'

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

expected=""
[ -f "$sumfile" ] && read -r expected _ < "$sumfile"
case "$expected" in
    *[!0-9a-f]*|"") expected="" ;;
esac
if [ "${#expected}" != 64 ]; then
    echo "The checksum of the setup script is missing (it is written by the Earshot installer)." >&2
    echo "Run the Earshot install again, then retry. Nothing was changed." >&2
    exit 1
fi

run_root() {
    if [ "$(id -u)" = 0 ]; then
        /bin/sh -c "$root_script" sh "$py" "$expected" "$action"
        return $?
    fi
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v pkexec >/dev/null 2>&1; then
        pkexec /bin/sh -c "$root_script" sh "$py" "$expected" "$action"
        rc=$?
        # 126: the prompt was dismissed or refused; 127: no polkit agent could ask.
        [ "$rc" = 127 ] || return "$rc"
    fi
    if [ -n "$gui" ] && command -v sudo >/dev/null 2>&1; then
        pw="$(kdialog --title "Earshot" --password "Password for $(id -un), to change the Bluetooth setup")" || return 126
        printf '%s\n' "$pw" | sudo -S -p '' /bin/sh -c "$root_script" sh "$py" "$expected" "$action"
        rc=$?
        pw=""
        return "$rc"
    fi
    sudo /bin/sh -c "$root_script" sh "$py" "$expected" "$action"
}

run_root
rc=$?
if [ "$rc" = 0 ]; then
    echo "Bluetooth setup finished."
else
    echo "The Bluetooth setup did not finish (exit $rc). Nothing else was changed." >&2
fi
exit "$rc"
