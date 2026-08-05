from __future__ import annotations

from typing import Any, Dict


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _bucket(score: float) -> str:
    if score >= 75:
        return "very_high"
    if score >= 55:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def analyze_demand_supply(snapshot: Dict[str, Any], *, direction: str) -> Dict[str, Any]:
    """Estimate demand, supply, and trap risk from observed market data.

    This does not claim to know institutional intent. It classifies visible
    footprints: money flow, delivery, candle location, VWAP, resistance/support,
    and extension. The goal is to block weak fresh entries and highlight where
    demand is actually being defended.
    """

    change_pct = _safe_float(snapshot.get("change_pct"))
    gap_pct = _safe_float(snapshot.get("gap_pct"))
    return_5d = _safe_float(snapshot.get("return_5d"))
    return_20d = _safe_float(snapshot.get("return_20d"))
    relative_volume = _safe_float(snapshot.get("relative_volume"), 1.0)
    intraday_volume = _safe_float(snapshot.get("intraday_volume_ratio"), 1.0)
    rsi = _safe_float(snapshot.get("rsi"), 50.0)
    close_location = _safe_float(snapshot.get("close_location"), 0.5)
    upper_wick = _safe_float(snapshot.get("upper_wick_pct"))
    lower_wick = _safe_float(snapshot.get("lower_wick_pct"))
    cmf = _safe_float(snapshot.get("cmf"))
    obv_slope = _safe_float(snapshot.get("obv_slope"))
    delivery_spike = _safe_float(snapshot.get("delivery_spike"), 1.0)
    distance_to_resistance = _safe_float(snapshot.get("distance_to_resistance_pct"), 99.0)
    distance_to_support = _safe_float(snapshot.get("distance_to_support_pct"), 99.0)
    tight_pct = _safe_float(snapshot.get("tight_consolidation_pct"), 99.0)
    bb_width = _safe_float(snapshot.get("bb_width_ratio"), 1.0)
    trend_regime = str(snapshot.get("trend_regime") or "range")

    breakout = bool(snapshot.get("breakout_20") or snapshot.get("intraday_breakout"))
    breakdown = bool(snapshot.get("breakdown_20"))
    above_vwap = bool(snapshot.get("above_vwap") or snapshot.get("intraday_above_vwap"))
    vwap_available = "above_vwap" in snapshot or "intraday_above_vwap" in snapshot
    ema20_available = "price_above_ema20" in snapshot
    ema50_available = "price_above_ema50" in snapshot
    above_ema20 = bool(snapshot.get("price_above_ema20"))
    above_ema50 = bool(snapshot.get("price_above_ema50"))
    near_resistance = bool(snapshot.get("near_resistance")) or distance_to_resistance <= 1.2
    near_support = bool(snapshot.get("near_support")) or distance_to_support <= 1.2
    high_volume = relative_volume >= 1.6 or intraday_volume >= 1.35
    very_high_volume = relative_volume >= 2.2 or intraday_volume >= 1.8
    delivery_available = bool(snapshot.get("delivery_available"))
    downtrend_context = trend_regime == "downtrend" or (
        ema20_available and ema50_available and not above_ema20 and not above_ema50
    )
    uptrend_context = trend_regime == "uptrend" or (
        ema20_available and ema50_available and above_ema20 and above_ema50
    )

    demand_score = 0.0
    supply_score = 0.0
    trap_score = 0.0
    bullish_reversal_score = 0.0
    bearish_reversal_score = 0.0
    selling_exhaustion_score = 0.0
    evidence: list[str] = []
    trap_reasons: list[str] = []

    if cmf >= 0.10:
        demand_score += 22
        evidence.append("CMF shows positive money flow")
    elif cmf >= 0.04:
        demand_score += 12
        evidence.append("CMF is mildly positive")
    elif cmf <= -0.10:
        supply_score += 22
        evidence.append("CMF shows distribution pressure")
    elif cmf <= -0.04:
        supply_score += 12
        evidence.append("CMF is mildly negative")

    if obv_slope > 0:
        demand_score += 16
        evidence.append("OBV slope supports accumulation")
    elif obv_slope < 0:
        supply_score += 16
        evidence.append("OBV slope shows supply")

    if vwap_available and above_vwap and close_location >= 0.65:
        demand_score += 12
        evidence.append("Price is holding above VWAP and closing strong")
    elif vwap_available and not above_vwap and close_location <= 0.45:
        supply_score += 12
        evidence.append("Price is below VWAP / closing weak")

    if lower_wick >= 0.30 and near_support:
        demand_score += 14
        evidence.append("Lower wick near support shows dip demand")
    if upper_wick >= 0.30 and near_resistance:
        supply_score += 18
        trap_score += 12
        evidence.append("Upper wick near resistance shows supply")
        trap_reasons.append("seller rejection near resistance")

    # A wide, high-volume candle is read from its own price response. It must
    # not inherit the direction of lagging EMA/RSI tallies.
    bullish_reversal_thrust = bool(
        change_pct >= 3.0
        and high_volume
        and close_location >= 0.72
        and upper_wick <= 0.28
    )
    bearish_reversal_thrust = bool(
        change_pct <= -3.0
        and high_volume
        and close_location <= 0.28
        and lower_wick <= 0.28
    )
    if bullish_reversal_thrust:
        bullish_reversal_score += 42
        demand_score += 28
        evidence.append("High-volume bullish range expansion closed near the session high")
        if very_high_volume:
            bullish_reversal_score += 8
        if vwap_available and above_vwap:
            bullish_reversal_score += 12
        if near_support or rsi <= 35:
            bullish_reversal_score += 18
        if downtrend_context:
            bullish_reversal_score += 8
    if bearish_reversal_thrust:
        bearish_reversal_score += 42
        supply_score += 28
        evidence.append("High-volume bearish range expansion closed near the session low")
        if very_high_volume:
            bearish_reversal_score += 8
        if vwap_available and not above_vwap:
            bearish_reversal_score += 12
        if near_resistance or rsi >= 65:
            bearish_reversal_score += 18
        if uptrend_context:
            bearish_reversal_score += 8

    # Oversold-at-support is exhaustion risk, not additional proof that the
    # bearish trend will continue.
    if rsi <= 32:
        selling_exhaustion_score += 25
    if near_support:
        selling_exhaustion_score += 20
    if lower_wick >= 0.30:
        selling_exhaustion_score += 20
    if high_volume and close_location >= 0.55:
        selling_exhaustion_score += 15
    if not breakdown and (near_support or rsi <= 32):
        selling_exhaustion_score += 10
    bullish_reversal_score += min(25, selling_exhaustion_score * 0.25)
    if selling_exhaustion_score >= 45:
        evidence.append("Oversold/support behaviour raises selling-exhaustion risk")

    if delivery_available and delivery_spike >= 1.2 and change_pct >= -0.5:
        demand_score += 12
        evidence.append(f"Delivery is elevated at {delivery_spike:.2f}x without price damage")
    elif delivery_available and delivery_spike >= 1.2 and change_pct < -0.5:
        supply_score += 14
        trap_score += 8
        evidence.append(f"Delivery is elevated at {delivery_spike:.2f}x on weakness")
        trap_reasons.append("delivery rose while price weakened")

    if tight_pct <= 6 and bb_width <= 0.85 and snapshot.get("higher_lows"):
        demand_score += 16
        evidence.append("Tight base with higher lows suggests demand absorption")

    if breakout and high_volume and close_location <= 0.50:
        supply_score += 18
        trap_score += 28
        trap_reasons.append("breakout failed to close near the high")
    if breakout and upper_wick >= 0.25:
        trap_score += 18
        trap_reasons.append("breakout candle has rejection wick")
    if gap_pct >= 2.0 and very_high_volume and close_location <= 0.55:
        supply_score += 16
        trap_score += 22
        trap_reasons.append("gap-up volume did not hold the high")
    if change_pct > 0 and high_volume and (cmf <= -0.04 or obv_slope < 0):
        supply_score += 18
        trap_score += 24
        trap_reasons.append("green move has distribution underneath")
    if direction == "bullish" and near_resistance and not breakout and high_volume:
        trap_score += 16
        trap_reasons.append("price is advertised near resistance before confirmation")
    if rsi >= 74 or return_5d >= 12 or return_20d >= 28:
        trap_score += 16
        trap_reasons.append("move is extended before fresh entry")
    if breakdown and lower_wick >= 0.35:
        trap_score += 12
        trap_reasons.append("bearish breakdown has snapback wick")

    demand_score = max(0, min(100, demand_score))
    supply_score = max(0, min(100, supply_score))
    trap_score = max(0, min(100, trap_score))
    bullish_reversal_score = max(0, min(100, bullish_reversal_score))
    bearish_reversal_score = max(0, min(100, bearish_reversal_score))
    selling_exhaustion_score = max(0, min(100, selling_exhaustion_score))

    if direction == "bearish" or downtrend_context:
        reversal_risk_score = bullish_reversal_score
        reversal_bias = "bullish" if reversal_risk_score >= 35 else "none"
    elif direction == "bullish" or uptrend_context:
        reversal_risk_score = bearish_reversal_score
        reversal_bias = "bearish" if reversal_risk_score >= 35 else "none"
    else:
        reversal_risk_score = max(bullish_reversal_score, bearish_reversal_score)
        reversal_bias = (
            "bullish"
            if bullish_reversal_score > bearish_reversal_score and reversal_risk_score >= 35
            else "bearish"
            if bearish_reversal_score > bullish_reversal_score and reversal_risk_score >= 35
            else "none"
        )
    reversal_watch = reversal_risk_score >= 45

    trend_points = 0.0
    if trend_regime in {"uptrend", "downtrend"}:
        trend_points += 35
    if ema20_available:
        trend_points += 20
    if ema50_available:
        trend_points += 20
    if abs(return_20d) >= 5:
        trend_points += min(25, abs(return_20d))
    trend_direction = (
        "bearish" if downtrend_context else "bullish" if uptrend_context else "range"
    )
    trend_strength = max(0, min(100, trend_points))

    if trap_score >= 70:
        status = "bull_trap_risk" if direction == "bullish" else "trap_risk"
        read = "Trap risk is high; avoid fresh entry until price retests and holds with lower supply."
    elif bullish_reversal_score >= 55 and downtrend_context:
        status = "bullish_reversal_candidate"
        read = (
            "The prior trend is bearish, but high-volume price response and/or selling exhaustion "
            "has opened a bullish reversal watch. Wait for reclaim/hold confirmation."
        )
    elif bearish_reversal_score >= 55 and uptrend_context:
        status = "bearish_reversal_risk"
        read = "The prior trend is bullish, but current price response warns of a bearish reversal."
    elif supply_score >= demand_score + 15:
        status = "supply_pressure"
        read = "Supply is stronger than demand; wait for rejection to clear before fresh long."
    elif demand_score >= supply_score + 15 and demand_score >= 45:
        status = "demand_absorption"
        read = "Demand is visible through money flow, VWAP/price action, or base absorption."
    elif demand_score >= 35:
        status = "early_demand"
        read = "Some demand evidence exists, but confirmation is still incomplete."
    else:
        status = "neutral"
        read = "No clean demand/supply edge from the current tape."

    if not evidence:
        evidence.append("Demand/supply evidence is limited in the current data.")

    footprint_signed_score = max(-100, min(100, demand_score - supply_score))
    footprint_bias = (
        "buying_pressure"
        if footprint_signed_score >= 20
        else "selling_pressure"
        if footprint_signed_score <= -20
        else "mixed_or_neutral"
    )
    availability = {
        "price_volume": "relative_volume" in snapshot or "intraday_volume_ratio" in snapshot,
        "delivery": delivery_available,
        "vwap": vwap_available,
        "cmf": "cmf" in snapshot and snapshot.get("cmf") is not None,
        "obv": "obv_slope" in snapshot and snapshot.get("obv_slope") is not None,
    }
    quality_score = sum(
        weight
        for key, weight in {
            "price_volume": 30,
            "delivery": 25,
            "vwap": 15,
            "cmf": 15,
            "obv": 15,
        }.items()
        if availability[key]
    )
    quality_grade = "high" if quality_score >= 80 else "medium" if quality_score >= 50 else "low"
    footprint = {
        "bias": footprint_bias,
        "score": round(footprint_signed_score, 1),
        "strength": round(abs(footprint_signed_score), 1),
        "evidence": list(dict.fromkeys(evidence))[:5],
        "data_quality": {
            "grade": quality_grade,
            "score": quality_score,
            "available": availability,
            "delivery_verified": delivery_available,
        },
        "identity_inference_supported": False,
        "score_is_calibrated_probability": False,
    }

    return {
        "status": status,
        "demandScore": round(demand_score, 1),
        "supplyScore": round(supply_score, 1),
        "trapRiskScore": round(trap_score, 1),
        "trapRisk": _bucket(trap_score),
        "smartMoneyRead": read,
        "evidence": list(dict.fromkeys(evidence))[:4],
        "trapReasons": list(dict.fromkeys(trap_reasons))[:3],
        "blockFreshEntry": direction == "bullish" and trap_score >= 60,
        "confirmationRule": (
            "For reversal watches, require a reclaim and hold above VWAP/trigger with a defined invalidation."
            if reversal_watch
            else "Buy only after trigger holds above VWAP/resistance and supply wick stays controlled."
        ),
        "trendDirection": trend_direction,
        "trendStrengthScore": round(trend_strength, 1),
        "reversalWatch": reversal_watch,
        "reversalBias": reversal_bias,
        "reversalRiskScore": round(reversal_risk_score, 1),
        "bullishReversalScore": round(bullish_reversal_score, 1),
        "bearishReversalScore": round(bearish_reversal_score, 1),
        "sellingExhaustionScore": round(selling_exhaustion_score, 1),
        "largeMoneyFootprint": footprint,
        "visibleFootprintRead": read,
    }
