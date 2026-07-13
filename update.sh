#!/bin/bash

set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/data/etc/dbus-mppsolar}"
STATE_DIR="${STATE_DIR:-/data/etc/dbus-mppsolar-state}"
BACKUP_DIR="${BACKUP_DIR:-/data/etc/dbus-mppsolar-backups}"
LOCK_DIR="${LOCK_DIR:-/var/lock/dbus-mppsolar-update.lock}"
NO_RESTART=0
DRY_RUN=0
UPDATE_STARTED=0
SERVICES_TOUCHED=0
ROLLBACK_IN_PROGRESS=0
OLD_REVISION=""
BACKUP_ARCHIVE=""
STATE_BACKUP=""
EXPECTED_SERIALS=""
SYSTEM_BACKUP_DIR=""
QML_TARGET="/opt/victronenergy/gui/qml/PageDeviceInfo.qml"
UDEV_RULE="/etc/udev/rules.d/99-mppsolar.rules"
INIT_SCRIPT="/etc/init.d/scan-hidraw.sh"
RCS_LINK="/etc/rcS.d/S99scan-hidraw"
UPDATE_SOURCE_DIR="${DBUS_MPP_UPDATE_SOURCE_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"

log() { printf '%s\n' "$*"; }

usage() {
    cat <<'EOF'
Usage: bash update.sh [--dry-run] [--no-restart]

  --dry-run     Validate the update without changing files or runtime state.
  --no-restart  Install files but do not migrate/restart running services.
EOF
}

for argument in "$@"; do
    case "$argument" in
        --dry-run) DRY_RUN=1 ;;
        --no-restart) NO_RESTART=1 ;;
        -h|--help) usage; exit 0 ;;
        *) log "Unknown argument: $argument"; usage; exit 2 ;;
    esac
done

if [ "${DBUS_MPP_UPDATE_FROM_COPY:-0}" != "1" ]; then
    updater_copy=$(mktemp /tmp/dbus-mppsolar-update.XXXXXX)
    cp "$0" "$updater_copy"
    chmod 700 "$updater_copy"
    export DBUS_MPP_UPDATE_FROM_COPY=1
    export DBUS_MPP_UPDATE_COPY="$updater_copy"
    export DBUS_MPP_UPDATE_SOURCE_DIR="$UPDATE_SOURCE_DIR"
    exec "$updater_copy" "$@"
fi

cleanup() {
    status=$?
    rmdir "$LOCK_DIR" 2>/dev/null || true
    [ -z "${DBUS_MPP_UPDATE_COPY:-}" ] || rm -f "$DBUS_MPP_UPDATE_COPY"
    exit "$status"
}

find_project_pids() {
    {
        python3 -c '
import dbus
bus = dbus.SystemBus()
daemon = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
prefixes = (
    "com.victronenergy.mppsolar.manager",
    "com.victronenergy.inverter.mppsolar-inverter.sn_",
    "com.victronenergy.solarcharger.mppsolar-charger.sn_",
)
for name in bus.list_names():
    if name.startswith(prefixes):
        print(int(daemon.GetConnectionUnixProcessID(
            name, dbus_interface="org.freedesktop.DBus"
        )))
' 2>/dev/null || true
        ps | grep -F "$INSTALL_DIR/mppsolar-manager.py" | grep -v grep | awk '{print $1}' || true
        ps | grep -F "$INSTALL_DIR/dbus-mppsolar.py" | grep -v grep | awk '{print $1}' || true
        ps | grep -F "$INSTALL_DIR/inverterd" | grep -v grep | awk '{print $1}' || true
        ps | grep -F "multilog t s25000 n4 /var/log/dbus-mppsolar" | grep -v grep | awk '{print $1}' || true
    } | sort -nu
}

stop_services() {
    local pidfile pid pids
    log "Stopping dbus-mppsolar services..."
    for pidfile in /var/run/dbus-mppsolar-manager.pid /var/run/dbus-mppsolar.hidraw*.pid; do
        [ -e "$pidfile" ] || continue
        start-stop-daemon --stop --pidfile "$pidfile" --retry TERM/25/KILL/5 || true
        rm -f "$pidfile"
    done
    pids=$(find_project_pids)
    for pid in $pids; do kill "$pid" 2>/dev/null || true; done
    sleep 2
    pids=$(find_project_pids)
    if [ -n "${pids//[[:space:]]/}" ]; then
        log "Forcing remaining project processes to stop: $pids"
        for pid in $pids; do kill -KILL "$pid" 2>/dev/null || true; done
        sleep 1
    fi
    pids=$(find_project_pids)
    [ -z "${pids//[[:space:]]/}" ] || { log "Unable to stop: $pids"; return 1; }
}

start_services() {
    log "Starting dbus-mppsolar manager..."
    "$INSTALL_DIR/scan-hidraw.sh"
}

set_runtime_permissions() {
    chmod +x \
        "$INSTALL_DIR/dbus-mppsolar.py" \
        "$INSTALL_DIR/mppsolar-manager.py" \
        "$INSTALL_DIR/migrate-config.py" \
        "$INSTALL_DIR/verify-runtime.py" \
        "$INSTALL_DIR/inverterd" \
        "$INSTALL_DIR/start-dbus-mppsolar.sh" \
        "$INSTALL_DIR/scan-hidraw.sh" \
        "$INSTALL_DIR/install-gui-extension.py" \
        "$INSTALL_DIR/install-gui-v2-plugin.sh" \
        "$INSTALL_DIR/build-gui-v2-plugin.sh" \
        "$INSTALL_DIR/update.sh"
}

list_expected_serials() {
    PYTHONPATH="$UPDATE_SOURCE_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        DBUS_MPP_INSTALL_DIR="$INSTALL_DIR" DBUS_MPP_STATE_DIR="$STATE_DIR" \
        DBUS_MPP_MANIFEST="$STATE_DIR/devices.json" \
        python3 -c 'from mppsolar_common import load_manifest; print(" ".join(sorted(load_manifest())))'
}

list_active_serials() {
    python3 -c '
import dbus
bus = dbus.SystemBus()
service = "com.victronenergy.mppsolar.manager"
if service not in bus.list_names():
    raise SystemExit(1)
def read(path):
    return bus.get_object(service, path).GetValue(
        dbus_interface="com.victronenergy.BusItem", timeout=5
    )
count = int(read("/DeviceCount"))
print(" ".join(str(read(f"/Devices/{index}/Serial")) for index in range(count)))
'
}

check_services() {
    local elapsed=0 names serial suffix ready legacy_arg=""
    [ ! -f "$INSTALL_DIR/config.json.legacy" ] || legacy_arg="--require-legacy-backup"
    log "Waiting for manager and serial-indexed D-Bus services..."
    while [ "$elapsed" -lt 60 ]; do
        names=$(dbus-send --system --print-reply --dest=org.freedesktop.DBus \
            /org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>/dev/null || true)
        ready=1
        printf '%s\n' "$names" | grep -Fq "com.victronenergy.mppsolar.manager" || ready=0
        for serial in $EXPECTED_SERIALS; do
            suffix="sn_$(printf '%s' "$serial" | tr - _)"
            printf '%s\n' "$names" | grep -Fq "com.victronenergy.inverter.mppsolar-inverter.$suffix" || ready=0
            printf '%s\n' "$names" | grep -Fq "com.victronenergy.solarcharger.mppsolar-charger.$suffix" || ready=0
        done
        if [ "$ready" -eq 1 ]; then
            if DBUS_MPP_EXPECTED_SERIALS="$EXPECTED_SERIALS" \
                python3 "$INSTALL_DIR/verify-runtime.py" --require-migrated $legacy_arg; then
                log "All expected D-Bus services and values are valid."
                return 0
            fi
            ready=0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    log "Expected D-Bus services did not return within 60 seconds."
    return 1
}

validate_installation() {
    log "Validating updated files..."
    python3 -m py_compile \
        "$INSTALL_DIR/dbus-mppsolar.py" \
        "$INSTALL_DIR/mppsolar-manager.py" \
        "$INSTALL_DIR/mppsolar_common.py" \
        "$INSTALL_DIR/migrate-config.py" \
        "$INSTALL_DIR/verify-runtime.py" \
        "$INSTALL_DIR/install-gui-extension.py"
    bash -n "$INSTALL_DIR/update.sh"
    bash -n "$INSTALL_DIR/install.sh"
    sh -n "$INSTALL_DIR/start-dbus-mppsolar.sh"
    sh -n "$INSTALL_DIR/scan-hidraw.sh"
    sh -n "$INSTALL_DIR/install-gui-v2-plugin.sh"
    sh -n "$INSTALL_DIR/build-gui-v2-plugin.sh"
    python3 -m json.tool "$INSTALL_DIR/gui-v2/MppSolarManager/MppSolarManager.json" >/dev/null
    [ -x "$INSTALL_DIR/inverterd" ]
}

backup_optional_file() {
    local source=$1 name=$2
    if [ -e "$source" ] || [ -L "$source" ]; then
        cp -p "$source" "$SYSTEM_BACKUP_DIR/$name"
    else
        : > "$SYSTEM_BACKUP_DIR/$name.absent"
    fi
}

backup_system_integration() {
    SYSTEM_BACKUP_DIR="$BACKUP_DIR/dbus-mppsolar-system-$timestamp-$OLD_REVISION"
    mkdir -p "$SYSTEM_BACKUP_DIR"
    backup_optional_file "$QML_TARGET" PageDeviceInfo.qml
    backup_optional_file /data/rc.local rc.local
    backup_optional_file "$UDEV_RULE" 99-mppsolar.rules
    backup_optional_file "$INIT_SCRIPT" scan-hidraw.sh
    if [ -L "$RCS_LINK" ]; then
        readlink "$RCS_LINK" > "$SYSTEM_BACKUP_DIR/rcs-link-target"
    elif [ -e "$RCS_LINK" ]; then
        cp -p "$RCS_LINK" "$SYSTEM_BACKUP_DIR/rcs-link-file"
    else
        : > "$SYSTEM_BACKUP_DIR/rcs-link.absent"
    fi
}

restore_optional_file() {
    local destination=$1 name=$2
    if [ -f "$SYSTEM_BACKUP_DIR/$name.absent" ]; then
        rm -f "$destination"
    elif [ -e "$SYSTEM_BACKUP_DIR/$name" ]; then
        cp -p "$SYSTEM_BACKUP_DIR/$name" "$destination"
    fi
}

restore_system_integration() {
    [ -n "$SYSTEM_BACKUP_DIR" ] && [ -d "$SYSTEM_BACKUP_DIR" ] || return 0
    restore_optional_file "$QML_TARGET" PageDeviceInfo.qml
    restore_optional_file /data/rc.local rc.local
    restore_optional_file "$UDEV_RULE" 99-mppsolar.rules
    restore_optional_file "$INIT_SCRIPT" scan-hidraw.sh
    rm -f "$RCS_LINK"
    if [ -f "$SYSTEM_BACKUP_DIR/rcs-link-target" ]; then
        ln -s "$(cat "$SYSTEM_BACKUP_DIR/rcs-link-target")" "$RCS_LINK"
    elif [ -f "$SYSTEM_BACKUP_DIR/rcs-link-file" ]; then
        cp -p "$SYSTEM_BACKUP_DIR/rcs-link-file" "$RCS_LINK"
    fi
    udevadm control --reload 2>/dev/null || true
}

install_boot_integration() {
    printf '%s\n' \
        'ACTION=="add|change|remove", SUBSYSTEM=="hidraw", KERNEL=="hidraw*", RUN+="/data/etc/dbus-mppsolar/start-dbus-mppsolar.sh"' \
        > "$UDEV_RULE"
    cp -p "$INSTALL_DIR/scan-hidraw.sh" "$INIT_SCRIPT"
    chmod +x "$INIT_SCRIPT"
    if [ -L "$RCS_LINK" ]; then
        [ "$(readlink "$RCS_LINK")" = "$INIT_SCRIPT" ] || {
            rm -f "$RCS_LINK"
            ln -s "$INIT_SCRIPT" "$RCS_LINK"
        }
    elif [ -e "$RCS_LINK" ]; then
        log "Refusing to replace non-symlink $RCS_LINK"
        return 1
    else
        ln -s "$INIT_SCRIPT" "$RCS_LINK"
    fi
    udevadm control --reload
}

restore_state() {
    [ -n "$STATE_BACKUP" ] && [ -f "$STATE_BACKUP" ] || return 0
    rm -rf "$STATE_DIR"
    mkdir -p "$STATE_DIR"
    tar -xzf "$STATE_BACKUP" -C "$STATE_DIR"
}

rollback() {
    ROLLBACK_IN_PROGRESS=1
    set +e
    log "Update failed; restoring revision $OLD_REVISION..."
    [ "$SERVICES_TOUCHED" -eq 0 ] || stop_services
    git -C "$INSTALL_DIR" reset --hard "$OLD_REVISION"
    git -C "$INSTALL_DIR" submodule update --init --recursive
    restore_state
    if [ -f "$INSTALL_DIR/config.json.legacy" ] && [ -e "$INSTALL_DIR/config.json" ]; then
        cp -p "$INSTALL_DIR/config.json.legacy" "$INSTALL_DIR/config.json"
    fi
    set_runtime_permissions 2>/dev/null || true
    restore_system_integration
    if [ "$SERVICES_TOUCHED" -eq 1 ]; then
        start_services
    fi
    log "Rollback completed. Backups retained in $BACKUP_DIR."
}

on_error() {
    status=$?; line=$1; trap - ERR
    log "Update error at line $line (status $status)."
    if [ "$UPDATE_STARTED" -eq 1 ] && [ "$ROLLBACK_IN_PROGRESS" -eq 0 ]; then rollback; fi
    exit "$status"
}

trap cleanup EXIT
trap 'on_error $LINENO' ERR
trap 'exit 130' INT
trap 'exit 143' TERM

[ -d "$INSTALL_DIR/.git" ] || { log "$INSTALL_DIR is not a Git checkout."; exit 1; }
if [ "$NO_RESTART" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    log "Run this updater as root, or use --no-restart."; exit 1
fi
mkdir "$LOCK_DIR" 2>/dev/null || { log "Another update is running ($LOCK_DIR)."; exit 1; }
cd "$INSTALL_DIR"
git rev-parse --verify '@{upstream}' >/dev/null 2>&1 || { log "No upstream branch configured."; exit 1; }

submodule_status=$(git submodule status --recursive)
grep -q '^-' <<< "$submodule_status" && { log "Git submodules are not initialized."; exit 1; }
unexpected_changes=$(git -c core.fileMode=false status --porcelain --untracked-files=all \
    | sed '/^.. config\.json$/d; /^.. config\.json\.legacy$/d; /^?? __pycache__\//d')
[ -z "$unexpected_changes" ] || {
    log "Update refused: local project changes exist:"; printf '%s\n' "$unexpected_changes"; exit 1;
}

if [ -f config.json ]; then python3 -m json.tool config.json >/dev/null; fi
if [ -f "$STATE_DIR/devices.json" ]; then python3 -m json.tool "$STATE_DIR/devices.json" >/dev/null; fi

log "Fetching upstream changes..."
git fetch --recurse-submodules
OLD_REVISION=$(git rev-parse HEAD)
REMOTE_REVISION=$(git rev-parse '@{upstream}')
[ "$OLD_REVISION" != "$REMOTE_REVISION" ] || { log "Already up to date at $OLD_REVISION."; exit 0; }
git merge-base --is-ancestor "$OLD_REVISION" "$REMOTE_REVISION" || { log "Local and upstream histories diverged."; exit 1; }
log "Update available: $OLD_REVISION -> $REMOTE_REVISION"
[ "$DRY_RUN" -eq 0 ] || { log "Dry run successful; no file or runtime state changed."; exit 0; }

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR" "$STATE_DIR"
BACKUP_ARCHIVE="$BACKUP_DIR/dbus-mppsolar-code-$timestamp-$OLD_REVISION.tar.gz"
STATE_BACKUP="$BACKUP_DIR/dbus-mppsolar-state-$timestamp-$OLD_REVISION.tar.gz"
tar --exclude='.git' -czf "$BACKUP_ARCHIVE" -C "$INSTALL_DIR" .
tar -tzf "$BACKUP_ARCHIVE" >/dev/null
backup_system_integration

if [ "$NO_RESTART" -eq 0 ] && [ -f config.json ]; then
    log "Migrating legacy configuration and live D-Bus history before shutdown..."
    PYTHONPATH="$UPDATE_SOURCE_DIR:$INSTALL_DIR/velib_python${PYTHONPATH:+:$PYTHONPATH}" \
        DBUS_MPP_INSTALL_DIR="$INSTALL_DIR" DBUS_MPP_STATE_DIR="$STATE_DIR" \
        DBUS_MPP_MANIFEST="$STATE_DIR/devices.json" \
        python3 "$UPDATE_SOURCE_DIR/migrate-config.py" --config "$INSTALL_DIR/config.json"
fi
if [ "$NO_RESTART" -eq 0 ]; then
    EXPECTED_SERIALS=$(list_active_serials 2>/dev/null || list_expected_serials)
fi

UPDATE_STARTED=1
if [ "$NO_RESTART" -eq 0 ]; then
    SERVICES_TOUCHED=1
    stop_services
fi
tar -czf "$STATE_BACKUP" -C "$STATE_DIR" .
tar -tzf "$STATE_BACKUP" >/dev/null

# Normalize the legacy tracked config after its safe copy became config.json.legacy.
if git ls-files --error-unmatch config.json >/dev/null 2>&1; then git checkout -- config.json; fi
git checkout -- .
log "Applying fast-forward update..."
git pull --ff-only --recurse-submodules
git submodule update --init --recursive
set_runtime_permissions
validate_installation

if [ "$NO_RESTART" -eq 0 ]; then
    install_boot_integration
    python3 "$INSTALL_DIR/install-gui-extension.py"
    if [ -d /data/apps ]; then "$INSTALL_DIR/install-gui-v2-plugin.sh"; fi
    start_services
    check_services
else
    log "Services and UI/boot integration were not changed (--no-restart)."
fi

UPDATE_STARTED=0
log "Update completed successfully at $(git rev-parse HEAD)."
log "Code backup: $BACKUP_ARCHIVE"
log "State backup: $STATE_BACKUP"
log "System integration backup: $SYSTEM_BACKUP_DIR"
