"""Delivery Volume Accumulation — feature-flagged, honest 'unknown' when data missing."""
from __future__ import annotations

import os
import logging
from typing import Dict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ENABLE_DELIVERY_CHECK: bool = os.getenv("ENABLE_DELIVERY_CHECK", "true").lower() == "true"

_DELIVERY_COLS = ("DELIV_PER", "DELIVERY_PCT", "DELIVERY_PERCENT")


def analyse_delivery(frame: pd.DataFrame) -> Dict:
    """
    Analyse delivery volume trend over 3d / 5d vs 20d average.
    Returns deliverySignal: accumulation | distribution_risk | neutral | unknown.
    Never fakes data — returns unknown when delivery column absent.
    """
    if not ENABLE_DELIVERY_CHECK:
        return _unknown("delivery_check_disabled")

    if frame is None or frame.empty or len(frame) < 10:
        return _unknown("insufficient_history")

    # ── Find delivery column ──────────────────────────────────────────────────
    delivery_raw: pd.Series | None = None
    for col in _DELIVERY_COLS:
        if col in frame.columns:
            delivery_raw = pd.to_numeric(frame[col], errors="coerce")
            if delivery_raw.notna().sum() >= 5:
                break
            delivery_raw = None

    if delivery_raw is None and "Deliverable Volume" in frame.columns:
        base_vol = frame["Volume"].astype(float).replace(0, np.nan)
        delivery_raw = pd.to_numeric(frame["Deliverable Volume"], errors="coerce") / base_vol

    if delivery_raw is None:
        logger.debug("[DELIVERY] No delivery column found — returning unknown")
        return _unknown("no_delivery_data_in_feed")

    try:
        # Normalise to 0-1 ratio if values look like percentages (>1)
        if delivery_raw.median() > 1:
            delivery_raw = delivery_raw / 100.0
        delivery = delivery_raw.clip(lower=0, upper=1).dropna()

        if len(delivery) < 5:
            return _unknown("too_few_delivery_rows")

        avg_20 = float(delivery.tail(20).mean())
        avg_5 = float(delivery.tail(5).mean())
        avg_3 = float(delivery.tail(3).mean())
        latest = float(delivery.iloc[-1])

        spike_vs_20 = round(latest / avg_20, 2) if avg_20 > 0 else 1.0
        trend_3d = round(avg_3 / avg_20, 2) if avg_20 > 0 else 1.0
        trend_5d = round(avg_5 / avg_20, 2) if avg_20 > 0 else 1.0

        # Price direction over last 3 days
        close = frame["Close"].astype(float)
        price_change_3d = float(close.pct_change(3).iloc[-1] * 100) if len(close) >= 4 else 0.0

        # ── Signal classification ─────────────────────────────────────────────
        delivery_rising = trend_3d >= 1.1 or spike_vs_20 >= 1.2
        delivery_falling = trend_3d <= 0.85

        if delivery_rising and price_change_3d >= -0.5:
            signal = "accumulation"
            reason = (
                f"Delivery ratio {latest:.1%} is {spike_vs_20:.2f}x the 20-day avg "
                f"with price flat/up — institutional accumulation likely."
            )
        elif delivery_rising and price_change_3d < -0.5:
            signal = "distribution_risk"
            reason = (
                f"Delivery rising ({spike_vs_20:.2f}x avg) while price fell {price_change_3d:.1f}% "
                "— possible distribution or forced selling."
            )
        elif delivery_falling:
            signal = "neutral"
            reason = "Delivery volume is below average — no strong accumulation or distribution signal."
        else:
            signal = "neutral"
            reason = "Delivery volume is near average — no clear directional signal."

        logger.debug(
            "[DELIVERY] signal=%s spike=%.2fx trend_3d=%.2fx price_3d=%.2f%%",
            signal, spike_vs_20, trend_3d, price_change_3d,
        )

        return {
            "deliverySignal": signal,
            "deliveryTrendScore": round((trend_3d + trend_5d) / 2, 2),
            "deliverySpikeVs20d": spike_vs_20,
            "deliveryTrend3d": trend_3d,
            "deliveryTrend5d": trend_5d,
            "deliveryLatestRatio": round(latest, 3),
            "deliveryAvg20d": round(avg_20, 3),
            "deliveryReason": reason,
            "deliveryAvailable": True,
        }

    except Exception as exc:
        logger.warning("[DELIVERY] analyse_delivery failed: %s", exc)
        return _unknown(f"compute_error: {exc}")


def _unknown(reason: str) -> Dict:
    return {
        "deliverySignal": "unknown",
        "deliveryTrendScore": None,
        "deliverySpikeVs20d": None,
        "deliveryTrend3d": None,
        "deliveryTrend5d": None,
        "deliveryLatestRatio": None,
        "deliveryAvg20d": None,
        "deliveryReason": reason,
        "deliveryAvailable": False,
    }
