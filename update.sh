#!/bin/bash

set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/data/etc/dbus-mppsolar}"
BACKUP_DIR="${BACKUP_DIR:-/data/etc/dbus-mppsolar-backups}"
LOCK_DIR="${LOCK_DIR:-/var/lock/dbus-mppsolar-update.lock}"
CONFIG_REL="config.json"
NO_RESTART=0
DRY_RUN=0
UPDATE_STARTED=0
ROLLBACK_IN_PROGRESS=0
SERVICES_TOUCHED=0
CONFIG_BACKUP=""
OLD_REVISION=""
EXPECTED_DEVICES=""

log() {
    printf '%s\n' "$*"
}

usage() {
    cat <<'EOF'
Usage: bash update.sh [--dry-run] [--no-restart]

  --dry-run     Check whether an update is possible without modifying files.
  --no-restart  Install the update but leave the running services untouched.
  -h, --help    Show this help.

The update is refused if files other than config.json contain local changes.
EOF
}

for argument in "$@"; do
    case "$argument" in
        --dry-run)
            DRY_RUN=1
            ;;
        --no-restart)
            NO_RESTART=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log "Unknown argument: $argument"
            usage
            exit 2
            ;;
    esac
done

# Running from a temporary copy prevents git pull from replacing this script
# while Bash is still reading it.
if [ "${DBUS_MPP_UPDATE_FROM_COPY:-0}" != "1" ]; then
    updater_copy=$(mktemp /tmp/dbus-mppsolar-update.XXXXXX)
    cp "$0" "$updater_copy"
    chmod 700 "$updater_copy"
    export DBUS_MPP_UPDATE_FROM_COPY=1
    export DBUS_MPP_UPDATE_COPY="$updater_copy"
    exec "$updater_copy" "$@"
fi

cleanup() {
    status=$?

    if [ -n "$CONFIG_BACKUP" ] && [ -f "$CONFIG_BACKUP" ]; then
        cp -p "$CONFIG_BACKUP" "$INSTALL_DIR/$CONFIG_REL"
        rm -f "$CONFIG_BACKUP"
        CONFIG_BACKUP=""
    fi

    rmdir "$LOCK_DIR" 2>/dev/null || true

    if [ -n "${DBUS_MPP_UPDATE_COPY:-}" ]; then
        rm -f "$DBUS_MPP_UPDATE_COPY"
    fi

    exit "$status"
}

list_expected_devices() {
    python3 -c '
import json
import pathlib
import sys

with open(sys.argv[1], encoding="utf-8") as config_file:
    config = json.load(config_file)

for device_path in config:
    if device_path.startswith("/dev/hidraw"):
        print(pathlib.Path(device_path).name)
' "$INSTALL_DIR/$CONFIG_REL"
}

stop_services() {
    local pidfile
    local pids
    local pid

    log "Stopping dbus-mppsolar services..."

    for pidfile in /var/run/dbus-mppsolar.hidraw*.pid; do
        [ -e "$pidfile" ] || continue
        start-stop-daemon --stop --pidfile "$pidfile" --retry TERM/15/KILL/5 || true
        rm -f "$pidfile"
    done

    # Remove only orphan processes which match this installation and a hidraw
    # device. This avoids the broad process matching used by install.sh.
    pids=$(ps | grep -F "$INSTALL_DIR/dbus-mppsolar.py --serial /dev/hidraw" | grep -v grep | awk '{print $1}' || true)
    for pid in $pids; do
        kill "$pid" 2>/dev/null || true
    done

    pids=$(ps | grep -F "$INSTALL_DIR/inverterd --usb-path /dev/hidraw" | grep -v grep | awk '{print $1}' || true)
    for pid in $pids; do
        kill "$pid" 2>/dev/null || true
    done

    sleep 2

    pids=$(ps | grep -F "$INSTALL_DIR/dbus-mppsolar.py --serial /dev/hidraw" | grep -v grep | awk '{print $1}' || true)
    pids="$pids $(ps | grep -F "$INSTALL_DIR/inverterd --usb-path /dev/hidraw" | grep -v grep | awk '{print $1}' || true)"
    if [ -n "${pids//[[:space:]]/}" ]; then
        log "Forcing the remaining project processes to stop: $pids"
        for pid in $pids; do
            kill -KILL "$pid" 2>/dev/null || true
        done
        sleep 1

        pids=$(ps | grep -F "$INSTALL_DIR/dbus-mppsolar.py --serial /dev/hidraw" | grep -v grep | awk '{print $1}' || true)
        pids="$pids $(ps | grep -F "$INSTALL_DIR/inverterd --usb-path /dev/hidraw" | grep -v grep | awk '{print $1}' || true)"
        if [ -n "${pids//[[:space:]]/}" ]; then
            log "Unable to stop project processes: $pids"
            return 1
        fi
    fi
}

start_services() {
    log "Starting dbus-mppsolar services..."
    "$INSTALL_DIR/scan-hidraw.sh"
}

set_runtime_permissions() {
    chmod +x \
        "$INSTALL_DIR/dbus-mppsolar.py" \
        "$INSTALL_DIR/inverterd" \
        "$INSTALL_DIR/start-dbus-mppsolar.sh" \
        "$INSTALL_DIR/scan-hidraw.sh" \
        "$INSTALL_DIR/update.sh"
}

check_services() {
    local elapsed=0
    local names
    local device
    local all_ready

    log "Waiting for D-Bus services..."
    while [ "$elapsed" -lt 30 ]; do
        names=$(dbus-send --system --print-reply \
            --dest=org.freedesktop.DBus \
            /org/freedesktop/DBus \
            org.freedesktop.DBus.ListNames 2>/dev/null || true)
        all_ready=1

        for device in $EXPECTED_DEVICES; do
            if ! printf '%s\n' "$names" | grep -Fq "com.victronenergy.inverter.mppsolar-inverter.$device"; then
                all_ready=0
            fi
            if ! printf '%s\n' "$names" | grep -Fq "com.victronenergy.solarcharger.mppsolar-charger.$device"; then
                all_ready=0
            fi
        done

        if [ "$all_ready" -eq 1 ]; then
            log "All expected D-Bus services are available."
            return 0
        fi

        sleep 1
        elapsed=$((elapsed + 1))
    done

    log "D-Bus services did not return within 30 seconds."
    return 1
}

validate_installation() {
    log "Validating updated files..."
    python3 -c 'from pathlib import Path; import sys; p=Path(sys.argv[1]); compile(p.read_text(), str(p), "exec")' \
        "$INSTALL_DIR/dbus-mppsolar.py"
    python3 -m json.tool "$INSTALL_DIR/$CONFIG_REL" >/dev/null
    bash -n "$INSTALL_DIR/update.sh"
    bash -n "$INSTALL_DIR/install.sh"
    bash -n "$INSTALL_DIR/start-dbus-mppsolar.sh"
    sh -n "$INSTALL_DIR/scan-hidraw.sh"
    [ -x "$INSTALL_DIR/inverterd" ]
}

restore_config() {
    if [ -n "$CONFIG_BACKUP" ] && [ -f "$CONFIG_BACKUP" ]; then
        cp -p "$CONFIG_BACKUP" "$INSTALL_DIR/$CONFIG_REL"
    fi
}

rollback() {
    ROLLBACK_IN_PROGRESS=1
    set +e

    log "Update failed; restoring revision $OLD_REVISION..."
    if [ "$SERVICES_TOUCHED" -eq 1 ]; then
        stop_services
    fi
    git -C "$INSTALL_DIR" reset --hard "$OLD_REVISION"
    git -C "$INSTALL_DIR" submodule update --init --recursive
    restore_config
    set_runtime_permissions

    if [ "$SERVICES_TOUCHED" -eq 1 ]; then
        start_services
        check_services
    fi

    log "Rollback completed. Backup retained in $BACKUP_DIR."
}

on_error() {
    status=$?
    line=$1
    trap - ERR

    log "Update error at line $line (status $status)."
    if [ "$UPDATE_STARTED" -eq 1 ] && [ "$ROLLBACK_IN_PROGRESS" -eq 0 ]; then
        rollback
    fi
    exit "$status"
}

on_signal() {
    status=$1
    trap - ERR INT TERM

    log "Update interrupted."
    if [ "$UPDATE_STARTED" -eq 1 ] && [ "$ROLLBACK_IN_PROGRESS" -eq 0 ]; then
        rollback
    fi
    exit "$status"
}

trap cleanup EXIT
trap 'on_error $LINENO' ERR
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

if [ ! -d "$INSTALL_DIR/.git" ]; then
    log "$INSTALL_DIR is not a Git checkout."
    exit 1
fi

if [ "$NO_RESTART" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    log "Run this updater as root, or use --no-restart."
    exit 1
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "Another update appears to be running ($LOCK_DIR exists)."
    exit 1
fi

cd "$INSTALL_DIR"

if ! git rev-parse --verify '@{upstream}' >/dev/null 2>&1; then
    log "The current branch has no upstream branch configured."
    exit 1
fi

submodule_status=$(git submodule status --recursive)
if grep -q '^-' <<< "$submodule_status"; then
    log "Update refused: one or more Git submodules are not initialized."
    log "Run: git submodule update --init --recursive"
    exit 1
fi

# Runtime executable bits used to be applied only by install.sh and therefore
# appeared as local Git changes. Ignore mode-only differences, but never ignore
# content changes.
unexpected_changes=$(git -c core.fileMode=false status --porcelain --untracked-files=all | sed '/^.. config\.json$/d')
if [ -n "$unexpected_changes" ]; then
    log "Update refused: local changes exist outside config.json:"
    printf '%s\n' "$unexpected_changes"
    log "Commit, revert, or move these changes before updating. Nothing was modified."
    exit 1
fi

EXPECTED_DEVICES=$(list_expected_devices)
if [ "$NO_RESTART" -eq 0 ] && [ -z "$EXPECTED_DEVICES" ]; then
    log "No /dev/hidraw device is configured; refusing an update that cannot be verified."
    exit 1
fi

if [ "$NO_RESTART" -eq 0 ]; then
    for device in $EXPECTED_DEVICES; do
        if [ ! -e "/dev/$device" ]; then
            log "Configured device /dev/$device is not connected; update refused."
            exit 1
        fi
    done
fi

log "Fetching upstream changes..."
git fetch --recurse-submodules

OLD_REVISION=$(git rev-parse HEAD)
REMOTE_REVISION=$(git rev-parse '@{upstream}')

if [ "$OLD_REVISION" = "$REMOTE_REVISION" ]; then
    log "Already up to date at $OLD_REVISION."
    exit 0
fi

if ! git merge-base --is-ancestor "$OLD_REVISION" "$REMOTE_REVISION"; then
    log "Update refused: local and upstream histories have diverged."
    exit 1
fi

log "Update available: $OLD_REVISION -> $REMOTE_REVISION"
if [ "$DRY_RUN" -eq 1 ]; then
    log "Dry run successful; no file was modified."
    exit 0
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"
backup_archive="$BACKUP_DIR/dbus-mppsolar-$timestamp-$OLD_REVISION.tar.gz"
log "Creating backup $backup_archive..."
tar --exclude='.git' -czf "$backup_archive" -C "$INSTALL_DIR" .
tar -tzf "$backup_archive" >/dev/null

if [ -f "$INSTALL_DIR/$CONFIG_REL" ]; then
    CONFIG_BACKUP=$(mktemp /tmp/dbus-mppsolar-config.XXXXXX)
    cp -p "$INSTALL_DIR/$CONFIG_REL" "$CONFIG_BACKUP"
    if git ls-files --error-unmatch "$CONFIG_REL" >/dev/null 2>&1; then
        git checkout -- "$CONFIG_REL"
    else
        rm -f "$INSTALL_DIR/$CONFIG_REL"
    fi
fi

UPDATE_STARTED=1
log "Applying fast-forward update..."
git pull --ff-only --recurse-submodules
git submodule update --init --recursive
restore_config
set_runtime_permissions
validate_installation

if [ "$NO_RESTART" -eq 0 ]; then
    SERVICES_TOUCHED=1
    stop_services
    start_services
    check_services
else
    log "Services were not restarted (--no-restart)."
fi

UPDATE_STARTED=0
log "Update completed successfully at $(git rev-parse HEAD)."
log "Backup: $backup_archive"
