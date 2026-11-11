#!/usr/bin/env python3
"""Kursdata fra Pi'en til et GitHub-repo, så Claude kan læse det.

To tilstande:
  python kurser.py          Overvågningsliste -> kurser.json   (cron, hvert 15. min på hverdage)
  python kurser.py univers  S&P 500 + Nasdaq-100 -> univers.json (cron, én gang om dagen)

Overvågningslisten læses fra tickers.txt i repoet (én ticker pr. linje, # = kommentar),
så den kan rettes fra GitHub uden at røre Pi'en. Filerne indeholder kun markedsdata,
ingen beholdning eller købskurser.

Kræver ~/.kurser.env:
    GITHUB_TOKEN=github_pat_...
    GITHUB_REPO=ditbrugernavn/kurser
Kræver: pip install yfinance requests pandas lxml
"""
import base64
import json
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

STANDARD = ["ASND", "NU", "SGI", "ZTS", "GOOGL", "ISRG"]  # bruges hvis tickers.txt mangler
FASTE = ["^GSPC", "^IXIC", "DKK=X"]  # indeks og dollarkurs er altid med
ENV_FIL = Path.home() / ".kurser.env"
UA = {"User-Agent": "kurser-pi/1.0 (privat hobbyprojekt)"}


def laes_env():
    cfg = {}
    for linje in ENV_FIL.read_text().splitlines():
        linje = linje.strip()
        if linje and not linje.startswith("#") and "=" in linje:
            k, v = linje.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg["GITHUB_TOKEN"], cfg["GITHUB_REPO"]


class Repo:
    def __init__(self, token, repo):
        self.base = f"https://api.github.com/repos/{repo}/contents/"
        self.h = {"Authorization": f"Bearer {token}",
                  "Accept": "application/vnd.github+json"}

    def hent(self, sti):
        """Returnerer (tekst, sha) eller (None, None) hvis filen ikke findes."""
        r = requests.get(self.base + sti, headers=self.h, timeout=30)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        d = r.json()
        return base64.b64decode(d["content"]).decode(), d["sha"]

    def skriv(self, sti, tekst, sha, besked):
        body = {"message": besked,
                "content": base64.b64encode(tekst.encode()).decode()}
        if sha:
            body["sha"] = sha
        r = requests.put(self.base + sti, headers=self.h, json=body, timeout=60)
        r.raise_for_status()


def pct(a, b):
    return round((a / b - 1) * 100, 2)


def laes_tickers(repo):
    tekst, _ = repo.hent("tickers.txt")
    if tekst is None:
        return STANDARD
    ud = []
    for linje in tekst.splitlines():
        t = linje.split("#")[0].strip().upper()
        if t:
            ud.append(t)
    return ud or STANDARD


def overvaagning(repo):
    tickers = list(dict.fromkeys(laes_tickers(repo) + FASTE))
    kurser = {}
    for sym in tickers:
        try:
            fi = yf.Ticker(sym).fast_info
            pris, forrige = fi["last_price"], fi["previous_close"]
            kurser[sym] = {
                "kurs": round(pris, 2),
                "forrige_luk": round(forrige, 2),
                "i_dag_pct": pct(pris, forrige),
                "52u_hoej": round(fi["year_high"], 2),
                "52u_lav": round(fi["year_low"], 2),
            }
        except Exception as e:  # én ticker må ikke vælte resten
            kurser[sym] = {"fejl": str(e)[:120]}
    if all("fejl" in v for v in kurser.values()):
        sys.exit("Ingen kurser hentet - springer upload over")

    gammel, sha = repo.hent("kurser.json")
    if gammel:
        try:
            if json.loads(gammel).get("kurser") == kurser:
                return  # intet nyt (markedet lukket)
        except ValueError:
            pass
    nu = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo.skriv("kurser.json",
               json.dumps({"opdateret_utc": nu, "kurser": kurser}, indent=1),
               sha, f"kurser {nu}")


def wiki_liste(url, ticker_navne):
    html = requests.get(url, headers=UA, timeout=30).text
    for tabel in pd.read_html(StringIO(html)):
        if isinstance(tabel.columns, pd.MultiIndex):  # flad ud hvis flere header-rækker
            tabel.columns = [" ".join(str(x) for x in c if "Unnamed" not in str(x)).strip()
                             for c in tabel.columns]
        tk = next((k for k in ticker_navne if k in tabel.columns), None)
        if tk is None or len(tabel) < 50:  # spring små tabeller over
            continue
        sektor = next((k for k in ("GICS Sector", "ICB Industry", "Sector")
                       if k in tabel.columns), None)
        navn = next((k for k in ("Security", "Company", "Name") if k in tabel.columns), tk)
        return {str(r[tk]).strip().replace(".", "-"):
                {"navn": r[navn], "sektor": r[sektor] if sektor else ""}
                for _, r in tabel.iterrows()}
    raise RuntimeError(f"Ingen passende tabel ({ticker_navne}) på {url}")


def univers(repo):
    liste = wiki_liste("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                       ("Symbol", "Ticker"))
    try:
        liste.update(wiki_liste("https://en.wikipedia.org/wiki/Nasdaq-100",
                                ("Ticker", "Symbol")))
    except Exception as e:  # Nasdaq-100 er et tillæg, S&P 500 er nok til at køre videre
        print(f"Advarsel: Nasdaq-100 sprunget over ({e})", file=sys.stderr)
    syms = sorted(liste)

    rækker = []
    for i in range(0, len(syms), 100):
        gruppe = syms[i:i + 100]
        df = yf.download(gruppe, period="1y", interval="1d", group_by="ticker",
                         auto_adjust=True, threads=True, progress=False)
        for s in gruppe:
            try:
                c = df[s]["Close"].dropna()
                if len(c) < 64:
                    continue
                k = c.iloc[-1]
                rækker.append({
                    "ticker": s, **liste[s],
                    "kurs": round(float(k), 2),
                    "d1_pct": pct(k, c.iloc[-2]),
                    "m1_pct": pct(k, c.iloc[-22]),
                    "m3_pct": pct(k, c.iloc[-64]),
                    "fra_52u_hoej_pct": pct(k, c.max()),
                })
            except Exception:
                continue
    if len(rækker) < 100:
        sys.exit(f"Kun {len(rækker)} aktier hentet - springer upload over")

    nu = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, sha = repo.hent("univers.json")
    repo.skriv("univers.json",
               json.dumps({"opdateret_utc": nu, "aktier": rækker}, separators=(",", ":")),
               sha, f"univers {nu}")


def main():
    repo = Repo(*laes_env())
    if len(sys.argv) > 1 and sys.argv[1] == "univers":
        univers(repo)
    else:
        overvaagning(repo)


if __name__ == "__main__":
    main()
