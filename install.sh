#!/usr/bin/env bash
# One-time preparation of a fresh Pi. Code, the systemd unit and the ALSA config
# are installed afterwards by ./deploy.ps1 from the development machine.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this script as your normal user, without sudo."
    exit 1
fi
sudo apt-get update
sudo apt-get install -y python3-venv python3-dev build-essential mpv ffmpeg
sudo usermod -aG spi,gpio,audio "$(id -un)"
mkdir -p "$HOME/jukebox-data"
echo
echo "Done. Make sure config.txt has dtparam=spi=on and dtoverlay=max98357a,"
echo "then run ./deploy.ps1 on the development machine."
