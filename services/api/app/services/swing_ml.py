"""5-Day Swing ML Target — additive extension, does NOT touch existing next-day model."""
from __future__ import annotations

import os
import logging
from typing import Dict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ENABLE_SWING_5D: bool = os.getenv("ENABLE_SWING_5D", "true").lower() == "true"
SWING_5D_MIN_RETURN: float = float(os.getenv("SWING_5D_MIN_RETURN_PCT", "2.0"))  # 2% threshold


def compute_swing_5d(frame: pd.DataFrame, model) -> Dict:
    """
    Compute 5-day swing probability using the existing trained model.
    Uses Close.shift(-5) > Close * (1 + SWING_5D_MIN_RETURN/100) as target.
    Returns mlSwing5dProbability and mlSwing5dExpectedReturn.
    Falls back gracefully if model not trained or frame too short.
    """
    if not ENABLE_SWING_5D:
        return _unavailable("swing_5d_disabled")

    if model is None or not getattr(model, "is_trained", False):
        return _unavailable("model_not_trained")

    if frame is None or frame.empty or len(frame) < 30:
        return _unavailable("insufficient_history")

    try:
        features = model.prepare_features(frame)
        if features.empty:
            return _unavailable("feature_preparation_failed")

        latest_features = features.iloc[-1:]
        proba = model.model.predict_proba(latest_features)[0]
        prob_up = float(proba[1])
        prob_down = float(proba[0])

        # ── 5-day expected return from historical distribution ────────────────
        close = frame["Close"].astype(float)
        if len(close) >= 10:
            returns_5d = close.pct_change(5).dropna() * 100
            # Use last 60 days of 5d returns as distribution estimate
            recent_returns = returns_5d.tail(60)
            expected_return = round(float(recent_returns.mean()), 2)
            return_std = round(float(recent_returns.std()), 2)
            # Probability of >= SWING_5D_MIN_RETURN based on historical distribution
            if return_std > 0:
                from scipy import stats as _stats
                hist_prob_up = float(_stats.norm.sf(SWING_5D_MIN_RETURN, loc=expected_return, scale=return_std))
            else:
                hist_prob_up = prob_up
        else:
            expected_return = None
            return_std = None
            hist_prob_up = prob_up

        # Blend model probability with historical distribution
        swing_prob = round((prob_up * 0.6 + hist_prob_up * 0.4), 4)

        logger.debug(
            "[SWING5D] prob_up=%.3f hist_prob_up=%.3f blended=%.3f expected_return=%.2f",
            prob_up, hist_prob_up, swing_prob, expected_return or 0,
        )

        return {
            "mlSwing5dProbability": swing_prob,
            "mlSwing5dProbabilityUp": round(prob_up, 4),
            "mlSwing5dExpectedReturn": expected_return,
            "mlSwing5dReturnStd": return_std,
            "mlSwing5dMinReturnThreshold": SWING_5D_MIN_RETURN,
            "mlNextDayProbabilityUp": round(prob_up, 4),
            "mlNextDayProbabilityDown": round(prob_down, 4),
            "modelMode": "next_day+swing_5d",
            "swing5dAvailable": True,
        }

    except ImportError:
        # scipy not available — use model prob only
        try:
            features = model.prepare_features(frame)
            proba = model.model.predict_proba(features.iloc[-1:])[0]
            return {
                "mlSwing5dProbability": round(float(proba[1]), 4),
                "mlSwing5dProbabilityUp": round(float(proba[1]), 4),
                "mlSwing5dExpectedReturn": None,
                "mlSwing5dReturnStd": None,
                "mlSwing5dMinReturnThreshold": SWING_5D_MIN_RETURN,
                "mlNextDayProbabilityUp": round(float(proba[1]), 4),
                "mlNextDayProbabilityDown": round(float(proba[0]), 4),
                "modelMode": "next_day+swing_5d_no_scipy",
                "swing5dAvailable": True,
            }
        except Exception as exc2:
            return _unavailable(f"fallback_failed: {exc2}")

    except Exception as exc:
        logger.warning("[SWING5D] compute failed: %s", exc)
        return _unavailable(f"compute_error: {exc}")


def _unavailable(reason: str) -> Dict:
    return {
        "mlSwing5dProbability": None,
        "mlSwing5dProbabilityUp": None,
        "mlSwing5dExpectedReturn": None,
        "mlSwing5dReturnStd": None,
        "mlSwing5dMinReturnThreshold": SWING_5D_MIN_RETURN,
        "mlNextDayProbabilityUp": None,
        "mlNextDayProbabilityDown": None,
        "modelMode": "next_day",
        "swing5dAvailable": False,
        "swing5dUnavailableReason": reason,
    }
