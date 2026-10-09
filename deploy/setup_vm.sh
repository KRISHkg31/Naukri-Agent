#!/usr/bin/env bash
# One-time setup on the Oracle Cloud Ubuntu VM. Run from inside the project folder:
#   cd ~/naukri-agent && bash deploy/setup_vm.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="$(whoami)"
cd "$APP_DIR"

echo "==> Timezone -> Asia/Kolkata"
sudo timedatectl set-timezone Asia/Kolkata

echo "==> System packages"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip rsync

# Chromium needs more than the 1 GB the E2.1.Micro shape has; add swap if none exists.
if ! swapon --show | grep -q .; then
  echo "==> Adding 2G swap"
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "==> Python venv + Playwright Chromium"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
sudo "$APP_DIR/.venv/bin/playwright" install-deps chromium
.venv/bin/playwright install chromium

mkdir -p data logs
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "!! Fill in $APP_DIR/.env"; }

grep -q '^SESSION_KEY=.\+' .env || { echo "SESSION_KEY=$(.venv/bin/python main.py gen-key | cut -d= -f2-)" >> .env; echo "==> Generated SESSION_KEY in .env"; }

echo "==> Telegram bot service (runs the 09/10/11/12 IST schedule itself)"
# The bot replaces the old single-user timer.
sudo systemctl disable --now naukri-agent.timer 2>/dev/null || true
sed "s|__APP_DIR__|$APP_DIR|g; s|__USER__|$APP_USER|g" deploy/naukri-bot.service \
  | sudo tee /etc/systemd/system/naukri-bot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable naukri-bot.service
sudo systemctl restart naukri-bot.service

echo
.venv/bin/python main.py validate
echo
systemctl status naukri-bot.service --no-pager | head -5
echo "Done. Open your Telegram bot and send /start."
