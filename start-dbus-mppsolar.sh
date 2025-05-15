#!/bin/bash

DEVICE=$1
SERIAL_DEV="/dev/$DEVICE"
APP=/data/etc/dbus-mppsolar/dbus-mppsolar.py
LOGDIR=/var/log/dbus-mppsolar.$DEVICE
PIDFILE="/var/run/dbus-mppsolar.$DEVICE.pid"

echo "UTC-$(date -u +%Y.%m.%d-%H:%M:%S) Starting dbus-mppsolar.py on $DEVICE"

mkdir -p "$LOGDIR"

exec start-stop-daemon --start \
  --make-pidfile --pidfile "$PIDFILE" \
  --exec /bin/sh -- -c \
  "exec python3 $APP --serial $SERIAL_DEV 2>&1 | multilog t s25000 n4 $LOGDIR"
