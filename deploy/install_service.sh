#!/bin/bash
# Installs the systemd service defined in urbanrain-analytics.service (this
# same directory) so the platform controller starts automatically on boot
# and restarts itself if it ever crashes.
#
# Review/edit urbanrain-analytics.service *before* running this if this
# device's paths, service user, or hailo-apps venv location differ from the
# defaults baked into it.
#
# Usage: sudo ./deploy/install_service.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Must be run as root: sudo ./deploy/install_service.sh" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="urbanrain-analytics.service"

cp "$SCRIPT_DIR/$UNIT_NAME" "/etc/systemd/system/$UNIT_NAME"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"

echo ""
echo "Installed and enabled $UNIT_NAME -- it will now start automatically on boot."
echo "Start it now with:   sudo systemctl start ${UNIT_NAME%.service}"
echo "Check status with:   sudo systemctl status ${UNIT_NAME%.service}"
echo "Follow logs with:    sudo journalctl -u ${UNIT_NAME%.service} -f"
