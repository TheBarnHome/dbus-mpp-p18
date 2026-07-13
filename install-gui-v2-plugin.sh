#!/bin/sh

set -eu

INSTALL_DIR="${INSTALL_DIR:-/data/etc/dbus-mppsolar}"
SOURCE="$INSTALL_DIR/gui-v2/MppSolarManager/MppSolarManager.json"
AVAILABLE="/data/apps/available/MppSolarManager"
ENABLED="/data/apps/enabled/MppSolarManager"

if [ ! -d /data/apps ] || [ ! -d /data/apps/available ] || [ ! -d /data/apps/enabled ]; then
    echo "Official /data/apps gui-v2 framework is not available; plugin not installed."
    exit 0
fi
if [ ! -s "$SOURCE" ]; then
    echo "Prepared plugin artifact is missing: $SOURCE" >&2
    exit 1
fi
python3 -m json.tool "$SOURCE" >/dev/null

mkdir -p "$AVAILABLE/gui-v2"
cp -p "$SOURCE" "$AVAILABLE/gui-v2/MppSolarManager.json"
if [ -L "$ENABLED" ]; then
    target=$(readlink "$ENABLED")
    if [ "$target" != "$AVAILABLE" ]; then
        echo "Refusing to replace unexpected symlink $ENABLED -> $target" >&2
        exit 1
    fi
elif [ -e "$ENABLED" ]; then
    echo "Refusing to replace non-symlink $ENABLED" >&2
    exit 1
else
    ln -s "$AVAILABLE" "$ENABLED"
fi
echo "MPP Solar gui-v2 plugin installed."
