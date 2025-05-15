#!/bin/bash

set -e

INSTALL_DIR="/data/etc/dbus-mppsolar"
UDEV_RULES_PATH="/etc/udev/rules.d/99-mppsolar.rules"
START_SCRIPT="$INSTALL_DIR/start-dbus-mppsolar.sh"
INIT_SCRIPT_PATH="/etc/init.d"
RCS_LINK="/etc/rcS.d/S99scan-hidraw"

if [ -x /opt/victronenergy/swupdate-scripts/set-feed.sh ]; then
    echo "🔄 Switching software feed to 'release'"
    /opt/victronenergy/swupdate-scripts/set-feed.sh release
else
    echo "⚠️  Feed switch script not found: /opt/victronenergy/swupdate-scripts/set-feed.sh"
fi
echo "📦 Mise à jour des paquets et installation des dépendances..."
opkg update
opkg install python3-pip git

echo "🐍 Installation de 'inverterd' via pip3..."
pip3 install inverterd

echo "🛠️ Création de la règle udev..."

cat <<EOF | tee "$UDEV_RULES_PATH" > /dev/null
ACTION=="change", SUBSYSTEM=="hidraw", KERNEL=="hidraw*", RUN+="$START_SCRIPT %k"
EOF

echo "✅ Règle udev créée à $UDEV_RULES_PATH"

if [ -f "$START_SCRIPT" ]; then
    chmod +x "$START_SCRIPT"
    chmod +x "$INSTALL_DIR/dbus-mppsolar.py"
    chmod +x "$INSTALL_DIR/inverterd"
    chmod +x "$INSTALL_DIR/start-dbus-mppsolar.sh"
    echo "✅ Droits d'exécution appliqués aux scripts"
else
    echo "❌ Le fichier $START_SCRIPT est introuvable !"
    exit 1
fi

echo "🔁 Rechargement des règles udev..."
udevadm control --reload
udevadm trigger

# Vérification du script d'init existant
if [ -f "$INIT_SCRIPT_PATH/scan-hidraw.sh" ]; then
    echo "ℹ️ Le script d'init $INIT_SCRIPT_PATH/scan-hidraw.sh existe déjà, aucune création nécessaire"
else
    cp "$INSTALL_DIR/scan-hidraw.sh" "$INIT_SCRIPT_PATH/scan-hidraw.sh"
    chmod +x "$INIT_SCRIPT_PATH/scan-hidraw.sh"
fi

# Ajout du lien dans /etc/rcS.d si nécessaire
if [ ! -L "$RCS_LINK" ]; then
    ln -s "$INIT_SCRIPT_PATH/scan-hidraw.sh" "$RCS_LINK"
    echo "✅ Lien symbolique ajouté : $RCS_LINK"
else
    echo "ℹ️ Le lien $RCS_LINK existe déjà"
fi

echo "🎉 Installation terminée avec succès."
