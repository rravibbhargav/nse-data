#!/usr/bin/env python3
"""
Parameter sweep for the 52W-low reversal setup.

Downloads 2 years of Nifty Total Market history once, then re-tests the
technical screen across combinations of the four filters that actually bite
(volume multiple, close position in range, lower-wick share, how close to the
52-week low) and three target sizes. Prints an expectancy table so the
parameters in reversal_scanner.py can be set from evidence instead of taste.

Expectancy is per TRIGGERED trade, in R:
    win  -> +target R   (T hit before the stop)
    loss -> -1R         (stop hit first; same-bar hits counted as the stop)
    open -> marked-to-market after TRADE_WINDOW days
Technical screen only -- the fundamental filter is not applied here.
Output: data/reversal_sweep.json + the run log.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from itertools import product

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing_scanner as sw
import reversal_scanner as rev
import reversal_backtest as bt

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = "data"
P = rev.P

VOLS = [2.0, 2.5, 3.0]
CLOSE_POS = [0.45, 0.55, 0.65]
WICKS = [0.40, 0.55]
NEAR_LOWS = [4.0, 7.0, 10.0]
TARGETS = [1.0, 1.5, 2.0]
TRIGGER_WINDOW = int(os.environ.get("TRIGGER_WINDOW", 5))
TRADE_WINDOW = int(os.environ.get("TRADE_WINDOW", 20))


def log(*a):
    print(*a, flush=True)


def forward(x, i, target_r):
    """Outcome of one signal for a given target multiple."""
    n = len(x)
    entry, sl = float(x["entry"].iloc[i]), float(x["sl"].iloc[i])
    risk = entry - sl
    tgt = entry + target_r * risk
    trig = None
    for j in range(i + 1, min(i + 1 + TRIGGER_WINDOW, n)):
        if float(x["h"].iloc[j]) >= entry:
            trig = j
            break
    if trig is None:
        return None
    for j in range(trig, min(trig + TRADE_WINDOW, n)):
        hit_sl = float(x["l"].iloc[j]) <= sl
        hit_t = float(x["h"].iloc[j]) >= tgt
        if hit_sl:
            return -1.0
        if hit_t:
            return float(target_r)
    last = float(x["c"].iloc[min(trig + TRADE_WINDOW, n) - 1])
    return (last - entry) / risk


def main():
    universe = rev.load_universe()
    log(f"universe: {len(universe)} symbols")
    import yfinance as yf
    syms = [s + ".NS" for s in universe]
    cols = {}
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
                    cols[t[:-3]] = bt.columns(sub)
            except KeyError:
                pass
        log(f"prepared {min(i + 100, len(syms))}/{len(syms)}")
    log(f"usable history for {len(cols)} symbols\n")

    # conditions that never change across the sweep, computed once
    base = {}
    for sym, x in cols.items():
        try:
            ok = (x["lo52"].notna() & x["vol20"].notna() & x["atr"].notna()
                  & (x["range_pct"] > 0)
                  & (x["c"] >= P["MIN_PRICE"])
                  & (x["oh_gap"] <= P["OPEN_HIGH_TOL_PCT"]) & (x["c"] < x["o"])
                  & (x["turnover"] >= P["MIN_TURNOVER_CR"])
                  & (x["range_pct"] >= P["MIN_RANGE_PCT"])
                  & (x["chg"] >= -P["MAX_FALL_PCT"])
                  & (x["risk_pct"] <= P["MAX_RISK_PCT"])
                  & (x["res"] > x["entry"])
                  & (x["headroom"] >= P["MIN_HEADROOM_R"]))
            base[sym] = ok.fillna(False)
        except Exception as e:
            log("skip", sym, e)

    results = []
    for vol, cp, wk, nl in product(VOLS, CLOSE_POS, WICKS, NEAR_LOWS):
        sigs = []           # (symbol, row index)
        for sym, x in cols.items():
            if sym not in base:
                continue
            m = (base[sym]
                 & (x["vol_ratio"] >= vol).fillna(False)
                 & (x["close_pos"] >= cp).fillna(False)
                 & (x["wick"] >= wk).fillna(False)
                 & (x["above_low"] <= nl).fillna(False))
            for i in np.where(m.values)[0]:
                if i < len(x) - 2:
                    sigs.append((sym, int(i)))
        row = dict(vol=vol, close_pos=cp, wick=wk, near_low=nl, signals=len(sigs))
        for tr in TARGETS:
            rs = []
            for sym, i in sigs:
                r = forward(cols[sym], i, tr)
                if r is not None:
                    rs.append(r)
            if rs:
                wins = sum(1 for r in rs if r >= tr - 1e-9)
                row[f"n_{tr}"] = len(rs)
                row[f"win_{tr}"] = round(wins / len(rs) * 100, 1)
                row[f"exp_{tr}"] = round(float(np.mean(rs)), 3)
            else:
                row[f"n_{tr}"], row[f"win_{tr}"], row[f"exp_{tr}"] = 0, 0.0, 0.0
        results.append(row)
        log(f"vol {vol} cp {cp} wick {wk} low<{nl}%  sig {len(sigs):>4}  "
            + "  ".join(f"T{tr}: n{row[f'n_{tr}']:>3} win {row[f'win_{tr}']:>5}% "
                        f"exp {row[f'exp_{tr}']:>+6.3f}R" for tr in TARGETS))

    log("\n================ BEST BY EXPECTANCY (min 25 triggered trades) ================")
    ranked = []
    for r in results:
        for tr in TARGETS:
            if r[f"n_{tr}"] >= 25:
                ranked.append((r[f"exp_{tr}"], tr, r))
    ranked.sort(key=lambda z: -z[0])
    for exp, tr, r in ranked[:15]:
        log(f"  exp {exp:+.3f}R  target {tr}R  | vol {r['vol']}x  close_pos {r['close_pos']}  "
            f"wick {r['wick']}  52W low <{r['near_low']}%  | signals {r['signals']}, "
            f"triggered {r[f'n_{tr}']}, win {r[f'win_{tr}']}%")
    if not ranked:
        log("  no combination produced 25+ triggered trades")

    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(dict(generated_at=datetime.now(IST).isoformat(timespec="seconds"),
                   symbols=len(cols), trigger_window=TRIGGER_WINDOW,
                   trade_window=TRADE_WINDOW, fixed_params=P, results=results),
              open(os.path.join(OUT_DIR, "reversal_sweep.json"), "w"), indent=1)
    log("\nsaved data/reversal_sweep.json")


if __name__ == "__main__":
    main()
