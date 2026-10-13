#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this script as your normal user, without sudo."
    exit 1
fi
app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_user="$(id -un)"
data_dir="$HOME/jukebox-data"
if [[ "$app_dir" == *[[:space:]]* || "$app_dir" == *%* ]]; then
    echo "Extract the application to a path without spaces or percent signs."
    exit 1
fi
sudo apt-get update
sudo apt-get install -y python3-venv python3-dev build-essential mpv ffmpeg
python3 -m venv "$app_dir/.venv"
"$app_dir/.venv/bin/pip" install -r "$app_dir/requirements.txt"
sudo usermod -aG spi,gpio,audio "$app_user"
mkdir -p "$data_dir"
unit_file="$(mktemp)"
trap 'rm -f "$unit_file"' EXIT
cat > "$unit_file" <<EOF
[Unit]
Description=NFC Jukebox Manager
After=sound.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$app_user
SupplementaryGroups=spi gpio audio
WorkingDirectory=$app_dir
ExecStart=$app_dir/.venv/bin/python $app_dir/server.py --data $data_dir
Restart=on-failure
RestartSec=5
TimeoutStopSec=10
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 644 "$unit_file" /etc/systemd/system/jukebox-manager.service
sudo systemctl daemon-reload
sudo systemctl enable jukebox-manager.service
sudo systemctl restart jukebox-manager.service
echo
echo "Open http://jukebox.local:8080 or http://PI_IP:8080"
echo "No access key is required."
echo
echo "Service status:"
sudo systemctl --no-pager --full status jukebox-manager.service || true
