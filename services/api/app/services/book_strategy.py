"""Connect closed-candle research rules to scanner/live decisions without I/O.

Book-inspired context and risk discipline, not an author's trading system or a
calibrated success probability. Numeric thresholds are engineering heuristics.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.candle_decision import RULES_VERSION, analyze_candle_setup, completed_candles
from app.services.trade_plan import actionable_plan_fields, assess_structural_plan, watch_only_plan_fields


def build_candle_context(daily: pd.DataFrame | None, intraday: pd.DataFrame | None,
                         *, enhanced: bool, now: Any = None) -> dict:
    # Preliminary universe scan does not run extra dataframe analysis. Only the
    # existing capped intraday shortlist receives the closed-bar engine.
    if not enhanced:
        return {"status": "UNAVAILABLE", "entry_ready": False, "available": False,
                "timeframe": "15m", "phase": "awaiting_intraday_analysis",
                "reasons": ["awaiting_capped_intraday_analysis"],
                "rules_version": RULES_VERSION, "basis": "research_heuristic"}
    closed = completed_candles(daily, timeframe="1d", now=now)
    higher = "neutral"
    if len(closed) >= 25:
        close = closed["Close"]
        ema = close.ewm(span=20, adjust=False).mean()
        if close.iloc[-1] > ema.iloc[-1] > ema.iloc[-6]:
            higher = "bullish"
        elif close.iloc[-1] < ema.iloc[-1] < ema.iloc[-6]:
            higher = "bearish"
    # MarketDataService stores intraday indexes as UTC with tz removed. Restore
    # that contract here; the standalone engine treats naive input as IST.
    if isinstance(intraday, pd.DataFrame) and isinstance(intraday.index, pd.DatetimeIndex) and intraday.index.tz is None:
        intraday = intraday.copy(deep=False)
        intraday.index = intraday.index.tz_localize("UTC")
    result = analyze_candle_setup(intraday, timeframe="15m", higher_timeframe_trend=higher, now=now)
    result["higher_timeframe_available"] = len(closed) >= 25
    if not result["higher_timeframe_available"] and result.get("entry_ready"):
        result.update(status="WAIT", entry_ready=False)
        result["reasons"] = [*result.get("reasons", []), "higher_timeframe_history_unavailable"]
    return result


def apply_candle_gate(raw: dict) -> dict:
    """Idempotent final gate, reapplied after enrichment to prevent BUY resurrection.

    A READY candle can arm live validation; it never issues a static buy. Legacy
    records without metadata retain their behavior and still face live risk gates.
    Existing market/trap/structural vetoes are never cleared here.
    """
    signal = dict(raw)
    candle = signal.get("candle_setup")
    if not isinstance(candle, dict):
        return signal
    candle = dict(candle)
    signal["candle_setup"] = candle
    direction = str(signal.get("direction") or "neutral").lower()
    candle_side = str(candle.get("direction") or "neutral").lower()
    conflict = candle_side in {"bullish", "bearish"} and candle_side != direction
    if conflict and candle.get("entry_ready"):
        candle.update(status="WAIT", entry_ready=False)
        candle["reasons"] = [*candle.get("reasons", []), "scanner_direction_conflicts_with_candle"]
    demand = signal.get("demand_supply") or {}
    demand = demand if isinstance(demand, dict) else {}
    bull_trap = signal.get("bull_trap") or {}
    trap_block = bool(demand.get("blockFreshEntry") or signal.get("blockFreshEntry")
                      or (isinstance(bull_trap, dict) and bull_trap.get("bull_trap_detected") and direction == "bullish")
                      or str(demand.get("trapRisk") or signal.get("trap_risk") or signal.get("trapRisk") or "").lower() in {"high", "very_high"})
    hard_block = bool(signal.get("marketGateBlocked") or signal.get("blockedBuyReason")
                      or trap_block
                      or signal.get("tradePlanGateBlocked") or signal.get("entry_plan_blocked")
                      or signal.get("chase_risk") or signal.get("overextended_fresh_entry")
                      or str(signal.get("effectiveAction") or signal.get("action") or "").upper()
                      in {"AVOID", "EXIT", "NO_TRADE", "AVOID_CHASE", "WAIT_FOR_PULLBACK",
                          "WAIT_FOR_BETTER_ENTRY", "AVOID_LATE_ENTRY", "PROFIT_BOOKING_RISK"})
    plan = assess_structural_plan(candle_side, candle.get("trigger"),
                                  candle.get("invalidation"), [candle.get("target")])
    plan_ok = plan["allowed"] and not conflict
    if not plan_ok and candle.get("entry_ready"):
        candle.update(status="WAIT", entry_ready=False)
        candle["reasons"] = [*candle.get("reasons", []), plan.get("blockReason") or "invalid_candle_trade_plan"]
    if plan_ok and not hard_block:
        signal.update(actionable_plan_fields(plan))
        signal["new_target"] = plan["target1"]
    else:
        # Do not leave legacy fabricated targets or stopless entry aliases behind.
        proposed = watch_only_plan_fields(plan)
        for key in ("entry_trigger", "safe_entry_price", "entry_zone", "invalidation",
                    "invalidation_level", "stop_loss", "target", "target_price", "target_1",
                    "target_2", "extended_target_price", "extended_target", "risk_reward",
                    "riskPct", "riskPerShare", "maxPositionPctAt1PctAccountRisk",
                    "proposed_entry", "structural_invalidation", "opposing_structure_target"):
            signal[key] = proposed[key]
        signal["new_target"] = None
    ready = bool(candle.get("status") == "READY" and candle.get("entry_ready") is True
                 and plan_ok and not hard_block)
    signal.update(
        candleGateReady=ready, candleGateBlocked=not ready,
        effectiveAction="WATCH", actionOverride="WATCH", action="WATCH", recommended_action="WATCH",
        allow_buy_call=False, allow_trade_call=False, attention_only=True, freshEntryAllowed=False,
        requires_live_confirmation=not hard_block and candle.get("status") != "INVALID",
        requires_full_tick=True, live_min_confirmations=max(3, int(signal.get("live_min_confirmations") or 3)),
        entry_plan_status=("ARMABLE_AFTER_LIVE_CONFIRMATION" if ready else
                           "CANDLE_" + str(candle.get("status") or "UNAVAILABLE")),
        is_valid_entry_after_move=False,
    )
    if hard_block or candle.get("status") == "INVALID":
        signal["entry_plan_blocked"] = True
        if trap_block:
            signal["entry_plan_blocked_reason"] = "Existing trap/supply gate blocks a fresh entry."
    elif plan_ok:
        signal["entry_plan_blocked"] = False
    signal["candleGateReason"] = "; ".join(str(x).replace("_", " ") for x in candle.get("reasons", []))
    signal["entry_plan_blocked_reason"] = (
        signal.get("entry_plan_blocked_reason") or signal["candleGateReason"]
        if signal.get("entry_plan_blocked") else None
    )
    return signal
