#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/psd_ai-ui.service"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: psd_ai-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing psd.ai UI service..."
echo "Make sure you've edited psd_ai-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable psd_ai-ui
sudo systemctl start psd_ai-ui
sudo systemctl status psd_ai-ui
