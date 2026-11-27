#!/usr/bin/env bash
# Dragons Den installer
#
# Symlinks the Klipper module and the Moonraker component from this repository
# into Klipper and Moonraker, copies the dragonsnest.cfg template into the
# printer config folder (only if none exists yet), adds the "Dragons Den"
# entry to Mainsail's sidebar (<config>/.theme/navi.json + custom.css, merged
# with any existing content), and restarts the services.
#
# Usage: ./install.sh [-k klipper_dir] [-m moonraker_dir] [-c config_dir]
#                     [-n] [-s] [-u]
#   -n  do not restart services
#   -s  skip the Mainsail sidebar entry
#   -u  uninstall (remove symlinks and sidebar entry; dragonsnest.cfg is kept)
set -euo pipefail

SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_PATH="${KLIPPER_PATH:-$HOME/klipper}"
MOONRAKER_PATH="${MOONRAKER_PATH:-$HOME/moonraker}"
CONFIG_PATH="${CONFIG_PATH:-$HOME/printer_data/config}"
RESTART=1
UNINSTALL=0
THEME=1

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
}

while getopts "k:m:c:nsuh" opt; do
    case "$opt" in
        k) KLIPPER_PATH="$OPTARG" ;;
        m) MOONRAKER_PATH="$OPTARG" ;;
        c) CONFIG_PATH="$OPTARG" ;;
        n) RESTART=0 ;;
        s) THEME=0 ;;
        u) UNINSTALL=1 ;;
        *) usage ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "Do not run this script as root." >&2
    exit 1
fi

KLIPPER_LINK="$KLIPPER_PATH/klippy/extras/dragons_den.py"
MOONRAKER_LINK="$MOONRAKER_PATH/moonraker/components/dragons_den.py"

# Keep Klipper/Moonraker git trees clean so update_manager does not flag them
# as dirty because of our untracked symlinks.
git_exclude() {
    local repo="$1" relpath="$2" exclude="$1/.git/info/exclude"
    [ -d "$repo/.git" ] || return 0
    mkdir -p "$repo/.git/info"
    grep -qxF "$relpath" "$exclude" 2>/dev/null || echo "$relpath" >> "$exclude"
}

link_file() {
    local src="$1" dst="$2"
    ln -sfn "$src" "$dst"
    echo "Linked $dst -> $src"
}

THEME_DIR="$CONFIG_PATH/.theme"

# Mainsail sidebar entry: merged into existing navi.json / custom.css.
theme() {
    local action="$1"
    [ "$THEME" -eq 1 ] || return 0
    if [ "$action" = install ] && [ ! -d "$CONFIG_PATH" ]; then
        echo "Config folder $CONFIG_PATH not found; skipping the Mainsail sidebar entry."
        return 0
    fi
    if python3 "$SRCDIR/scripts/theme_install.py" "$action" "$THEME_DIR" "$SRCDIR/mainsail"; then
        echo "Mainsail sidebar entry: ${action}ed in $THEME_DIR (reload Mainsail to see it)"
    else
        echo "Mainsail sidebar entry not ${action}ed; see the message above." >&2
    fi
}

restart_services() {
    [ "$RESTART" -eq 1 ] || return 0
    if command -v systemctl >/dev/null 2>&1; then
        echo "Restarting klipper and moonraker..."
        sudo systemctl restart klipper || echo "Could not restart klipper" >&2
        sudo systemctl restart moonraker || echo "Could not restart moonraker" >&2
    fi
}

if [ "$UNINSTALL" -eq 1 ]; then
    rm -f "$KLIPPER_LINK" "$MOONRAKER_LINK"
    theme uninstall
    echo "Removed Dragons Den symlinks. $CONFIG_PATH/dragonsnest.cfg was kept."
    restart_services
    exit 0
fi

if [ ! -d "$KLIPPER_PATH/klippy/extras" ]; then
    echo "Klipper not found at $KLIPPER_PATH (use -k)." >&2
    exit 1
fi

link_file "$SRCDIR/klippy/extras/dragons_den.py" "$KLIPPER_LINK"
git_exclude "$KLIPPER_PATH" "klippy/extras/dragons_den.py"

if [ -d "$MOONRAKER_PATH/moonraker/components" ]; then
    link_file "$SRCDIR/moonraker/dragons_den.py" "$MOONRAKER_LINK"
    git_exclude "$MOONRAKER_PATH" "moonraker/components/dragons_den.py"
else
    echo "Moonraker not found at $MOONRAKER_PATH; skipping the camera page component (use -m)."
fi

if [ -d "$CONFIG_PATH" ]; then
    if [ -e "$CONFIG_PATH/dragonsnest.cfg" ]; then
        echo "Keeping existing $CONFIG_PATH/dragonsnest.cfg"
    else
        # Copied, not linked: dragons_den writes to this file, and writes into
        # the repository would block Moonraker updates.
        cp "$SRCDIR/config/dragonsnest.cfg" "$CONFIG_PATH/dragonsnest.cfg"
        echo "Copied dragonsnest.cfg to $CONFIG_PATH"
    fi
else
    echo "Config folder $CONFIG_PATH not found; copy config/dragonsnest.cfg yourself (use -c)."
fi

theme install

restart_services
echo "Dragons Den installed. Add [include dragonsnest.cfg] to printer.cfg."
