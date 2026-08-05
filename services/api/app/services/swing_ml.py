"""Five-day model adapter with explicit horizon and calibration checks."""
from __future__ import annotations

import os
import logging
from typing import Dict

import pandas as pd

logger = logging.getLogger(__name__)

ENABLE_SWING_5D: bool = os.getenv("ENABLE_SWING_5D", "true").lower() == "true"
SWING_5D_MIN_RETURN: float = float(os.getenv("SWING_5D_MIN_RETURN_PCT", "2.0"))  # 2% threshold


def compute_swing_5d(frame: pd.DataFrame, model) -> Dict:
    """
    Use a model only when it was explicitly trained for a five-session target.
    A next-day model is not relabelled as a five-day probability.
    """
    if not ENABLE_SWING_5D:
        return _unavailable("swing_5d_disabled")

    if model is None or not getattr(model, "is_trained", False):
        return _unavailable("model_not_trained")

    target_horizon = getattr(model, "target_horizon_days", getattr(model, "horizon_days", None))
    if target_horizon != 5:
        return _unavailable("model_not_trained_for_5d_target")

    if frame is None or frame.empty or len(frame) < 30:
        return _unavailable("insufficient_history")

    try:
        features = model.prepare_features(frame)
        if features.empty:
            return _unavailable("feature_preparation_failed")

        latest_features = features.iloc[-1:]
        proba = model.model.predict_proba(latest_features)[0]
        model_score_up = float(proba[1])
        model_score_down = float(proba[0])
        probability_calibrated = bool(getattr(model, "is_probability_calibrated", False))

        # ── 5-day expected return from historical distribution ────────────────
        close = frame["Close"].astype(float)
        if len(close) >= 10:
            returns_5d = close.pct_change(5).dropna() * 100
            # Use last 60 days of 5d returns as distribution estimate
            recent_returns = returns_5d.tail(60)
            expected_return = round(float(recent_returns.mean()), 2)
            return_std = round(float(recent_returns.std()), 2)
        else:
            expected_return = None
            return_std = None

        logger.debug(
            "[SWING5D] model_score_up=%.3f calibrated=%s expected_return=%.2f",
            model_score_up, probability_calibrated, expected_return or 0,
        )

        return {
            "mlSwing5dProbability": round(model_score_up, 4) if probability_calibrated else None,
            "mlSwing5dProbabilityUp": round(model_score_up, 4) if probability_calibrated else None,
            "mlSwing5dModelScoreUp": round(model_score_up * 100, 1),
            "mlSwing5dScoreMeaning": "Model alignment score, not probability" if not probability_calibrated else "Calibrated probability",
            "mlSwing5dProbabilityCalibrated": probability_calibrated,
            "mlSwing5dExpectedReturn": expected_return,
            "mlSwing5dReturnStd": return_std,
            "mlSwing5dMinReturnThreshold": SWING_5D_MIN_RETURN,
            "mlNextDayProbabilityUp": None,
            "mlNextDayProbabilityDown": None,
            "modelMode": "five_day_target",
            "swing5dAvailable": True,
        }

    except Exception as exc:
        logger.warning("[SWING5D] compute failed: %s", exc)
        return _unavailable(f"compute_error: {exc}")


def _unavailable(reason: str) -> Dict:
    return {
        "mlSwing5dProbability": None,
        "mlSwing5dProbabilityUp": None,
        "mlSwing5dModelScoreUp": None,
        "mlSwing5dScoreMeaning": None,
        "mlSwing5dProbabilityCalibrated": False,
        "mlSwing5dExpectedReturn": None,
        "mlSwing5dReturnStd": None,
        "mlSwing5dMinReturnThreshold": SWING_5D_MIN_RETURN,
        "mlNextDayProbabilityUp": None,
        "mlNextDayProbabilityDown": None,
        "modelMode": "next_day",
        "swing5dAvailable": False,
        "swing5dUnavailableReason": reason,
    }
