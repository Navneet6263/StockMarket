from __future__ import annotations

from typing import Any, Dict


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_pct(price: float, level: float) -> float:
    if not price or not level:
        return 0.0
    return ((price - level) / level) * 100


def _bucket(score: int) -> str:
    if score >= 75:
        return "very_high"
    if score >= 55:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def analyze_entry_timing(snapshot: Dict[str, Any], *, direction: str) -> Dict[str, Any]:
    """Classify whether a directional signal is early, actionable, or already late.

    This uses only technical/liquidity evidence already present in the scanner. It is
    intentionally conservative for bullish fresh entries because late breakouts often
    meet profit-booking supply before continuing higher.
    """

    price = _safe_float(snapshot.get("price") or snapshot.get("close"))
    change_pct = _safe_float(snapshot.get("change_pct"))
    gap_pct = _safe_float(snapshot.get("gap_pct"))
    return_5d = _safe_float(snapshot.get("return_5d"))
    return_20d = _safe_float(snapshot.get("return_20d"))
    relative_volume = _safe_float(snapshot.get("relative_volume"), 1.0)
    intraday_volume_ratio = _safe_float(snapshot.get("intraday_volume_ratio"), 1.0)
    rsi = _safe_float(snapshot.get("rsi"), 50.0)
    close_location = _safe_float(snapshot.get("close_location"), 0.5)
    upper_wick_pct = _safe_float(snapshot.get("upper_wick_pct"))
    lower_wick_pct = _safe_float(snapshot.get("lower_wick_pct"))
    distance_to_resistance = _safe_float(snapshot.get("distance_to_resistance_pct"), 99.0)
    distance_to_support = _safe_float(snapshot.get("distance_to_support_pct"), 99.0)
    ema_20 = _safe_float(snapshot.get("ema_20"))
    rolling_vwap = _safe_float(snapshot.get("rolling_vwap"))
    ema20_distance_pct = _distance_pct(price, ema_20)
    vwap_distance_pct = _distance_pct(price, rolling_vwap)
    cmf = _safe_float(snapshot.get("cmf"))
    obv_slope = _safe_float(snapshot.get("obv_slope"))
    delivery_spike = _safe_float(snapshot.get("delivery_spike"))

    breakout = bool(snapshot.get("breakout_20") or snapshot.get("intraday_breakout"))
    breakdown = bool(snapshot.get("breakdown_20"))
    above_vwap = bool(snapshot.get("above_vwap") or snapshot.get("intraday_above_vwap"))
    near_resistance = bool(snapshot.get("near_resistance")) or distance_to_resistance <= 1.2
    near_support = bool(snapshot.get("near_support")) or distance_to_support <= 1.2
    strong_volume = relative_volume >= 1.6 or intraday_volume_ratio >= 1.35
    volume_spike = relative_volume >= 2.0 or intraday_volume_ratio >= 1.8

    reasons: list[str] = []
    labels: list[str] = []
    seller_pressure_score = 0
    pullback_score = 0

    if direction == "bullish":
        if near_resistance and not breakout:
            seller_pressure_score += 18
            reasons.append("Price is still pressing into resistance; breakout supply is not cleared.")
            labels.append("PROFIT_BOOKING_ZONE")
        elif near_resistance:
            seller_pressure_score += 8
            reasons.append("Price is near the old resistance zone, so profit-booking supply can appear.")

        if upper_wick_pct >= 0.35:
            seller_pressure_score += 18
            reasons.append("Long upper wick shows sellers rejected higher prices.")
            labels.append("SELLER_REJECTION")
        elif upper_wick_pct >= 0.22 and near_resistance:
            seller_pressure_score += 10
            reasons.append("Upper wick near resistance shows mild supply pressure.")

        if rsi >= 78:
            seller_pressure_score += 18
            reasons.append("RSI is overheated above 78; fresh entry is vulnerable to profit booking.")
            labels.append("RSI_OVERHEAT")
        elif rsi >= 72:
            seller_pressure_score += 10
            reasons.append("RSI is stretched; wait for cooling or retest before fresh entry.")

        if change_pct >= 5.0 or gap_pct >= 3.0:
            seller_pressure_score += 18
            reasons.append("Large same-day move/gap increases late-entry risk.")
            labels.append("CLIMACTIC_MOVE")
        elif change_pct >= 3.0 and volume_spike:
            seller_pressure_score += 14
            reasons.append("Fast green candle with volume spike can invite profit booking.")
            labels.append("VOLUME_EXHAUSTION")

        if return_5d >= 12 or return_20d >= 25:
            seller_pressure_score += 16
            reasons.append("Stock has already moved sharply over 5-20 sessions.")
            labels.append("EXTENDED_MOVE")
        elif return_5d >= 8:
            seller_pressure_score += 8
            reasons.append("Recent 5-day run-up is stretched; better entry usually comes on retest.")

        if ema20_distance_pct >= 8 or vwap_distance_pct >= 5:
            seller_pressure_score += 14
            reasons.append("Price is far above 20 EMA/VWAP, so risk/reward is poor for fresh entry.")
            labels.append("FAR_FROM_MEAN")
        elif ema20_distance_pct >= 5 or vwap_distance_pct >= 3:
            seller_pressure_score += 7
            reasons.append("Price is mildly extended from EMA/VWAP.")

        if cmf <= -0.05 or obv_slope < 0:
            seller_pressure_score += 12
            reasons.append("CMF/OBV shows distribution pressure under the bullish move.")
            labels.append("SELLER_DOMINANCE")

        if delivery_spike >= 1.2 and change_pct < 0:
            seller_pressure_score += 10
            reasons.append("Elevated delivery on weakness suggests supply is being absorbed slowly.")

        if breakout and close_location <= 0.45 and upper_wick_pct >= 0.25:
            seller_pressure_score += 22
            reasons.append("Breakout is not holding near the high; fake breakout risk is elevated.")
            labels.append("FAKE_BREAKOUT_RISK")

        if near_support or (ema_20 and 0 <= ema20_distance_pct <= 3.0):
            pullback_score += 18
        if 45 <= rsi <= 63:
            pullback_score += 10
        if relative_volume <= 1.15:
            pullback_score += 8

        if seller_pressure_score >= 75:
            entry_quality = "avoid"
            entry_timing = "climactic_candle"
            best_action = "AVOID_CHASE"
            setup_stage = "PROFIT_BOOKING_ZONE"
            reentry_plan = "Wait for a pullback toward VWAP/20 EMA or the breakout level, then enter only after price holds with lower selling volume."
        elif seller_pressure_score >= 55:
            entry_quality = "poor"
            entry_timing = "extended_move"
            best_action = "WAIT_FOR_PULLBACK"
            setup_stage = "AVOID_LATE_ENTRY"
            reentry_plan = "Do not buy the first spike. Wait for 2-5% cooling, a retest, or a fresh base with tight invalidation."
        elif breakout and above_vwap and strong_volume and rsi <= 72:
            entry_quality = "good"
            entry_timing = "early_breakout"
            best_action = "BUY_ONLY_ON_TRIGGER_HOLD"
            setup_stage = "EARLY_BREAKOUT"
            reentry_plan = "Entry is valid only while price holds above breakout/VWAP and upper wick stays controlled."
        elif pullback_score >= 24:
            entry_quality = "good"
            entry_timing = "pullback_retest"
            best_action = "BUY_ONLY_ON_RETEST_HOLD"
            setup_stage = "PULLBACK_RETEST"
            reentry_plan = "Prefer entry near support/VWAP/20 EMA after volume contracts and price starts turning up."
        elif near_resistance:
            entry_quality = "watch"
            entry_timing = "near_trigger"
            best_action = "WATCH_TRIGGER"
            setup_stage = "ALERT_ABOVE_LEVEL"
            reentry_plan = "No buy before resistance clears with volume and price sustains above the trigger."
        else:
            entry_quality = "watch"
            entry_timing = "mid_trend"
            best_action = "WAIT_FOR_RETEST"
            setup_stage = "MID_TREND"
            reentry_plan = "Wait for a clean trigger or pullback; avoid entering in the middle of the candle."

    elif direction == "bearish":
        if breakdown and not above_vwap and strong_volume:
            seller_pressure_score += 35
            reasons.append("Breakdown has volume and is below VWAP, so seller control is active.")
            labels.append("SELLER_DOMINANCE")
            entry_quality = "watch"
            entry_timing = "bearish_breakdown"
            best_action = "AVOID_FRESH_LONG"
            setup_stage = "BEARISH_CONFIRMED"
            reentry_plan = "For longs, wait until price reclaims VWAP/resistance; bearish risk stays active below breakdown."
        elif lower_wick_pct >= 0.35 or rsi <= 28:
            seller_pressure_score += 20
            reasons.append("Downside is stretched and could snap back; avoid late bearish entry.")
            labels.append("SNAPBACK_RISK")
            entry_quality = "poor"
            entry_timing = "late_breakdown"
            best_action = "AVOID_FRESH_LONG"
            setup_stage = "BEARISH_RISK"
            reentry_plan = "Avoid fresh long until weakness is invalidated; bearish entries need a failed bounce."
        else:
            seller_pressure_score += 25 if not above_vwap else 10
            entry_quality = "watch"
            entry_timing = "bearish_watch"
            best_action = "AVOID_FRESH_LONG"
            setup_stage = "BEARISH_RISK"
            reentry_plan = "Avoid fresh long unless price reclaims VWAP and invalidates the bearish setup."
    else:
        entry_quality = "watch"
        entry_timing = "no_clear_edge"
        best_action = "NO_TRADE"
        setup_stage = "NO_CLEAR_EDGE"
        reentry_plan = "Wait for direction, trigger, volume, and invalidation to become clear."

    seller_pressure = _bucket(int(min(100, seller_pressure_score)))
    profit_booking_risk = _bucket(int(min(100, seller_pressure_score if direction == "bullish" else seller_pressure_score * 0.6)))
    attention_only = entry_quality in {"poor", "avoid"} or best_action in {"WATCH_TRIGGER", "WAIT_FOR_RETEST", "AVOID_CHASE", "WAIT_FOR_PULLBACK"}
    allow_buy_call = direction == "bullish" and entry_quality == "good" and best_action.startswith("BUY_ONLY")

    if not reasons:
        reasons.append("No major late-entry or seller-pressure signal detected yet.")
    if not labels:
        labels.append("ENTRY_TIMING_WATCH" if entry_quality == "watch" else "ENTRY_TIMING_OK")

    return {
        "entry_quality": entry_quality,
        "entry_timing": entry_timing,
        "seller_pressure": seller_pressure,
        "seller_pressure_score": int(min(100, seller_pressure_score)),
        "profit_booking_risk": profit_booking_risk,
        "best_action": best_action,
        "setup_stage": setup_stage,
        "reentry_plan": reentry_plan,
        "reasons": list(dict.fromkeys(reasons))[:5],
        "trade_labels": list(dict.fromkeys(labels)),
        "attention_only": attention_only,
        "allow_buy_call": allow_buy_call,
    }
