#!/bin/bash
# NOVA ERA instalacija: arhivira staru eru, cisti stanje, pali Motor 2.0
set -e
echo "== VALOVI NOVA ERA instalacija =="
systemctl stop valovi-most || true
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p /opt/valovi/arhiva-stara-era
for f in /opt/valovi/data/most_state.json /opt/valovi/data/most_dnevnik.jsonl; do
  if [ -f "$f" ]; then cp "$f" "/opt/valovi/arhiva-stara-era/$(basename $f).$STAMP"; fi
done
rm -f /opt/valovi/data/most_state.json /opt/valovi/data/most_dnevnik.jsonl
IZVOR="$(cd "$(dirname "$0")" && pwd)"
cp "$IZVOR/motor.py" /opt/valovi/most/motor.py
cp "$IZVOR/params.json" /opt/valovi/most/params.json
cp "$IZVOR/tokeni.json" /opt/valovi/most/tokeni.json
python3 -m py_compile /opt/valovi/most/motor.py
systemctl start valovi-most
echo "== Gotovo. Stara era arhivirana u /opt/valovi/arhiva-stara-era =="
echo "   journalctl -u valovi-most -n 25 --no-pager"
