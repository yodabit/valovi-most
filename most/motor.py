#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# VALOVI MOST — MOTOR 2.0 "NOVA ERA"
# Futures paper simulacija (Kraken Futures cjenik), USD, dvije knjige:
#   dnevni (6000 USD, max 10, tajmer 48h) + tjedni (4000 USD, max 5, tajmer 7d)
# Filozofija izlaza: OPRUGA — lokot na break-even (ulaz+izlaz placeni),
#   pametni trailing (ljestvica + gorivo), tajmer od lokota, stop za minus.
# Piramida: fiksnih +200 USD po dokupu, do 5 dokupa. Filter plime cuva ulaze.

import json, os, time, urllib.request
from datetime import datetime, timedelta, timezone

VERZIJA = "most-2.0"
BAZA = "/opt/valovi"
PARAMS_PATH = BAZA + "/most/params.json"
TOKENI_PATH = BAZA + "/most/tokeni.json"
STATE_PATH = BAZA + "/data/most_state.json"
DNEVNIK_PATH = BAZA + "/data/most_dnevnik.jsonl"

# ---------- pomocnici ----------

def sada_utc():
    return datetime.now(timezone.utc)

def ts():
    return sada_utc().strftime("%d.%m. %H:%M UTC")

def log(msg):
    print(f"[{ts()}] {msg}", flush=True)

def http_json(url, pokusaja=3):
    for i in range(pokusaja):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "valovi-most"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == pokusaja - 1:
                log(f"HTTP problem ({url.split('?')[0]}): {e}")
            time.sleep(1 + i)
    return None

def atomski_zapis(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

def dnevnik_zapis(z):
    with open(DNEVNIK_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(z, ensure_ascii=False) + "\n")

def ema_niz(zatvaranja, n):
    if not zatvaranja:
        return []
    k = 2.0 / (n + 1)
    e = [zatvaranja[0]]
    for c in zatvaranja[1:]:
        e.append(c * k + e[-1] * (1 - k))
    return e

def rsi(zatvaranja, n=14):
    if len(zatvaranja) < n + 1:
        return 50.0
    dob, gub = [], []
    for i in range(1, len(zatvaranja)):
        d = zatvaranja[i] - zatvaranja[i - 1]
        dob.append(max(d, 0))
        gub.append(max(-d, 0))
    ad = sum(dob[:n]) / n
    ag = sum(gub[:n]) / n
    for i in range(n, len(dob)):
        ad = (ad * (n - 1) + dob[i]) / n
        ag = (ag * (n - 1) + gub[i]) / n
    if ag == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ad / ag)

def prosjecni_raspon_pct(svijece, n=14):
    if len(svijece) < 2:
        return None
    rasponi = []
    for s in svijece[-n:]:
        if s["close"]:
            rasponi.append((s["high"] - s["low"]) / s["close"] * 100.0)
    return sum(rasponi) / len(rasponi) if rasponi else None

def signal_tokena(svijece, ema_b, ema_s, rsi_g, rsi_d):
    z = [s["close"] for s in svijece]
    if len(z) < ema_s + 2:
        return None, 50.0
    eb = ema_niz(z, ema_b)
    es = ema_niz(z, ema_s)
    r = rsi(z)
    if eb[-2] <= es[-2] and eb[-1] > es[-1] and r < rsi_g:
        return "LONG", r
    if eb[-2] >= es[-2] and eb[-1] < es[-1] and r > rsi_d:
        return "SHORT", r
    return None, r

def gorivo_vala(smjer, rsi_v, pl_pct, val_pct):
    # 0-100: koliko val jos ima daha (RSI udaljenost + pojedeni dio prosjecnog vala)
    if smjer == "LONG":
        rsi_dio = max(0.0, min(1.0, (75.0 - rsi_v) / 40.0))
    else:
        rsi_dio = max(0.0, min(1.0, (rsi_v - 25.0) / 40.0))
    if val_pct and val_pct > 0:
        val_dio = max(0.0, min(1.0, 1.0 - max(pl_pct, 0.0) / (val_pct * 1.5)))
    else:
        val_dio = 0.5
    return round((rsi_dio * 0.6 + val_dio * 0.4) * 100.0)

def nova_knjiga(sredstva):
    return {
        "valuta": "USD",
        "sredstva_pocetna": sredstva,
        "kasa": sredstva,          # ziva municija (reinvest je mijenja)
        "pozicije": {},
        "sef": 0.0,
        "realizirano_period": 0.0,
        "period_od": None,
        "pauza_do": None,
        "broj_trejdova": 0, "dobitni": 0, "gubitni": 0,
        "zadnji_izlaz_signal": {},
    }

# ---------- trziste (Kraken javni API, USD parovi) ----------

ALIAS = {"BTC": "XBT", "DOGE": "XDG"}

class Trziste:
    def __init__(self, tokeni):
        self.parovi = {}
        self.mrtvi = []
        for t in tokeni:
            self.parovi[t] = (ALIAS.get(t, t)) + "USD"
        self.svijece = {}   # (tok, interval) -> lista
        self.cijene = {}
        self.rot = {}       # interval -> pokazivac rotacije

    def rijesi_parove(self):
        info = http_json("https://api.kraken.com/0/public/AssetPairs")
        if not info or "result" not in info:
            log("POZOR: AssetPairs nedostupan — nastavljam s pretpostavkama")
            return
        dostupni = " ".join(info["result"].keys())
        for t, par in list(self.parovi.items()):
            if par not in dostupni:
                log(f"POZOR: {par} ne postoji na Krakenu — preskacem token")
                self.mrtvi.append(t)
                del self.parovi[t]
        log(f"parovi rijeseni: {len(self.parovi)} tokena aktivno (USD)")

    def osvjezi_svijece(self, interval_min, max_poziva):
        greske = 0
        lista = sorted(self.parovi.keys())
        start = self.rot.get(interval_min, 0) % max(len(lista), 1)
        kruzna = lista[start:] + lista[:start]
        bez = [t for t in kruzna if (t, interval_min) not in self.svijece]
        sa = [t for t in kruzna if (t, interval_min) in self.svijece]
        red = (bez + sa)[:max_poziva]
        self.rot[interval_min] = (start + max_poziva) % max(len(lista), 1)
        for t in red:
            j = http_json(f"https://api.kraken.com/0/public/OHLC?pair={self.parovi[t]}&interval={interval_min}")
            if not j or "result" not in j:
                greske += 1
                continue
            kljuc = [k for k in j["result"] if k != "last"]
            if not kljuc:
                greske += 1
                continue
            sv = [{"time": s[0], "open": float(s[1]), "high": float(s[2]),
                   "low": float(s[3]), "close": float(s[4])} for s in j["result"][kljuc[0]]]
            self.svijece[(t, interval_min)] = sv
            if sv:
                self.cijene[t] = sv[-1]["close"]
            time.sleep(0.3)
        return greske

    def zatvorene(self, tok, interval_min):
        sv = self.svijece.get((tok, interval_min), [])
        return sv[:-1] if len(sv) > 1 else sv

# ---------- motor ----------

class Motor:
    def __init__(self):
        self.params = self._ucitaj_params()
        tokeni = json.load(open(TOKENI_PATH, encoding="utf-8"))["tokeni"]
        self.trziste = Trziste(tokeni)
        self.state = self._ucitaj_state()
        self.zadnji_funding = time.time()

    def _ucitaj_params(self):
        return json.load(open(PARAMS_PATH, encoding="utf-8"))

    def _ucitaj_state(self):
        if os.path.exists(STATE_PATH):
            try:
                s = json.load(open(STATE_PATH, encoding="utf-8"))
                if s.get("verzija") == VERZIJA and "knjige" in s:
                    log("stanje ucitano — nastavljam gdje sam stao")
                    return s
                log("stanje stare ere pronadjeno — NOVA ERA krece svjeze (staro arhivirano)")
                os.replace(STATE_PATH, STATE_PATH + ".stara-era")
            except Exception as e:
                log(f"stanje neispravno ({e}) — krecem svjeze")
        return {
            "verzija": VERZIJA,
            "start": ts(),
            "knjige": {
                "dnevni": nova_knjiga(self.params["dnevni"]["sredstva_u_prometu"]),
                "tjedni": nova_knjiga(self.params["tjedni"]["sredstva_u_prometu"]),
            },
            "more": {"sirina": None, "stanje": "?", "prosla_sirina": None},
            "zadnja_runda": None,
        }

    def spremi(self):
        self.state["zadnja_runda"] = ts()
        atomski_zapis(STATE_PATH, self.state)

    # ---------- filter plime ----------

    def izracunaj_more(self):
        zeleni, ukupno = 0, 0
        for t in self.trziste.parovi:
            sv = self.trziste.zatvorene(t, 1440)
            if len(sv) < 25:
                continue
            z = [s["close"] for s in sv]
            e = ema_niz(z, 21)
            ukupno += 1
            if z[-1] > e[-1]:
                zeleni += 1
        if ukupno == 0:
            return
        sirina = round(zeleni / ukupno * 100.0)
        m = self.state["more"]
        m["prosla_sirina"] = m.get("sirina")
        m["sirina"] = sirina
        if sirina > 60:
            m["stanje"] = "PLIMA"
        elif sirina < 30:
            m["stanje"] = "OSEKA"
        else:
            m["stanje"] = "MIJESANO"

    def more_dopusta(self, smjer):
        m = self.state["more"]
        st = m.get("stanje", "?")
        if st == "PLIMA" and smjer == "SHORT":
            return False, "plima — kasno za short"
        if st == "OSEKA":
            if smjer == "SHORT":
                return False, "oseka — kasno za short"
            pros = m.get("prosla_sirina")
            if pros is None or m["sirina"] < pros + self.params.get("plima_potvrda_bodova", 10):
                return False, "oseka — cekam potvrdnu zelenu"
        return True, ""

    # ---------- knjizenje ----------

    def _naknada(self, iznos):
        return iznos * self.params["naknade"]["taker_pct"] / 100.0

    def _otvori(self, ime_k, k, p, tok, smjer, cijena):
        iznos = p["ulaz_usd"]
        if k["kasa"] - self._angazirano(k) < iznos:
            return
        nakn = self._naknada(iznos)
        k["pozicije"][tok] = {
            "smjer": smjer, "prvi_ulaz": cijena, "prosjek": cijena,
            "ulozeno": iznos, "kolicina": iznos / cijena,
            "dokupa": 0, "naknade_usd": round(nakn, 4), "funding_usd": 0.0,
            "vrijeme_ulaza": ts(), "ulaz_ts": time.time(),
            "zadnja_stepenica": cijena,
            "stop_pct": -p["stop_loss_pct"],
            "lokot": False, "lokot_ts": None,
            "najbolji_pl": 0.0, "najbolji_neto": 0.0,
        }
        log(f"[{ime_k}] ULAZ {smjer} {tok} po {cijena:g}, ulog {iznos:.0f}$")

    def _dokupi(self, ime_k, k, p, tok, poz, cijena):
        iznos = p["dokup"]["iznos_usd"]
        if k["kasa"] - self._angazirano(k) < iznos:
            return
        nakn = self._naknada(iznos)
        nova_kol = iznos / cijena
        poz["prosjek"] = (poz["prosjek"] * poz["kolicina"] + cijena * nova_kol) / (poz["kolicina"] + nova_kol)
        poz["kolicina"] += nova_kol
        poz["ulozeno"] += iznos
        poz["naknade_usd"] = round(poz["naknade_usd"] + nakn, 4)
        poz["dokupa"] += 1
        poz["zadnja_stepenica"] = cijena
        poz["lokot"] = False          # nova karta za platiti — lokot se ponovno zaradjuje
        poz["lokot_ts"] = None
        log(f"[{ime_k}] DOKUP#{poz['dokupa']} {tok} po {cijena:g} +{iznos:.0f}$ → prosjek {poz['prosjek']:g}")

    def _zatvori(self, ime_k, k, p, tok, poz, cijena, tko):
        vrijednost = poz["kolicina"] * cijena
        izlazna = self._naknada(vrijednost)
        if poz["smjer"] == "LONG":
            bruto = vrijednost - poz["ulozeno"]
        else:
            bruto = (poz["prosjek"] - cijena) * poz["kolicina"]
        neto = bruto - poz["naknade_usd"] - poz["funding_usd"] - izlazna
        k["broj_trejdova"] += 1
        if neto >= 0:
            k["dobitni"] += 1
        else:
            k["gubitni"] += 1
        k["realizirano_period"] = round(k["realizirano_period"] + neto, 2)
        if p.get("reinvestiraj"):
            k["kasa"] = round(k["kasa"] + neto, 2)
        else:
            k["sef"] = round(k["sef"] + neto, 2)
        min_plijen = p.get("opruga", {}).get("min_plijen_usd", 1.0)
        if neto < min_plijen and tko in ("LOKOT-IZLAZ", "TAJMER"):
            k["zadnji_izlaz_signal"].pop(tok, None)   # trzaj/nula — napadni opet na istom signalu
        else:
            k["zadnji_izlaz_signal"][tok] = poz["smjer"]
        del k["pozicije"][tok]
        dnevnik_zapis({
            "vrijeme": ts(), "knjiga": ime_k, "token": tok, "smjer": poz["smjer"],
            "ulaz_po": round(poz["prosjek"], 8), "izlaz_po": round(cijena, 8),
            "ulozeno_usd": round(poz["ulozeno"], 2), "dokupa": poz["dokupa"],
            "naknade_usd": round(poz["naknade_usd"] + izlazna, 4),
            "funding_usd": round(poz["funding_usd"], 4),
            "neto_usd": round(neto, 2), "zatvorio": tko,
            "sef": k["sef"], "kasa": k["kasa"],
        })
        log(f"[{ime_k}] {tko} {tok} {poz['smjer']} po {cijena:g} → neto {neto:+.2f}$ | SEF {k['sef']:+.2f}$ | kasa {k['kasa']:.0f}$")

    def _angazirano(self, k):
        return sum(x["ulozeno"] for x in k["pozicije"].values())

    # ---------- funding (svakih 8h, konzervativno placamo) ----------

    def _funding(self):
        if time.time() - self.zadnji_funding < 8 * 3600:
            return
        stopa = self.params["naknade"].get("funding_8h_pct", 0.01) / 100.0
        for ime_k, k in self.state["knjige"].items():
            for tok, poz in k["pozicije"].items():
                poz["funding_usd"] = round(poz["funding_usd"] + poz["ulozeno"] * stopa, 4)
        self.zadnji_funding = time.time()
        log("funding obracunat (8h)")

    # ---------- izlazni lanac: OPRUGA ----------

    def _izlazni_lanac(self, ime_k, k, p, tok, poz, cijena, rsi_v, val_pct):
        smjer = poz["smjer"]
        prosjek = poz["prosjek"]
        pl = ((cijena / prosjek - 1.0) if smjer == "LONG" else (prosjek / cijena - 1.0)) * 100.0
        poz["najbolji_pl"] = max(poz["najbolji_pl"], pl)
        izlazna = self._naknada(poz["kolicina"] * cijena)
        trosak = poz["naknade_usd"] + poz["funding_usd"] + izlazna
        neto = poz["ulozeno"] * pl / 100.0 - trosak
        poz["neto_zivo"] = round(neto, 2)
        poz["najbolji_neto"] = max(poz.get("najbolji_neto", 0.0), neto)

        # 1) STOP (minus nema tajmer — cuva ga samo cijena)
        if pl <= poz["stop_pct"]:
            tko = "LOKOT-IZLAZ" if poz["lokot"] and poz["stop_pct"] >= 0 else "STOP"
            self._zatvori(ime_k, k, p, tok, poz, cijena, tko)
            return

        o = p.get("opruga", {})
        if not o.get("upaljen", True):
            return

        # 2) LOKOT: ulaz + izlaz placeni → izlaz siguran bez ogrebotine
        if not poz["lokot"] and neto > 0:
            poz["lokot"] = True
            poz["lokot_ts"] = time.time()
            be_pct = trosak / poz["ulozeno"] * 100.0
            poz["stop_pct"] = max(poz["stop_pct"], be_pct)
            log(f"[{ime_k}] LOKOT {tok}: karte placene, izlaz osiguran — tajmer {o.get('tajmer_h', 48)}h")

        if poz["lokot"]:
            # 3) OPRUGA: razmak po netu (ljestvica) x gorivo
            razmak = 1.2
            for prag, r in o.get("ljestvica", [[1, 1.2], [3, 0.8], [8, 0.5], [999999, 0.3]]):
                if neto <= prag:
                    razmak = r
                    break
            g = gorivo_vala(smjer, rsi_v, pl, val_pct)
            poz["gorivo"] = g
            if g < o.get("gorivo_stegni_ispod", 40):
                razmak *= 0.5
            elif g > o.get("gorivo_pusti_iznad", 70):
                razmak *= 1.5
            poz["opruga_razmak"] = round(razmak, 2)
            min_plijen = o.get("min_plijen_usd", 1.0)
            if neto >= min_plijen and pl <= poz["najbolji_pl"] - razmak:
                self._zatvori(ime_k, k, p, tok, poz, cijena, "OPRUGA")
                return
            # 4) TAJMER od lokota: koliko je — toliko je, kuci sa zelenim
            sati = (time.time() - (poz["lokot_ts"] or time.time())) / 3600.0
            poz["tajmer_h"] = round(sati, 1)
            if sati >= o.get("tajmer_h", 48):
                self._zatvori(ime_k, k, p, tok, poz, cijena, "TAJMER")
                return

        # 5) DOKUP: fiksnih +200$, do max, samo u minusu
        d = p["dokup"]
        if d["upaljen"] and poz["dokupa"] < d["max_dokupa"] and pl < 0:
            step = poz["zadnja_stepenica"]
            if smjer == "LONG":
                pogodjena = cijena <= step * (1 - d["razmak_pct"] / 100.0)
            else:
                pogodjena = cijena >= step * (1 + d["razmak_pct"] / 100.0)
            if pogodjena:
                self._dokupi(ime_k, k, p, tok, poz, cijena)

    # ---------- ulazi ----------

    def _trazi_ulaze(self, ime_k, k, p, interval):
        if not p["bot_radi"] or p.get("pauza_novih_ulaza"):
            return
        if len(k["pozicije"]) >= p["max_pozicija"]:
            return
        for tok, cijena in self.trziste.cijene.items():
            if tok in k["pozicije"]:
                continue
            if k["kasa"] - self._angazirano(k) < p["ulaz_usd"]:
                break
            svijece = self.trziste.zatvorene(tok, interval)
            sig, rsi_v = signal_tokena(svijece, p["ema_brzi"], p["ema_spori"],
                                       p["rsi_gornji"], p["rsi_donji"])
            if not sig:
                continue
            if sig == "SHORT" and not p.get("short_dozvoljen", True):
                continue
            ok, razlog = self.more_dopusta(sig)
            if not ok:
                continue
            if k["zadnji_izlaz_signal"].get(tok) == sig:
                continue
            k["zadnji_izlaz_signal"].pop(tok, None)
            if len(k["pozicije"]) >= p["max_pozicija"]:
                return
            self._otvori(ime_k, k, p, tok, sig, cijena)

    # ---------- runda ----------

    def runda(self):
        self.params = self._ucitaj_params()
        a = self.trziste.osvjezi_svijece(self.params["dnevni"]["svijece_interval_min"], 6)
        b = self.trziste.osvjezi_svijece(1440, 4)
        self.izracunaj_more()
        self._funding()
        for ime_k, k in self.state["knjige"].items():
            p = self.params[ime_k]
            interval = p["svijece_interval_min"]
            for tok in list(k["pozicije"].keys()):
                if tok not in self.trziste.cijene:
                    continue
                cijena = self.trziste.cijene[tok]
                poz = k["pozicije"][tok]
                sv = self.trziste.zatvorene(tok, interval)
                _, rsi_v = signal_tokena(sv, p["ema_brzi"], p["ema_spori"],
                                         p["rsi_gornji"], p["rsi_donji"])
                val_pct = prosjecni_raspon_pct(sv, 14)
                self._izlazni_lanac(ime_k, k, p, tok, poz, cijena, rsi_v, val_pct)
            self._trazi_ulaze(ime_k, k, p, interval)
        self.spremi()
        m = self.state["more"]
        for ime_k, k in self.state["knjige"].items():
            drzi = len(k["pozicije"])
            ang = self._angazirano(k)
            log(f"[{ime_k}] pozicija {drzi} ({ang:.0f}$) | kasa {k['kasa']:.0f}$ | trejdova {k['broj_trejdova']} "
                f"| SEF {k['sef']:+.2f}$ | period {k['realizirano_period']:+.2f}$ | more {m.get('stanje','?')} {m.get('sirina','?')}%")

    def vrti(self):
        log(f"VALOVI MOST {VERZIJA} start — NOVA ERA: futures paper, USD, "
            f"{len(self.trziste.parovi)} tokena, dvije knjige (dnevni {self.params['dnevni']['sredstva_u_prometu']}$ "
            f"+ tjedni {self.params['tjedni']['sredstva_u_prometu']}$)")
        self.trziste.rijesi_parove()
        log("ucitavam svijece za sve tokene (prvi krug traje par minuta)...")
        for _ in range(12):
            a = self.trziste.osvjezi_svijece(self.params["dnevni"]["svijece_interval_min"], 6)
            b = self.trziste.osvjezi_svijece(1440, 4)
            if len(self.trziste.svijece) >= len(self.trziste.parovi) * 2:
                break
        cik = int(self.params.get("ciklus_sekundi", 60))
        while True:
            try:
                self.runda()
            except Exception as e:
                log(f"GRESKA u rundi: {e} — nastavljam za {cik}s")
            time.sleep(cik)

if __name__ == "__main__":
    Motor().vrti()
