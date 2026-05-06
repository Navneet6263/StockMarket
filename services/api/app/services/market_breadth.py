"""Market Breadth Gate — feature-flagged, reads benchmark frame only, no fake data."""
from __future__ import annotations

import os
import logging
from typing import Dict, List

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ── Feature flag ──────────────────────────────────────────────────────────────
ENABLE_BREADTH_GATE: bool = os.getenv("ENABLE_BREADTH_GATE", "true").lower() == "true"
BEARISH_BLOCK_FRESH_BUY: bool = os.getenv("BEARISH_BLOCK_FRESH_BUY", "true").lower() == "true"


def compute_market_breadth(
    benchmark_frame: pd.DataFrame,
    all_signals: List[Dict],
) -> Dict:
    """
    Compute market mood from benchmark OHLC + scanned signal list.
    Returns marketContext dict. Never fakes data — returns 'unknown' when insufficient.
    """
    if not ENABLE_BREADTH_GATE:
        return _unknown_context("breadth_gate_disabled")

    if benchmark_frame is None or benchmark_frame.empty or len(benchmark_frame) < 50:
        logger.debug("[BREADTH] Insufficient benchmark history — returning unknown")
        return _unknown_context("insufficient_benchmark_history")

    try:
        close = benchmark_frame["Close"].astype(float)
        ema_20 = close.ewm(span=20, adjust=False).mean()
        ema_50 = close.ewm(span=50, adjust=False).mean()

        latest_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) > 1 else latest_close
        daily_change_pct = round((latest_close / prev_close - 1) * 100, 2)

        above_ema20 = bool(latest_close > float(ema_20.iloc[-1]))
        above_ema50 = bool(latest_close > float(ema_50.iloc[-1]))
        ema20_slope = float(ema_20.pct_change(5).iloc[-1] * 100)

        # Advance/decline from scanned signals
        advancing = sum(1 for s in all_signals if (s.get("change_pct") or 0) > 0)
        declining = sum(1 for s in all_signals if (s.get("change_pct") or 0) < 0)
        total = len(all_signals) or 1
        ad_ratio = round(advancing / max(declining, 1), 2)

        above_ema20_count = sum(1 for s in all_signals if s.get("price_above_ema20") or s.get("chart_features", {}).get("moving_average_trend") == "uptrend")
        pct_above_ema20 = round(above_ema20_count / total * 100, 1)

        # ── Mood logic ────────────────────────────────────────────────────────
        bearish_signals = 0
        bullish_signals = 0

        if above_ema20:
            bullish_signals += 1
        else:
            bearish_signals += 2  # weight: below 20 EMA is stronger bear signal

        if above_ema50:
            bullish_signals += 1
        else:
            bearish_signals += 1

        if daily_change_pct >= 0.3:
            bullish_signals += 1
        elif daily_change_pct <= -0.5:
            bearish_signals += 1

        if ema20_slope > 0:
            bullish_signals += 1
        elif ema20_slope < -0.1:
            bearish_signals += 1

        if pct_above_ema20 >= 55:
            bullish_signals += 1
        elif pct_above_ema20 <= 35:
            bearish_signals += 1

        if bearish_signals >= 4:
            mood = "bearish"
        elif bullish_signals >= 4:
            mood = "bullish"
        else:
            mood = "neutral"

        logger.debug(
            "[BREADTH] mood=%s above_ema20=%s above_ema50=%s daily_chg=%.2f pct_above_ema20=%.1f",
            mood, above_ema20, above_ema50, daily_change_pct, pct_above_ema20,
        )

        return {
            "marketMood": mood,
            "benchmarkAboveEma20": above_ema20,
            "benchmarkAboveEma50": above_ema50,
            "benchmarkDailyChangePct": daily_change_pct,
            "ema20Slope": round(ema20_slope, 3),
            "advanceDeclineRatio": ad_ratio,
            "pctStocksAboveEma20": pct_above_ema20,
            "advancingCount": advancing,
            "decliningCount": declining,
            "totalScanned": total,
            "breadthGateEnabled": True,
            "freshBuyBlocked": mood == "bearish" and BEARISH_BLOCK_FRESH_BUY,
            "blockedReason": "Market breadth weak; fresh buy calls blocked." if mood == "bearish" and BEARISH_BLOCK_FRESH_BUY else None,
        }

    except Exception as exc:
        logger.warning("[BREADTH] compute failed: %s", exc)
        return _unknown_context(f"compute_error: {exc}")


def should_block_buy(market_context: Dict) -> bool:
    """Returns True if fresh BUY calls should be blocked based on market mood."""
    if not ENABLE_BREADTH_GATE or not BEARISH_BLOCK_FRESH_BUY:
        return False
    return bool(market_context.get("freshBuyBlocked"))


def _unknown_context(reason: str) -> Dict:
    return {
        "marketMood": "unknown",
        "benchmarkAboveEma20": None,
        "benchmarkAboveEma50": None,
        "benchmarkDailyChangePct": None,
        "ema20Slope": None,
        "advanceDeclineRatio": None,
        "pctStocksAboveEma20": None,
        "advancingCount": None,
        "decliningCount": None,
        "totalScanned": None,
        "breadthGateEnabled": ENABLE_BREADTH_GATE,
        "freshBuyBlocked": False,
        "blockedReason": None,
        "unavailableReason": reason,
    }
