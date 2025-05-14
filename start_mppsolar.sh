#!/bin/bash

DEVICE_PATH="$1"
DEVICE_NAME=$(basename "$DEVICE_PATH")
SERVICE_NAME="dbus-mppsolar.${DEVICE_NAME}"
SERVICE_DIR="/service"
SERVICE_PATH="/etc/sv/${SERVICE_NAME}"
LOG_DIR="/var/log/${SERVICE_NAME}"
SCRIPT_PATH="/data/etc/dbus-mppsolar/dbus-mppsolar.py"

# Créer le répertoire du service si nécessaire
if [ ! -d "$SERVICE_PATH" ]; then
  mkdir -p "$SERVICE_PATH/log"

  # Script de lancement du service
  cat > "$SERVICE_PATH/run" <<EOF
#!/bin/sh
exec /usr/bin/env python3 ${SCRIPT_PATH} --serial /dev/${DEVICE_NAME}
EOF
  chmod +x "$SERVICE_PATH/run"

  # Script de log (optionnel)
  cat > "$SERVICE_PATH/log/run" <<EOF
#!/bin/sh
exec svlogd -tt ${LOG_DIR}
EOF
  chmod +x "$SERVICE_PATH/log/run"
fi

# Activer le service (via lien symbolique)
if [ ! -L "${SERVICE_DIR}/${SERVICE_NAME}" ]; then
  ln -s "$SERVICE_PATH" "${SERVICE_DIR}/${SERVICE_NAME}"
fi
