#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VALOVI MOST — motor, Faza 1 (paper)
Jedan proces, dvije knjige: DNEVNI (satne svijece) i TJEDNI (dnevne svijece).
Kraken javne cijene. Nista se ne trguje pravim parama — motor simulira,
knjizi istinu (naknade, rollover) i pise dnevnik za buducu plocu.

Zakoni ugradjeni:
- prosjek je jedina istina pozicije (dokup vuce prosjek, sve mjere od prosjeka)
- izlazni lanac: stop -> zakljucaj -> (zona: trailing/gorivo/domet) -> zetva/grizanje
- osiguraci: limit gubitka perioda (auto-pauza), max istog smjera, short izlozenost
- knjige ne diraju jedna drugu; svaka ima svoja sredstva i svoj SEF
- atomsko pisanje stanja; svaka runda u try/except; parametri se citaju na pocetku runde
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = "/opt/valovi/data"
STATE_PATH = os.path.join(DATA, "most_state.json")
DNEVNIK_PATH = os.path.join(DATA, "most_dnevnik.jsonl")
PARAMS_PATH = os.path.join(BASE, "params.json")
TOKENI_PATH = os.path.join(BASE, "tokeni.json")

KRAKEN = "https://api.kraken.com/0/public"
MAX_OHLC_PO_RUNDI = 8          # koliko tokena osvjezimo po rundi (rate limit prijateljski)
KANDIDAT_SVIJECA = 60          # koliko svijeca drzimo po tokenu

VERZIJA = "most-1.0"


# ============================================================ pomocno

def sada_utc():
    return datetime.now(timezone.utc)

def ts():
    return sada_utc().strftime("%d.%m. %H:%M UTC")

def log(msg):
    print(f"[{ts()}] {msg}", flush=True)

def http_json(url, pokusaji=3):
    zadnja = None
    for i in range(pokusaji):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"valovi-{VERZIJA}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.loads(r.read().decode())
            if j.get("error"):
                raise RuntimeError(f"Kraken error: {j['error']}")
            return j["result"]
        except Exception as e:
            zadnja = e
            time.sleep(1 + i)
    raise RuntimeError(f"HTTP neuspjeh {url}: {zadnja}")

def atomski_zapis(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

def dnevnik_zapis(z):
    os.makedirs(DATA, exist_ok=True)
    with open(DNEVNIK_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(z, ensure_ascii=False) + "\n")


# ============================================================ indikatori

def ema_niz(zatvaranja, n):
    if len(zatvaranja) < n:
        return []
    k = 2.0 / (n + 1)
    e = sum(zatvaranja[:n]) / n
    out = [e]
    for c in zatvaranja[n:]:
        e = c * k + e * (1 - k)
        out.append(e)
    return out

def rsi(zatvaranja, n=14):
    if len(zatvaranja) < n + 1:
        return None
    dob, gub = [], []
    for i in range(1, len(zatvaranja)):
        d = zatvaranja[i] - zatvaranja[i - 1]
        dob.append(max(d, 0.0))
        gub.append(max(-d, 0.0))
    pd = sum(dob[:n]) / n
    pg = sum(gub[:n]) / n
    for i in range(n, len(dob)):
        pd = (pd * (n - 1) + dob[i]) / n
        pg = (pg * (n - 1) + gub[i]) / n
    if pg == 0:
        return 100.0
    rs = pd / pg
    return 100.0 - 100.0 / (1.0 + rs)

def prosjecni_raspon_pct(svijece, n=14):
    """Prosjecni (high-low)/close raspon zadnjih n svijeca, u %."""
    if len(svijece) < n:
        return None
    dio = svijece[-n:]
    r = [ (s["high"] - s["low"]) / s["close"] * 100.0 for s in dio if s["close"] > 0 ]
    return sum(r) / len(r) if r else None


# ============================================================ kraken podaci

class Trziste:
    """Cijene i svijece s Krakena, s kesiranjem i postivanjem rate limita."""

    def __init__(self, tokeni, valuta):
        self.valuta = valuta
        self.parovi = {}          # token -> kraken par (npr. "XXBTZEUR")
        self.svijece = {}         # (token, interval) -> {"kad": epoch, "lista": [...]}
        self._rjesi_parove(tokeni)

    def _rjesi_parove(self, tokeni):
        rez = http_json(f"{KRAKEN}/AssetPairs")
        for t in tokeni:
            nadjen = None
            for ime, info in rez.items():
                ws = info.get("wsname", "")   # npr. "SOL/EUR"
                if ws == f"{t}/{self.valuta}" or ws == f"X{t}/{self.valuta}":
                    nadjen = ime
                    break
            # BTC je na Krakenu XBT
            if not nadjen and t == "BTC":
                for ime, info in rez.items():
                    if info.get("wsname") == f"XBT/{self.valuta}":
                        nadjen = ime
                        break
            if nadjen:
                self.parovi[t] = nadjen
            else:
                log(f"POZOR: {t}/{self.valuta} ne postoji na Krakenu — preskacem token")
        log(f"parovi rijeseni: {len(self.parovi)}/{len(tokeni)} tokena aktivno")

    def cijene(self):
        """Sve cijene jednim pozivom."""
        if not self.parovi:
            return {}
        upit = ",".join(self.parovi.values())
        rez = http_json(f"{KRAKEN}/Ticker?pair={upit}")
        out = {}
        obrnuto = {v: k for k, v in self.parovi.items()}
        for ime, info in rez.items():
            tok = obrnuto.get(ime)
            if tok:
                out[tok] = float(info["c"][0])
        return out

    def osvjezi_svijece(self, interval_min, max_poziva):
        """Osvjezi svijece za tokene ciji je kes zastario; najvise max_poziva poziva."""
        poziva = 0
        sad = time.time()
        for tok, par in self.parovi.items():
            if poziva >= max_poziva:
                break
            k = (tok, interval_min)
            kes = self.svijece.get(k)
            if kes and sad - kes["kad"] < interval_min * 60 * 0.5:
                continue
            try:
                rez = http_json(f"{KRAKEN}/OHLC?pair={par}&interval={interval_min}")
                kljuc = next(x for x in rez if x != "last")
                lista = []
                for s in rez[kljuc][-(KANDIDAT_SVIJECA + 1):]:
                    lista.append({
                        "t": int(s[0]), "open": float(s[1]), "high": float(s[2]),
                        "low": float(s[3]), "close": float(s[4]), "vol": float(s[6]),
                    })
                if lista:
                    lista = lista[:-1]  # zadnja jos traje — samo zatvorene
                self.svijece[k] = {"kad": sad, "lista": lista}
                poziva += 1
                time.sleep(0.4)
            except Exception as e:
                log(f"svijece {tok}/{interval_min}m greska: {e}")
                poziva += 1
        return poziva

    def zatvorene(self, tok, interval_min):
        k = self.svijece.get((tok, interval_min))
        return k["lista"] if k else []


# ============================================================ analiza tokena

def signal_tokena(svijece, ema_b, ema_s, rsi_g, rsi_d):
    """Vrati 'LONG', 'SHORT' ili None + RSI vrijednost."""
    zat = [s["close"] for s in svijece]
    if len(zat) < max(ema_s + 2, 16):
        return None, None
    eb = ema_niz(zat, ema_b)
    es = ema_niz(zat, ema_s)
    r = rsi(zat, 14)
    if not eb or not es or r is None:
        return None, None
    if eb[-1] > es[-1]:
        return ("LONG" if r < rsi_g else None), r
    if eb[-1] < es[-1]:
        return ("SHORT" if r > rsi_d else None), r
    return None, r

def tjedni_rezim(dnevne_svijece, ema_b=9, ema_s=21):
    """BOCNO / TREND_GORE / TREND_DOLJE + koliko dana u smjeru."""
    zat = [s["close"] for s in dnevne_svijece]
    if len(zat) < ema_s + 3:
        return "BOCNO", 0
    eb = ema_niz(zat, ema_b)
    es = ema_niz(zat, ema_s)
    n = min(len(eb), len(es))
    eb, es = eb[-n:], es[-n:]
    smjer = []
    for i in range(n):
        if eb[i] > es[i] * 1.002:
            smjer.append(1)
        elif eb[i] < es[i] * 0.998:
            smjer.append(-1)
        else:
            smjer.append(0)
    zadnji = smjer[-1]
    if zadnji == 0:
        return "BOCNO", 0
    dana = 0
    for s in reversed(smjer):
        if s == zadnji:
            dana += 1
        else:
            break
    return ("TREND_GORE" if zadnji == 1 else "TREND_DOLJE"), dana

def gorivo_vala(smjer, rsi_v, pl_pct, val_pct, svijece, rsi_g, rsi_d):
    """0-100: koliko daha val jos ima. RSI prostor + pojedeni val + snaga svijeca."""
    if rsi_v is None or not val_pct:
        return 50
    # 1) RSI prostor do pregrijanja (za LONG) / rasprodanosti (za SHORT)
    if smjer == "LONG":
        prostor = (rsi_g - rsi_v) / max(rsi_g - 50.0, 1.0)
    else:
        prostor = (rsi_v - rsi_d) / max(50.0 - rsi_d, 1.0)
    prostor = max(0.0, min(1.0, prostor))
    # 2) koliki je dio prosjecnog vala vec pojeden
    pojedeno = max(0.0, min(1.5, max(pl_pct, 0.0) / val_pct))
    svjezina = max(0.0, 1.0 - pojedeno)
    # 3) snaga zadnje 3 svijece u smjeru + volumen vs prosjek 20
    snaga = 0.5
    if len(svijece) >= 21:
        tri = svijece[-3:]
        u_smjeru = sum(
            1 for s in tri
            if (s["close"] > s["open"]) == (smjer == "LONG")
        ) / 3.0
        pv = sum(s["vol"] for s in svijece[-21:-1]) / 20.0
        vol_omjer = min(2.0, svijece[-1]["vol"] / pv) / 2.0 if pv > 0 else 0.5
        snaga = 0.6 * u_smjeru + 0.4 * vol_omjer
    g = 100.0 * (0.40 * prostor + 0.35 * svjezina + 0.25 * snaga)
    return int(round(max(0.0, min(100.0, g))))


# ============================================================ knjiga

def nova_knjiga():
    return {
        "pozicije": {},          # token -> pozicija
        "sef": 0.0,
        "realizirano_period": 0.0,
        "period_od": None,
        "pauza_do": None,        # ISO vrijeme ili None (osigurac)
        "broj_trejdova": 0, "dobitni": 0, "gubitni": 0,
        "zadnji_izlaz_signal": {},   # token -> signal pri zadnjem izlazu (protiv vrtnje)
    }

def pocetak_perioda(ime_knjige, t=None):
    t = t or sada_utc()
    if ime_knjige == "dnevni":
        return t.replace(hour=0, minute=0, second=0, microsecond=0)
    # tjedni: ponedjeljak 00:00
    p = t - timedelta(days=t.weekday())
    return p.replace(hour=0, minute=0, second=0, microsecond=0)

def kraj_perioda(ime_knjige, t=None):
    t = t or sada_utc()
    if ime_knjige == "dnevni":
        return pocetak_perioda(ime_knjige, t) + timedelta(days=1)
    return pocetak_perioda(ime_knjige, t) + timedelta(days=7)


class Motor:
    def __init__(self):
        self.params = self._ucitaj_params()
        tokeni = json.load(open(TOKENI_PATH, encoding="utf-8"))["tokeni"]
        self.trziste = Trziste(tokeni, self.params["valuta"])
        self.state = self._ucitaj_state()
        self.zadnji_rollover = time.time()

    # ---------- stanje ----------

    def _ucitaj_params(self):
        return json.load(open(PARAMS_PATH, encoding="utf-8"))

    def _ucitaj_state(self):
        if os.path.exists(STATE_PATH):
            try:
                s = json.load(open(STATE_PATH, encoding="utf-8"))
                if "knjige" in s:
                    log("stanje ucitano — nastavljam gdje sam stao")
                    return s
            except Exception as e:
                log(f"stanje neispravno ({e}) — krecem svjeze")
        return {
            "verzija": VERZIJA,
            "start": ts(),
            "knjige": {"dnevni": nova_knjiga(), "tjedni": nova_knjiga()},
            "zadnja_runda": None,
        }

    def spremi(self):
        self.state["zadnja_runda"] = ts()
        atomski_zapis(STATE_PATH, self.state)

    # ---------- knjizenje ----------

    def _zatvori(self, ime_k, k, p, tok, poz, cijena, tko, naknada_pct):
        smjer = poz["smjer"]
        ulozeno = poz["ulozeno"]
        prosjek = poz["prosjek"]
        if smjer == "LONG":
            bruto_pct = (cijena / prosjek - 1.0) * 100.0
        else:
            bruto_pct = (prosjek / cijena - 1.0) * 100.0
        ulazne = poz["naknade_eur"]
        izlazna = ulozeno * naknada_pct / 100.0
        rollover = poz.get("rollover_eur", 0.0)
        pl = ulozeno * bruto_pct / 100.0 - ulazne - izlazna - rollover
        k["sef"] += pl
        k["realizirano_period"] += pl
        k["broj_trejdova"] += 1
        if pl >= 0: k["dobitni"] += 1
        else: k["gubitni"] += 1
        k["zadnji_izlaz_signal"][tok] = smjer
        saldo = p["sredstva_u_prometu"] + k["sef"]
        dnevnik_zapis({
            "portfelj": ime_k, "token": tok, "smjer": smjer,
            "kupljeno": poz["vrijeme_ulaza"], "kupljeno_po": round(poz["prvi_ulaz"], 8),
            "prosjek": round(prosjek, 8), "dokupa": poz["dokupa"],
            "prodano": ts(), "prodano_po": round(cijena, 8),
            "ulog_eur": round(ulozeno, 2),
            "naknade_eur": round(ulazne + izlazna + rollover, 2),
            "pl_eur": round(pl, 2), "zatvorio": tko,
            "saldo_eur": round(saldo, 2),
        })
        log(f"[{ime_k}] {tko} {tok} {smjer} po {cijena:.6g} "
            f"(prosjek {prosjek:.6g}, {poz['dokupa']}x dokup) "
            f"P/L {pl:+.2f}€ | SEF {k['sef']:+.2f}€")
        del k["pozicije"][tok]

    def _otvori(self, ime_k, k, p, tok, smjer, cijena, iznos):
        naknada = iznos * self.params["naknade"]["maker_pct"] / 100.0
        k["pozicije"][tok] = {
            "smjer": smjer, "prvi_ulaz": cijena, "prosjek": cijena,
            "ulozeno": iznos, "kolicina": iznos / cijena,
            "dokupa": 0, "naknade_eur": naknada, "rollover_eur": 0.0,
            "vrijeme_ulaza": ts(), "zadnja_stepenica": cijena,
            "stop_pct": -abs(p["stop_loss_pct"]),
            "zakljucano": False, "u_zoni": False, "najbolji_pl": 0.0,
            "zetva_pct": None,   # None = opca; ploca kasnije pise TOMO zetvu ovdje
        }
        log(f"[{ime_k}] ULAZ {smjer} {tok} po {cijena:.6g}, ulog {iznos:.0f}€")

    def _dokupi(self, ime_k, k, p, tok, poz, cijena, iznos, rucno=False):
        naknada = iznos * self.params["naknade"]["maker_pct"] / 100.0
        nova_kol = iznos / cijena
        uk_kol = poz["kolicina"] + nova_kol
        poz["prosjek"] = (poz["prosjek"] * poz["kolicina"] + cijena * nova_kol) / uk_kol
        poz["kolicina"] = uk_kol
        poz["ulozeno"] += iznos
        poz["naknade_eur"] += naknada
        poz["dokupa"] += 1
        poz["zadnja_stepenica"] = cijena
        log(f"[{ime_k}] DOKUP#{poz['dokupa']}{' (rucno)' if rucno else ''} "
            f"{tok} po {cijena:.6g} +{iznos:.0f}€ → prosjek {poz['prosjek']:.6g}")

    # ---------- osiguraci ----------

    def _osiguraci_ok_za_ulaz(self, ime_k, k, p, smjer, iznos):
        os_ = p["osiguraci"]
        # limit gubitka perioda → pauza
        if k["pauza_do"]:
            if sada_utc().isoformat() < k["pauza_do"]:
                return False, "osigurac-pauza"
            k["pauza_do"] = None
            k["realizirano_period"] = 0.0
        limit = -os_["limit_gubitka_pct"] / 100.0 * p["sredstva_u_prometu"]
        if k["realizirano_period"] <= limit:
            k["pauza_do"] = kraj_perioda(ime_k).isoformat()
            log(f"[{ime_k}] 🔌 OSIGURAC PREGORIO: period P/L "
                f"{k['realizirano_period']:+.2f}€ ≤ {limit:.2f}€ — pauza do {k['pauza_do']}")
            return False, "osigurac-pregorio"
        # max istog smjera
        isti = sum(1 for x in k["pozicije"].values() if x["smjer"] == smjer)
        if isti >= os_["max_istog_smjera"]:
            return False, "max-smjer"
        # short izlozenost (paper simulacija margin jastuka)
        if smjer == "SHORT":
            izl = sum(x["ulozeno"] for x in k["pozicije"].values() if x["smjer"] == "SHORT")
            if izl + iznos > os_["max_short_izlozenost_pct"] / 100.0 * p["sredstva_u_prometu"]:
                return False, "short-izlozenost"
        return True, None

    def _period_reset(self, ime_k, k):
        poc = pocetak_perioda(ime_k).isoformat()
        if k["period_od"] != poc:
            k["period_od"] = poc
            k["realizirano_period"] = 0.0
            if k["pauza_do"] and sada_utc().isoformat() >= k["pauza_do"]:
                k["pauza_do"] = None
                log(f"[{ime_k}] osigurac se ohladio — novi period, lov nastavlja")

    # ---------- izlazni lanac ----------

    def _izlazni_lanac(self, ime_k, k, p, tok, poz, cijena, svijece, rsi_v, val_pct):
        smjer = poz["smjer"]
        prosjek = poz["prosjek"]
        pl = ((cijena / prosjek - 1.0) if smjer == "LONG" else (prosjek / cijena - 1.0)) * 100.0
        poz["najbolji_pl"] = max(poz["najbolji_pl"], pl)
        taker = self.params["naknade"]["taker_pct"]
        maker = self.params["naknade"]["maker_pct"]

        # 1) STOP (od prosjeka; zakljucan stop = 0 ili vise)
        if pl <= poz["stop_pct"]:
            tko = "🔒 ZAKLJUCAJ" if poz["zakljucano"] and poz["stop_pct"] >= 0 else "🛑 STOP"
            self._zatvori(ime_k, k, p, tok, poz, cijena, tko, taker)
            return

        # 2) ZAKLJUCAJ: na +X% stop skace na prosjek (nula)
        if not poz["zakljucano"] and pl >= p["zakljucaj_na_pct"]:
            poz["zakljucano"] = True
            poz["stop_pct"] = 0.0
            log(f"[{ime_k}] 🔒 {tok} zakljucan (P/L {pl:+.2f}%): stop na prosjeku")

        # zetveni cilj: TOMO zetva pozicije > pametna zetva > opca
        cilj = poz["zetva_pct"]
        if cilj is None:
            if p.get("pametna_zetva") and val_pct:
                cilj = max(2.0, p["pametna_zetva_faktor"] * val_pct)
            else:
                cilj = p["auto_zetva_pct"]
        domet = max(cilj * 1.2, (val_pct or cilj) )

        # 3) U ZONI (grizanje aktivno): trailing / gorivo / domet beru
        if poz["u_zoni"]:
            if pl <= poz["najbolji_pl"] - p["trailing_pct"]:
                self._zatvori(ime_k, k, p, tok, poz, cijena, "⛽ GRIZANJE-TRAILING", taker)
                return
            g = gorivo_vala(smjer, rsi_v, pl, val_pct, svijece,
                            p["rsi_gornji"], p["rsi_donji"])
            if g < p["grizi_dok_gorivo_iznad"]:
                self._zatvori(ime_k, k, p, tok, poz, cijena, "⛽ GRIZANJE-GORIVO", maker)
                return
            if pl >= domet:
                self._zatvori(ime_k, k, p, tok, poz, cijena, "⛽ GRIZANJE-DOMET", maker)
                return
            return

        # 4) ZETVA (ili ulaz u zonu ako grizanje upaljeno i gorivo zeleno)
        if cilj and cilj > 0 and pl >= cilj:
            if p.get("auto_grizanje"):
                g = gorivo_vala(smjer, rsi_v, pl, val_pct, svijece,
                                p["rsi_gornji"], p["rsi_donji"])
                if g >= p["grizi_dok_gorivo_iznad"]:
                    poz["u_zoni"] = True
                    poz["zakljucano"] = True
                    poz["stop_pct"] = max(poz["stop_pct"], cilj / 2.0)  # zarada zakljucana
                    log(f"[{ime_k}] ⛽ {tok} gorivo {g}% — grizem u zonu "
                        f"(stop dignut na +{poz['stop_pct']:.2f}%)")
                    return
            self._zatvori(ime_k, k, p, tok, poz, cijena, "🍇 BOT-ZETVA", maker)
            return

        # 5) DOKUP (bot, do max; potvrdna svijeca; short provjerava izlozenost)
        d = p["dokup"]
        if d["upaljen"] and poz["dokupa"] < d["max_dokupa"] and pl < 0:
            step = poz.get("zadnja_stepenica", poz["prvi_ulaz"])
            if smjer == "LONG":
                sljedeca = step * (1 - d["razmak_pct"] / 100.0)
                pogodjena = cijena <= sljedeca
            else:
                sljedeca = step * (1 + d["razmak_pct"] / 100.0)
                pogodjena = cijena >= sljedeca
            if pogodjena:
                if d["potvrdna_svijeca"] and svijece:
                    z = svijece[-1]
                    zelena = z["close"] > z["open"]
                    if (smjer == "LONG" and not zelena) or (smjer == "SHORT" and zelena):
                        return  # nozevi jos padaju — cekamo potvrdu
                osnovni = p["sredstva_u_prometu"] / p["max_pozicija"]
                iznos = osnovni * (d["mnozitelj"] ** (poz["dokupa"] + 1))
                if smjer == "SHORT":
                    izl = sum(x["ulozeno"] for x in k["pozicije"].values()
                              if x["smjer"] == "SHORT")
                    maxi = p["osiguraci"]["max_short_izlozenost_pct"] / 100.0 * p["sredstva_u_prometu"]
                    if izl + iznos > maxi:
                        return  # margin jastuk vazniji od stepenice
                angazirano = sum(x["ulozeno"] for x in k["pozicije"].values())
                if angazirano + iznos <= p["sredstva_u_prometu"]:
                    self._dokupi(ime_k, k, p, tok, poz, cijena, iznos)

    # ---------- ulazi ----------

    def _trazi_ulaze(self, ime_k, k, p, cijene, interval):
        if not p["bot_radi"] or p["pauza_novih_ulaza"]:
            return
        if len(k["pozicije"]) >= p["max_pozicija"]:
            return
        osnovni = p["sredstva_u_prometu"] / p["max_pozicija"]
        angazirano = sum(x["ulozeno"] for x in k["pozicije"].values())
        for tok, cijena in cijene.items():
            if tok in k["pozicije"]:
                continue
            if angazirano + osnovni > p["sredstva_u_prometu"]:
                break
            svijece = self.trziste.zatvorene(tok, interval)
            sig, rsi_v = signal_tokena(svijece, p["ema_brzi"], p["ema_spori"],
                                       p["rsi_gornji"], p["rsi_donji"])
            if not sig:
                continue
            if sig == "SHORT" and not p.get("short_dozvoljen", False):
                continue   
            # protiv vrtnje: ne ulazi ponovno u isti smjer iz kojeg smo tek izasli
            if k["zadnji_izlaz_signal"].get(tok) == sig:
                continue
            k["zadnji_izlaz_signal"].pop(tok, None)
            # tjedni: ulaz samo u trend tjedan u smjeru trenda
            if p.get("ulaz_samo_trend_tjedan"):
                dnevne = self.trziste.zatvorene(tok, 1440)
                rez, _ = tjedni_rezim(dnevne)
                if sig == "LONG" and rez != "TREND_GORE":
                    continue
                if sig == "SHORT" and rez != "TREND_DOLJE":
                    continue
            ok, razlog = self._osiguraci_ok_za_ulaz(ime_k, k, p, sig, osnovni)
            if not ok:
                if razlog in ("osigurac-pregorio",):
                    return
                continue
            if len(k["pozicije"]) >= p["max_pozicija"]:
                return
            self._otvori(ime_k, k, p, tok, sig, cijene[tok], osnovni)
            angazirano += osnovni

    # ---------- rollover za shortove ----------

    def _rollover(self):
        if time.time() - self.zadnji_rollover < 4 * 3600:
            return
        self.zadnji_rollover = time.time()
        pct = self.params["naknade"]["rollover_pct_4h"] / 100.0
        for ime_k, k in self.state["knjige"].items():
            for tok, poz in k["pozicije"].items():
                if poz["smjer"] == "SHORT":
                    naj = poz["ulozeno"] * pct
                    poz["rollover_eur"] = poz.get("rollover_eur", 0.0) + naj

    # ---------- glavna runda ----------

    def runda(self):
        self.params = self._ucitaj_params()   # parametri se citaju na pocetku runde
        cijene = self.trziste.cijene()
        if not cijene:
            log("nema cijena — preskacem rundu")
            return

        # svijece: dnevna knjiga (satne) + tjedna (dnevne), rate-limit prijateljski
        self.trziste.osvjezi_svijece(self.params["dnevni"]["svijece_interval_min"],
                                     MAX_OHLC_PO_RUNDI)
        self.trziste.osvjezi_svijece(1440, max(2, MAX_OHLC_PO_RUNDI // 2))

        self._rollover()

        for ime_k in ("dnevni", "tjedni"):
            p = self.params[ime_k]
            k = self.state["knjige"][ime_k]
            self._period_reset(ime_k, k)
            if not p["bot_radi"]:
                continue
            interval = p["svijece_interval_min"]

            # izlazni lanac za postojece pozicije
            for tok in list(k["pozicije"].keys()):
                if tok not in cijene:
                    continue
                poz = k["pozicije"][tok]
                svijece = self.trziste.zatvorene(tok, interval)
                _, rsi_v = signal_tokena(svijece, p["ema_brzi"], p["ema_spori"],
                                         p["rsi_gornji"], p["rsi_donji"])
                val_src = self.trziste.zatvorene(tok, 1440)
                if ime_k == "tjedni":
                    val_pct = None
                    if len(val_src) >= 28:
                        tj = []
                        for i in range(4):
                            blok = val_src[-(7 * (i + 1)):len(val_src) - 7 * i or None]
                            if blok:
                                hi = max(s["high"] for s in blok)
                                lo = min(s["low"] for s in blok)
                                tj.append((hi - lo) / blok[-1]["close"] * 100.0)
                        val_pct = sum(tj) / len(tj) if tj else None
                else:
                    val_pct = prosjecni_raspon_pct(val_src, 14)
                self._izlazni_lanac(ime_k, k, p, tok, poz, cijene[tok],
                                    svijece, rsi_v, val_pct)

            # novi ulazi
            self._trazi_ulaze(ime_k, k, p, cijene, interval)

        self.spremi()

        # sazetak
        for ime_k, k in self.state["knjige"].items():
            drzi = len(k["pozicije"])
            ang = sum(x["ulozeno"] for x in k["pozicije"].values())
            log(f"[{ime_k}] pozicija {drzi} ({ang:.0f}€) | trejdova {k['broj_trejdova']} "
                f"| SEF {k['sef']:+.2f}€ | period {k['realizirano_period']:+.2f}€"
                + (" | ⏸ OSIGURAC" if k["pauza_do"] else ""))

    def vrti(self):
        log(f"VALOVI MOST {VERZIJA} start — rezim {self.params['rezim']}, "
            f"{len(self.trziste.parovi)} tokena, dvije knjige (dnevni+tjedni)")
        if self.params["rezim"] != "paper":
            log("POZOR: Faza 1 podrzava samo paper — gasim se.")
            return
        # startni burst svijeca (strpljivo, rate-limit prijateljski)
        log("ucitavam svijece za sve tokene (prvi krug traje par minuta)...")
        for _ in range(10):
            a = self.trziste.osvjezi_svijece(self.params["dnevni"]["svijece_interval_min"], 6)
            b = self.trziste.osvjezi_svijece(1440, 4)
            if a == 0 and b == 0:
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
