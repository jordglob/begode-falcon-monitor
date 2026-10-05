#!/usr/bin/env bash
# Install the Falcon monitor on a small Linux board that rides along on the wheel
# (Raspberry Pi OS, Debian, Armbian - 64-bit). Run as the normal user, not as root:
#
#     git clone https://github.com/jordglob/begode-falcon-monitor.git && cd begode-falcon-monitor
#     bash deploy/install.sh
#
# It asks for sudo only to install system packages and to let the service start at boot.
# Safe to run again: every step checks what is already there.
set -euo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="$HOME/.config/systemd/user/begode-falcon.service"

echo "== 1/5 system packages (python venv, bluetooth)"
if ! python3 -c 'import venv, ensurepip' 2>/dev/null || ! command -v bluetoothctl >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y python3-venv python3-pip bluez openssl avahi-daemon avahi-utils
else
  echo "   already installed"
fi

echo "== 2/5 python environment"
[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
# the three packages the app needs to run; requirements.txt adds the test tools and the
# terrain-model readers (numpy, tifffile, imagecodecs), which a logger board can do without
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet bleak fastapi uvicorn

echo "== 3/5 bluetooth on, and usable without a login session"
sudo systemctl enable --now bluetooth
sudo usermod -aG bluetooth "$USER" 2>/dev/null || true
sudo loginctl enable-linger "$USER"

echo "== 4/5 service"
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Begode Falcon monitor (read-only BLE, web on :8096, https on :8443)

[Service]
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python -u -m falcon.server
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT_EOF
systemctl --user daemon-reload
systemctl --user enable --now begode-falcon.service

echo "== 5/5 check"
for _ in $(seq 1 20); do
  if curl -s -m 2 -o /dev/null http://localhost:8096/api/version; then break; fi
  sleep 1
done
curl -s -m 3 http://localhost:8096/api/version || { echo "the app did not answer - see: journalctl --user -u begode-falcon"; exit 1; }
echo
echo "Done. One address on every network: https://falcon.local:8443 (accept the certificate warning once)."
echo "Plain http, without the phone's GPS: http://falcon.local:8096 - or http://$(hostname -I | awk '{print $1}'):8096"
