#!/usr/bin/env python3
"""
NSE daily data job (standard library only, no extra installs).

Runs on GitHub every weekday evening. Downloads NSE data, does ALL the maths,
and writes ONE small file: data/latest.json
Claude only reads that file and writes the analysis text.

What it computes:
  - market breadth (from bhavcopy)
  - 52-week highs / lows, bulk and block deals, sector index moves
  - price band changes (compared with data/sec_list_snapshot.csv)
  - LC-to-UC screen (stocks that hit lower circuit in the last 7 trading days
    and are at / near upper circuit today)

MODE env var: "schedule" / "workflow_dispatch" = strict (today only, waits for NSE),
              "push" = test run (uses the latest trading day available).
"""
import csv, io, json, os, subprocess, time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MODE = os.environ.get("MODE", "schedule")
STRICT = MODE != "push"
OUT_DIR = "data"
ARCHIVE = "https://nsearchives.nseindia.com"


def log(*a):
    print(*a, flush=True)


def curl_get(url, referer="https://www.nseindia.com/", retries=5, delay=4):
    for _ in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "25", "-w", "\n__S__%{http_code}", "-A", UA,
                 "-H", "Referer: " + referer, "-H", "Accept: */*", url],
                capture_output=True, text=True, timeout=40)
            out = r.stdout
            if "\n__S__" in out:
                body, st = out.rsplit("\n__S__", 1)
                if st.strip() == "200" and body.strip() and not body.lstrip().startswith("<"):
                    return body
        except Exception:
            pass
        time.sleep(delay)
    return None


def parse_csv(text):
    return [{(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
            for r in csv.DictReader(io.StringIO(text))]


def get_bhav(d, retries=4):
    t = curl_get(f"{ARCHIVE}/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv",
                 retries=retries)
    if not t:
        return None
    rows = parse_csv(t)
    if not rows:
        return None
    # NSE sometimes serves an older file on holidays: DATE1 must match
    if rows[0].get("DATE1", "").lower() != d.strftime("%d-%b-%Y").lower():
        return None
    return rows


def get_bands(d):
    t = curl_get(f"{ARCHIVE}/content/equities/sec_list_{d.strftime('%d%m%Y')}.csv", retries=4)
    if not t:
        return None
    out = {}
    for r in parse_csv(t):
        sym, ser = r.get("Symbol", ""), r.get("Series", "")
        if sym:
            out[(sym, ser)] = r.get("Band", "")
    return out


def prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def find_today():
    d = datetime.now(IST).date()
    if STRICT:
        deadline = time.time() + 25 * 60
        while True:
            rows = get_bhav(d, retries=2) if d.weekday() < 5 else None
            if rows:
                return d, rows
            if d.weekday() >= 5 or time.time() > deadline:
                return d, None
            log("bhavcopy not published yet, waiting 60s ...")
            time.sleep(60)
    for _ in range(10):
        if d.weekday() < 5:
            rows = get_bhav(d, retries=2)
            if rows:
                return d, rows
        d -= timedelta(days=1)
    return d, None


# ---------- helpers for numbers ----------
def D(x):
    return Decimal(str(x).replace(",", "").strip())


def tick_for(p):
    if p < 250: return Decimal("0.01")
    if p < 1000: return Decimal("0.05")
    if p < 5000: return Decimal("0.10")
    if p < 10000: return Decimal("0.50")
    if p < 20000: return Decimal("1.00")
    return Decimal("5.00")


def uc_price(prev, band):
    raw = prev * (1 + band / 100)
    t = tick_for(raw)
    uc = (raw / t).to_integral_value(rounding=ROUND_DOWN) * t
    if uc <= prev:
        uc = prev + t
    return uc, t


def lc_price(prev, band):
    raw = prev * (1 - band / 100)
    t = tick_for(raw)
    lc = (raw / t).to_integral_value(rounding=ROUND_UP) * t
    if lc >= prev:
        lc = prev - t
    return lc, t


def band_value(s):
    try:
        b = D(s)
        return b if b > 0 else None
    except Exception:
        return None


# ---------- breadth ----------
def compute_breadth(eq_rows):
    changes, turnover = [], 0.0
    for r in eq_rows:
        try:
            prev, close = float(r["PREV_CLOSE"]), float(r["CLOSE_PRICE"])
            if prev != 0:
                changes.append((close - prev) / prev * 100.0)
        except Exception:
            pass
        try:
            turnover += float(r.get("TURNOVER_LACS", 0) or 0)
        except Exception:
            pass
    total = len(eq_rows)
    adv = sum(1 for c in changes if c > 0)
    dec = sum(1 for c in changes if c < 0)
    unch = sum(1 for c in changes if c == 0)
    avg = sum(changes) / len(changes) if changes else 0.0
    return {
        "total": total, "adv": adv, "dec": dec, "unch": unch,
        "avg_move": round(avg, 2),
        "pct_red": round(dec / total * 100.0, 1) if total else 0.0,
        "ad_ratio": round(adv / dec, 2) if dec else None,
        "turnover_lakh_cr": round(turnover / 1e7, 2),
    }


# ---------- live NSE APIs ----------
def fetch_52wk():
    ref = "https://www.nseindia.com/market-data/52-week-high-low"
    th = curl_get("https://www.nseindia.com/api/live-analysis-data-52weekhighstock", ref)
    tl = curl_get("https://www.nseindia.com/api/live-analysis-data-52weeklowstock", ref)
    try:
        highs = json.loads(th).get("data", []) if th else []
        lows = json.loads(tl).get("data", []) if tl else []
    except Exception:
        highs, lows = [], []

    def pchg(r):
        try: return abs(float(r.get("pChange", 0)))
        except Exception: return 0

    def top(lst):
        return [{"symbol": r.get("symbol", ""), "pChange": r.get("pChange")}
                for r in sorted(lst, key=lambda r: -pchg(r))[:5]]

    return {"highs_count": len(highs), "lows_count": len(lows),
            "top_highs": top(highs), "top_lows": top(lows)}


def fetch_deals(date_disp, kind):
    url = (f"https://www.nseindia.com/api/historicalOR/bulk-block-short-deals"
           f"?optionType={kind}&from={date_disp}&to={date_disp}")
    t = curl_get(url, "https://www.nseindia.com/report-detail/display-bulk-and-block-deals")
    try:
        data = json.loads(t).get("data", []) if t else []
    except Exception:
        data = []

    def val(r):
        try: return float(r.get("BD_QTY_TRD", 0)) * float(r.get("BD_TP_WATP", 0))
        except Exception: return 0

    items = sorted(data, key=lambda r: -val(r))[:10]
    return {"ok": t is not None, "count": len(data),
            "items": [{"symbol": r.get("BD_SYMBOL", ""), "value_cr": round(val(r) / 1e7, 2),
                       "client": (r.get("BD_CLIENT_NAME") or "")[:60],
                       "side": r.get("BD_BUY_SELL", "")} for r in items]}


def fetch_indices():
    t = curl_get("https://www.nseindia.com/api/allIndices",
                 "https://www.nseindia.com/market-data/live-market-indices")
    try:
        data = json.loads(t).get("data", []) if t else []
    except Exception:
        return {}
    out = {}
    for r in data:
        name = (r.get("index") or r.get("indexName") or "").strip()
        try:
            out[name] = {"pChange": round(float(r.get("percentChange")), 2),
                         "last": float(r.get("last"))}
        except Exception:
            continue
    return out


def price_band_changes(update_snapshot):
    t = curl_get(f"{ARCHIVE}/content/equities/sec_list.csv")
    if not t:
        return {"ok": False, "count": 0, "first_run": False, "changed": []}
    snap = os.path.join(OUT_DIR, "sec_list_snapshot.csv")
    today = {r.get("Symbol", ""): r.get("Band", "") for r in parse_csv(t) if r.get("Symbol")}
    first_run = not os.path.exists(snap)
    changed = []
    if not first_run:
        with open(snap) as f:
            prev = {r.get("Symbol", ""): r.get("Band", "") for r in parse_csv(f.read())}
        changed = [{"symbol": s, "old": prev[s], "new": b}
                   for s, b in today.items() if s in prev and prev[s] != b]
    if update_snapshot:
        with open(snap, "w") as f:
            f.write(t)
    return {"ok": True, "count": len(today) if first_run else len(changed),
            "first_run": first_run, "changed": changed}


# ---------- LC-to-UC screen ----------
def lc_uc_screen(days):
    """days[0] = today, days[1..] = earlier trading days (newest first)."""
    bands = {}
    for i in range(1, len(days)):
        bands[i] = get_bands(days[i][0])
        time.sleep(1)

    def bands_for(i):
        for j in range(i + 1, len(days)):
            if bands.get(j):
                return bands[j]
        return {}

    eq = []
    for _, rows in days:
        eq.append({r["SYMBOL"]: r for r in rows if r.get("SERIES") == "EQ"})

    window_idx = list(range(1, min(8, len(days))))      # 7 trading days before today
    if not window_idx:
        return {"window": "", "matches": []}
    w_old, w_new = days[window_idx[-1]][0], days[window_idx[0]][0]
    window = f"{w_old.strftime('%d %b')}–{w_new.strftime('%d %b')}"
    today_date = days[0][0]
    band_today = bands_for(0)
    matches = []

    for sym, r in eq[0].items():
        try:
            close, high = D(r["CLOSE_PRICE"]), D(r["HIGH_PRICE"])
            qty = int(float(r["TTL_TRD_QNTY"]))
            prev = D(r["PREV_CLOSE"])
        except Exception:
            continue
        if close < 100 or qty < 100000:
            continue
        band = band_value(band_today.get((sym, "EQ"), ""))
        if band is None:
            continue
        uc, t = uc_price(prev, band)
        if high > uc + 2 * t:
            continue
        lc_days = []
        for k in reversed(window_idx):                    # oldest -> newest
            rk = eq[k].get(sym)
            bk = band_value(bands_for(k).get((sym, "EQ"), "")) if rk else None
            if not rk or bk is None:
                continue
            try:
                pk, low, clk = D(rk["PREV_CLOSE"]), D(rk["LOW_PRICE"]), D(rk["CLOSE_PRICE"])
            except Exception:
                continue
            lc, tk = lc_price(pk, bk)
            if low <= lc and not (low < lc - 2 * tk):
                label = days[k][0].strftime("%d %b") + (" (lock)" if clk == lc else "")
                lc_days.append(label)
        if not lc_days:
            continue
        near = (uc - close) / uc <= Decimal("0.02")
        if not (high >= uc or near):
            continue
        cat = "a" if close == uc else ("b" if high >= uc else "c")

        def num(key):
            try: return float(D(r[key]))
            except Exception: return None

        dp = num("DELIV_PER")
        matches.append({
            "date": today_date.strftime("%Y-%m-%d"), "cat": cat, "symbol": sym,
            "band": float(band), "prev_close": float(prev), "open": num("OPEN_PRICE"),
            "high": float(high), "low": num("LOW_PRICE"), "close": float(close),
            "uc": float(uc), "dist_pct": round(float((uc - close) / uc * 100), 2),
            "deliv_pct": dp, "qty": qty, "turnover_lacs": num("TURNOVER_LACS"),
            "lc_days": ", ".join(lc_days), "lc_count": len(lc_days), "window": window,
        })
    matches.sort(key=lambda m: (m["cat"], m["dist_pct"]))
    return {"window": window, "matches": matches}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now = datetime.now(IST)
    d, rows = find_today()
    out_path = os.path.join(OUT_DIR, "latest.json")
    if not rows:
        res = {"date_iso": d.strftime("%Y-%m-%d"), "date_disp": d.strftime("%d-%m-%Y"),
               "trading_day": False, "generated_at": now.isoformat(), "mode": MODE}
        json.dump(res, open(out_path, "w"), indent=1)
        log("No trading data for", d)
        return
    log("Trading day:", d)

    # earlier trading days (need today + 8 earlier)
    days = [(d, rows)]
    cur, tries = d, 0
    while len(days) < 9 and tries < 25:
        cur = prev_weekday(cur)
        tries += 1
        r = get_bhav(cur, retries=3)
        if r:
            days.append((cur, r))
        time.sleep(1)
    log("Loaded", len(days), "trading days")

    eq_today = [r for r in rows if r.get("SERIES") == "EQ"]
    date_disp = d.strftime("%d-%m-%Y")
    res = {
        "date_iso": d.strftime("%Y-%m-%d"), "date_disp": date_disp,
        "trading_day": True, "generated_at": now.isoformat(), "mode": MODE,
        "breadth": compute_breadth(eq_today),
        "week52": fetch_52wk(),
        "bulk_deals": fetch_deals(date_disp, "bulk_deals"),
        "block_deals": fetch_deals(date_disp, "block_deals"),
        "price_band": price_band_changes(update_snapshot=STRICT),
        "sector_moves": fetch_indices(),
        "lcuc": lc_uc_screen(days),
    }
    json.dump(res, open(out_path, "w"), indent=1)
    log("Done. breadth:", res["breadth"]["total"], "EQ stocks;",
        "LC-UC matches:", len(res["lcuc"]["matches"]),
        "; 52wk highs/lows:", res["week52"]["highs_count"], res["week52"]["lows_count"])


if __name__ == "__main__":
    main()
