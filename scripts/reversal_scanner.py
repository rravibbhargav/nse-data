#!/usr/bin/env python3
"""
SKT 52-Week Low Reversal Scanner  ("fake open-high, real absorption")
---------------------------------------------------------------------
Idea (as specified by the RA):
  A stock sitting near its 52-week low prints an OPEN = HIGH candle, which
  normally screams weakness -- price never traded above the open all day.
  But the volume tells the opposite story: huge volume came in, the fall was
  absorbed, and the candle closed well off its low with a long lower wick.
  That makes the open-high "dikhane ka" (cosmetic) while real buying happened
  underneath. Entry is taken only when price takes out that day's high, so the
  trap is confirmed before any money goes in.

Screens, in order (cheap -> expensive):
  1. Universe   : Nifty Total Market (~750) with Nifty 500 fallback
  2. Technical  : near 52W low + open=high + volume spike + absorption wick
  3. Fundamental: mcap >= 1000 cr, price >= 100, positive EPS, ROE, D/E
  4. Risk-reward: SL under the absorption low, headroom to prior resistance
  5. Dhan intraday (optional): close above VWAP, late-session volume share

Data   : yfinance EOD + NSE bhavcopy patch + yfinance fundamentals
         + Dhan intraday charts (only for the shortlist, if token is set)
Output : data/reversal_scan.json
         data/reversal_scan_latest.csv
         data/reversal_scan_history.csv   (+ Telegram message if secrets set)

Nothing here is a recommendation; it is a research shortlist for the RA.
"""
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing_scanner as sw          # reuse curl_get / download / bhavcopy / telegram

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = "data"
TM_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv"
TM_CACHE = os.path.join(OUT_DIR, "nifty_total_market.csv")
DHAN_SCRIP = "https://images.dhan.co/api-data/api-scrip-master.csv"

# ----------------------------- tunable parameters ---------------------------
P = dict(
    # --- hard requirements from the RA ---
    MIN_PRICE=100.0,           # Rs, closing price
    MIN_MCAP_CR=1000.0,        # Rs crore
    # --- "sitting at 52-week low" ---
    NEAR_52W_LOW_PCT=7.0,      # close within X% of the 52-week low
    # --- the cosmetic open-high candle ---
    OPEN_HIGH_TOL_PCT=0.35,    # (high - open) / open <= X%  -> open is the high
    MUST_CLOSE_BELOW_OPEN=True,
    # --- volume says the opposite ---
    VOL_MULT=2.0,              # today's volume vs 20-day average
    MIN_TURNOVER_CR=2.0,       # today's traded value, Rs crore (liquidity)
    # --- absorption proof inside the candle ---
    CLOSE_POS_MIN=0.45,        # (close - low) / (high - low)
    LOWER_WICK_MIN=0.40,       # lower wick as share of the day's range
    MAX_FALL_PCT=4.5,          # absorbed, not a freefall: close vs prev close
    MIN_RANGE_PCT=1.2,         # day must actually have a fight in it
    # --- risk / reward ---
    ENTRY_BUFFER_PCT=0.20,     # trigger = today's high + X%
    ATR_SL_BUFFER=0.25,        # SL = day low - X * ATR(14)
    MAX_RISK_PCT=7.0,          # entry-to-SL distance as % of entry
    MIN_HEADROOM_R=1.2,        # room up to the nearest supply shelf, in R multiples
    # --- fundamental quality ---
    ROE_MIN=10.0,              # %
    DE_MAX=1.0,                # debt / equity (skipped for banks & NBFCs)
    REQUIRE_POSITIVE_EPS=True,
    ALLOW_GRADES=("A", "B"),   # fundamental grade kept in the output
    # --- Dhan intraday confirmation (optional) ---
    DHAN_INTERVAL="5",
    TOP_N=12,
)
# ---------------------------------------------------------------------------

FIN_WORDS = ("bank", "financ", "nbfc", "insur", "capital market",
             "asset manage", "housing financ")


def log(*a):
    print(*a, flush=True)


# ------------------------------- universe ----------------------------------
def load_universe():
    """Nifty Total Market (~750 names, largecap -> smallcap). Falls back to
    its own cache, then to the Nifty 500 list the swing scanner keeps."""
    os.makedirs(OUT_DIR, exist_ok=True)
    text = sw.curl_get(TM_URL)
    if text and "Symbol" in text.splitlines()[0]:
        open(TM_CACHE, "w", encoding="utf-8").write(text)
        log("Nifty Total Market list downloaded from NSE")
    elif os.path.exists(TM_CACHE):
        text = open(TM_CACHE, encoding="utf-8").read()
        log("NSE download failed -> using cached Total Market list")
    else:
        log("Total Market list unavailable -> falling back to Nifty 500")
        return sw.load_universe()
    uni = {}
    for r in csv.DictReader(io.StringIO(text)):
        sym = (r.get("Symbol") or "").strip()
        if sym:
            uni[sym] = (r.get("Industry") or "").strip()
    return uni


# ------------------------------ technicals ---------------------------------
def analyse(sym, df, industry):
    """Technical half of the screen. Returns a dict or None."""
    o, h, l, c, v = df["Open"], df["High"], df["Low"], df["Close"], df["Volume"]
    if len(c) < 150:
        return None

    close, open_, high, low = (float(c.iloc[-1]), float(o.iloc[-1]),
                               float(h.iloc[-1]), float(l.iloc[-1]))
    prev_close = float(c.iloc[-2])
    vol = float(v.iloc[-1])
    if not all(np.isfinite(x) and x > 0 for x in (close, open_, high, low, prev_close, vol)):
        return None
    if high < low or close < low or close > high:
        return None

    # 1. price floor
    if close < P["MIN_PRICE"]:
        return None

    # 2. parked at the 52-week low
    lo52 = float(l.iloc[-250:].min())
    hi52 = float(h.iloc[-250:].max())
    if not np.isfinite(lo52) or lo52 <= 0:
        return None
    above_low_pct = (close / lo52 - 1) * 100
    if above_low_pct > P["NEAR_52W_LOW_PCT"]:
        return None

    # 3. the cosmetic open = high candle
    open_high_gap = (high - open_) / open_ * 100
    if open_high_gap > P["OPEN_HIGH_TOL_PCT"]:
        return None
    if P["MUST_CLOSE_BELOW_OPEN"] and close >= open_:
        return None

    # 4. volume calling the bluff
    vol20 = float(v.iloc[-21:-1].mean())
    if not np.isfinite(vol20) or vol20 <= 0:
        return None
    vol_ratio = vol / vol20
    if vol_ratio < P["VOL_MULT"]:
        return None
    turnover_cr = close * vol / 1e7
    if turnover_cr < P["MIN_TURNOVER_CR"]:
        return None

    # 5. absorption inside the candle
    rng = high - low
    range_pct = rng / low * 100
    if range_pct < P["MIN_RANGE_PCT"]:
        return None
    close_pos = (close - low) / rng                       # 1.0 = closed at high
    body_low = min(open_, close)
    lower_wick_share = (body_low - low) / rng
    chg_pct = (close / prev_close - 1) * 100
    if close_pos < P["CLOSE_POS_MIN"]:
        return None
    if lower_wick_share < P["LOWER_WICK_MIN"]:
        return None
    if chg_pct < -P["MAX_FALL_PCT"]:
        return None

    # 6. risk / reward
    _, atr_s = sw.adx(h, l, c)
    atr = float(atr_s.iloc[-1])
    if not np.isfinite(atr) or atr <= 0:
        atr = rng
    entry = round(high * (1 + P["ENTRY_BUFFER_PCT"] / 100), 2)
    sl = round(low - P["ATR_SL_BUFFER"] * atr, 2)
    risk = entry - sl
    if risk <= 0:
        return None
    risk_pct = risk / entry * 100
    if risk_pct > P["MAX_RISK_PCT"]:
        return None
    hi20 = float(h.iloc[-21:-1].max())                    # nearest supply shelf
    hi50 = float(h.iloc[-51:-1].max())
    # if today's high already clears the 20-day high, that shelf is gone -> look higher
    res = hi20 if hi20 > entry * 1.01 else hi50
    if not np.isfinite(res) or res <= entry:
        return None
    headroom_r = (res - entry) / risk
    if headroom_r < P["MIN_HEADROOM_R"]:
        return None
    t1 = round(entry + 1.5 * risk, 2)
    t2 = round(entry + max(2.5, min(headroom_r, 4.0)) * risk, 2)

    # context
    rsi14 = float(sw.rsi(c).iloc[-1])
    dist_52w_high = (close / hi52 - 1) * 100
    down_1m = (close / float(c.iloc[-22]) - 1) * 100
    down_3m = (close / float(c.iloc[-64]) - 1) * 100
    tail = l.iloc[-250:]
    days_since_low = int(len(tail) - 1 - int(np.argmin(tail.values)))

    tech_score = (
        min(vol_ratio, 5) / 5 * 30                                  # volume thrust
        + close_pos * 20                                            # closed off the low
        + lower_wick_share * 15                                     # long absorption tail
        + max(0.0, (P["NEAR_52W_LOW_PCT"] - above_low_pct)) / P["NEAR_52W_LOW_PCT"] * 10
        + min(headroom_r, 5) / 5 * 10                               # room to run
        + max(0.0, min(10.0, (35 - rsi14)))                         # oversold bonus
    )

    return dict(
        symbol=sym, industry=industry,
        close=round(close, 2), open=round(open_, 2), high=round(high, 2),
        low=round(low, 2), chg_pct=round(chg_pct, 2),
        open_high_gap_pct=round(open_high_gap, 3),
        vol_ratio=round(vol_ratio, 2), turnover_cr=round(turnover_cr, 1),
        close_pos=round(close_pos, 2), lower_wick=round(lower_wick_share, 2),
        range_pct=round(range_pct, 2),
        above_52w_low_pct=round(above_low_pct, 2),
        from_52w_high_pct=round(dist_52w_high, 1),
        days_since_52w_low=days_since_low,
        rsi=round(rsi14, 1), atr=round(atr, 2),
        entry=entry, sl=sl, risk_pct=round(risk_pct, 2),
        t1=t1, t2=t2, rr_t1=1.5, rr_t2=round((t2 - entry) / risk, 2),
        headroom_r=round(headroom_r, 2), resistance=round(res, 2),
        ret_1m=round(down_1m, 1), ret_3m=round(down_3m, 1),
        tech_score=round(tech_score, 1),
    )


# ----------------------------- fundamentals --------------------------------
def fundamentals(sym, industry):
    """yfinance fundamentals for one symbol -> dict (only called for the
    handful that clear the technical screen)."""
    import yfinance as yf
    info = {}
    for attempt in range(3):
        try:
            info = yf.Ticker(sym + ".NS").info or {}
            if info.get("marketCap"):
                break
        except Exception as e:
            log("  info error", sym, e)
        time.sleep(2)

    def num(key):
        x = info.get(key)
        try:
            x = float(x)
            return x if np.isfinite(x) else None
        except (TypeError, ValueError):
            return None

    mcap = num("marketCap")
    mcap_cr = round(mcap / 1e7, 0) if mcap else None
    roe = num("returnOnEquity")
    roe = round(roe * 100, 1) if roe is not None and abs(roe) < 10 else (
        round(roe, 1) if roe is not None else None)
    de = num("debtToEquity")
    de = round(de / 100, 2) if de is not None else None      # yfinance reports %
    eps = num("trailingEps")
    pe = num("trailingPE")
    pb = num("priceToBook")
    pm = num("profitMargins")
    pm = round(pm * 100, 1) if pm is not None else None
    rev_g = num("revenueGrowth")
    rev_g = round(rev_g * 100, 1) if rev_g is not None else None
    earn_g = num("earningsGrowth")
    earn_g = round(earn_g * 100, 1) if earn_g is not None else None
    sector = (info.get("sector") or "").strip()
    is_fin = any(w in (industry + " " + sector).lower() for w in FIN_WORDS)

    fails, warns = [], []
    if mcap_cr is None:
        fails.append("mcap unknown")
    elif mcap_cr < P["MIN_MCAP_CR"]:
        fails.append(f"mcap {mcap_cr:.0f}cr < {P['MIN_MCAP_CR']:.0f}cr")
    if P["REQUIRE_POSITIVE_EPS"]:
        if eps is None:
            warns.append("EPS unknown")
        elif eps <= 0:
            fails.append("negative EPS")
    if pm is not None and pm <= 0:
        fails.append("loss making")
    if roe is None:
        warns.append("ROE unknown")
    elif roe < P["ROE_MIN"]:
        fails.append(f"ROE {roe:.1f}% < {P['ROE_MIN']:.0f}%")
    if not is_fin:
        if de is None:
            warns.append("D/E unknown")
        elif de > P["DE_MAX"]:
            fails.append(f"D/E {de:.2f} > {P['DE_MAX']:.2f}")
    if rev_g is not None and rev_g < -15:
        warns.append(f"sales {rev_g:.0f}%")
    if earn_g is not None and earn_g < -25:
        warns.append(f"profit {earn_g:.0f}%")

    grade = "A" if not fails and not warns else ("B" if not fails else "C")
    f_score = 0.0
    if roe is not None:
        f_score += min(max(roe, 0), 30) / 30 * 12
    if de is not None:
        f_score += max(0.0, (1.5 - min(de, 1.5))) / 1.5 * 6
    elif is_fin:
        f_score += 3
    if pm is not None:
        f_score += min(max(pm, 0), 25) / 25 * 6
    if rev_g is not None and rev_g > 0:
        f_score += min(rev_g, 25) / 25 * 3
    if earn_g is not None and earn_g > 0:
        f_score += min(earn_g, 40) / 40 * 3

    return dict(mcap_cr=mcap_cr, roe=roe, de=de, eps=eps, pe=round(pe, 1) if pe else None,
                pb=round(pb, 2) if pb else None, profit_margin=pm,
                sales_growth=rev_g, profit_growth=earn_g, sector=sector,
                is_financial=bool(is_fin), fund_grade=grade,
                fund_score=round(f_score, 1),
                fund_fails="; ".join(fails), fund_warns="; ".join(warns))


# -------------------------- Dhan intraday check ----------------------------
def dhan_token():
    return (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()


def dhan_scrip_map(symbols):
    """{symbol: securityId} for NSE equity, from Dhan's scrip master."""
    text = sw.curl_get(DHAN_SCRIP, referer="https://dhan.co/")
    if not text:
        log("Dhan scrip master download failed")
        return {}
    want = set(symbols)
    out = {}
    try:
        rows = csv.DictReader(io.StringIO(text))
        for r in rows:
            r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
            exch = r.get("SEM_EXM_EXCH_ID") or r.get("EXCH_ID") or ""
            seg = (r.get("SEM_SEGMENT") or r.get("SEGMENT") or "").upper()
            inst = (r.get("SEM_INSTRUMENT_NAME") or r.get("INSTRUMENT") or "").upper()
            series = (r.get("SEM_SERIES") or r.get("SERIES") or "").upper()
            sid = r.get("SEM_SMST_SECURITY_ID") or r.get("SECURITY_ID") or ""
            sym = (r.get("SEM_TRADING_SYMBOL") or r.get("TRADING_SYMBOL") or "").upper()
            if exch.upper() != "NSE" or not sid or sym not in want:
                continue
            if seg and seg not in ("E", "EQUITY", "NSE_EQ"):
                continue
            if inst and inst not in ("EQUITY", "ES"):
                continue
            if series and series not in ("EQ", "BE", ""):
                continue
            out.setdefault(sym, sid)
    except Exception as e:
        log("scrip master parse error", e)
    log(f"Dhan security ids mapped for {len(out)}/{len(symbols)} symbols")
    return out


def dhan_intraday(sec_id, day_iso):
    token = dhan_token()
    if not token or not sec_id:
        return None
    body = json.dumps({
        "securityId": str(sec_id), "exchangeSegment": "NSE_EQ", "instrument": "EQUITY",
        "interval": P["DHAN_INTERVAL"], "oi": False,
        "fromDate": day_iso, "toDate": day_iso,
    })
    import subprocess
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "25", "-X", "POST",
             "https://api.dhan.co/v2/charts/intraday",
             "-H", "Content-Type: application/json",
             "-H", "Accept: application/json",
             "-H", "access-token: " + token,
             "--data-binary", "@-"],
            input=body, capture_output=True, text=True, timeout=40)
        d = json.loads(r.stdout)
        if not isinstance(d, dict) or "close" not in d or not d.get("close"):
            log("  dhan: no candles", str(r.stdout)[:160])
            return None
        return d
    except Exception as e:
        log("  dhan error", e)
        return None


def dhan_confirm(candles):
    """VWAP and late-session volume behaviour from intraday candles."""
    try:
        o = [float(x) for x in candles["open"]]
        h = [float(x) for x in candles["high"]]
        lw = [float(x) for x in candles["low"]]
        c = [float(x) for x in candles["close"]]
        v = [float(x) for x in candles["volume"]]
    except Exception:
        return None
    n = len(c)
    if n < 10 or sum(v) <= 0:
        return None
    tp = [(h[i] + lw[i] + c[i]) / 3 for i in range(n)]
    vwap = sum(tp[i] * v[i] for i in range(n)) / sum(v)
    half = n // 2
    first_vol, second_vol = sum(v[:half]), sum(v[half:])
    up_vol = sum(v[i] for i in range(n) if c[i] >= o[i])
    low_idx = int(np.argmin(lw))
    out = dict(
        dhan_vwap=round(vwap, 2),
        dhan_close_vs_vwap_pct=round((c[-1] / vwap - 1) * 100, 2),
        dhan_above_vwap=bool(c[-1] >= vwap),
        dhan_second_half_vol_pct=round(second_vol / (first_vol + second_vol) * 100, 1),
        dhan_up_candle_vol_pct=round(up_vol / sum(v) * 100, 1),
        dhan_low_at_session_pct=round(low_idx / max(n - 1, 1) * 100, 1),
        dhan_candles=n,
    )
    pts = (out["dhan_above_vwap"] * 2
           + (out["dhan_second_half_vol_pct"] >= 50)
           + (out["dhan_up_candle_vol_pct"] >= 50)
           + (out["dhan_low_at_session_pct"] <= 60))
    out["dhan_confirm"] = "STRONG" if pts >= 4 else ("OK" if pts >= 2 else "WEAK")
    out["dhan_score"] = round(pts / 5 * 15, 1)
    return out


# --------------------------------- output ----------------------------------
COLS = ["date", "symbol", "industry", "sector", "score", "fund_grade", "close", "chg_pct",
        "open", "high", "low", "entry", "sl", "risk_pct", "t1", "t2", "rr_t2",
        "vol_ratio", "turnover_cr", "close_pos", "lower_wick", "open_high_gap_pct",
        "above_52w_low_pct", "from_52w_high_pct", "days_since_52w_low", "rsi",
        "mcap_cr", "roe", "de", "pe", "pb", "profit_margin", "sales_growth",
        "profit_growth", "headroom_r", "dhan_confirm", "dhan_above_vwap",
        "dhan_second_half_vol_pct", "dhan_up_candle_vol_pct", "fund_warns", "reasons"]


def save(picks, stats, date_iso, date_disp):
    os.makedirs(OUT_DIR, exist_ok=True)
    out = dict(date_iso=date_iso, date_disp=date_disp,
               generated_at=datetime.now(IST).isoformat(timespec="seconds"),
               params=P, stats=stats, picks=picks[:P["TOP_N"]], all_passed=len(picks))
    json.dump(out, open(os.path.join(OUT_DIR, "reversal_scan.json"), "w"), indent=1,
              default=str)
    rows = [[date_iso] + [p.get(c, "") for c in COLS[1:]] for p in picks[:P["TOP_N"]]]
    with open(os.path.join(OUT_DIR, "reversal_scan_latest.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(rows)
    hist = os.path.join(OUT_DIR, "reversal_scan_history.csv")
    existing = set()
    if os.path.exists(hist):
        with open(hist, newline="") as f:
            existing = {(r[0], r[1]) for r in csv.reader(f) if r and r[0] != "date"}
    need_header = not os.path.exists(hist) or os.path.getsize(hist) == 0
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(COLS)
        for r in rows:
            if (str(r[0]), str(r[1])) not in existing:
                w.writerow(r)


def _f(x, suffix=""):
    return "n/a" if x is None else f"{x}{suffix}"


def telegram_text(date_disp, picks, stats):
    head = (f"<b>🔄 SKT 52W-Low Reversal Scan — {date_disp}</b>\n"
            f"Scanned {stats['scanned']} | open=high + vol absorption: {stats['tech_pass']} "
            f"| fundamentally sound: {stats['fund_pass']}\n"
            f"Setup: 52W low pe open=high (dikhawa) + {P['VOL_MULT']}x volume pe absorption\n\n")
    if not picks:
        return head + ("Aaj ek bhi stock poore filters pass nahi kar paya. "
                       "Is setup me patience hi edge hai. 🙂")
    lines = []
    for i, p in enumerate(picks[:P["TOP_N"]], 1):
        dh = p.get("dhan_confirm") or "n/a"
        lines.append(
            f"<b>{i}. {p['symbol']}</b> ({(p.get('industry') or '')[:16]}) — "
            f"score {p['score']} | fund {p['fund_grade']}\n"
            f"   CMP ₹{p['close']} ({p['chg_pct']:+.1f}%) | 52W low se +{p['above_52w_low_pct']}%\n"
            f"   Vol {p['vol_ratio']}x | wick {int(p['lower_wick'] * 100)}% | "
            f"close {int(p['close_pos'] * 100)}% of range | VWAP: {dh}\n"
            f"   <b>Entry &gt; ₹{p['entry']}</b> | SL ₹{p['sl']} ({p['risk_pct']}%) | "
            f"T1 ₹{p['t1']} | T2 ₹{p['t2']} (1:{p['rr_t2']})\n"
            f"   mcap ₹{p['mcap_cr']:.0f}cr | ROE {_f(p['roe'], '%')} | D/E {_f(p['de'])}"
            + (f" | ⚠ {p['fund_warns']}" if p.get("fund_warns") else ""))
    foot = ("\n\n<i>Counter-trend setup hai: entry sirf tab jab price us din ka high "
            "cross kare (kal ya uske baad), warna setup fail. SL closing basis pe, "
            "position size risk % se. Research shortlist only, not a recommendation.</i>")
    return head + "\n\n".join(lines) + foot


def already_done(date_iso):
    if os.environ.get("FORCE", "false").lower() == "true":
        return False
    try:
        old = json.load(open(os.path.join(OUT_DIR, "reversal_scan.json")))
        return old.get("date_iso") == date_iso
    except Exception:
        return False


# ---------------------------------- main -----------------------------------
def main():
    universe = load_universe()
    log(f"universe: {len(universe)} symbols")
    frames, _ = sw.download_prices(list(universe))
    log(f"price data for {len(frames)} symbols")
    if len(frames) < 200:
        sys.exit("Too few symbols downloaded — aborting (yfinance issue?)")
    bdate, bhav = sw.latest_bhav()
    frames, patched = sw.patch_with_bhav(frames, bdate, bhav)
    last = max(df.index[-1] for df in frames.values())
    date_iso, date_disp = last.strftime("%Y-%m-%d"), last.strftime("%d %b %Y")
    if already_done(date_iso):
        log(f"{date_iso} already scanned and sent -> skipping")
        return
    frames = {s: d for s, d in frames.items() if d.index[-1] == last}

    # ---- technical screen ----
    tech = []
    for sym, df in frames.items():
        try:
            r = analyse(sym, df, universe.get(sym, ""))
            if r:
                tech.append(r)
        except Exception as e:
            log("skip", sym, e)
    tech.sort(key=lambda x: -x["tech_score"])
    log(f"technical pass: {len(tech)} -> {[t['symbol'] for t in tech]}")

    # ---- fundamental screen (only the survivors) ----
    picks = []
    for t in tech[:40]:
        f = fundamentals(t["symbol"], t["industry"])
        t.update(f)
        keep = f["fund_grade"] in P["ALLOW_GRADES"]
        log(f"  {t['symbol']:<12} grade {f['fund_grade']} mcap {f['mcap_cr']} "
            f"ROE {f['roe']} D/E {f['de']} {'KEEP' if keep else 'DROP: ' + f['fund_fails']}")
        if keep:
            picks.append(t)
    log(f"fundamental pass: {len(picks)}")

    # ---- Dhan intraday confirmation (optional) ----
    if picks and dhan_token():
        ids = dhan_scrip_map([p["symbol"] for p in picks])
        for p in picks:
            cs = dhan_intraday(ids.get(p["symbol"]), date_iso)
            conf = dhan_confirm(cs) if cs else None
            if conf:
                p.update(conf)
            else:
                p["dhan_confirm"] = "n/a"
            time.sleep(0.6)
    else:
        for p in picks:
            p["dhan_confirm"] = "n/a" if not dhan_token() else "no-data"
        if picks:
            log("DHAN_ACCESS_TOKEN not set -> skipping intraday VWAP confirmation")

    # ---- final score & reasons ----
    for p in picks:
        p["score"] = round(p["tech_score"] + p.get("fund_score", 0)
                           + p.get("dhan_score", 0), 1)
        bits = [f"52W low +{p['above_52w_low_pct']}%",
                f"open=high ({p['open_high_gap_pct']}%)",
                f"vol {p['vol_ratio']}x",
                f"wick {int(p['lower_wick'] * 100)}%",
                f"close {int(p['close_pos'] * 100)}% of range"]
        if p.get("dhan_confirm") not in (None, "n/a", "no-data"):
            bits.append(f"VWAP {p['dhan_confirm']}")
        bits.append(f"fund {p['fund_grade']}")
        p["reasons"] = ", ".join(bits)
    picks.sort(key=lambda x: -x["score"])

    stats = dict(scanned=len(frames), tech_pass=len(tech), fund_pass=len(picks),
                 dhan=bool(dhan_token()))
    for p in picks[:P["TOP_N"]]:
        log(f"  {p['symbol']:<12} score {p['score']:>5}  entry {p['entry']:>9}  "
            f"SL {p['sl']:>9}  {p['reasons']}")
    save(picks, stats, date_iso, date_disp)
    sw.send_telegram(telegram_text(date_disp, picks, stats))


if __name__ == "__main__":
    main()
