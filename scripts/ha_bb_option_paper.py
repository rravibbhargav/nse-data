#!/usr/bin/env python3
"""SKT HA + Bollinger option-buying PAPER engine (1-min Heikin Ashi).

Strategy (user's rules):
  Pre-market: note Nifty / Sensex spot -> nearest strike (Nifty 50, Sensex 100).
  Watch that strike's CE and PE of the current expiry, 1-min Heikin Ashi chart.
  Bollinger(20, 2). Premium touches the LOWER band, then the first GREEN HA candle
  forms -> buy above that candle's HIGH.
  SL = swing low (HA low of the preceding RED candle) - 1 point.
  2 lots: lot 1 exits at 1:2. Then lot 2 SL -> cost to cost, lot 2 exits at 1:3.
  If SL hits in BOTH the CE and the PE on the same day -> no new trade that day.

PAPER ONLY. No orders are placed. Dhan option-chain is polled for spot + premiums
and 1-min candles are built from those samples.

Env: DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

DHAN_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")


def _client_id_from_token(tok):
    """The Dhan JWT carries dhanClientId in its payload, so no extra secret is needed."""
    try:
        import base64
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return str(json.loads(base64.urlsafe_b64decode(p)).get("dhanClientId", ""))
    except Exception:
        return ""


DHAN_CLIENT = os.environ.get("DHAN_CLIENT_ID", "") or _client_id_from_token(DHAN_TOKEN)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

BB_N, BB_K = 20, 2.0
RR1, RR2 = 2.0, 3.0
SL_BUFFER = 1.0
TICK = 0.05
POLL_GAP = 3.6          # seconds between any two option-chain calls (limit ~1 / 3s)
LOG_CSV = "data/ha_bb_paper_trades.csv"

SYMBOLS = {
    "NIFTY":  dict(scrip=13, seg="IDX_I", step=50,  lot=65),
    "SENSEX": dict(scrip=51, seg="IDX_I", step=100, lot=20),
}

OPEN_T = (9, 15)
CLOSE_T = (15, 30)
SPOT_T = (9, 7)         # pre-open window is 09:00-09:08; price is settled by ~09:07


def now():
    return datetime.now(IST)


def log(*a):
    print(f"[{now():%H:%M:%S}]", *a, flush=True)


def tg(msg):
    log("TG:", msg.replace("\n", " | ")[:300])
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        subprocess.run(["curl", "-s", "-o", "/dev/null", "-m", "20", "-X", "POST",
                        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                        "--form-string", f"chat_id={TG_CHAT}",
                        "--form-string", "parse_mode=HTML",
                        "--form-string", f"text={msg}"],
                       capture_output=True, timeout=30)
    except Exception as e:
        log("tg fail", e)


def dhan(path, body, retries=3):
    data = json.dumps(body)
    for i in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "30", "-X", "POST",
                 f"https://api.dhan.co/v2{path}",
                 "-H", "Content-Type: application/json",
                 "-H", "Accept: application/json",
                 "-H", f"access-token: {DHAN_TOKEN}",
                 "-H", f"client-id: {DHAN_CLIENT}",
                 "--data-binary", data],
                capture_output=True, text=True, timeout=45)
            d = json.loads(r.stdout)
            if isinstance(d, dict) and d.get("status") == "failed":
                log("api failed", path, str(d)[:140])
                time.sleep(4)
                continue
            return d
        except Exception as e:
            log("api err", path, type(e).__name__, str(e)[:90])
            time.sleep(4)
    return None


# ------------------------------------------------------------------ candles
class Series:
    """Builds 1-min OHLC from price samples, then HA + Bollinger."""

    def __init__(self, name):
        self.name = name
        self.bars = []          # completed: dict(t,o,h,l,c)
        self.cur = None
        self.cur_min = None

    def sample(self, px, ts):
        m = ts.replace(second=0, microsecond=0)
        if self.cur_min != m:
            if self.cur:
                self.bars.append(self.cur)
            self.cur_min = m
            self.cur = dict(t=m, o=px, h=px, l=px, c=px)
            return True          # a bar just completed
        c = self.cur
        c["h"] = max(c["h"], px)
        c["l"] = min(c["l"], px)
        c["c"] = px
        return False

    def ha(self):
        out = []
        for i, b in enumerate(self.bars):
            hc = (b["o"] + b["h"] + b["l"] + b["c"]) / 4.0
            ho = (b["o"] + b["c"]) / 2.0 if i == 0 else (out[-1]["ho"] + out[-1]["hc"]) / 2.0
            out.append(dict(t=b["t"], ho=ho, hc=hc,
                            hh=max(b["h"], ho, hc), hl=min(b["l"], ho, hc),
                            o=b["o"], h=b["h"], l=b["l"], c=b["c"],
                            green=hc > ho))
        return out

    def lower_band(self, ha, i):
        if i + 1 < BB_N:
            return None
        w = [x["hc"] for x in ha[i + 1 - BB_N:i + 1]]
        m = sum(w) / BB_N
        var = sum((x - m) ** 2 for x in w) / BB_N
        return m - BB_K * (var ** 0.5)


# ------------------------------------------------------------------ trades
class Trade:
    def __init__(self, sym, strike, opt, entry, sl, lot):
        self.sym, self.strike, self.opt = sym, strike, opt
        self.entry, self.sl, self.lot = entry, sl, lot
        self.risk = round(entry - sl, 2)
        self.t1 = round(entry + RR1 * self.risk, 2)
        self.t2 = round(entry + RR2 * self.risk, 2)
        self.lot2_sl = sl
        self.filled = False
        self.lot1_exit = None
        self.lot2_exit = None
        self.signal_time = now().strftime("%H:%M")
        self.fill_time = None
        self.outcome = None

    def head(self):
        return f"<b>{self.sym} {self.strike} {self.opt}</b>"

    def alert_signal(self):
        tg(f"\U0001f4e2 SIGNAL (paper)\n{self.head()}\n"
           f"Buy above: {self.entry}\nSL: {self.sl}  (risk {self.risk})\n"
           f"T1 1:2 = {self.t1}\nT2 1:3 = {self.t2}\n"
           f"Qty: 2 x {self.lot} = {2*self.lot}")

    def on_bar(self, bar):
        """Returns True when the trade is finished."""
        hi, lo, cl = bar["h"], bar["l"], bar["c"]
        if not self.filled:
            if hi >= self.entry:
                self.filled = True
                self.fill_time = bar["t"].strftime("%H:%M")
                tg(f"✅ ENTRY filled (paper)\n{self.head()} @ {self.entry}\n"
                   f"SL {self.sl} | T1 {self.t1} | T2 {self.t2}")
            else:
                return False
        # stop checked before target inside the same candle (conservative)
        if self.lot1_exit is None:
            if lo <= self.sl:
                self.lot1_exit = self.lot2_exit = self.sl
                self.outcome = "SL hit"
                tg(f"❌ SL HIT (paper)\n{self.head()} @ {self.sl}\n"
                   f"Loss: {round((self.sl-self.entry)*self.lot*2,2)}")
                return True
            if hi >= self.t1:
                self.lot1_exit = self.t1
                self.lot2_sl = self.entry
                tg(f"\U0001f3af T1 HIT 1:2 (paper)\n{self.head()} @ {self.t1}\n"
                   f"Lot 1 booked. Lot 2 SL -> cost to cost ({self.entry}). Target {self.t2}")
            return False
        if lo <= self.lot2_sl:
            self.lot2_exit = self.lot2_sl
            self.outcome = "T1 hit, lot2 cost-to-cost"
            tg(f"⚪ Lot 2 exited at cost to cost (paper)\n{self.head()} @ {self.lot2_sl}")
            return True
        if hi >= self.t2:
            self.lot2_exit = self.t2
            self.outcome = "T2 hit"
            tg(f"\U0001f3c6 T2 HIT 1:3 (paper)\n{self.head()} @ {self.t2}\n"
               f"Total: {round((self.lot1_exit-self.entry)*self.lot + (self.t2-self.entry)*self.lot,2)}")
            return True
        return False

    def force_close(self, px):
        if not self.filled:
            return False
        if self.lot1_exit is None:
            self.lot1_exit = self.lot2_exit = px
        elif self.lot2_exit is None:
            self.lot2_exit = px
        self.outcome = (self.outcome or "") + " EOD close"
        return True

    def gross(self):
        if self.lot1_exit is None:
            return 0.0
        return round(((self.lot1_exit - self.entry) + (self.lot2_exit - self.entry)) * self.lot, 2)

    def row(self, day):
        return [day, self.sym, self.strike, self.opt, self.signal_time, self.fill_time or "",
                self.entry, self.sl, self.risk, self.t1, self.t2,
                self.lot1_exit if self.lot1_exit is not None else "",
                self.lot2_exit if self.lot2_exit is not None else "",
                2 * self.lot, self.gross(), self.outcome or "no fill"]


# ------------------------------------------------------------------ engine
def nearest_expiry(sym):
    c = SYMBOLS[sym]
    d = dhan("/optionchain/expirylist", {"UnderlyingScrip": c["scrip"], "UnderlyingSeg": c["seg"]})
    if not d or not d.get("data"):
        return None
    today = now().date().isoformat()
    for e in sorted(d["data"]):
        if e >= today:
            return e
    return None


def chain(sym, expiry):
    c = SYMBOLS[sym]
    return dhan("/optionchain", {"UnderlyingScrip": c["scrip"], "UnderlyingSeg": c["seg"],
                                 "Expiry": expiry})


def strike_key(oc, strike):
    for k in oc:
        try:
            if abs(float(k) - strike) < 0.01:
                return k
        except ValueError:
            continue
    return None


def main():
    if not DHAN_TOKEN:
        tg("⚠️ HA-BB paper engine: DHAN_ACCESS_TOKEN missing, not starting.")
        sys.exit(1)

    day = now().date().isoformat()
    state = {}
    for sym in SYMBOLS:
        exp = nearest_expiry(sym)
        time.sleep(POLL_GAP)
        if not exp:
            log("no expiry for", sym)
            continue
        state[sym] = dict(expiry=exp, strike=None, keys={}, spot=None,
                          series={}, trades=[], open={}, sl_hits=set(), stopped=False,
                          armed={})
        log(sym, "expiry", exp)

    # ---- wait for pre-market spot capture time
    while now().hour * 60 + now().minute < SPOT_T[0] * 60 + SPOT_T[1]:
        time.sleep(20)

    lines = []
    for sym, st in state.items():
        c = chain(sym, st["expiry"])
        time.sleep(POLL_GAP)
        if not c or not c.get("data"):
            log("no chain", sym)
            continue
        spot = c["data"].get("last_price")
        step = SYMBOLS[sym]["step"]
        strike = round(spot / step) * step
        k = strike_key(c["data"].get("oc", {}), strike)
        if not k:
            log("strike not in chain", sym, strike)
            continue
        st.update(spot=spot, strike=int(strike), key=k)
        st["series"] = {"CE": Series(f"{sym} {int(strike)} CE"),
                        "PE": Series(f"{sym} {int(strike)} PE")}
        lines.append(f"{sym}: spot {spot} -> <b>{int(strike)}</b> CE &amp; PE (exp {st['expiry']})")

    if not lines:
        tg("⚠️ HA-BB paper: could not build watchlist today.")
        sys.exit(1)

    late = ""
    if now().hour * 60 + now().minute > SPOT_T[0] * 60 + SPOT_T[1] + 3:
        late = f"\n⚠️ Late start — spot taken at {now():%H:%M}, not pre-market."
    tg("\U0001f514 <b>HA + Bollinger paper test</b>\n" + "\n".join(lines) + late +
       f"\n\n1-min Heikin Ashi, BB({BB_N},{int(BB_K)})\nLower band touch → first green HA candle → buy above its high"
       f"\nSL = swing low − {int(SL_BUFFER)} | Lot1 1:2, Lot2 1:3 cost-to-cost"
       "\n\n<i>Paper only. No orders placed.</i>")

    # ---- wait for the open
    while now().hour * 60 + now().minute < OPEN_T[0] * 60 + OPEN_T[1]:
        time.sleep(5)

    # sanity check: compare the pre-open spot we used against the spot just after open.
    # If the pre-open figure was stale (previous close), this is how we find out.
    time.sleep(45)
    checks = []
    for sym, st in state.items():
        if not st.get("strike"):
            continue
        c = chain(sym, st["expiry"])
        time.sleep(POLL_GAP)
        if c and c.get("data"):
            o = c["data"].get("last_price")
            if o:
                checks.append(f"{sym}: pre-open {st['spot']} → after open {o} "
                              f"(diff {round(o - st['spot'], 2)})")
    if checks:
        tg("\U0001f50e <b>Spot check</b>\n" + "\n".join(checks) +
           "\n<i>Strike pre-open price se hi fix rakha gaya hai.</i>")

    log("engine running")
    last_px = {}
    while now().hour * 60 + now().minute < CLOSE_T[0] * 60 + CLOSE_T[1]:
        for sym, st in state.items():
            if not st.get("strike"):
                continue
            c = chain(sym, st["expiry"])
            time.sleep(POLL_GAP)
            if not c or not c.get("data"):
                continue
            node = c["data"].get("oc", {}).get(st["key"], {})
            ts = now()
            for opt in ("CE", "PE"):
                leg = node.get(opt.lower()) or {}
                px = leg.get("last_price")
                if not px:
                    continue
                last_px[(sym, opt)] = px
                ser = st["series"][opt]
                completed = ser.sample(float(px), ts)
                if completed:
                    handle_bar(st, sym, opt, ser)
        # end-of-day guard
        if st_all_stopped(state):
            log("all symbols stopped for the day")
            break

    # ---- square off and report
    rows = []
    for sym, st in state.items():
        for opt, tr in list(st.get("open", {}).items()):
            px = last_px.get((sym, opt))
            if px and tr.force_close(float(px)):
                tg(f"\U0001f534 EOD square-off (paper)\n{tr.head()} @ {px}")
            st["trades"].append(tr)
            st["open"].pop(opt, None)
        rows += [tr.row(day) for tr in st["trades"]]

    write_log(rows)
    report(state, day)


def st_all_stopped(state):
    live = [st for st in state.values() if st.get("strike")]
    return bool(live) and all(st["stopped"] for st in live)


def handle_bar(st, sym, opt, ser):
    """Called when a 1-min candle has just completed."""
    ha = ser.ha()
    if not ha:
        return
    i = len(ha) - 1
    bar = ha[i]

    # manage an open trade on this leg
    tr = st["open"].get(opt)
    if tr is not None:
        if tr.on_bar(bar):
            st["trades"].append(tr)
            st["open"].pop(opt, None)
            if tr.outcome == "SL hit":
                st["sl_hits"].add(opt)
                if {"CE", "PE"} <= st["sl_hits"]:
                    st["stopped"] = True
                    tg(f"\U0001f6d1 <b>{sym} day stop</b>\nCE and PE both hit SL. "
                       f"No new trade today (as per rules).")
        return

    if st["stopped"]:
        return

    lower = ser.lower_band(ha, i)
    if lower is None:
        return

    armed = st["armed"].get(opt)
    # 1) lower band touched -> arm
    if bar["hl"] <= lower:
        st["armed"][opt] = dict(since=i)
        armed = st["armed"][opt]
    if not armed:
        return

    # 2) first green HA candle after the touch -> entry above its high
    if not bar["green"]:
        return
    # swing low = HA low of the preceding red candle
    k = i - 1
    while k >= 0 and ha[k]["green"]:
        k -= 1
    if k < 0:
        return
    entry = round(bar["hh"] + TICK, 2)
    sl = round(ha[k]["hl"] - SL_BUFFER, 2)
    if entry - sl <= 0:
        st["armed"].pop(opt, None)
        return
    tr = Trade(sym, st["strike"], opt, entry, sl, SYMBOLS[sym]["lot"])
    st["open"][opt] = tr
    st["armed"].pop(opt, None)
    tr.alert_signal()


def write_log(rows):
    if not rows:
        return
    os.makedirs("data", exist_ok=True)
    new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "symbol", "strike", "opt", "signal_time", "fill_time",
                        "entry", "sl", "risk", "t1", "t2", "lot1_exit", "lot2_exit",
                        "qty", "gross_pnl", "outcome"])
        w.writerows(rows)
    log("wrote", len(rows), "rows to", LOG_CSV)


def report(state, day):
    trades = [t for st in state.values() for t in st["trades"]]
    filled = [t for t in trades if t.filled]
    if not filled:
        tg(f"\U0001f4ca <b>HA-BB paper {day}</b>\nNo entry triggered today.")
        return
    tot = sum(t.gross() for t in filled)
    lines = [f"\U0001f4ca <b>HA-BB paper {day}</b>",
             f"Trades: {len(filled)}   Gross: <b>{tot:,.0f}</b>", ""]
    for t in filled:
        lines.append(f"{t.sym} {t.strike} {t.opt} @ {t.entry} → {t.gross():,.0f}  ({t.outcome})")
    lines.append("\n<i>Gross, before charges. Paper only.</i>")
    tg("\n".join(lines))


if __name__ == "__main__":
    main()
