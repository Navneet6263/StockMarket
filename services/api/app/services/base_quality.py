"""Multi-Candle Base Quality Scorer — feature-flagged, pure pandas, no fake data."""
from __future__ import annotations

import os
import logging
from typing import Dict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ENABLE_BASE_QUALITY: bool = os.getenv("ENABLE_BASE_QUALITY", "true").lower() == "true"
BASE_LOOKBACK: int = int(os.getenv("BASE_QUALITY_LOOKBACK", "40"))  # candles


def score_base_quality(frame: pd.DataFrame) -> Dict:
    """
    Score the quality of the current base/consolidation using last BASE_LOOKBACK candles.
    Returns baseQualityScore 0-100, pattern type, levels, reasons.
    Never crashes — returns unavailable dict on any error.
    """
    if not ENABLE_BASE_QUALITY:
        return _unavailable("base_quality_disabled")

    if frame is None or frame.empty or len(frame) < 20:
        return _unavailable("insufficient_history")

    try:
        window = frame.tail(BASE_LOOKBACK).copy()
        close = window["Close"].astype(float)
        high = window["High"].astype(float)
        low = window["Low"].astype(float)
        volume = window["Volume"].astype(float).replace(0, np.nan)

        price = float(close.iloc[-1])
        base_high = float(high.max())
        base_low = float(low.min())
        base_range_pct = round((base_high - base_low) / price * 100, 2) if price else 99.0

        score = 0
        labels = []
        reasons = []

        # ── 1. Tight range (20 pts) ───────────────────────────────────────────
        if base_range_pct <= 8:
            score += 20
            labels.append("tight_range")
            reasons.append(f"Base range is tight at {base_range_pct:.1f}% over {BASE_LOOKBACK} candles.")
        elif base_range_pct <= 15:
            score += 10
            reasons.append(f"Base range is moderate at {base_range_pct:.1f}%.")

        # ── 2. Volatility contraction (20 pts) ───────────────────────────────
        atr_early = _atr(window.head(BASE_LOOKBACK // 2))
        atr_late = _atr(window.tail(BASE_LOOKBACK // 2))
        if atr_early > 0 and atr_late < atr_early * 0.8:
            score += 20
            labels.append("vcp_contraction")
            reasons.append("ATR is contracting — VCP-style volatility compression detected.")
        elif atr_early > 0 and atr_late < atr_early * 0.95:
            score += 10
            reasons.append("Mild ATR contraction visible.")

        # ── 3. Higher lows (15 pts) ───────────────────────────────────────────
        lows_series = low.reset_index(drop=True)
        thirds = len(lows_series) // 3
        if thirds > 0:
            low_early = float(lows_series.iloc[:thirds].min())
            low_mid = float(lows_series.iloc[thirds:2*thirds].min())
            low_late = float(lows_series.iloc[2*thirds:].min())
            if low_late > low_mid > low_early:
                score += 15
                labels.append("higher_lows")
                reasons.append("Higher lows forming — structure is improving inside the base.")
            elif low_late > low_early:
                score += 7
                reasons.append("Partial higher lows visible.")

        # ── 4. Support zone respect (15 pts) ─────────────────────────────────
        support_level = float(low.quantile(0.15))
        support_touches = int(((low - support_level).abs() / price * 100 <= 1.5).sum())
        if support_touches >= 3:
            score += 15
            labels.append("support_respect")
            reasons.append(f"Support zone touched {support_touches}x — buyers defending the level.")
        elif support_touches >= 2:
            score += 8
            reasons.append(f"Support touched {support_touches}x.")

        # ── 5. Resistance pressure (15 pts) ──────────────────────────────────
        resistance_level = float(high.quantile(0.85))
        resistance_tests = int(((high - resistance_level).abs() / price * 100 <= 1.5).sum())
        if resistance_tests >= 2:
            score += 15
            labels.append("resistance_pressure")
            reasons.append(f"Resistance tested {resistance_tests}x — breakout level is well-defined.")
        elif resistance_tests >= 1:
            score += 7

        # ── 6. Volume dry-up / spike pattern (15 pts) ────────────────────────
        vol_avg = float(volume.mean())
        vol_late_avg = float(volume.tail(10).mean()) if len(volume) >= 10 else vol_avg
        if vol_avg > 0 and vol_late_avg < vol_avg * 0.75:
            score += 15
            labels.append("volume_dryup")
            reasons.append("Volume has dried up during the base — a visible contraction pattern; participant identity is unknown.")
        elif vol_avg > 0 and vol_late_avg < vol_avg * 0.9:
            score += 7
            reasons.append("Volume is declining during consolidation.")

        score = min(score, 100)

        # ── Pattern type ──────────────────────────────────────────────────────
        if "vcp_contraction" in labels and "higher_lows" in labels:
            pattern_type = "VCP"
        elif "tight_range" in labels and "volume_dryup" in labels:
            pattern_type = "flat_base"
        elif "higher_lows" in labels and "support_respect" in labels:
            pattern_type = "ascending_base"
        elif "tight_range" in labels:
            pattern_type = "tight_consolidation"
        elif score >= 30:
            pattern_type = "loose_base"
        else:
            pattern_type = "no_clear_base"

        # ── Levels ────────────────────────────────────────────────────────────
        breakout_trigger = round(resistance_level * 1.002, 2)
        invalidation_level = round(support_level * 0.99, 2)

        logger.debug(
            "[BASE] symbol score=%d pattern=%s range=%.1f%%",
            score, pattern_type, base_range_pct,
        )

        return {
            "baseQualityScore": score,
            "basePatternType": pattern_type,
            "baseLabels": list(dict.fromkeys(labels)),
            "baseRange": {
                "high": round(base_high, 2),
                "low": round(base_low, 2),
                "rangePct": base_range_pct,
            },
            "breakoutTrigger": breakout_trigger,
            "invalidationLevel": invalidation_level,
            "baseReason": "; ".join(reasons[:4]) or "Base evidence is limited.",
            "baseLookbackCandles": BASE_LOOKBACK,
            "baseAvailable": True,
        }

    except Exception as exc:
        logger.warning("[BASE] score_base_quality failed: %s", exc)
        return _unavailable(f"compute_error: {exc}")


def _atr(window: pd.DataFrame) -> float:
    if window.empty or len(window) < 2:
        return 0.0
    high = window["High"].astype(float)
    low = window["Low"].astype(float)
    close = window["Close"].astype(float)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.mean())


def _unavailable(reason: str) -> Dict:
    return {
        "baseQualityScore": None,
        "basePatternType": "unavailable",
        "baseLabels": [],
        "baseRange": None,
        "breakoutTrigger": None,
        "invalidationLevel": None,
        "baseReason": reason,
        "baseLookbackCandles": BASE_LOOKBACK,
        "baseAvailable": False,
    }
