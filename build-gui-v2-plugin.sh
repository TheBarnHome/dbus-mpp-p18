#!/bin/sh

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLUGIN_DIR="$ROOT/gui-v2/MppSolarManager"
COMPILER="${GUI_V2_PLUGIN_COMPILER:-/opt/victronenergy/gui-v2/gui-v2-plugin-compiler.py}"

if [ ! -f "$COMPILER" ]; then
    echo "gui-v2 plugin compiler not found: $COMPILER" >&2
    exit 1
fi

cd "$PLUGIN_DIR"
python3 "$COMPILER" \
    --name MppSolarManager \
    --version 1.0 \
    --min-required-version v1.2.13 \
    --settings MppSolarManager_PageSettings.qml

python3 -m json.tool MppSolarManager.json >/dev/null
test -s MppSolarManager.json
echo "Prepared gui-v2 artifact: $PLUGIN_DIR/MppSolarManager.json"
