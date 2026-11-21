#!/usr/bin/env python3
"""
Does the NSE delivery-percentage filter actually improve the 52W-low reversal
setup? Takes the signals already recorded in data/reversal_backtest.json,
pulls each symbol's delivery history around its signal date, computes
delivery% / 20-day-average-delivery%, and splits the outcomes by that ratio.

Primary source : NSE security-wise historical API (one call per symbol)
Fallback       : full bhavcopy for the dates still missing

Output: data/delivery_validate.json + the run log.
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing_scanner as sw

OUT_DIR = "data"
BT = os.path.join(OUT_DIR, "reversal_backtest.json")
COOKIE_JAR = "/tmp/nse_cookies.txt"
THRESHOLDS = [1.0, 1.2, 1.3, 1.5, 2.0]
MIN_PRIOR = 10          # prior days needed before a ratio is trusted


def log(*a):
    print(*a, flush=True)


def nse_session():
    """Warm a cookie jar on nseindia.com so the JSON APIs answer."""
    subprocess.run(["curl", "-s", "-m", "25", "-c", COOKIE_JAR, "-o", "/dev/null",
                    "-A", sw.UA, "https://www.nseindia.com/"],
                   capture_output=True, timeout=40)
    subprocess.run(["curl", "-s", "-m", "25", "-b", COOKIE_JAR, "-c", COOKIE_JAR,
                    "-o", "/dev/null", "-A", sw.UA,
                    "-H", "Referer: https://www.nseindia.com/",
                    "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE"],
                   capture_output=True, timeout=40)


def nse_deliv_series(symbol, frm, to):
    """[(date_iso, deliv_pct)] from the security-wise historical API."""
    url = ("https://www.nseindia.com/api/historicalOR/generateSecurityWiseHistoricalData"
           f"?from={frm.strftime('%d-%m-%Y')}&to={to.strftime('%d-%m-%Y')}"
           f"&symbol={symbol}&type=priceVolumeDeliverable&series=EQ")
    for _ in range(2):
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "30", "-b", COOKIE_JAR, "-c", COOKIE_JAR,
                 "-A", sw.UA, "-H", "Referer: https://www.nseindia.com/get-quotes/equity",
                 "-H", "Accept: */*", url],
                capture_output=True, text=True, timeout=45)
            d = json.loads(r.stdout)
            rows = d.get("data") or []
            out = []
            for row in rows:
                ds = row.get("mTIMESTAMP") or row.get("CH_TIMESTAMP") or ""
                pct = row.get("COP_DELIV_PERC")
                if not ds or pct in (None, "", "-"):
                    continue
                try:
                    dt = datetime.strptime(ds, "%d-%b-%Y").date()
                except ValueError:
                    continue
                try:
                    out.append((dt.isoformat(), float(pct)))
                except (TypeError, ValueError):
                    continue
            if out:
                return sorted(out)
        except Exception:
            pass
        time.sleep(2)
    return []


def bhav_deliv(d):
    t = sw.curl_get(f"{sw.ARCHIVE}/products/content/sec_bhavdata_full_"
                    f"{d.strftime('%d%m%Y')}.csv")
    if not t:
        return None
    rows = [{(k or "").strip(): (v.strip() if isinstance(v, str) else v)
             for k, v in r.items()} for r in csv.DictReader(io.StringIO(t))]
    if not rows or rows[0].get("DATE1", "").lower() != d.strftime("%d-%b-%Y").lower():
        return None
    out = {}
    for r in rows:
        if r.get("SERIES") != "EQ":
            continue
        try:
            out[r["SYMBOL"]] = float(r["DELIV_PER"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def summarise(tag, rows):
    """rows: list of signal dicts that have an outcome."""
    trig = [r for r in rows if r["outcome"] != "no_trigger"]
    if not trig:
        return f"  {tag:<28} signals {len(rows):>3}  triggered 0"
    t1 = sum(1 for r in trig if r["outcome"] == "t1")
    sl = sum(1 for r in trig if r["outcome"] == "sl")
    exp = float(np.mean([r["r"] for r in trig]))
    return (f"  {tag:<28} signals {len(rows):>3}  triggered {len(trig):>3}  "
            f"T1 {t1:>3}  SL {sl:>3}  win {t1 / len(trig) * 100:>5.1f}%  "
            f"exp {exp:>+6.3f}R")


def main():
    if not os.path.exists(BT):
        sys.exit("data/reversal_backtest.json missing — run the backtest first")
    signals = json.load(open(BT))["signals"]
    log(f"{len(signals)} signals from the backtest")

    by_symbol = defaultdict(list)
    for s in signals:
        by_symbol[s["symbol"]].append(s)

    nse_session()
    series = {}
    api_ok = 0
    for sym, rows in by_symbol.items():
        dates = sorted(datetime.strptime(r["date"], "%Y-%m-%d").date() for r in rows)
        frm, to = dates[0] - timedelta(days=45), dates[-1] + timedelta(days=3)
        got = nse_deliv_series(sym, frm, to)
        if got:
            series[sym] = got
            api_ok += 1
        time.sleep(0.4)
    log(f"delivery history via NSE API for {api_ok}/{len(by_symbol)} symbols")

    # fallback: bhavcopy for dates still missing (capped so the run stays sane)
    missing = [s for s in signals if s["symbol"] not in series]
    if missing:
        need = set()
        for s in missing:
            d0 = datetime.strptime(s["date"], "%Y-%m-%d").date()
            back, d, guard = 0, d0, 0
            while back < 21 and guard < 40:
                guard += 1
                if d.weekday() < 5:
                    need.add(d)
                    back += 1
                d -= timedelta(days=1)
        need = sorted(need)[-250:]
        log(f"{len(missing)} signals need the bhavcopy fallback: {len(need)} days")
        tmp = defaultdict(list)
        for d in need:
            got = bhav_deliv(d)
            if not got:
                continue
            for s in missing:
                pct = got.get(s["symbol"])
                if pct is not None:
                    tmp[s["symbol"]].append((d.isoformat(), pct))
        for sym, vals in tmp.items():
            series[sym] = sorted(set(vals))
        log(f"fallback filled {len(tmp)} symbols")

    enriched, no_data = [], 0
    for s in signals:
        hist = series.get(s["symbol"]) or []
        today = next((p for d, p in hist if d == s["date"]), None)
        prior = [p for d, p in hist if d < s["date"]][-20:]
        if today is None or len(prior) < MIN_PRIOR:
            no_data += 1
            continue
        avg = sum(prior) / len(prior)
        if avg <= 0:
            no_data += 1
            continue
        s = dict(s, deliv_pct=round(today, 1), deliv_avg=round(avg, 1),
                 deliv_ratio=round(today / avg, 2))
        enriched.append(s)
    log(f"delivery ratio computed for {len(enriched)} signals ({no_data} without data)\n")

    log("================ ALL SIGNALS (volume >= 2.0x) ================")
    log(summarise("no delivery filter", enriched))
    for th in THRESHOLDS:
        log(summarise(f"delivery >= {th}x avg", [r for r in enriched if r["deliv_ratio"] >= th]))

    tuned = [r for r in enriched if r["vol_ratio"] >= 2.5]
    log("\n================ TUNED SIGNALS (volume >= 2.5x) ================")
    log(summarise("no delivery filter", tuned))
    for th in THRESHOLDS:
        log(summarise(f"delivery >= {th}x avg", [r for r in tuned if r["deliv_ratio"] >= th]))

    log("\n================ SIGNAL DETAIL (newest first) ================")
    for r in sorted(enriched, key=lambda z: z["date"], reverse=True)[:30]:
        log(f"  {r['date']} {r['symbol']:<12} vol {r['vol_ratio']:>4}x  "
            f"deliv {r['deliv_pct']:>5}% vs {r['deliv_avg']:>5}% = {r['deliv_ratio']:>4}x  "
            f"-> {r['outcome']:<10} {r['r']:>5}R")

    json.dump(dict(generated_at=datetime.now().isoformat(timespec="seconds"),
                   signals=enriched, thresholds=THRESHOLDS, without_data=no_data),
              open(os.path.join(OUT_DIR, "delivery_validate.json"), "w"), indent=1)
    log("\nsaved data/delivery_validate.json")


if __name__ == "__main__":
    main()
