#!/usr/bin/env python3
"""
SKT Intraday Breadth-Sector Strategy  --  PAPER / DEMO engine
See STRATEGY_BREADTH.md for the rules. No real orders are placed here.

Live mode   : runs 09:15 -> 15:20 IST, polls every 60s.
Replay mode : REPLAY_DATE=YYYY-MM-DD re-runs that day on 5-min history
              (sector/breadth picks must then be given via REPLAY_SYMBOLS).

Env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (optional)
  DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID      (intraday 5-min OHLCV)
  RISK_RUPEES   default 2500
  MAX_TRADES    default 3
  REPLAY_DATE, REPLAY_SYMBOLS, REPLAY_DIR
"""
import csv, json, math, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone, date

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = os.environ.get("OUT_DIR", "data")
RISK = float(os.environ.get("RISK_RUPEES", "2500"))
MAX_TRADES = int(os.environ.get("MAX_TRADES", "3"))
CAPITAL = float(os.environ.get("CAPITAL", "100000"))      # intraday capital
LEVERAGE = float(os.environ.get("LEVERAGE", "5"))          # MIS equity leverage
MAX_POS_VALUE = CAPITAL * LEVERAGE / MAX_TRADES             # per trade cap
MAX_MOVE_PCT = 5.0
ORDER_VALID_CANDLES = 1   # order sirf agle 1 candle tak; uske baad cancel ya shift
EMA_LEN = 10
BREADTH_MIN_EDGE_PCT = 2.0     # advances/declines ka difference isse kam -> no trade
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DHAN_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

SECTOR_INDICES = [
    "NIFTY BANK", "NIFTY AUTO", "NIFTY FINANCIAL SERVICES", "NIFTY FMCG",
    "NIFTY IT", "NIFTY MEDIA", "NIFTY METAL", "NIFTY PHARMA",
    "NIFTY PSU BANK", "NIFTY PRIVATE BANK", "NIFTY REALTY",
    "NIFTY HEALTHCARE INDEX", "NIFTY CONSUMER DURABLES", "NIFTY OIL & GAS",
]


def log(*a):
    print(datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ NSE http
_COOKIE = "/tmp/nse_cookies.txt"


def _prime():
    subprocess.run(["curl", "-s", "-m", "20", "-o", "/dev/null", "-c", _COOKIE,
                    "-A", UA, "https://www.nseindia.com/"],
                   capture_output=True, timeout=30)


def nse_json(url, retries=4):
    for i in range(retries):
        if i == 0 or not os.path.exists(_COOKIE):
            _prime()
        r = subprocess.run(["curl", "-s", "-m", "25", "-b", _COOKIE, "-c", _COOKIE,
                            "-A", UA, "-H", "Referer: https://www.nseindia.com/",
                            "-H", "Accept: application/json", url],
                           capture_output=True, text=True, timeout=40)
        try:
            return json.loads(r.stdout)
        except Exception:
            time.sleep(3)
            _prime()
    return None


def tg(msg):
    log("TG:", msg.replace("\n", " | ")[:300])
    if not (TG_TOKEN and TG_CHAT):
        return
    subprocess.run(["curl", "-s", "-o", "/dev/null", "-m", "20", "-X", "POST",
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    "--form-string", f"chat_id={TG_CHAT}",
                    "--form-string", "parse_mode=HTML",
                    "--form-string", f"text={msg}"],
                   capture_output=True, timeout=30)


# ------------------------------------------------------------- 1. direction
def market_breadth():
    """NIFTY 500 advances / declines."""
    d = nse_json("https://www.nseindia.com/api/allIndices")
    if not d or "data" not in d:
        return None
    rows = {x.get("index", "").upper(): x for x in d["data"]}
    ref = rows.get("NIFTY 500") or rows.get("NIFTY TOTAL MARKET")
    if not ref:
        return None
    adv = int(ref.get("advances") or 0)
    dec = int(ref.get("declines") or 0)
    tot = max(adv + dec, 1)
    edge = abs(adv - dec) / tot * 100
    side = "LONG" if adv > dec else "SHORT"
    if edge < BREADTH_MIN_EDGE_PCT:
        side = "NONE"
    return dict(advances=adv, declines=dec, edge_pct=round(edge, 2), side=side,
                index=ref.get("index"), rows=rows)


def pick_sector(rows, side):
    cands = []
    for name in SECTOR_INDICES:
        r = rows.get(name)
        if not r:
            continue
        try:
            cands.append((name, float(r.get("percentChange"))))
        except Exception:
            pass
    if not cands:
        return None, None
    cands.sort(key=lambda x: x[1], reverse=(side == "LONG"))
    return cands[0][0], cands[0][1]


# ------------------------------------------------------------- 2. stock pick
def fno_symbols():
    d = nse_json("https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O")
    if not d or "data" not in d:
        return set()
    return {x["symbol"] for x in d["data"] if x.get("symbol")}


def sector_members(index_name):
    q = index_name.replace("&", "%26").replace(" ", "%20")
    d = nse_json(f"https://www.nseindia.com/api/equity-stockIndices?index={q}")
    if not d or "data" not in d:
        return []
    out = []
    for x in d["data"]:
        sym = x.get("symbol")
        if not sym or sym.upper().startswith("NIFTY"):
            continue
        try:
            out.append(dict(symbol=sym, pchg=float(x.get("pChange")),
                            ltp=float(x.get("lastPrice"))))
        except Exception:
            pass
    return out


def select_stocks(index_name, side):
    fno = fno_symbols()
    mem = [m for m in sector_members(index_name) if m["symbol"] in fno]
    if side == "LONG":
        ok = [m for m in mem if 0 < m["pchg"] <= MAX_MOVE_PCT]
        ok.sort(key=lambda m: m["pchg"], reverse=True)
    else:
        ok = [m for m in mem if -MAX_MOVE_PCT <= m["pchg"] < 0]
        ok.sort(key=lambda m: m["pchg"])
    return ok[:MAX_TRADES]


# ------------------------------------------------------------- 3. 5-min data
_SCRIP = {}


def scrip_map():
    global _SCRIP
    if _SCRIP:
        return _SCRIP
    p = "/tmp/dhan_scrip.csv"
    if not os.path.exists(p):
        subprocess.run(["curl", "-s", "-m", "120", "-o", p,
                        "https://images.dhan.co/api-data/api-scrip-master.csv"],
                       capture_output=True, timeout=180)
    try:
        with open(p, newline="", encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f):
                if (row.get("SEM_EXM_EXCH_ID") == "NSE"
                        and row.get("SEM_SEGMENT") == "E"
                        and row.get("SEM_SERIES") in ("EQ", "BE")):
                    _SCRIP[row.get("SEM_TRADING_SYMBOL", "").strip()] = row.get("SEM_SMST_SECURITY_ID")
    except Exception as e:
        log("scrip map error", e)
    return _SCRIP


def dhan_5min(symbol, days_back=6):
    sid = scrip_map().get(symbol)
    if not (sid and DHAN_TOKEN):
        return []
    today = datetime.now(IST).date()
    body = json.dumps({
        "securityId": str(sid), "exchangeSegment": "NSE_EQ", "instrument": "EQUITY",
        "interval": "5",
        "fromDate": str(today - timedelta(days=days_back)), "toDate": str(today),
    })
    r = subprocess.run(["curl", "-s", "-m", "30", "-X", "POST",
                        "https://api.dhan.co/v2/charts/intraday",
                        "-H", "Content-Type: application/json",
                        "-H", "Accept: application/json",
                        "-H", f"access-token: {DHAN_TOKEN}",
                        "--data-binary", body],
                       capture_output=True, text=True, timeout=45)
    try:
        d = json.loads(r.stdout)
    except Exception:
        log("dhan parse fail", symbol, r.stdout[:200])
        return []
    if "open" not in d:
        log("dhan err", symbol, str(d)[:200])
        return []
    out = []
    for i in range(len(d["open"])):
        ts = datetime.fromtimestamp(d["timestamp"][i], tz=timezone.utc).astimezone(IST)
        out.append(dict(t=ts, o=float(d["open"][i]), h=float(d["high"][i]),
                        l=float(d["low"][i]), c=float(d["close"][i]),
                        v=float(d["volume"][i])))
    return out


def ema(vals, length=EMA_LEN):
    if not vals:
        return []
    k = 2 / (length + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


# ------------------------------------------------------------- 4. trade state
class Trade:
    def __init__(self, symbol, side):
        self.symbol, self.side = symbol, side
        self.state = "WATCH"      # WATCH -> PENDING -> OPEN -> CLOSED
        self.min_vol = None
        self.sig = None           # signal candle
        self.sig_expiry = None    # candle time after which order cancels
        self.entry = self.sl = self.t1 = None
        self.qty = self.qty_open = 0
        self.t1_done = False
        self.expired = False
        self.exits = []           # (reason, price, qty)
        self.pnl = 0.0

    def rupee(self, px_out, qty):
        d = (px_out - self.entry) if self.side == "LONG" else (self.entry - px_out)
        return d * qty


def new_signal(tr, c, min_vol_before):
    if min_vol_before is None or c["v"] >= min_vol_before:
        return False
    if tr.side == "SHORT":
        return c["c"] > c["o"]          # green candle in a short setup
    return c["c"] < c["o"]              # red candle in a long setup


def arm(tr, c):
    shift = tr.state == "PENDING"
    old = tr.entry
    tr.sig = c
    if tr.side == "SHORT":
        tr.entry, tr.sl = c["l"], c["h"]
    else:
        tr.entry, tr.sl = c["h"], c["l"]
    risk = abs(tr.sl - tr.entry)
    if risk <= 0:
        tr.state = "WATCH"
        return None
    qty = int(RISK // risk)
    cap_qty = int(MAX_POS_VALUE // max(tr.entry, 1))
    capped = qty > cap_qty
    qty = min(qty, cap_qty)
    if qty < 2:
        tr.state = "WATCH"
        tr.sig_expiry = None
        return f"{tr.symbol}: signal mila par SL {risk:.2f} bada hai, qty {qty} -> skip"
    tr.qty = qty - (qty % 2)
    tr.qty_open = tr.qty
    tr.t1 = tr.entry - 2 * risk if tr.side == "SHORT" else tr.entry + 2 * risk
    tr.state = "PENDING"
    tr.sig_expiry = 0
    tr.expired = False
    head = (f"↔️ <b>{tr.symbol}</b> order SHIFT: purana entry {old:.2f} cancel, "
            f"naya green candle ka volume aur kam\n"
            if shift and old else
            f"⏳ <b>{tr.symbol}</b> {tr.side} order armed\n")
    note = " (margin cap)" if capped else ""
    return (head +
            f"Entry {tr.entry:.2f} | SL {tr.sl:.2f} | T1 {tr.t1:.2f}\n"
            f"Qty {tr.qty}{note} (risk Rs {tr.qty*risk:.0f}) | signal vol {c['v']:.0f}")


def resolve_pending(tr, c, min_before, can_signal):
    """Pending order ka faisla: naye candle pe shift, warna cancel."""
    msgs = []
    if can_signal and new_signal(tr, c, min_before):
        m = arm(tr, c)
        tr.expired = False
        if m:
            msgs.append(m)
        return msgs
    if tr.state == "PENDING" and tr.expired:
        tr.state = "WATCH"
        tr.sig_expiry = None
        tr.expired = False
        tr.entry = tr.sl = tr.t1 = None
        tr.qty = tr.qty_open = 0
        msgs.append(f"\u274c {tr.symbol}: entry trigger nahi hui aur naya setup bhi nahi "
                    f"-> order cancel, wait.")
    return msgs


def step(tr, c, ema_val):
    """Ek complete 5-min candle ko trade state pe apply karo. Returns messages."""
    msgs = []
    if tr.state == "PENDING":
        hit = (c["l"] <= tr.entry) if tr.side == "SHORT" else (c["h"] >= tr.entry)
        if hit:
            tr.state = "OPEN"
            msgs.append(f"✅ <b>{tr.symbol}</b> {tr.side} ENTRY {tr.entry:.2f} "
                        f"x{tr.qty} | SL {tr.sl:.2f} | T1 {tr.t1:.2f}")
        else:
            tr.sig_expiry = (tr.sig_expiry or 0) + 1
            if tr.sig_expiry >= ORDER_VALID_CANDLES:
                tr.expired = True        # resolve_pending(): shift ya cancel decide karega
            return msgs
    if tr.state != "OPEN":
        return msgs

    sl_hit = (c["h"] >= tr.sl) if tr.side == "SHORT" else (c["l"] <= tr.sl)
    t1_hit = (c["l"] <= tr.t1) if tr.side == "SHORT" else (c["h"] >= tr.t1)

    # same candle me dono -> conservative: SL pehle
    if sl_hit:
        q = tr.qty_open
        tr.pnl += tr.rupee(tr.sl, q)
        tr.exits.append(("SL" if not tr.t1_done else "COST", tr.sl, q))
        tr.qty_open = 0
        tr.state = "CLOSED"
        msgs.append(f"\U0001f6d1 <b>{tr.symbol}</b> {'SL' if not tr.t1_done else 'cost-to-cost'} "
                    f"{tr.sl:.2f} x{q} | trade PnL Rs {tr.pnl:.0f}")
        return msgs

    if t1_hit and not tr.t1_done:
        q = tr.qty // 2
        tr.pnl += tr.rupee(tr.t1, q)
        tr.exits.append(("T1", tr.t1, q))
        tr.qty_open -= q
        tr.t1_done = True
        tr.sl = tr.entry
        msgs.append(f"\U0001f3af <b>{tr.symbol}</b> T1 {tr.t1:.2f} x{q} hit | "
                    f"aadha nikla, SL cost pe {tr.entry:.2f} | booked Rs {tr.pnl:.0f}")

    if tr.t1_done and ema_val is not None and tr.qty_open > 0:
        trail = (c["c"] > ema_val) if tr.side == "SHORT" else (c["c"] < ema_val)
        if trail:
            q = tr.qty_open
            tr.pnl += tr.rupee(c["c"], q)
            tr.exits.append(("EMA10", c["c"], q))
            tr.qty_open = 0
            tr.state = "CLOSED"
            msgs.append(f"\U0001f3c1 <b>{tr.symbol}</b> 10EMA exit {c['c']:.2f} x{q} | "
                        f"trade PnL Rs {tr.pnl:.0f}")
    return msgs


def force_close(tr, px):
    if tr.state == "OPEN" and tr.qty_open > 0:
        q = tr.qty_open
        tr.pnl += tr.rupee(px, q)
        tr.exits.append(("1515", px, q))
        tr.qty_open = 0
    tr.state = "CLOSED"


# ------------------------------------------------------------- 5. engine
def run_day(trades, bar_feed, today):
    """bar_feed(symbol) -> list of completed 5-min candles for today + history."""
    seen = {t.symbol: 0 for t in trades}
    while True:
        now = datetime.now(IST)
        for tr in trades:
            if tr.state == "CLOSED":
                continue
            bars = bar_feed(tr.symbol)
            tod = [b for b in bars if b["t"].date() == today]
            closes = [b["c"] for b in bars]
            e = ema(closes)
            done = len(tod) - 1 if now.minute % 5 != 0 else len(tod)
            done = max(done, 0)
            while seen[tr.symbol] < len(tod):
                i = seen[tr.symbol]
                c = tod[i]
                if c["t"] + timedelta(minutes=5) > now:
                    break                     # candle abhi chal raha hai
                seen[tr.symbol] += 1
                prev_vols = [x["v"] for x in tod[:i]]
                min_before = min(prev_vols) if prev_vols else None
                gi = bars.index(c)
                ema_val = e[gi] if gi < len(e) else None
                for m in step(tr, c, ema_val):
                    tg(m)
                if tr.state in ("WATCH", "PENDING"):
                    for m in resolve_pending(tr, c, min_before, i >= 3):
                        tg(m)
        if now.hour > 15 or (now.hour == 15 and now.minute >= 15):
            break
        if all(t.state == "CLOSED" for t in trades):
            break
        time.sleep(60)
    return trades


def summary(trades, tag):
    tot = sum(t.pnl for t in trades)
    lines = [f"\U0001f4ca <b>Paper summary {tag}</b>"]
    for t in trades:
        if t.entry and t.exits:
            lines.append(f"{t.symbol} {t.side} e{t.entry:.2f} -> "
                         + ", ".join(f"{r}@{p:.2f}x{q}" for r, p, q in t.exits)
                         + f" = Rs {t.pnl:.0f}")
        else:
            lines.append(f"{t.symbol} {t.side}: koi entry nahi")
    lines.append(f"<b>Total: Rs {tot:.0f}</b> (paper, charges ke pehle)")
    return "\n".join(lines)


def log_csv(trades, day, side, sector):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, "paper_trades.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "side", "sector", "symbol", "entry", "sl", "t1",
                        "qty", "exits", "pnl"])
        for t in trades:
            w.writerow([day, side, sector, t.symbol,
                        f"{t.entry:.2f}" if t.entry else "",
                        f"{t.sl:.2f}" if t.sl else "",
                        f"{t.t1:.2f}" if t.t1 else "", t.qty,
                        "; ".join(f"{r}@{p_:.2f}x{q}" for r, p_, q in t.exits),
                        round(t.pnl, 2)])


def main():
    replay = os.environ.get("REPLAY_DATE")
    if replay:
        return replay_mode(replay)

    today = datetime.now(IST).date()
    # 9:25 tak wait
    while True:
        now = datetime.now(IST)
        if (now.hour, now.minute) >= (9, 25):
            break
        time.sleep(20)

    b = market_breadth()
    if not b:
        tg("⚠️ NSE breadth data nahi mila, aaj paper run skip.")
        return
    if b["side"] == "NONE":
        tg(f"⚖️ Breadth barabar (adv {b['advances']} / dec {b['declines']}, "
           f"edge {b['edge_pct']}%) -> aaj NO TRADE.")
        return
    sector, spct = pick_sector(b["rows"], b["side"])
    picks = select_stocks(sector, b["side"]) if sector else []
    head = (f"\U0001f514 <b>{today} paper plan</b>\n"
            f"Breadth: adv {b['advances']} / dec {b['declines']} -> <b>{b['side']}</b>\n"
            f"Sector: {sector} ({spct:+.2f}%)\n"
            f"Stocks: " + (", ".join(f"{p['symbol']} {p['pchg']:+.2f}%" for p in picks) or "koi eligible nahi"))
    tg(head)
    if not picks:
        return

    cache = {}

    def feed(sym):
        now = time.time()
        c = cache.get(sym)
        if c and now - c[0] < 50:
            return c[1]
        bars = dhan_5min(sym)
        cache[sym] = (now, bars)
        return bars

    trades = [Trade(p["symbol"], b["side"]) for p in picks]
    run_day(trades, feed, today)
    for tr in trades:
        if tr.state == "OPEN":
            bars = feed(tr.symbol)
            force_close(tr, bars[-1]["c"] if bars else tr.entry)
            tg(f"⏰ <b>{tr.symbol}</b> 15:15 force exit | trade PnL Rs {tr.pnl:.0f}")
    tg(summary(trades, str(today)))
    log_csv(trades, today, b["side"], sector)


def replay_mode(day_str):
    """Ek purane din pe rules chalaake dekho (code verify karne ke liye)."""
    day = date.fromisoformat(day_str)
    side = os.environ.get("REPLAY_SIDE", "SHORT")
    syms = [s.strip() for s in os.environ.get("REPLAY_SYMBOLS", "").split(",") if s.strip()]
    trades = []
    for sym in syms:
        bars = dhan_5min(sym, days_back=(date.today() - day).days + 6)
        tod = [b for b in bars if b["t"].date() == day]
        if not tod:
            log("no bars", sym)
            continue
        closes = [b["c"] for b in bars]
        e = ema(closes)
        tr = Trade(sym, side)
        for i, c in enumerate(tod):
            gi = bars.index(c)
            ev = e[gi] if gi < len(e) else None
            for m in step(tr, c, ev):
                log(m.replace("<b>", "").replace("</b>", ""))
            if tr.state in ("WATCH", "PENDING"):
                prev = [x["v"] for x in tod[:i]]
                for m in resolve_pending(tr, c, min(prev) if prev else None, i >= 3):
                    log(c["t"].strftime("%H:%M"), m.replace("<b>", "").replace("</b>", ""))
            if c["t"].hour == 15 and c["t"].minute >= 10:
                force_close(tr, c["c"])
        force_close(tr, tod[-1]["c"])
        trades.append(tr)
    print(summary(trades, day_str).replace("<b>", "").replace("</b>", ""))


if __name__ == "__main__":
    main()
