# Trading Engine Architecture & Logic Guide

This document provides a detailed breakdown of how the trading system scans the market, reads charts, detects patterns, and identifies optimal trading entries.

## 1. System Overview Flow

The system acts as an institutional-grade scanner. It does not just look at a single indicator; it combines **Technical Analysis, Supply/Demand Zones (GTF), and Live Price Action** to find the safest setups.

```text
  [Market Universe (All NSE/BSE Stocks)]
                   │
                   ▼ (Filters out Low Volume / Penny Stocks)
         [Data Fetching Layer]
                   │
                   ▼ (Fetches 6-Month Daily & 15-Min Intraday)
        [Technical Engine Core]
           ┌───────┴───────┐
           ▼               ▼
   [Indicators]     [GTF Zones & Patterns]
     (RSI, VWAP,         (Proximal/Distal, 
      EMA, Volume)        Breakout/Pullback)
           │               │
           └───────┬───────┘
                   ▼
           [Scoring Engine] (Assigns 100-Point Score)
                   │
                   ▼
           Is Risk/Reward > 1.3 ?
           /                   \
        YES                     NO
        /                         \
[Dashboard & Hot Picks]       [Watchlist / Ignore]
        │
        ▼
((Live Action Monitor))
   (Checks WebSocket)
        │
        ▼ (Live Tick Hits Entry Level)
[Real-Time Tab Alert & Telegram]
```

## 2. Market Universe & Pre-filtering
Before analyzing a chart, the system filters out the "noise".
- **Liquidity Check:** Avoids illiquid stocks (e.g., minimum 50,000 volume threshold for BSE).
- **Price Check:** Avoids extreme penny stocks unless there is a massive volume anomaly.
- **Circuit Filter:** Rejects stocks stuck in upper/lower circuits to avoid trapping the user.

## 3. How the Engine Reads a Chart (Indicators)

When the system opens a chart, it computes multiple layers of data simultaneously:

### Trend & Moving Averages
- **EMA 20:** Short-term trend and pullback support.
- **EMA 50:** Medium-term trend.
- **EMA 200:** Long-term institutional trend.
- **VWAP (Volume Weighted Average Price):** Used to verify if institutional buying is supporting the current price.

### Momentum & Volume
- **RSI (Relative Strength Index):**
  - `< 40`: Oversold (Good for base-building).
  - `60 - 70`: Strong Momentum (Breakout zone).
  - `> 80`: Overextended / Chase Risk (Avoid).
- **Volume Ratio (Relative Volume):** Compares today's volume to the 20-day average. A ratio > 1.5 indicates smart money entry.
- **OBV (On-Balance Volume):** Checks if volume is increasing on up-days and decreasing on down-days (Accumulation).

---

## 4. GTF Strategy (Supply & Demand Zones)

The system heavily relies on Institutional Supply & Demand zones rather than generic support/resistance.

```text
[ Base Formation (Narrow Candles) ]
                │
                ▼ (Explosive Green "Elephant" Candle)
[      Demand Zone Created      ]
  (Top: Proximal Line)
  (Bottom: Distal Line)
                │
                ▼ (Price pulls back over next few days)
[ Price Drops to Proximal Line  ]
                │
                ▼
[ Institutional Buy Order Triggered ]
```

**How it detects Demand Zones:**
1. Looks for a sequence of narrow body candles (Base).
2. Followed by a massive "Elephant" green candle (imbalance).
3. The Base is marked with:
   - **Proximal Line:** The top of the base (Entry Trigger).
   - **Distal Line:** The bottom of the base (Invalidation/Stop Loss).
4. **Trigger Condition:** When price returns to the Proximal Line, the system flags it as `RETEST_ENTRY`.

---

## 5. Pattern Recognition & Breakouts

The system uses mathematical ratios to detect classic chart patterns:

### Tight Consolidation (VCP / Base)
- **Condition:** Price moves within a very tight range (e.g., < 4% variance) over multiple days.
- **Logic:** Volatility contraction means a big move is coming. 

### Breakouts & Pullbacks
- **Breakout Condition:** Price crosses a 20-day or 50-day resistance level with Volume > 1.5x average.
- **Pullback Condition:** Stock broke out recently, retreated on *low volume*, and is now bouncing off the EMA 20 or VWAP.

> [!TIP]
> The system prefers **Pullbacks** over **Breakouts** to maintain a higher Risk-to-Reward ratio and avoid fakeouts.

---

## 6. The Scoring System (The 100-Point Engine)

Every stock gets a score. Only stocks with high scores make it to your screen.

| Metric | Weight | Description |
| :--- | :--- | :--- |
| **Trend** | 25 pts | Price > EMA 20 > EMA 50 |
| **Volume** | 25 pts | Relative Volume Spike, Institutional buying |
| **Momentum** | 20 pts | RSI > 55, MACD crossover |
| **Pattern** | 15 pts | VCP, Flag, Cup & Handle detected |
| **Market/Macro**| 15 pts | Nifty trend is positive, Sector is performing well |

> [!WARNING]
> **Chase Risk Penalty:** If a stock has run up too fast (e.g., price is 15% above EMA 20), the system deducts massive points and tags it as `AVOID_LATE_ENTRY` or `PROFIT_BOOKING_RISK`.

---

## 7. Entry, Target, and Stop Loss Calculation

The most critical part of the system is the **Trade Plan**. It ensures you never take a blind trade.

```text
[ Calculate Invalidation Level ]
   (Lowest Support/Distal Line)
                │
                ▼
           [ Stop Loss ]
                │
[ Identify Structure/Resistance ]
        (Next Major Pivot)
                │
                ▼
           [ Target 1 ]
                │
[ Calculate Safe Entry Price ]
  (Max 1% above Support/EMA)
                │
                ▼
        [ Entry Price ]

     ==> RISK / REWARD CALCULATION <==
     (Is Profit 1.5x bigger than Loss?)
                │
          RR > 1.5 ?
         /          \
       YES           NO
       /               \
[Approve Trade]   [Reject / Watch Only]
```

- **Safe Entry Price:** Calculated as the nearest support (EMA 20, VWAP, or Proximal Line) + a tiny 0.6% buffer. It forces the user to buy at the bottom of the dip, not the top of the candle.
- **Stop Loss:** Placed just below the Distal Line (GTF) or major support structure, preventing stop-loss hunting.
- **Risk/Reward (RR):** If the potential profit (Target - Entry) is not at least 1.5x the potential loss (Entry - Stop Loss), the stock is rejected.

---

## 8. Real-Time Execution (Live Action Tab)

While the scanner runs every 30 seconds in the background, the **Live Action** tab is powered by a millisecond-level WebSocket.

1. The Background Scanner identifies a stock (e.g., `KINGFA`) with a `safe_entry_price` of `₹100`.
2. The system registers `KINGFA` and `100` into the `EntryMonitor`.
3. The WebSocket reads every live tick (99.5, 99.8, 100.1...).
4. The moment the live price is within **±1.5%** of `₹100`, it immediately pushes it to the **Live Action** tab and sends a Telegram alert. 
5. If the stock flies away (goes to ₹105) or breaks down (goes to ₹90), it is removed from Live Action to protect you.
