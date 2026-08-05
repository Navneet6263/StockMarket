"""Shared structural trade-plan validation.

The scanner may observe a valid demand/supply zone without having a safe trade
from the current price.  This module keeps those two ideas separate: structure
can remain visible while a wide stop, missing opposing level, or poor reward to
risk leaves the setup watch-only.
"""
from __future__ import annotations

import os
from typing import Any, Iterable


MAX_STRUCTURAL_RISK_PCT = max(
    0.25, float(os.getenv("MAX_STRUCTURAL_RISK_PCT", "4.0"))
)
MIN_STRUCTURAL_RISK_REWARD = max(
    0.1, float(os.getenv("MIN_STRUCTURAL_RISK_REWARD", "1.5"))
)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def nearest_opposing_structure(
    direction: str,
    entry: float,
    candidates: Iterable[Any],
) -> float | None:
    """Return the nearest real opposing level; never manufacture an R target."""

    levels = [level for value in candidates if (level := _number(value)) is not None]
    if str(direction).lower() == "bearish":
        valid = [level for level in levels if level < entry]
        return max(valid) if valid else None
    valid = [level for level in levels if level > entry]
    return min(valid) if valid else None


def assess_structural_plan(
    direction: str,
    entry: Any,
    stop: Any,
    opposing_levels: Iterable[Any],
    *,
    max_risk_pct: float = MAX_STRUCTURAL_RISK_PCT,
    min_risk_reward: float = MIN_STRUCTURAL_RISK_REWARD,
) -> dict[str, Any]:
    """Validate stop distance and reward against observed market structure."""

    side = -1 if str(direction).lower() == "bearish" else 1
    entry_value = _number(entry)
    stop_value = _number(stop)
    target_value = (
        nearest_opposing_structure(direction, entry_value, opposing_levels)
        if entry_value is not None
        else None
    )
    result: dict[str, Any] = {
        "allowed": False,
        "direction": "bearish" if side < 0 else "bullish",
        "entry": entry_value,
        "stop": stop_value,
        "target1": target_value,
        "riskPerShare": None,
        "riskPct": None,
        "riskReward": None,
        "maxPositionPctAt1PctAccountRisk": None,
        "maxStructuralRiskPct": round(float(max_risk_pct), 3),
        "minimumStructuralRiskReward": round(float(min_risk_reward), 3),
        "blockCode": None,
        "blockReason": None,
        "targetMethod": "nearest_observed_opposing_structure",
    }

    if entry_value is None or stop_value is None:
        result.update(
            blockCode="INCOMPLETE_PLAN",
            blockReason="Entry or structural invalidation is unavailable.",
        )
        return result
    if side * (entry_value - stop_value) <= 0:
        result.update(
            blockCode="INVALID_STOP_SIDE",
            blockReason="Structural invalidation is on the wrong side of entry.",
        )
        return result

    risk = abs(entry_value - stop_value)
    risk_pct = risk / entry_value * 100
    result.update(
        riskPerShare=round(risk, 4),
        riskPct=round(risk_pct, 3),
        maxPositionPctAt1PctAccountRisk=round(min(100.0, 100.0 / risk_pct), 2),
    )
    if risk_pct > max_risk_pct:
        result.update(
            blockCode="STRUCTURAL_STOP_TOO_WIDE",
            blockReason=(
                f"Structural stop is {risk_pct:.2f}% away; wait for a closer entry "
                f"instead of tightening it inside the zone (cap {max_risk_pct:.2f}%)."
            ),
        )
        return result
    if target_value is None:
        result.update(
            blockCode="NO_OPPOSING_STRUCTURE_TARGET",
            blockReason="No observed opposing structure provides a defensible target.",
        )
        return result

    reward = side * (target_value - entry_value)
    risk_reward = reward / risk
    result["riskReward"] = round(risk_reward, 3)
    if risk_reward < min_risk_reward:
        result.update(
            blockCode="INSUFFICIENT_STRUCTURE_REWARD",
            blockReason=(
                f"Nearest opposing structure offers only {risk_reward:.2f}R; "
                f"wait for a better entry (minimum {min_risk_reward:.2f}R)."
            ),
        )
        return result

    result["allowed"] = True
    return result


def actionable_plan_fields(plan: dict[str, Any]) -> dict[str, Any]:
    """Common API fields for a validated structural plan.

    Target 2 is intentionally absent.  After target 1 the lifecycle engine
    manages the remaining runner with its trailing stop instead of inventing a
    farther price objective.
    """

    entry = float(plan["entry"])
    stop = float(plan["stop"])
    target = float(plan["target1"])
    return {
        "entry_trigger": round(entry, 2),
        "safe_entry_price": round(entry, 2),
        "entry_zone": {"low": round(entry * 0.997, 2), "high": round(entry * 1.003, 2)},
        "invalidation": round(stop, 2),
        "invalidation_level": round(stop, 2),
        "stop_loss": round(stop, 2),
        "target": round(target, 2),
        "target_price": round(target, 2),
        "target_1": round(target, 2),
        "target_2": None,
        "extended_target_price": None,
        "extended_target": None,
        "risk_reward": plan["riskReward"],
        "riskPct": plan["riskPct"],
        "riskPerShare": plan["riskPerShare"],
        "maxPositionPctAt1PctAccountRisk": plan["maxPositionPctAt1PctAccountRisk"],
        "targetMethod": plan["targetMethod"],
        "entry_plan_status": "ARMABLE_AFTER_LIVE_CONFIRMATION",
        "entry_plan_blocked": False,
        "enforce_structural_risk_cap": True,
        "max_structural_risk_pct": plan["maxStructuralRiskPct"],
    }


def watch_only_plan_fields(plan: dict[str, Any]) -> dict[str, Any]:
    """Clear stale actionable levels while preserving the observed proposal."""

    return {
        "proposed_entry": plan.get("entry"),
        "structural_invalidation": plan.get("stop"),
        "opposing_structure_target": plan.get("target1"),
        "entry_trigger": None,
        "safe_entry_price": None,
        "entry_zone": None,
        "invalidation": None,
        "invalidation_level": None,
        "stop_loss": None,
        "target": None,
        "target_price": None,
        "target_1": None,
        "target_2": None,
        "extended_target_price": None,
        "extended_target": None,
        "risk_reward": plan.get("riskReward"),
        "riskPct": plan.get("riskPct"),
        "riskPerShare": plan.get("riskPerShare"),
        "maxPositionPctAt1PctAccountRisk": plan.get("maxPositionPctAt1PctAccountRisk"),
        "targetMethod": plan.get("targetMethod"),
        "entry_plan_status": plan.get("blockCode") or "WATCH_ONLY",
        "entry_plan_blocked": True,
        "entry_plan_blocked_reason": plan.get("blockReason"),
        "enforce_structural_risk_cap": True,
        "max_structural_risk_pct": plan.get("maxStructuralRiskPct"),
        "action": "WAIT_FOR_BETTER_ENTRY",
        "recommended_action": "WAIT_FOR_BETTER_ENTRY",
        "attention_only": True,
        "allow_buy_call": False,
        "allow_trade_call": False,
        "requires_live_confirmation": False,
        "requires_full_tick": False,
        "freshEntryAllowed": False,
        "alert_level": "watchlist",
    }
