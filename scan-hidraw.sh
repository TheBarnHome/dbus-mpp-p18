#!/bin/sh
### BEGIN INIT INFO
# Provides:          scan-hidraw
# Required-Start:    $local_fs
# Default-Start:     S
# Short-Description: Scan and start dbus-mppsolar for hidraw devices
### END INIT INFO

START_SCRIPT="/data/etc/dbus-mppsolar/start-dbus-mppsolar.sh"

echo "Starting HIDRAW device scan..."

for dev in /dev/hidraw*; do
  [ -e "$dev" ] || continue

  devname=$(basename "$dev")
  pidfile="/var/run/dbus-mppsolar.$devname.pid"

  if [ ! -f "$pidfile" ]; then
    echo "Launching dbus-mppsolar for $devname"
    $START_SCRIPT "$devname" &
  else
    echo "Already running: $devname"
  fi
done
