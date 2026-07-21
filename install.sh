#!/bin/bash
set -e
IZVOR="$(cd "$(dirname "$0")" && pwd)"

echo "== Valovi Most instalacija =="

mkdir -p /opt/valovi/most /opt/valovi/data
cp "$IZVOR/most/motor.py"   /opt/valovi/most/
cp "$IZVOR/most/tokeni.json" /opt/valovi/most/
cp "$IZVOR/most/params.json" /opt/valovi/most/
cp "$IZVOR/valovi-most.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable valovi-most >/dev/null 2>&1
systemctl restart valovi-most

echo "== Gotovo. Motor radi. Provjera: =="
echo "   journalctl -u valovi-most -n 30 --no-pager"
