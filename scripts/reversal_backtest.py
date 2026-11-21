#!/usr/bin/env python3
"""
Backtest / tuning harness for the 52W-low reversal setup.
Run manually (workflow_dispatch). Does three things:

  1. FUNNEL  : how many symbol-days survive each filter over 2 years, so we can
               see which condition is the binding one.
  2. HISTORY : every signal the technical screen would have fired, with what
               happened next -- did price trigger above that day's high, and
               then did T1 (1.5R) come before the stop.
  3. TODAY   : same funnel for the latest bar only.

Fundamentals are NOT applied here (too many API calls for a 2-year sweep),
so these numbers are the technical setup's raw behaviour.
Output: data/reversal_backtest.json + the run log.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing_scanner as sw
import reversal_scanner as rev

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = "data"
P = rev.P
TRIGGER_WINDOW = int(os.environ.get("TRIGGER_WINDOW", 5))    # days to cross the high
TRADE_WINDOW = int(os.environ.get("TRADE_WINDOW", 20))       # days to reach T1 / SL

STAGES = ["bars", "price>=100", "near 52W low", "open=high", "volume spike",
          "turnover", "range", "close position", "lower wick", "not freefall",
          "risk% ok", "headroom ok"]


def log(*a):
    print(*a, flush=True)


def columns(df):
    o, h, l, c, v = df["Open"], df["High"], df["Low"], df["Close"], df["Volume"]
    out = pd.DataFrame(index=df.index)
    out["o"], out["h"], out["l"], out["c"], out["v"] = o, h, l, c, v
    out["lo52"] = l.rolling(250, min_periods=200).min()
    out["above_low"] = (c / out["lo52"] - 1) * 100
    out["oh_gap"] = (h - o) / o * 100
    out["vol20"] = v.rolling(20).mean().shift(1)
    out["vol_ratio"] = v / out["vol20"]
    out["turnover"] = c * v / 1e7
    rng = (h - l).replace(0, np.nan)
    out["range_pct"] = rng / l * 100
    out["close_pos"] = (c - l) / rng
    out["wick"] = (pd.concat([o, c], axis=1).min(axis=1) - l) / rng
    out["chg"] = (c / c.shift(1) - 1) * 100
    _, atr = sw.adx(h, l, c)
    out["atr"] = atr
    out["entry"] = h * (1 + P["ENTRY_BUFFER_PCT"] / 100)
    out["sl"] = l - P["ATR_SL_BUFFER"] * atr
    out["risk"] = out["entry"] - out["sl"]
    out["risk_pct"] = out["risk"] / out["entry"] * 100
    hi20 = h.rolling(20).max().shift(1)
    hi50 = h.rolling(50).max().shift(1)
    out["res"] = np.where(hi20 > out["entry"] * 1.01, hi20, hi50)
    out["headroom"] = (out["res"] - out["entry"]) / out["risk"]
    out["t1"] = out["entry"] + 1.5 * out["risk"]
    return out


def masks(x):
    """Cumulative filter masks, in the same order as the live scanner."""
    m = [
        x["lo52"].notna() & x["vol20"].notna() & x["atr"].notna() & (x["range_pct"] > 0),
        x["c"] >= P["MIN_PRICE"],
        x["above_low"] <= P["NEAR_52W_LOW_PCT"],
        (x["oh_gap"] <= P["OPEN_HIGH_TOL_PCT"]) & (x["c"] < x["o"]),
        x["vol_ratio"] >= P["VOL_MULT"],
        x["turnover"] >= P["MIN_TURNOVER_CR"],
        x["range_pct"] >= P["MIN_RANGE_PCT"],
        x["close_pos"] >= P["CLOSE_POS_MIN"],
        x["wick"] >= P["LOWER_WICK_MIN"],
        x["chg"] >= -P["MAX_FALL_PCT"],
        x["risk_pct"] <= P["MAX_RISK_PCT"],
        (x["res"] > x["entry"]) & (x["headroom"] >= P["MIN_HEADROOM_R"]),
    ]
    cum, acc = [], pd.Series(True, index=x.index)
    for mi in m:
        acc = acc & mi.fillna(False)
        cum.append(acc.copy())
    return cum


def forward(x, i):
    """What happened after a signal on row i. Returns a result dict."""
    n = len(x)
    entry, sl, t1 = x["entry"].iloc[i], x["sl"].iloc[i], x["t1"].iloc[i]
    risk = x["risk"].iloc[i]
    trig = None
    for j in range(i + 1, min(i + 1 + TRIGGER_WINDOW, n)):
        if x["h"].iloc[j] >= entry:
            trig = j
            break
    if trig is None:
        return dict(outcome="no_trigger", r=0.0, days=0)
    for j in range(trig, min(trig + TRADE_WINDOW, n)):
        hit_sl = x["l"].iloc[j] <= sl
        hit_t1 = x["h"].iloc[j] >= t1
        if hit_sl and hit_t1:                      # same bar -> assume the stop
            return dict(outcome="sl", r=-1.0, days=j - trig)
        if hit_sl:
            return dict(outcome="sl", r=-1.0, days=j - trig)
        if hit_t1:
            return dict(outcome="t1", r=1.5, days=j - trig)
    last = x["c"].iloc[min(trig + TRADE_WINDOW, n) - 1]
    return dict(outcome="open", r=round(float((last - entry) / risk), 2),
                days=TRADE_WINDOW)


def main():
    universe = rev.load_universe()
    log(f"universe: {len(universe)} symbols")
    frames = {}
    import yfinance as yf
    syms = [s + ".NS" for s in universe]
    for i in range(0, len(syms), 100):
        part = syms[i:i + 100]
        try:
            df = yf.download(part, period="2y", interval="1d", group_by="ticker",
                             auto_adjust=False, threads=True, progress=False)
        except Exception as e:
            log("download error", e)
            continue
        for t in part:
            try:
                sub = df[t].dropna(subset=["Close"]) if isinstance(df.columns, pd.MultiIndex) else df
                if len(sub) >= 260:
                    frames[t[:-3]] = sub
            except KeyError:
                pass
        log(f"downloaded {min(i + 100, len(syms))}/{len(syms)}")
    log(f"usable history for {len(frames)} symbols")

    funnel = np.zeros(len(STAGES), dtype=int)
    today_funnel = np.zeros(len(STAGES), dtype=int)
    signals = []
    for sym, df in frames.items():
        try:
            x = columns(df)
            cum = masks(x)
            for k, mk in enumerate(cum):
                funnel[k] += int(mk.sum())
                today_funnel[k] += int(bool(mk.iloc[-1]))
            hits = np.where(cum[-1].values)[0]
            for i in hits:
                if i >= len(x) - 2:          # too fresh to judge
                    continue
                f = forward(x, int(i))
                signals.append(dict(
                    date=str(x.index[i].date()), symbol=sym,
                    close=round(float(x["c"].iloc[i]), 2),
                    above_low=round(float(x["above_low"].iloc[i]), 2),
                    vol_ratio=round(float(x["vol_ratio"].iloc[i]), 2),
                    wick=round(float(x["wick"].iloc[i]), 2),
                    close_pos=round(float(x["close_pos"].iloc[i]), 2),
                    entry=round(float(x["entry"].iloc[i]), 2),
                    sl=round(float(x["sl"].iloc[i]), 2),
                    risk_pct=round(float(x["risk_pct"].iloc[i]), 2),
                    headroom=round(float(x["headroom"].iloc[i]), 2), **f))
        except Exception as e:
            log("skip", sym, e)

    signals.sort(key=lambda s: s["date"])
    n = len(signals)
    trig = [s for s in signals if s["outcome"] != "no_trigger"]
    t1 = [s for s in trig if s["outcome"] == "t1"]
    slh = [s for s in trig if s["outcome"] == "sl"]
    opn = [s for s in trig if s["outcome"] == "open"]
    avg_r = round(float(np.mean([s["r"] for s in trig])), 2) if trig else 0.0

    log("\n================ FUNNEL (2 years, all symbol-days) ================")
    for name, cnt in zip(STAGES, funnel):
        log(f"  {name:<18} {cnt:>9,}")
    log("\n================ FUNNEL (latest bar only) ================")
    for name, cnt in zip(STAGES, today_funnel):
        log(f"  {name:<18} {cnt:>5}")

    log(f"\n================ SIGNALS: {n} over ~2 years ================")
    if n:
        log(f"  triggered above the high : {len(trig)} ({len(trig)/n*100:.0f}%)")
        if trig:
            log(f"  T1 (1.5R) hit            : {len(t1)} ({len(t1)/len(trig)*100:.0f}% of triggered)")
            log(f"  stopped out              : {len(slh)} ({len(slh)/len(trig)*100:.0f}%)")
            log(f"  still open after {TRADE_WINDOW}d     : {len(opn)}")
            log(f"  average R per triggered   : {avg_r}")
        log("\n  last 25 signals:")
        for s in signals[-25:]:
            log(f"   {s['date']} {s['symbol']:<12} +{s['above_low']:>5}% off low  "
                f"vol {s['vol_ratio']:>4}x  wick {s['wick']:>4}  entry {s['entry']:>8} "
                f"risk {s['risk_pct']:>4}%  -> {s['outcome']:<10} {s['r']:>5}R")
    else:
        log("  no signals at all -> the filter set is too tight")

    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(dict(generated_at=datetime.now(IST).isoformat(timespec="seconds"),
                   params=P, trigger_window=TRIGGER_WINDOW, trade_window=TRADE_WINDOW,
                   symbols=len(frames), stages=STAGES,
                   funnel=funnel.tolist(), today_funnel=today_funnel.tolist(),
                   signals=signals,
                   summary=dict(signals=n, triggered=len(trig), t1=len(t1),
                                sl=len(slh), open=len(opn), avg_r=avg_r)),
              open(os.path.join(OUT_DIR, "reversal_backtest.json"), "w"), indent=1)
    log("\nsaved data/reversal_backtest.json")


if __name__ == "__main__":
    main()
