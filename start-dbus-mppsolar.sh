#!/bin/bash
#
# Start script for gps_dbus
#   First parameter: hidraw device to use
#
# Keep this script running with daemon tools. If it exits because the
# connection crashes, or whatever, daemon tools will start a new one.
#

. /opt/victronenergy/serial-starter/run-service.sh

app=/data/etc/dbus-mppsolar/dbus-mppsolar.py
logdir=/var/log/dbus-mppsolar.$1

# Baudrates to use
start -s /dev/$1 -- sh -c "exec python3 $app /dev/$1 2>&1 | multilog t s25000 n4 $logdir"

