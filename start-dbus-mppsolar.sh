#!/bin/sh

set -eu

# Venus OS rcS may start this script without exporting PATH. Keep every command
# resolvable and invoke Python by its absolute path so sys.executable is valid.
PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
export PATH

INSTALL_DIR="${INSTALL_DIR:-/data/etc/dbus-mppsolar}"
APP="$INSTALL_DIR/mppsolar-supervisor.py"
PYTHON="${PYTHON:-/usr/bin/python3}"
PIDFILE="${PIDFILE:-/var/run/dbus-mppsolar-manager.pid}"
LOGDIR="${LOGDIR:-/var/log/dbus-mppsolar-manager}"

if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        exit 0
    fi
    rm -f "$PIDFILE"
fi

mkdir -p "$LOGDIR"
echo "UTC-$(date -u +%Y.%m.%d-%H:%M:%S) Starting mppsolar-supervisor.py"

exec start-stop-daemon --start --background \
    --make-pidfile --pidfile "$PIDFILE" \
    --exec /bin/sh -- -c \
    "exec '$PYTHON' '$APP' >> '$LOGDIR/current' 2>&1"
