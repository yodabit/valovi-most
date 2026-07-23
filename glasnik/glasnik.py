#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# VALOVI GLASNIK — nosi stanje motora na GitHub (docs/stanje.json)
# Cita: /opt/valovi/data/most_state.json + most_dnevnik.jsonl
# Token: /opt/valovi/glasnik_token (samo root, NIKAD u repo)

import json, base64, time, urllib.request, urllib.error

REPO = "yodabit/valovi-most"
CILJ = "docs/stanje.json"
TOKEN_DAT = "/opt/valovi/glasnik_token"
STATE_DAT = "/opt/valovi/data/most_state.json"
DNEVNIK_DAT = "/opt/valovi/data/most_dnevnik.jsonl"
PARAMS_DAT = "/opt/valovi/most/params.json"
MAX_DNEVNIK = 200  # zadnjih N zapisa

API = "https://api.github.com/repos/%s/contents/%s" % (REPO, CILJ)

def log(msg):
    print(time.strftime("[%d.%m. %H:%M UTC] ", time.gmtime()) + msg, flush=True)

def ucitaj_token():
    with open(TOKEN_DAT) as f:
        return f.read().strip()

def ucitaj_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log("ne mogu procitati %s (%s)" % (path, e))
        return default

def ucitaj_dnevnik():
    zapisi = []
    try:
        with open(DNEVNIK_DAT) as f:
            linije = f.readlines()[-MAX_DNEVNIK:]
        for l in linije:
            l = l.strip()
            if not l:
                continue
            try:
                zapisi.append(json.loads(l))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    except Exception as e:
        log("dnevnik problem (%s)" % e)
    return zapisi

def github_zahtjev(url, token, metoda="GET", tijelo=None):
    req = urllib.request.Request(url, method=metoda)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "valovi-glasnik")
    data = None
    if tijelo is not None:
        data = json.dumps(tijelo).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"greska": str(e)}

def main():
    token = ucitaj_token()
    state = ucitaj_json(STATE_DAT, {})
    params = ucitaj_json(PARAMS_DAT, {})
    paket = {
        "generirano_utc": time.strftime("%d.%m.%Y %H:%M:%S UTC", time.gmtime()),
        "generirano_ts": int(time.time()),
        "izvor": "valovi-most paper",
        "state": state,
        "params": params,
        "dnevnik": ucitaj_dnevnik(),
    }
    sadrzaj = base64.b64encode(
        json.dumps(paket, ensure_ascii=False, indent=1).encode("utf-8")
    ).decode("ascii")

    # postojeci sha (treba za update)
    status, odg = github_zahtjev(API, token)
    sha = odg.get("sha") if status == 200 else None

    tijelo = {"message": "glasnik " + paket["generirano_utc"],
              "content": sadrzaj, "branch": "main"}
    if sha:
        tijelo["sha"] = sha

    status, odg = github_zahtjev(API, token, "PUT", tijelo)
    if status in (200, 201):
        log("stanje odneseno na GitHub (%d znakova)" % len(sadrzaj))
    else:
        log("GRESKA push (%s): %s" % (status, str(odg)[:200]))

if __name__ == "__main__":
    main()
