#!/bin/bash

set -e

INSTALL_DIR="/data/etc/dbus-mppsolar"

echo "🔄 Resetting local changes..."
cd "$INSTALL_DIR"
git reset --hard

echo "⬇️ Pulling latest changes..."
git pull --recurse-submodules

echo "🚀 Launching install script..."
bash install.sh
