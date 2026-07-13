#!/bin/sh
### BEGIN INIT INFO
# Provides:          dbus-mppsolar-manager
# Required-Start:    $local_fs
# Default-Start:     S
# Short-Description: Start the MPP Solar P18 hotplug manager
### END INIT INFO

set -eu

START_SCRIPT="${INSTALL_DIR:-/data/etc/dbus-mppsolar}/start-dbus-mppsolar.sh"

echo "Starting MPP Solar hotplug manager..."
exec "$START_SCRIPT"
