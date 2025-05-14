#!/bin/bash

LOGFILE="/var/log/start_mppsolar.log"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

echo "[$TIMESTAMP] 🔌 Script start_mppsolar.sh lancé avec les arguments : $@" >> "$LOGFILE"

DEVICE_PATH="$1"

if [ -z "$DEVICE_PATH" ]; then
    echo "[$TIMESTAMP] ❌ Aucun périphérique spécifié." >> "$LOGFILE"
    exit 1
fi

echo "[$TIMESTAMP] ➕ Nouveau périphérique détecté : $DEVICE_PATH" >> "$LOGFILE"

# Exemple de lancement du service : adapter selon ton besoin réel
SERVICE_NAME="dbus-mppsolar.${DEVICE_PATH##*/}"

echo "[$TIMESTAMP] 🛠️ Création du service $SERVICE_NAME" >> "$LOGFILE"
/data/etc/dbus_mppsolar/create-service.sh "$DEVICE_PATH" >> "$LOGFILE" 2>&1

if [ $? -eq 0 ]; then
    echo "[$TIMESTAMP] ✅ Service $SERVICE_NAME créé avec succès." >> "$LOGFILE"
else
    echo "[$TIMESTAMP] ❌ Échec de la création du service $SERVICE_NAME." >> "$LOGFILE"
    exit 1
fi

exit 0
