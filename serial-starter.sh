#! /bin/bash
exec 2>&1

. $(dirname $0)/functions.sh

BASE_DIR='/opt/victronenergy'
CACHE_DIR='/data/var/lib/serial-starter'
SERVICE_DIR='/var/volatile/services'
SS_CONFIG='/etc/venus/serial-starter.conf'

# Remove stale service symlinks
find -L /service -maxdepth 1 -type l -delete

mkdir -p "$CACHE_DIR"
mkdir -p "$SERVICE_DIR"

get_property() {
    udevadm info --query=property --name="$1" | sed -n "s/^$2=//p"
}

get_product() {
    devname=$1
    devpath=/sys/class/hidraw/$devname/device

    if [ ! -e "$devpath" ]; then
        echo ignore
        return
    fi

    ve_product=$(get_property /dev/$devname VE_PRODUCT)

    if [ -n "$ve_product" ]; then
        echo $ve_product
        return
    fi

    # fallback: ID_MODEL for HID devices
    get_property /dev/$devname ID_MODEL || echo ignore
}

get_program() {
    devname=$1
    product=$2

    ve_service=$(get_property /dev/$devname VE_SERVICE)

    if [ -n "$ve_service" ]; then
        # If a seperate debug console is lacking a ve-direct port can be used instead..
        if [ "$ve_service" = "vedirect-or-vegetty" ]; then
            if [ -e /service/vegetty ]; then
                echo ignore
            else
                echo vedirect
            fi
            return
        fi

        echo $ve_service
        return
    fi

    case $product in
        builtin-mkx)
            echo mkx
            ;;
        builtin-vedirect)
            echo vedirect
            ;;
        ignore)
            echo ignore
            ;;
        *)
            echo default
            ;;
    esac
}

create_service() {
    service=$1
    hid=$2

    # check if service already exists
    test -d "/service/$SERVICE" && return 0

    tmpl=$BASE_DIR/service-templates/$service

    # check existence of service template
    if [ ! -d "$tmpl" ]; then
        echo "ERROR: no service template for $service"
        return 1
    fi

    echo "INFO: Create daemontools service $SERVICE"

    # copy service
    cp -a "$tmpl" "$SERVICE_DIR/$SERVICE"

    # Patch run files for tty device
    sed -i "s:HID:$hid:" "$SERVICE_DIR/$SERVICE/run"
    sed -i "s:HID:$hid:" "$SERVICE_DIR/$SERVICE/log/run"

    # Create symlink to /service
    ln -sf "$SERVICE_DIR/$SERVICE" "/service/$SERVICE"

    # wait for svscan to find service
    sleep 6
}

start_service() {
    eval service="\$svc_$1"
    hid=$2

    if [ -z "$service" ]; then
        echo "ERROR: unknown service $1"
        return 1
    fi

    SERVICE="${service}.${hid}"

    if ! create_service $service $hid; then
        unlock_tty $hid
        return 1
    fi

    # update product string
    sed -i "s:PRODUCT\(=[^ ]*\)*:PRODUCT=$PRODUCT:" "$SERVICE_DIR/$SERVICE/run"

    svc -u "/service/$SERVICE/log"

    if [ $AUTOPROG = n ]; then
        echo "INFO: Start service $SERVICE"
        svc -u "/service/$SERVICE"
    else
        echo "INFO: Start service $SERVICE once"
        svc -o "/service/$SERVICE"
    fi
}

# recursively expand aliases, removing duplicates
expand_alias() {
    set -- $(echo $1 | tr : ' ')

    for v; do
        eval x="\$exp_$v"
        test -n "$x" && continue

        eval e="\$alias_$v"
        eval "exp_$v=1"

        if [ -n "$e" ]; then
            expand_alias "$e"
        else
            echo $v
        fi
    done
}

# expand aliases and return colon separated list
get_alias() (
    set -- $(expand_alias $1)
    IFS=:
    echo "$*"
)

check_val() {
    if echo "$1" | grep -Eqv "^$2+\$"; then
        echo "ERROR: $3 ${1:+'$1'}" >&2
    fi
}

load_config() {
    cfg=$1

    test -r "$cfg" || return

    echo "INFO: loading config file $cfg" >&2

    sed 's/#.*//' "$cfg" | while read keyword name value; do
        # ignore blank lines
        test -z "$keyword" && continue

        case $keyword in
            service)
                check_val "$name" '[[:alnum:]_]' 'invalid service name'
                check_val "$value" '[[:alnum:]_:.-]' 'invalid service value'
                echo "svc_$name=$value"
                ;;
            alias)
                check_val "$name" '[[:alnum:]_]' 'invalid alias name'
                check_val "$value" '[[:alnum:]_:-]' 'invalid alias value'
                echo "alias_$name=$value"
                ;;
            include)
                check_val "$name" . 'include: name required'
                if [ -d "$name" ]; then
                    for file in "$name"/*.conf; do
                        load_config "$file"
                    done
                else
                    load_config "$name"
                fi
                ;;
            *)
                echo "ERROR: unknown keyword $keyword" >&2
                ;;
        esac
    done
}

echo "serstart starting"

eval $(load_config "$SS_CONFIG")

echo "looking for hidraw"
while true; do
    HIDDEVS=$(ls /dev/hidraw* 2>/dev/null | xargs -n1 basename)
    for HID in $HIDDEVS; do
        CACHE_FILE="$CACHE_DIR/$HID"
        PROG_FILE="/tmp/$HID.prog"

        lock_tty $HID || continue

        # device may have vanished while running for loop
        if ! test -e /dev/$HID; then
            unlock_tty $HID
            continue
        fi

        # check for a known device
        PRODUCT=$(get_product $HID)
        PROGRAMS=$(get_program $HID $PRODUCT)
        PROGRAMS=$(get_alias $PROGRAMS)

        if [ "$PROGRAMS" = ignore ]; then
            rm /dev/$HID
            unlock_tty $HID
            continue
        elif [ "${PROGRAMS%%:*}" = "${PROGRAMS}" ]; then
            AUTOPROG=n
            PROGRAM=$PROGRAMS
            rm /dev/$HID
        else
            AUTOPROG=y

            if [ -f "$PROG_FILE" ]; then
                # next entry in probe cycle
                PROGRAM=$(cat $PROG_FILE)
            elif [ -f "$CACHE_FILE" ]; then
                # last used program
                PROGRAM=$(cat $CACHE_FILE)
            fi

            if ! echo ":$PROGRAMS:" | grep -q ":$PROGRAM:"; then
                # invalid cache, reset
                PROGRAM=${PROGRAMS%%:*}
            fi
        fi

        for n in $(echo $PROGRAMS | tr : ' '); do
            mkdir -p /run/serial-starter/$n
            ln -sf /dev/$HID /run/serial-starter/$n/$HID
        done

        echo "$PROGRAM" >"$CACHE_FILE"

        start_service $PROGRAM $HID &

        if [ $AUTOPROG = y ]; then
            NEXT=${PROGRAMS#*${PROGRAM}:}
            NEXT=${NEXT%%:*}
            echo "$NEXT" >"$PROG_FILE"
        fi

        sleep 1
    done

    sleep 2
done