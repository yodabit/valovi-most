# VALOVI MOST — motor, Faza 1 (paper)

Jedan proces, dvije knjige: DNEVNI (satne svijece) i TJEDNI (dnevne svijece).
Kraken javne cijene, LONG i SHORT. Nista se ne trguje pravim parama.

## Instalacija (VNC konzola — komande BEZ dvotocke)

    cd /opt/valovi
    curl -sLo most.tgz github.com/yodabit/valovi-most/archive/refs/heads/main.tar.gz
    tar xf most.tgz
    bash valovi-most-main/install.sh

## Provjera

    journalctl -u valovi-most -n 30 --no-pager

## Promjena parametara: uredi most/params.json na GitHubu pa ponovi 4 komande.
Stanje i dnevnik u /opt/valovi/data se NE diraju.
