#!/bin/bash

set -e

CONF_FILE="/etc/venus/serial-starter.conf"
BACKUP_FILE="${CONF_FILE}.bak.$(date +%F_%T)"
INSTALL_DIR="/data/etc/dbus-mppsolar"
SERVICE_TEMPLATE_SRC="$INSTALL_DIR/service"
SERVICE_TEMPLATE_DST="/opt/victronenergy/service-templates/dbus-mppsolar"
UDEV_RULES_PATH="/etc/udev/rules.d/99-mppsolar.rules"
START_SCRIPT="/data/etc/dbus-mppsolar/start_mppsolar.sh"

echo "🔧 Backing up $CONF_FILE to $BACKUP_FILE"
cp "$CONF_FILE" "$BACKUP_FILE"

# 1. Add mppsolar service if not already present
if ! grep -q "^service[[:space:]]\+mppsolar[[:space:]]\+dbus-mppsolar" "$CONF_FILE"; then
    echo "✅ Adding 'mppsolar' service definition"
    echo "service mppsolar        dbus-mppsolar" >> "$CONF_FILE"
else
    echo "ℹ️  'mppsolar' service is already defined"
fi

# 2. Update 'default' alias
if grep -q "^alias[[:space:]]\+default" "$CONF_FILE"; then
    echo "✅ Updating 'default' alias to include 'mppsolar'"
    sed -i -E 's/^(alias[[:space:]]+default[[:space:]]+)(.*)/\1mppsolar:\2/' "$CONF_FILE"
    sed -i -E 's/(:?mppsolar)(:.*)?(:mppsolar)+/\1\2/' "$CONF_FILE"
else
    echo "✅ Adding new 'default' alias"
    echo "alias   default         mppsolar:gps:vedirect" >> "$CONF_FILE"
fi

# 3. Copy the service template
if [ -d "$SERVICE_TEMPLATE_SRC" ]; then
    echo "📁 Copying service template to $SERVICE_TEMPLATE_DST"
    cp -R "$SERVICE_TEMPLATE_SRC" "$SERVICE_TEMPLATE_DST"
else
    echo "❌ Service template source directory not found: $SERVICE_TEMPLATE_SRC"
    exit 1
fi

# 5. Switch to 'release' feed
if [ -x /opt/victronenergy/swupdate-scripts/set-feed.sh ]; then
    echo "🔄 Switching software feed to 'release'"
    /opt/victronenergy/swupdate-scripts/set-feed.sh release
else
    echo "⚠️  Feed switch script not found: /opt/victronenergy/swupdate-scripts/set-feed.sh"
fi

# 6. Install packages
echo "📦 Updating packages and installing dependencies"
opkg update
opkg install python3-pip git

# 7. Install inverterd
echo "🐍 Installing 'inverterd' via pip3"
pip3 install inverterd

echo "🛠️ Installation de la règle udev..."

# Créer la règle udev
cat <<EOF | tee "$UDEV_RULES_PATH" > /dev/null
ACTION=="add", KERNEL=="hidraw[0-9]*", ATTRS{idVendor}=="0665", ATTRS{idProduct}=="5161", RUN+="${START_SCRIPT} %E{DEVNAME}"
EOF

echo "✅ Règle udev créée à $UDEV_RULES_PATH"

# Donner les droits d'exécution au script
if [ -f "$START_SCRIPT" ]; then
    chmod +x "$START_SCRIPT"
    echo "✅ Droits d'exécution ajoutés à $START_SCRIPT"
else
    echo "❌ Le fichier $START_SCRIPT est introuvable !"
    exit 1
fi

# Recharger udev
echo "🔁 Rechargement des règles udev..."
udevadm control --reload
udevadm trigger

echo "✅ All steps completed successfully."
