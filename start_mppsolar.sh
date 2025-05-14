#!/bin/bash

LOGFILE="/var/log/create_mppsolar_service.log"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

DEVICE_PATH="$1"
DEVICE_NAME=$(basename "$DEVICE_PATH")
SERVICE_NAME="dbus-mppsolar.${DEVICE_NAME}"
SERVICE_DIR="/service"
SERVICE_PATH="'/var/volatile/services/${SERVICE_NAME}"
LOG_DIR="/var/log/${SERVICE_NAME}"
SCRIPT_PATH="/data/etc/dbus-mppsolar/dbus_mppsolar.py"

echo "[$TIMESTAMP] 📦 Création du service pour $DEVICE_NAME ($DEVICE_PATH)" >> "$LOGFILE"

# Créer le répertoire du service si nécessaire
if [ ! -d "$SERVICE_PATH" ]; then
  echo "[$TIMESTAMP] 📁 Création du dossier $SERVICE_PATH" >> "$LOGFILE"
  mkdir -p "$SERVICE_PATH/log"

  echo "[$TIMESTAMP] 📝 Génération du script de lancement" >> "$LOGFILE"
  cat > "$SERVICE_PATH/run" <<EOF
#!/bin/sh
exec /usr/bin/env python3 ${SCRIPT_PATH} --device /dev/${DEVICE_NAME}
EOF
  chmod +x "$SERVICE_PATH/run"
  echo "[$TIMESTAMP] ✅ Script de lancement créé et rendu exécutable." >> "$LOGFILE"

  echo "[$TIMESTAMP] 📝 Génération du script de log" >> "$LOGFILE"
  cat > "$SERVICE_PATH/log/run" <<EOF
#!/bin/sh
exec svlogd -tt ${LOG_DIR}
EOF
  chmod +x "$SERVICE_PATH/log/run"
  echo "[$TIMESTAMP] ✅ Script de log créé et rendu exécutable." >> "$LOGFILE"
else
  echo "[$TIMESTAMP] ℹ️ Le dossier $SERVICE_PATH existe déjà. Aucune action." >> "$LOGFILE"
fi

# Activer le service (via lien symbolique)
if [ ! -L "${SERVICE_DIR}/${SERVICE_NAME}" ]; then
  echo "[$TIMESTAMP] 🔗 Activation du service via symlink dans ${SERVICE_DIR}" >> "$LOGFILE"
  ln -s "$SERVICE_PATH" "${SERVICE_DIR}/${SERVICE_NAME}"
  echo "[$TIMESTAMP] ✅ Service ${SERVICE_NAME} activé." >> "$LOGFILE"
else
  echo "[$TIMESTAMP] ℹ️ Le service ${SERVICE_NAME} est déjà activé." >> "$LOGFILE"
fi

exit 0
