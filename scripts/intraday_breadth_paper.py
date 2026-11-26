#!/usr/bin/env python3
"""
SKT Intraday Breadth-Sector Strategy  --  PAPER / DEMO engine
See STRATEGY_BREADTH.md for the rules. No real orders are placed here.

Live mode   : runs 09:15 -> 15:20 IST, polls every 60s.
Replay mode : REPLAY_DATE=YYYY-MM-DD re-runs that day on 5-min history
              (sector/breadth picks must then be given via REPLAY_SYMBOLS).

Env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (optional)
  DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID      (live quotes + intraday 5-min OHLCV;
                                          CLIENT_ID marketfeed quote ke liye zaroori)
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


# ------------------------------------------------------- universe + live feed
# NOTE: www.nseindia.com/api/* GitHub runners (Azure/US IP) se block hota hai -
# 26-Nov-2026 ko breadth isi wajah se roz None aa rahi thi. Sirf
# nsearchives.nseindia.com chalta hai, isliye universe wahan ki CSV se aur live
# price/pchg Dhan marketfeed quote se lete hain (dono Actions se reachable).
ARCHIVE = "https://nsearchives.nseindia.com"
DHAN_CLIENT = os.environ.get("DHAN_CLIENT_ID", "")

INDEX_FILES = {
    "NIFTY 500": ("ind_nifty500list.csv",),
    "NIFTY BANK": ("ind_niftybanklist.csv",),
    "NIFTY AUTO": ("ind_niftyautolist.csv",),
    "NIFTY FINANCIAL SERVICES": ("ind_niftyfinancelist.csv",
                                 "ind_niftyfinancialserviceslist.csv"),
    "NIFTY FMCG": ("ind_niftyfmcglist.csv",),
    "NIFTY IT": ("ind_niftyitlist.csv",),
    "NIFTY MEDIA": ("ind_niftymedialist.csv",),
    "NIFTY METAL": ("ind_niftymetallist.csv",),
    "NIFTY PHARMA": ("ind_niftypharmalist.csv",),
    "NIFTY PSU BANK": ("ind_niftypsubanklist.csv",),
    "NIFTY PRIVATE BANK": ("ind_nifty_privatebanklist.csv",
                           "ind_niftyprivatebanklist.csv"),
    "NIFTY REALTY": ("ind_niftyrealtylist.csv",),
    "NIFTY HEALTHCARE INDEX": ("ind_niftyhealthcarelist.csv",),
    "NIFTY CONSUMER DURABLES": ("ind_niftyconsumerdurableslist.csv",),
    "NIFTY OIL & GAS": ("ind_niftyoilgaslist.csv",),
}


def archive_file(rel, cache_key, retries=4):
    """nsearchives se file /tmp me laao; path return karo ('' = fail)."""
    p = f"/tmp/nsearch_{cache_key}"
    if os.path.exists(p) and os.path.getsize(p) > 200:
        return p
    for _ in range(retries):
        subprocess.run(["curl", "-s", "-m", "30", "-o", p, "-A", UA,
                        "-H", "Referer: https://www.nseindia.com/",
                        f"{ARCHIVE}{rel}"],
                       capture_output=True, timeout=45)
        if os.path.exists(p) and os.path.getsize(p) > 200:
            return p
        time.sleep(2)
    log("archive fail", rel)
    return ""


def index_members(name):
    """Index ke constituents (symbols). NSE ki published index CSV se."""
    for fn in INDEX_FILES.get(name, ()):
        p = archive_file("/content/indices/" + fn, fn)
        if not p:
            continue
        out = []
        try:
            with open(p, newline="", encoding="utf-8", errors="ignore") as f:
                for row in csv.DictReader(f):
                    s = (row.get("Symbol") or row.get("SYMBOL") or "").strip().upper()
                    if s:
                        out.append(s)
        except Exception as e:
            log("index csv parse fail", fn, e)
        if out:
            return out
    log("index list missing", name)
    return []


def fno_symbols():
    """F&O underlyings. fo_mktlots.csv se; fallback repo ki static list."""
    out = set()
    p = archive_file("/content/fo/fo_mktlots.csv", "fo_mktlots")
    if p:
        sm = scrip_map()
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    for cell in line.split(",")[:3]:
                        c = cell.strip().upper()
                        if c and c.isalnum() and 2 <= len(c) <= 20 and c in sm:
                            out.add(c)
                            break
        except Exception as e:
            log("fno parse fail", e)
    if not out:
        sp = os.path.join(OUT_DIR, "fno_symbols.txt")
        if os.path.exists(sp):
            with open(sp) as f:
                out = {l.strip().upper() for l in f if l.strip()}
            log("fno list: repo fallback", len(out))
    log("fno symbols", len(out))
    return out


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


def dhan_quote(symbols):
    """symbol -> dict(ltp, prev_close, pchg, vol). Dhan /v2/marketfeed/quote."""
    out = {}
    if not (DHAN_TOKEN and DHAN_CLIENT):
        log("dhan quote skip: " + ("DHAN_CLIENT_ID missing" if DHAN_TOKEN
                                   else "DHAN_ACCESS_TOKEN missing"))
        return out
    sm = scrip_map()
    ids, back = [], {}
    for s in symbols:
        sid = sm.get(s)
        if sid:
            ids.append(int(sid))
            back[str(sid)] = s
    if not ids:
        log("dhan quote: koi security id resolve nahi hui")
        return out
    for i in range(0, len(ids), 900):
        body = json.dumps({"NSE_EQ": ids[i:i + 900]})
        r = subprocess.run(["curl", "-s", "-m", "30", "-X", "POST",
                            "https://api.dhan.co/v2/marketfeed/quote",
                            "-H", "Content-Type: application/json",
                            "-H", "Accept: application/json",
                            "-H", f"access-token: {DHAN_TOKEN}",
                            "-H", f"client-id: {DHAN_CLIENT}",
                            "--data-binary", body],
                           capture_output=True, text=True, timeout=45)
        try:
            d = json.loads(r.stdout)
        except Exception:
            log("quote parse fail", r.stdout[:250])
            time.sleep(1.1)
            continue
        seg = (d.get("data") or {}).get("NSE_EQ") or {}
        if not seg:
            log("quote empty", str(d)[:250])
        for sid, q in seg.items():
            sym = back.get(str(sid))
            if not sym:
                continue
            try:
                ltp = float(q.get("last_price") or 0)
                prev = float((q.get("ohlc") or {}).get("close") or 0)
            except Exception:
                continue
            if ltp <= 0 or prev <= 0:
                continue
            out[sym] = dict(ltp=ltp, prev_close=prev, vol=float(q.get("volume") or 0),
                            pchg=round((ltp - prev) / prev * 100, 2))
        time.sleep(1.1)        # Dhan rate limit: 1 req/sec
    return out


# ------------------------------------------------------------- 1. direction
def market_breadth():
    """NIFTY 500 advances/declines + sector strength, Dhan live quotes se."""
    universe = index_members("NIFTY 500")
    sectors = {n: index_members(n) for n in SECTOR_INDICES}
    want = set(universe)
    for mem in sectors.values():
        want.update(mem)
    if not want:
        log("universe empty: nsearchives se index lists nahi aayi")
        return None
    q = dhan_quote(sorted(want))
    if not q:
        return None
    uq = [q[s] for s in universe if s in q] or list(q.values())
    adv = sum(1 for x in uq if x["pchg"] > 0)
    dec = sum(1 for x in uq if x["pchg"] < 0)
    tot = max(adv + dec, 1)
    edge = abs(adv - dec) / tot * 100
    side = "LONG" if adv > dec else "SHORT"
    if edge < BREADTH_MIN_EDGE_PCT:
        side = "NONE"
    spct = {}
    for name, mem in sectors.items():
        vals = [q[s]["pchg"] for s in mem if s in q]
        if len(vals) >= 3:                    # equal-weighted sector move
            spct[name] = round(sum(vals) / len(vals), 2)
    log(f"breadth adv {adv} / dec {dec} edge {edge:.2f}% side {side} | "
        f"quotes {len(q)}/{len(want)} | sectors {len(spct)}")
    return dict(advances=adv, declines=dec, edge_pct=round(edge, 2), side=side,
                index="NIFTY 500", quotes=q, sector_pchg=spct)


def pick_sector(sector_pchg, side):
    if not sector_pchg:
        return None, None
    items = sorted(sector_pchg.items(), key=lambda x: x[1], reverse=(side == "LONG"))
    return items[0][0], items[0][1]


# ------------------------------------------------------------- 2. stock pick
def select_stocks(index_name, side, quotes):
    fno = fno_symbols()
    if not fno:
        log("F&O list nahi mili - filter skip ho raha hai")
    cands = []
    for s in index_members(index_name):
        qd = quotes.get(s)
        if not qd or (fno and s not in fno):
            continue
        cands.append(dict(symbol=s, pchg=qd["pchg"], ltp=qd["ltp"]))
    if side == "LONG":
        ok = [m for m in cands if 0 < m["pchg"] <= MAX_MOVE_PCT]
        ok.sort(key=lambda m: m["pchg"], reverse=True)
    else:
        ok = [m for m in cands if -MAX_MOVE_PCT <= m["pchg"] < 0]
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
        why = ("Dhan ka DHAN_CLIENT_ID secret set nahi hai"
               if (DHAN_TOKEN and not DHAN_CLIENT) else
               "Dhan quote / NSE index list nahi mili")
        tg(f"⚠️ Breadth data nahi mila ({why}), aaj paper run skip. "
           f"Log me detail hai.")
        return
    if b["side"] == "NONE":
        tg(f"⚖️ Breadth barabar (adv {b['advances']} / dec {b['declines']}, "
           f"edge {b['edge_pct']}%) -> aaj NO TRADE.")
        return
    sector, spct = pick_sector(b["sector_pchg"], b["side"])
    picks = select_stocks(sector, b["side"], b["quotes"]) if sector else []
    head = (f"\U0001f514 <b>{today} paper plan</b>\n"
            f"Breadth: adv {b['advances']} / dec {b['declines']} -> <b>{b['side']}</b>\n"
            f"Sector: {sector} ({spct:+.2f}% eq-wt)\n"
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
