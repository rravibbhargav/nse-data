# SKT Intraday Breadth-Sector Strategy  (paper / demo phase)

Nothing here is a recommendation. Research + personal-trading system only.

## 1. Direction (one time, 9:25 IST)
- NSE market breadth dekho (NIFTY 500 ke advances vs declines).
- Advances > Declines  -> LONG day  (sirf buy trades)
- Declines > Advances  -> SHORT day (sirf sell trades)
- Barabar / difference < 2%  -> NO TRADE, din band.

## 2. Sector (9:25 IST)
- Saare NSE sectoral indices ka % change (equal-weighted: index ke
  constituents ke % change ka average).
- LONG day  -> sabse top performer sector
- SHORT day -> sabse jyada gira hua sector

## 3. Stock selection (9:25 IST)
Us sector ke constituents me se:
- sirf F&O (SECURITIES IN F&O list) stocks
- LONG day : pChange > 0 aur pChange <= +5.0%
- SHORT day: pChange < 0 aur pChange >= -5.0%
- Ranking: direction ke saath sabse strong (LONG me sabse jyada badha hua <=5%,
  SHORT me sabse jyada gira hua >=-5%)
- Top 3 stocks tak (max 3 open trades, sab ek hi direction me)

## 4. Entry (5-min timeframe, 9:30 ke baad)
- 9:15, 9:20, 9:25 ke 3 candles me trade NAHI, par unka volume note hota hai.
- Har complete 5-min candle ka volume track hota hai.
- SHORT entry signal: ek GREEN candle (close > open) bane jiska volume
  aaj ke sabhi pehle 5-min candles ke volume me se sabse kam volume se BHI KAM ho.
- LONG entry signal: ek RED candle (close < open) same volume condition ke saath.
- Order: SHORT -> signal candle ke LOW pe sell stop, SL = signal candle ka HIGH
         LONG  -> signal candle ke HIGH pe buy stop, SL = signal candle ka LOW
- Order sirf AGLE 1 candle tak valid. Us candle me:
  - entry trigger ho gayi -> trade live
  - trigger nahi hui, par yeh candle khud naya valid setup hai
    (SHORT me green + volume aaj ke sabhi pehle candles ke minimum se bhi kam)
    -> purana order cancel, order is naye candle pe SHIFT (naya entry/SL/qty)
  - trigger nahi hui aur volume condition bhi pass nahi -> purana order CANCEL,
    wapas wait. Trade sirf tab jab poora setup mile.
- Min-volume har candle ke saath update hota rehta hai, isliye har naye signal ka
  volume pehle wale signal candle se bhi kam hona chahiye.
- Ek stock me ek hi trade per day.

## 5. Quantity
- Risk per trade = Rs 2500 (fixed)
- Risk per share = |SL - Entry|
- Qty = floor(2500 / risk per share), minimum 2 (half exit ke liye even number)
- Agar qty < 2 aaye -> trade skip (SL bahut bada hai)
- Product: MIS (intraday cash equity)

## 6. Exit
1. Target 1 = 1:2 (Entry +/- 2 x risk) -> aadha qty exit, aur SL ko cost-to-cost
   (entry price) pe shift.
2. Baaki qty ka trail: jab 5-min candle 10 EMA ke
   - SHORT me OOPAR close kare -> full exit
   - LONG me NEECHE close kare -> full exit
   (10 EMA 5-min close pe, pichle din ka data seed karke)
3. SL hit -> exit (pehle target ke baad SL = cost, i.e. breakeven).
4. 15:15 IST -> jo bacha hai sab market pe exit.

## 7. Demo phase (abhi)
- Koi real order NAHI. Sab paper.
- Rozana Telegram pe: 9:25 ka direction + sector + chune hue stocks,
  phir har entry / target1 / SL / exit ka alert.
- Din ke end me PnL summary + data/paper_trades.csv me log.
- Charges assumption (cash MIS, delivery nahi): brokerage Rs 20/order ya 0.03%
  (jo kam ho) + STT 0.025% sell side + exchange 0.00297% + GST 18% + stamp 0.003% buy.
- 20-30 trading din ke baad stats dekhenge: win%, avg R, max DD. Tab decide
  karenge ki real paisa lagana hai ya rule tweak karna hai.

## Data source (Nov 2026)
`www.nseindia.com/api/*` GitHub Actions runners (Azure/US IP) se block hai,
isliye engine wahan se kuch nahi leta:

| Cheez | Source |
|---|---|
| Index constituents (NIFTY 500 + 14 sector) | `nsearchives.nseindia.com/content/indices/*.csv` |
| F&O underlyings | `nsearchives.nseindia.com/content/fo/fo_mktlots.csv`, fallback `data/fno_symbols.txt` |
| Live LTP / prev close / % change | Dhan `POST /v2/marketfeed/quote` |
| 5-min OHLCV | Dhan `POST /v2/charts/intraday` |

Dhan marketfeed quote ke liye `DHAN_ACCESS_TOKEN` **aur** `DHAN_CLIENT_ID`
dono secrets chahiye. Client id na ho to run skip hota hai aur Telegram pe
wajah aa jaati hai.
