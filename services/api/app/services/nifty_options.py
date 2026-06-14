"""Nifty Pattern Detector — detects when Nifty is about to break or breakdown.
Gives CE/PE signal based on Nifty's own chart structure, not individual stocks.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Dict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ENABLE_NIFTY_OPTIONS = os.getenv("ENABLE_NIFTY_OPTIONS", "true").lower() == "true"


def analyze_nifty_for_options(nifty_frame: pd.DataFrame) -> Dict:
    """
    Read Nifty chart and detect:
    - Breakout forming (CE signal)
    - Breakdown forming (PE signal)
    - Support/Resistance levels
    - Pattern type
    Returns option signal only when pattern is clear.
    """
    if not ENABLE_NIFTY_OPTIONS:
        return _no_signal("nifty_options_disabled")

    if nifty_frame is None or nifty_frame.empty or len(nifty_frame) < 50:
        return _no_signal("insufficient_nifty_data")

    try:
        close = nifty_frame["Close"].astype(float)
        high = nifty_frame["High"].astype(float)
        low = nifty_frame["Low"].astype(float)
        volume = nifty_frame["Volume"].astype(float).replace(0, np.nan)

        latest_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        daily_change = round((latest_close / prev_close - 1) * 100, 2)

        # Key levels
        resistance_20 = float(high.rolling(20).max().iloc[-2])  # yesterday's 20-day high
        support_20 = float(low.rolling(20).min().iloc[-2])
        resistance_50 = float(high.rolling(50).max().iloc[-2])
        support_50 = float(low.rolling(50).min().iloc[-2])

        ema_20 = float(close.ewm(span=20).mean().iloc[-1])
        ema_50 = float(close.ewm(span=50).mean().iloc[-1])
        ema_200 = float(close.ewm(span=200).mean().iloc[-1])

        # ATR for stop calculation
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss_s = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        rs = gain / (loss_s.replace(0, np.nan))
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])

        # Volume ratio
        vol_avg = float(volume.rolling(20).mean().iloc[-1])
        vol_today = float(volume.iloc[-1])
        vol_ratio = round(vol_today / vol_avg, 2) if vol_avg > 0 else 1.0

        # Distance to levels
        dist_to_resistance = round((resistance_20 - latest_close) / latest_close * 100, 2)
        dist_to_support = round((latest_close - support_20) / latest_close * 100, 2)

        # Higher lows check (last 10 days)
        lows_5 = float(low.tail(5).min())
        lows_prev_5 = float(low.iloc[-10:-5].min())
        higher_lows = lows_5 > lows_prev_5

        # Lower highs check
        highs_5 = float(high.tail(5).max())
        highs_prev_5 = float(high.iloc[-10:-5].max())
        lower_highs = highs_5 < highs_prev_5

        # Bollinger squeeze
        sma_20 = close.rolling(20).mean()
        std_20 = close.rolling(20).std()
        bb_width = float(((sma_20.iloc[-1] + 2*std_20.iloc[-1]) - (sma_20.iloc[-1] - 2*std_20.iloc[-1])) / latest_close * 100)
        bb_squeeze = bb_width < 4.0  # tight bands

        # ── BREAKOUT DETECTION (CE Signal) ────────────────────────────────────
        breakout_score = 0
        breakout_reasons = []

        if latest_close > resistance_20:
            breakout_score += 30
            breakout_reasons.append(f"Nifty broke above 20-day high {resistance_20:.0f}")
        elif dist_to_resistance <= 0.5:
            breakout_score += 15
            breakout_reasons.append(f"Nifty within 0.5% of 20-day resistance {resistance_20:.0f}")

        if latest_close > ema_20 and ema_20 > ema_50:
            breakout_score += 15
            breakout_reasons.append("Price above 20 & 50 EMA, trend aligned")

        if higher_lows:
            breakout_score += 10
            breakout_reasons.append("Higher lows forming — buyers stepping in")

        if vol_ratio >= 1.3 and daily_change > 0:
            breakout_score += 15
            breakout_reasons.append(f"Volume spike {vol_ratio}x on green day")

        if rsi >= 55 and rsi <= 70:
            breakout_score += 10
            breakout_reasons.append(f"RSI {rsi:.0f} — strong but not overbought")

        if bb_squeeze:
            # Only credit squeeze in the direction the trend is already leaning,
            # not both — avoids cancelling out the directional edge
            if latest_close >= ema_20:  # Trend leans bullish
                breakout_score += 10
                breakout_reasons.append("Bollinger squeeze above EMA — bullish expansion likely")
            # else: squeeze in downtrend gets credit in breakdown section below

        # ── BREAKDOWN DETECTION (PE Signal) ───────────────────────────────────
        breakdown_score = 0
        breakdown_reasons = []

        if latest_close < support_20:
            breakdown_score += 30
            breakdown_reasons.append(f"Nifty broke below 20-day low {support_20:.0f}")
        elif dist_to_support <= 0.5:
            breakdown_score += 15
            breakdown_reasons.append(f"Nifty within 0.5% of 20-day support {support_20:.0f}")

        if latest_close < ema_20 and ema_20 < ema_50:
            breakdown_score += 15
            breakdown_reasons.append("Price below 20 & 50 EMA, trend weak")

        if lower_highs:
            breakdown_score += 10
            breakdown_reasons.append("Lower highs forming — sellers in control")

        if vol_ratio >= 1.3 and daily_change < 0:
            breakdown_score += 15
            breakdown_reasons.append(f"Volume spike {vol_ratio}x on red day")

        if rsi <= 45 and rsi >= 25:
            breakdown_score += 10
            breakdown_reasons.append(f"RSI {rsi:.0f} — weak but not oversold")

        if bb_squeeze:
            # Only credit squeeze in the breakdown direction if trend leans bearish
            if latest_close < ema_20:  # Trend leans bearish
                breakdown_score += 10
                breakdown_reasons.append("Bollinger squeeze below EMA — bearish expansion likely")

        # ── DECISION ──────────────────────────────────────────────────────────
        if breakout_score >= 60 and breakout_score > breakdown_score:
            signal = "CE"
            action = "NIFTY_CE_READY" if breakout_score >= 75 else "NIFTY_CE_WATCH"
            score = breakout_score
            reasons = breakout_reasons
            entry = round(resistance_20, 0)
            stop = round(max(ema_20, latest_close - atr * 1.5), 0)
            target = round(latest_close + atr * 2.5, 0)
            pattern = "Nifty Breakout" if latest_close > resistance_20 else "Nifty Near Breakout"

        elif breakdown_score >= 60 and breakdown_score > breakout_score:
            signal = "PE"
            action = "NIFTY_PE_READY" if breakdown_score >= 75 else "NIFTY_PE_WATCH"
            score = breakdown_score
            reasons = breakdown_reasons
            entry = round(support_20, 0)
            stop = round(min(ema_20, latest_close + atr * 1.5), 0)
            target = round(latest_close - atr * 2.5, 0)
            pattern = "Nifty Breakdown" if latest_close < support_20 else "Nifty Near Breakdown"

        else:
            return _no_signal(
                "no_clear_pattern",
                levels={
                    "resistance20": round(resistance_20, 0),
                    "support20": round(support_20, 0),
                    "ema20": round(ema_20, 0),
                    "ema50": round(ema_50, 0),
                    "latestClose": round(latest_close, 0),
                    "rsi": round(rsi, 1),
                    "volRatio": vol_ratio,
                    "breakoutScore": breakout_score,
                    "breakdownScore": breakdown_score,
                    "bbSqueeze": bb_squeeze,
                },
            )

        # ── Build response ────────────────────────────────────────────────────
        return {
            "signal": signal,
            "action": action,
            "pattern": pattern,
            "score": score,
            "niftyPrice": round(latest_close, 0),
            "dailyChange": daily_change,
            "entry": entry,
            "stop": stop,
            "target": target,
            "riskReward": round(abs(target - latest_close) / abs(latest_close - stop), 1) if abs(latest_close - stop) > 0 else 0,
            "reasons": reasons[:4],
            "levels": {
                "resistance20": round(resistance_20, 0),
                "support20": round(support_20, 0),
                "resistance50": round(resistance_50, 0),
                "support50": round(support_50, 0),
                "ema20": round(ema_20, 0),
                "ema50": round(ema_50, 0),
                "ema200": round(ema_200, 0),
            },
            "indicators": {
                "rsi": round(rsi, 1),
                "atr": round(atr, 0),
                "volRatio": vol_ratio,
                "bbSqueeze": bb_squeeze,
                "higherLows": higher_lows,
                "lowerHighs": lower_highs,
            },
            "rule": (
                f"Buy ATM/ITM {signal} of current/next weekly expiry. "
                f"Entry only if Nifty sustains {'above' if signal == 'CE' else 'below'} {entry:.0f}. "
                f"Stop: {stop:.0f}. Target: {target:.0f}. "
                f"Max risk: 0.5% of capital. No averaging."
            ),
            "warning": (
                "Nifty options are index options — fast decay. "
                "Exit if pattern fails within 30 min of entry. "
                "Do not hold overnight unless strongly in profit."
            ),
            "available": True,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        logger.warning("[NIFTY_OPTIONS] analysis failed: %s", exc)
        return _no_signal(f"error: {exc}")


def _no_signal(reason: str, levels: Dict = None) -> Dict:
    return {
        "signal": "NO_TRADE",
        "action": "WAIT",
        "pattern": "No clear Nifty pattern",
        "score": 0,
        "reasons": [reason],
        "levels": levels or {},
        "indicators": {},
        "rule": "No Nifty option trade. Wait for clear breakout or breakdown pattern.",
        "warning": "Sideways Nifty = theta decay kills option buyers. Stay cash.",
        "available": False,
        "reason": reason,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
