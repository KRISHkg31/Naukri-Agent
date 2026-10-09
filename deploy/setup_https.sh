#!/usr/bin/env bash
# Public HTTPS for the WhatsApp webhook + login page: Caddy (automatic Let's Encrypt
# certificate) in front of the bot's local web server on 127.0.0.1:8080.
#   bash deploy/setup_https.sh krish-naukri.duckdns.org
# Also needs ingress rules for TCP 80 and 443 in the Oracle Cloud security list.
set -euo pipefail
DOMAIN="${1:?usage: setup_https.sh <domain>}"

echo "==> Opening ports 80/443 in the VM firewall (Oracle images reject them by default)"
for port in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
sudo netfilter-persistent save >/dev/null 2>&1 || sudo apt-get install -y iptables-persistent

echo "==> Installing Caddy"
command -v caddy >/dev/null || { sudo apt-get update -y && sudo apt-get install -y caddy; }

echo "==> Caddyfile for $DOMAIN"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
    @bot path /whatsapp/webhook* /login/* /health
    handle @bot {
        reverse_proxy 127.0.0.1:8080
    }
    handle {
        respond "Not found" 404
    }
}
EOF
sudo systemctl enable caddy
sudo systemctl restart caddy

grep -q '^PUBLIC_BASE_URL=.\+' .env || echo "PUBLIC_BASE_URL=https://$DOMAIN" >> .env
grep -q '^WHATSAPP_VERIFY_TOKEN=.\+' .env \
  || echo "WHATSAPP_VERIFY_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)" >> .env
echo
echo "Done. Webhook URL for Meta:  https://$DOMAIN/whatsapp/webhook"
echo "Verify token for Meta:       $(grep '^WHATSAPP_VERIFY_TOKEN=' .env | cut -d= -f2-)"
