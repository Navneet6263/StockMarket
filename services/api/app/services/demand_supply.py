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

    breakout = bool(snapshot.get("breakout_20") or snapshot.get("intraday_breakout"))
    breakdown = bool(snapshot.get("breakdown_20"))
    above_vwap = bool(snapshot.get("above_vwap") or snapshot.get("intraday_above_vwap"))
    near_resistance = bool(snapshot.get("near_resistance")) or distance_to_resistance <= 1.2
    near_support = bool(snapshot.get("near_support")) or distance_to_support <= 1.2
    high_volume = relative_volume >= 1.6 or intraday_volume >= 1.35
    very_high_volume = relative_volume >= 2.2 or intraday_volume >= 1.8
    delivery_available = bool(snapshot.get("delivery_available"))

    demand_score = 0.0
    supply_score = 0.0
    trap_score = 0.0
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

    if above_vwap and close_location >= 0.65:
        demand_score += 12
        evidence.append("Price is holding above VWAP and closing strong")
    elif not above_vwap and close_location <= 0.45:
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
    # RELAXED: Was RSI 74 / return_5d 12 / return_20d 28
    # Now: RSI 76 / return_5d 15 / return_20d 32 - more room for genuine moves
    if rsi >= 76 or return_5d >= 15 or return_20d >= 32:
        trap_score += 16
        trap_reasons.append("move is extended before fresh entry")
    if breakdown and lower_wick >= 0.35:
        trap_score += 12
        trap_reasons.append("bearish breakdown has snapback wick")

    demand_score = max(0, min(100, demand_score))
    supply_score = max(0, min(100, supply_score))
    trap_score = max(0, min(100, trap_score))

    # ── Status Classification ────────────────────────────────────────────
    if trap_score >= 70:
        status = "bull_trap_risk" if direction == "bullish" else "trap_risk"
        read = "Trap risk is high; avoid fresh entry until price retests and holds with lower supply."
    elif supply_score >= demand_score + 15:
        status = "supply_pressure"
        read = "Supply is stronger than demand; wait for rejection to clear before fresh long."
    elif demand_score >= supply_score + 15 and demand_score >= 35:  # FIX: threshold lowered 45→35
        status = "demand_absorption"
        read = "Demand is visible through money flow, VWAP/price action, or base absorption."
    elif demand_score >= 28:
        status = "early_demand"
        read = "Some demand evidence exists, but confirmation is still incomplete."
    else:
        status = "neutral"
        read = "No clean demand/supply edge from the current tape."

    # ── Direction Conflict Detection ──────────────────────────────────────
    # If the tape strongly disagrees with the scored direction, flag it.
    direction_conflict = False
    conflict_note = None

    if direction == "bullish" and supply_score >= 40 and demand_score <= 18:
        # System says bullish but supply completely dominates — conflict
        direction_conflict = True
        conflict_note = (
            f"⚠️ Direction conflict: Bullish signal but supply ({supply_score}) "
            f"far exceeds demand ({demand_score}). Wait for supply to clear."
        )
    elif direction == "bearish" and demand_score >= 35 and supply_score <= 18:
        # System says bearish but demand completely dominates — conflict
        direction_conflict = True
        conflict_note = (
            f"⚠️ Direction conflict: Bearish signal but demand ({demand_score}) "
            f"far exceeds supply ({supply_score}). Avoid short; wait for demand to fade."
        )

    if direction_conflict and conflict_note:
        evidence.append(conflict_note)

    # ── GTF (Forming Demand) special case ────────────────────────────────
    # When both demand and supply are elevated and equal — institutions are
    # fighting. Mark as Forming Demand rather than neutral.
    gtf_forming = (
        demand_score >= 40
        and supply_score >= 40
        and abs(demand_score - supply_score) <= 10
    )
    if gtf_forming and status == "neutral":
        status = "forming_demand"
        read = "GTF: Institutional Pending Orders Present"

    if not evidence:
        evidence.append("Demand/supply evidence is limited in the current data.")

    return {
        "status": status,
        "demandScore": round(demand_score, 1),
        "supplyScore": round(supply_score, 1),
        "trapRiskScore": round(trap_score, 1),
        "trapRisk": _bucket(trap_score),
        "smartMoneyRead": read,
        "evidence": list(dict.fromkeys(evidence))[:4],
        "trapReasons": list(dict.fromkeys(trap_reasons))[:3],
        # FIX: blockFreshEntry now works for BOTH bullish AND bearish conflicts
        "blockFreshEntry": (
            (direction == "bullish" and trap_score >= 60)
            or (direction == "bullish" and direction_conflict)
            or (direction == "bearish" and direction_conflict)
        ),
        "directionConflict": direction_conflict,
        "conflictNote": conflict_note,
        "confirmationRule": "Buy only after trigger holds above VWAP/resistance and supply wick stays controlled.",
    }
