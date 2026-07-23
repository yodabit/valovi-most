#!/bin/bash
# Instalacija Valovi glasnika
set -e
echo "== Valovi Glasnik instalacija =="
IZVOR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p /opt/valovi/glasnik
cp "$IZVOR/glasnik.py" /opt/valovi/glasnik/glasnik.py
cp "$IZVOR/valovi-glasnik.service" /etc/systemd/system/
cp "$IZVOR/valovi-glasnik.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now valovi-glasnik.timer
systemctl start valovi-glasnik.service
echo "== Gotovo. Provjera =="
echo "   journalctl -u valovi-glasnik -n 10 --no-pager"
