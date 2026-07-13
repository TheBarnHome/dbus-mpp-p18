#!/bin/bash

set -e

INSTALL_DIR="/data/etc/dbus-mppsolar"
UDEV_RULES_PATH="/etc/udev/rules.d/99-mppsolar.rules"
START_SCRIPT="$INSTALL_DIR/start-dbus-mppsolar.sh"
INIT_SCRIPT_PATH="/etc/init.d"
RCS_LINK="/etc/rcS.d/S99scan-hidraw"

if [ -f "$INSTALL_DIR/config.json" ]; then
    echo "🔄 Migrating legacy configuration and live history before stopping services..."
    python3 "$INSTALL_DIR/migrate-config.py"
fi

echo "🛑 Stopping existing dbus-mppsolar and inverterd processes..."

# Generic kill function
kill_processes() {
    local pattern=$1
    echo "🔍 Looking for processes matching: $pattern"
    PIDS=$(ps | grep "$pattern" | grep -v grep | awk '{print $1}')
    for PID in $PIDS; do
        echo "⚙️ Killing process $PID ($pattern)"
        kill "$PID" || echo "⚠️ Could not kill process $PID"
    done
}

# Kill known processes
kill_processes "dbus-mppsolar.py"
kill_processes "mppsolar-manager.py"
kill_processes "inverterd --usb-path"
kill_processes "multilog.*dbus-mppsolar"

echo "✅ All matching processes have been stopped."

# --- Begin installation ---

if [ -x /opt/victronenergy/swupdate-scripts/set-feed.sh ]; then
    echo "🔄 Switching software feed to 'release'"
    /opt/victronenergy/swupdate-scripts/set-feed.sh release
else
    echo "⚠️  Feed switch script not found: /opt/victronenergy/swupdate-scripts/set-feed.sh"
fi

echo "📦 Updating packages and installing dependencies..."
#opkg update
opkg install python3-pip git

echo "🐍 Installing 'inverterd' via pip3..."
pip3 install inverterd

echo "🛠️ Creating the udev rule..."

cat <<EOF | tee "$UDEV_RULES_PATH" > /dev/null
ACTION=="add|change|remove", SUBSYSTEM=="hidraw", KERNEL=="hidraw*", RUN+="$START_SCRIPT"
EOF

echo "✅ udev rule created at $UDEV_RULES_PATH"

if [ -f "$START_SCRIPT" ]; then
    chmod +x "$START_SCRIPT"
    chmod +x "$INSTALL_DIR/dbus-mppsolar.py"
    chmod +x "$INSTALL_DIR/mppsolar-manager.py"
    chmod +x "$INSTALL_DIR/mppsolar-supervisor.py"
    chmod +x "$INSTALL_DIR/migrate-config.py"
    chmod +x "$INSTALL_DIR/verify-runtime.py"
    chmod +x "$INSTALL_DIR/inverterd"
    chmod +x "$INSTALL_DIR/start-dbus-mppsolar.sh"
    chmod +x "$INSTALL_DIR/scan-hidraw.sh"
    chmod +x "$INSTALL_DIR/install-gui-extension.py"
    echo "✅ Execution permissions applied to scripts"
else
    echo "❌ The file $START_SCRIPT was not found!"
    exit 1
fi

echo "🔁 Reloading udev rules..."
udevadm control --reload
udevadm trigger

mkdir -p /data/etc/dbus-mppsolar-state

echo "🖥️ Applying the idempotent Classic UI extension..."
python3 "$INSTALL_DIR/install-gui-extension.py" --restart-gui

if [ -d /data/apps ]; then
    echo "🧩 Official gui-v2 app directory detected; installing the prepared plugin"
    "$INSTALL_DIR/install-gui-v2-plugin.sh"
else
    echo "ℹ️  gui-v2 plugin kept prepared (official /data/apps framework not available)"
fi

# Check if the init script already exists
if [ -f "$INIT_SCRIPT_PATH/scan-hidraw.sh" ]; then
    echo "ℹ️ The init script $INIT_SCRIPT_PATH/scan-hidraw.sh already exists, no creation needed"
else
    cp "$INSTALL_DIR/scan-hidraw.sh" "$INIT_SCRIPT_PATH/scan-hidraw.sh"
    chmod +x "$INIT_SCRIPT_PATH/scan-hidraw.sh"
fi

# Add symlink in /etc/rcS.d if needed
if [ ! -L "$RCS_LINK" ]; then
    ln -s "$INIT_SCRIPT_PATH/scan-hidraw.sh" "$RCS_LINK"
    echo "✅ Symlink added: $RCS_LINK"
else
    echo "ℹ️ The symlink $RCS_LINK already exists"
fi

"$INSTALL_DIR/scan-hidraw.sh"

echo "🎉 Installation completed successfully."
