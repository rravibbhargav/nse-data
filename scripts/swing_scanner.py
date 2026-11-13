#!/usr/bin/env python3
"""
SKT Swing / Short-term Momentum Scanner (Nifty 500)
---------------------------------------------------
Finds stocks breaking out of tight consolidations with volume, strong trend
and relative strength -> candidates that tend to run for several days.

Data   : yfinance (free)      Universe : Nifty 500 (NSE list, cached)
Output : data/swing_scan.json, data/swing_scan_latest.csv,
         data/swing_scan_history.csv  (+ Telegram message if secrets set)

Nothing here is a recommendation; it is a research shortlist for the RA.
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = "data"
N500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
N500_CACHE = os.path.join(OUT_DIR, "nifty500.csv")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ----------------------------- tunable parameters ---------------------------
P = dict(
    MIN_PRICE=50,            # Rs
    MIN_TURNOVER_CR=10,      # 20-day avg daily turnover, Rs crore
    VOL_MULT=1.5,            # today's volume vs 20-day avg
    BREAKOUT_LOOKBACK=20,    # close above prior N-day high
    NEAR_52W_PCT=3.0,        # or within X% of 52-week high
    TIGHT_RANGE_DAYS=10,     # consolidation window
    TIGHT_RANGE_PCT=8.0,     # (high-low)/low of window, %
    RSI_MIN=55, RSI_MAX=75,
    ADX_MIN=20,
    MAX_EXT_FROM_EMA20=8.0,  # skip if close is >8% above EMA20 (chasing)
    TOP_N=15,
)
# ---------------------------------------------------------------------------


def log(*a):
    print(*a, flush=True)


def curl_get(url, referer="https://www.nseindia.com/", retries=4, delay=3):
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


def load_universe():
    """Nifty 500 symbols + industry. Downloads from NSE, falls back to cache."""
    os.makedirs(OUT_DIR, exist_ok=True)
    text = curl_get(N500_URL)
    if text and "Symbol" in text.splitlines()[0]:
        with open(N500_CACHE, "w", encoding="utf-8") as f:
            f.write(text)
        log("Nifty 500 list downloaded from NSE")
    elif os.path.exists(N500_CACHE):
        text = open(N500_CACHE, encoding="utf-8").read()
        log("NSE download failed -> using cached Nifty 500 list")
    else:
        sys.exit("No Nifty 500 list available (download failed, no cache)")
    rows = list(csv.DictReader(io.StringIO(text)))
    uni = {}
    for r in rows:
        sym = (r.get("Symbol") or "").strip()
        if sym:
            uni[sym] = (r.get("Industry") or "").strip()
    return uni


def download_prices(symbols, index_symbol="^CRSLDX"):
    import yfinance as yf
    tickers = [s + ".NS" for s in symbols]
    frames = {}
    chunk = 100
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        for attempt in range(3):
            try:
                df = yf.download(part, period="1y", interval="1d", group_by="ticker",
                                 auto_adjust=False, threads=True, progress=False)
                break
            except Exception as e:
                log("download error", e)
                time.sleep(10)
                df = None
        if df is None or df.empty:
            continue
        for t in part:
            try:
                sub = df[t].dropna(subset=["Close"]) if isinstance(df.columns, pd.MultiIndex) else df
                if len(sub) >= 120:
                    frames[t[:-3]] = sub
            except KeyError:
                pass
        log(f"downloaded {min(i + chunk, len(tickers))}/{len(tickers)}")
    idx = None
    for sym in (index_symbol, "^NSEI"):
        try:
            idx = yf.download(sym, period="1y", interval="1d", auto_adjust=False, progress=False)
            if isinstance(idx.columns, pd.MultiIndex):
                idx.columns = idx.columns.get_level_values(0)
            if not idx.empty:
                break
        except Exception:
            idx = None
    return frames, idx


# ------------------------------ indicators ---------------------------------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def adx(high, low, close, n=14):
    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), atr


def analyse(sym, df, idx_ret_1m, industry):
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    if len(c) < 120 or c.iloc[-1] < P["MIN_PRICE"]:
        return None
    ema20, ema50 = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    r = rsi(c)
    a, atr = adx(h, l, c)
    vol20 = v.rolling(20).mean()
    turnover_cr = (c * v).rolling(20).mean().iloc[-1] / 1e7
    close, prev_close = c.iloc[-1], c.iloc[-2]
    hi20_prior = h.iloc[-P["BREAKOUT_LOOKBACK"] - 1:-1].max()
    hi52 = h.iloc[-250:].max()
    win = df.iloc[-P["TIGHT_RANGE_DAYS"] - 1:-1]
    tight_pct = (win["High"].max() - win["Low"].min()) / win["Low"].min() * 100
    ret_1m = (close / c.iloc[-22] - 1) * 100
    ret_3m = (close / c.iloc[-64] - 1) * 100
    vol_ratio = v.iloc[-1] / vol20.iloc[-1] if vol20.iloc[-1] else 0
    ext20 = (close / ema20.iloc[-1] - 1) * 100
    ema50_rising = ema50.iloc[-1] > ema50.iloc[-6]
    rs_excess = ret_1m - idx_ret_1m

    # ---------------- hard filters ----------------
    if not all(np.isfinite(x) for x in (turnover_cr, r.iloc[-1], a.iloc[-1], vol_ratio,
                                         rs_excess, tight_pct, ext20)):
        return None
    if turnover_cr < P["MIN_TURNOVER_CR"]:
        return None
    if not (close > ema20.iloc[-1] > ema50.iloc[-1] and ema50_rising):
        return None
    breakout = close > hi20_prior
    near52 = close >= hi52 * (1 - P["NEAR_52W_PCT"] / 100)
    if not (breakout or near52):
        return None
    if vol_ratio < P["VOL_MULT"]:
        return None
    if not (P["RSI_MIN"] <= r.iloc[-1] <= P["RSI_MAX"]):
        return None
    if a.iloc[-1] < P["ADX_MIN"]:
        return None
    if rs_excess <= 0:
        return None
    if ext20 > P["MAX_EXT_FROM_EMA20"]:
        return None
    if close <= prev_close:          # must close up on breakout day
        return None

    # ---------------- score 0-100 ----------------
    score = 0.0
    score += min(vol_ratio, 4) / 4 * 25                       # volume thrust
    score += max(0, (P["TIGHT_RANGE_PCT"] * 1.5 - tight_pct)) / (P["TIGHT_RANGE_PCT"] * 1.5) * 20  # tight base
    score += min(max(rs_excess, 0), 15) / 15 * 20             # relative strength
    score += min(max(a.iloc[-1] - 20, 0), 25) / 25 * 15       # trend strength
    score += 10 if breakout else 5                            # fresh 20D breakout
    score += max(0, 10 - abs(r.iloc[-1] - 65))                # RSI sweet spot ~65
    score = round(score, 1)

    # ---------------- trade levels ----------------
    sl = round(float(min(ema20.iloc[-1], l.iloc[-10:].min())), 2)
    sl = round(min(sl, close - 1.0 * float(atr.iloc[-1])), 2) if sl >= close else sl
    risk = close - sl
    t1, t2 = round(close + 1.5 * risk, 2), round(close + 2.5 * risk, 2)
    reasons = []
    if breakout:
        reasons.append(f"{P['BREAKOUT_LOOKBACK']}D breakout")
    if near52:
        reasons.append("near 52W high")
    if tight_pct <= P["TIGHT_RANGE_PCT"]:
        reasons.append(f"tight base {tight_pct:.1f}%")
    reasons.append(f"vol {vol_ratio:.1f}x")
    reasons.append(f"RS +{rs_excess:.1f}%")

    return dict(
        symbol=sym, industry=industry, score=score, close=round(float(close), 2),
        chg_pct=round((close / prev_close - 1) * 100, 2), entry=round(float(close), 2),
        sl=sl, risk_pct=round(risk / close * 100, 2), t1=t1, t2=t2,
        vol_ratio=round(float(vol_ratio), 2), rsi=round(float(r.iloc[-1]), 1),
        adx=round(float(a.iloc[-1]), 1), ret_1m=round(float(ret_1m), 1),
        ret_3m=round(float(ret_3m), 1), rs_excess=round(float(rs_excess), 1),
        tight_range_pct=round(float(tight_pct), 1), ext_ema20_pct=round(float(ext20), 1),
        pct_from_52w=round(float((close / hi52 - 1) * 100), 1),
        turnover_cr=round(float(turnover_cr), 1), breakout_20d=bool(breakout),
        reasons=", ".join(reasons),
    )


def run_scan(frames, idx, universe):
    idx_ret_1m = float("nan")
    if idx is not None:
        ic = idx["Close"].dropna()
        if len(ic) > 22:
            idx_ret_1m = float(ic.iloc[-1] / ic.iloc[-22] - 1) * 100
    if not np.isfinite(idx_ret_1m):
        # fallback: median 1-month return of the universe itself
        rets = [float(d["Close"].iloc[-1] / d["Close"].iloc[-22] - 1) * 100
                for d in frames.values() if len(d) > 22]
        idx_ret_1m = float(np.nanmedian(rets)) if rets else 0.0
        log(f"index data unavailable -> using universe median 1M return {idx_ret_1m:.2f}%")
    picks, scanned = [], 0
    for sym, df in frames.items():
        try:
            scanned += 1
            res = analyse(sym, df, idx_ret_1m, universe.get(sym, ""))
            if res:
                picks.append(res)
        except Exception as e:
            log("skip", sym, e)
    picks.sort(key=lambda x: -x["score"])
    return picks, scanned, idx_ret_1m


def telegram_text(date_disp, picks, scanned, idx_ret_1m):
    head = (f"<b>📈 SKT Swing Scanner — {date_disp}</b>\n"
            f"Nifty500 scanned: {scanned} | Index 1M: {idx_ret_1m:+.1f}%\n"
            f"Setup: trend + tight base + breakout + volume + RS\n\n")
    if not picks:
        return head + "Aaj koi stock filters pass nahi kar paya. No trade is also a trade. 🙂"
    lines = []
    for i, p in enumerate(picks[:P["TOP_N"]], 1):
        lines.append(
            f"<b>{i}. {p['symbol']}</b> ({p['industry'][:18]}) — score {p['score']}\n"
            f"   CMP ₹{p['close']} ({p['chg_pct']:+.1f}%) | SL ₹{p['sl']} ({p['risk_pct']}%)\n"
            f"   T1 ₹{p['t1']} | T2 ₹{p['t2']} | {p['reasons']}")
    foot = ("\n\n<i>Research shortlist only, not a recommendation. Entry next day near CMP, "
            "SL on closing basis, position size by risk %.</i>")
    return head + "\n".join(lines) + foot


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("Telegram secrets not set -> skipping send")
        return
    import urllib.request, urllib.parse
    for part in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        data = urllib.parse.urlencode({"chat_id": chat, "text": part,
                                       "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30)
        except Exception as e:
            log("telegram error", e)


def save(picks, scanned, idx_ret_1m, date_iso, date_disp):
    os.makedirs(OUT_DIR, exist_ok=True)
    out = dict(date_iso=date_iso, date_disp=date_disp,
               generated_at=datetime.now(IST).isoformat(timespec="seconds"),
               scanned=scanned, index_ret_1m=round(idx_ret_1m, 2), params=P,
               picks=picks[:P["TOP_N"]], all_passed=len(picks))
    json.dump(out, open(os.path.join(OUT_DIR, "swing_scan.json"), "w"), indent=1)
    cols = ["date", "symbol", "industry", "score", "close", "chg_pct", "entry", "sl", "risk_pct",
            "t1", "t2", "vol_ratio", "rsi", "adx", "ret_1m", "rs_excess", "tight_range_pct",
            "pct_from_52w", "turnover_cr", "breakout_20d", "reasons"]
    rows = [[date_iso] + [p[c] for c in cols[1:]] for p in picks[:P["TOP_N"]]]
    with open(os.path.join(OUT_DIR, "swing_scan_latest.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(cols); w.writerows(rows)
    hist = os.path.join(OUT_DIR, "swing_scan_history.csv")
    existing = set()
    if os.path.exists(hist):
        with open(hist, newline="") as f:
            existing = {(r[0], r[1]) for r in csv.reader(f) if r and r[0] != "date"}
    need_header = not os.path.exists(hist) or os.path.getsize(hist) == 0
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(cols)
        for r in rows:
            if (r[0], r[1]) not in existing:
                w.writerow(r)


ARCHIVE = "https://nsearchives.nseindia.com"


def latest_bhav():
    """Most recent NSE full bhavcopy (EQ series) -> (date, {symbol: row})."""
    today = datetime.now(IST).date()
    for back in range(0, 7):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        t = curl_get(f"{ARCHIVE}/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv")
        if not t:
            continue
        rows = [{(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
                for r in csv.DictReader(io.StringIO(t))]
        if not rows or rows[0].get("DATE1", "").lower() != d.strftime("%d-%b-%Y").lower():
            continue
        eq = {r["SYMBOL"]: r for r in rows if r.get("SERIES") == "EQ"}
        log(f"NSE bhavcopy found for {d} ({len(eq)} EQ rows)")
        return d, eq
    log("NSE bhavcopy not available -> Yahoo data only")
    return None, {}


def patch_with_bhav(frames, bdate, bhav):
    """Append the bhavcopy day to any symbol whose Yahoo data stops before it."""
    if not bdate:
        return frames, 0
    ts = pd.Timestamp(bdate)
    patched = 0
    for sym, df in list(frames.items()):
        r = bhav.get(sym)
        if r is None:
            continue
        last = df.index[-1]
        last_d = last.date() if hasattr(last, "date") else last
        if last_d >= bdate:
            continue
        try:
            row = {"Open": float(r["OPEN_PRICE"]), "High": float(r["HIGH_PRICE"]),
                   "Low": float(r["LOW_PRICE"]), "Close": float(r["CLOSE_PRICE"]),
                   "Adj Close": float(r["CLOSE_PRICE"]), "Volume": float(r["TTL_TRD_QNTY"])}
        except (KeyError, ValueError):
            continue
        idx_ts = ts.tz_localize(last.tz) if getattr(last, "tz", None) is not None else ts
        new = pd.DataFrame([row], index=pd.DatetimeIndex([idx_ts]))
        frames[sym] = pd.concat([df, new[[c for c in df.columns if c in new.columns]]])
        patched += 1
    log(f"patched {patched} symbols with NSE bhavcopy for {bdate}")
    return frames, patched


def already_done(date_iso):
    """Automatic runs skip if this trading day was already scanned and sent.
    Only a manual 'Run workflow' (workflow_dispatch) forces a resend."""
    if os.environ.get("MODE", "schedule") == "workflow_dispatch":
        return False
    try:
        old = json.load(open(os.path.join(OUT_DIR, "swing_scan.json")))
        return old.get("date_iso") == date_iso
    except Exception:
        return False


def main():
    universe = load_universe()
    log(f"universe: {len(universe)} symbols")
    frames, idx = download_prices(list(universe))
    log(f"price data for {len(frames)} symbols")
    if len(frames) < 200:
        sys.exit("Too few symbols downloaded — aborting (yfinance issue?)")
    bdate, bhav = latest_bhav()
    frames, patched = patch_with_bhav(frames, bdate, bhav)
    last = max(df.index[-1] for df in frames.values())
    date_iso, date_disp = last.strftime("%Y-%m-%d"), last.strftime("%d %b %Y")
    if already_done(date_iso):
        log(f"{date_iso} already scanned and sent -> skipping this backup run")
        return
    # index return would be stale if Yahoo lacked the latest day -> use universe median
    try:
        if idx is not None and patched and idx.dropna(subset=["Close"]).index[-1] < last:
            idx = None
    except Exception:
        idx = None
    # keep only symbols that have today's bar
    frames = {s: d for s, d in frames.items() if d.index[-1] == last}
    picks, scanned, idx_ret_1m = run_scan(frames, idx, universe)
    log(f"{len(picks)} passed filters; top {P['TOP_N']} saved")
    for p in picks[:P["TOP_N"]]:
        log(f"  {p['symbol']:<12} score {p['score']:>5}  CMP {p['close']:>9}  SL {p['sl']:>9}  {p['reasons']}")
    save(picks, scanned, idx_ret_1m, date_iso, date_disp)
    send_telegram(telegram_text(date_disp, picks, scanned, idx_ret_1m))


if __name__ == "__main__":
    main()
