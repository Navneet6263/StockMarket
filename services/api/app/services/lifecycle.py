from __future__ import annotations

from typing import Any


def directional_return(direction: str, entry: float, current: float) -> float:
    if not entry or not current:
        return 0.0
    if direction == "bullish":
        return ((current / entry) - 1) * 100
    if direction == "bearish":
        return ((entry / current) - 1) * 100
    return 0.0


def target_progress(direction: str, entry: float, current: float, target: float) -> float:
    if not entry or not current or not target or target == entry:
        return 0.0
    if direction == "bullish":
        return max(0.0, min(200.0, ((current - entry) / (target - entry)) * 100))
    if direction == "bearish":
        return max(0.0, min(200.0, ((entry - current) / (entry - target)) * 100))
    return 0.0


def build_lifecycle_advice(
    *,
    direction: str,
    entry_price: float,
    current_price: float,
    target_1: float,
    target_2: float | None = None,
    stop_loss: float | None = None,
    trailing_stop: float | None = None,
    above_vwap: bool | None = None,
    above_ema20: bool | None = None,
    volume_ok: bool = False,
    overextended: bool = False,
    volume_fading: bool = False,
    expired: bool = False,
    lifecycle_state: str | None = None,
    target_1_hit_before: bool = False,
    target_1_hit_now: bool = False,
    target_2_hit_before: bool = False,
    target_2_hit_now: bool = False,
    trailing_active: bool | None = None,
) -> dict[str, Any]:
    entry = float(entry_price or current_price or 0)
    current = float(current_price or entry or 0)
    target = float(target_1 or entry or 0)
    target_two = float(target_2 or 0)
    stop = float(stop_loss or entry or 0)
    trail = float(trailing_stop or stop or 0)
    pnl = directional_return(direction, entry, current)
    progress = target_progress(direction, entry, current, target)
    prior_state = str(lifecycle_state or "").upper()
    target_1_was_hit = bool(target_1_hit_before)
    target_1_is_hit = target_1_was_hit or bool(target_1_hit_now)
    target_2_was_hit = bool(target_2_hit_before)
    # A trailing stop is a winner-management tool.  Before T1 it must not
    # silently replace the original invalidation/stop for a fresh setup.
    trail_is_active = target_1_was_hit if trailing_active is None else bool(trailing_active)

    payload = {
        "hold_or_exit": "WAIT",
        "scanner_call_status": "ACTIVE",
        "lifecycle_state": prior_state or "ARMED",
        "exit_signal": False,
        "reason_for_exit_decision": "Setup is waiting for clearer lifecycle confirmation.",
        "exit_reason": None,
        "current_pnl_pct": round(pnl, 2),
        "target_progress_pct": round(progress, 2),
    }

    if not current or direction == "neutral":
        payload["reason_for_exit_decision"] = "No current price is available for lifecycle evaluation."
        return payload

    if direction == "bullish":
        stop_hit = bool(stop and current <= stop)
        trail_broken = bool(trail and current <= trail)
        target_2_hit = bool(target_two and current >= target_two)
        target_1_hit = bool(target and current >= target)
        structure_holds = (above_vwap is not False) and (above_ema20 is not False)
    else:
        stop_hit = bool(stop and current >= stop)
        trail_broken = bool(trail and current >= trail)
        target_2_hit = bool(target_two and current <= target_two)
        target_1_hit = bool(target and current <= target)
        structure_holds = (above_vwap is not True) and (above_ema20 is not True)

    if stop_hit:
        if target_1_was_hit:
            return payload | {
                "hold_or_exit": "EXIT",
                "scanner_call_status": "EXIT_SUGGESTED",
                "lifecycle_state": "CLOSED",
                "exit_signal": True,
                "reason_for_exit_decision": "Post-target protective stop hit; exit the remaining position.",
                "exit_reason": "post_target_stop_hit",
            }
        return payload | {
            "hold_or_exit": "EXIT",
            "scanner_call_status": "STOP_LOSS_HIT",
            "lifecycle_state": "INVALIDATED",
            "exit_signal": True,
            "reason_for_exit_decision": "Stop-loss hit.",
            "exit_reason": "stop_loss_hit",
        }
    if trail_is_active and trail_broken:
        return payload | {
            "hold_or_exit": "EXIT",
            "scanner_call_status": "EXIT_SUGGESTED",
            "lifecycle_state": "CLOSED",
            "exit_signal": True,
            "reason_for_exit_decision": "Trailing stop broken.",
            "exit_reason": "trailing_stop_broken",
        }
    if target_2_hit_now or (target_2_hit and not target_2_was_hit):
        return payload | {
            "hold_or_exit": "PARTIAL_BOOK",
            "scanner_call_status": "TARGET_2_HIT",
            "lifecycle_state": "PARTIAL_BOOK",
            "reason_for_exit_decision": "Target 2 hit; protect remaining profit with a trailing stop.",
            "exit_reason": None,
        }
    if target_1_hit_now or (target_1_hit and not target_1_was_hit):
        return payload | {
            "hold_or_exit": "PARTIAL_BOOK",
            "scanner_call_status": "TARGET_1_HIT",
            "lifecycle_state": "PARTIAL_BOOK",
            "reason_for_exit_decision": "Target hit, book partial profit.",
            "exit_reason": None,
        }
    if target_1_is_hit:
        if expired:
            return payload | {
                "hold_or_exit": "EXIT",
                "scanner_call_status": "EXIT_SUGGESTED",
                "lifecycle_state": "MEMORY_COMPLETE",
                "exit_signal": True,
                "reason_for_exit_decision": "Winner-management window completed; review or exit the remaining position.",
                "exit_reason": "winner_memory_complete",
            }
        if overextended and volume_fading:
            return payload | {
                "hold_or_exit": "PARTIAL_BOOK",
                "scanner_call_status": "PARTIAL_BOOK",
                "lifecycle_state": "PARTIAL_BOOK",
                "reason_for_exit_decision": "Momentum faded after an overextended move; reduce, then trail the balance.",
                "exit_reason": None,
            }

        if direction == "bullish":
            is_retest = bool(target and current < target)
        else:
            is_retest = bool(target and current > target)
        if is_retest:
            return payload | {
                "hold_or_exit": "HOLD",
                "scanner_call_status": "RETEST",
                "lifecycle_state": "RETEST",
                "reason_for_exit_decision": "T1 was achieved; the position is retesting the breakout pivot while the trailing stop holds.",
            }
        if prior_state == "RETEST":
            return payload | {
                "hold_or_exit": "HOLD",
                "scanner_call_status": "REARMED",
                "lifecycle_state": "REARMED",
                "reason_for_exit_decision": "The post-target retest reclaimed its pivot; continuation is re-armed.",
            }
        if prior_state == "REARMED":
            return payload | {
                "hold_or_exit": "HOLD",
                "scanner_call_status": "CONTINUATION",
                "lifecycle_state": "CONTINUATION",
                "reason_for_exit_decision": "The re-armed winner is continuing above its breakout pivot; trail the position.",
            }
        return payload | {
            "hold_or_exit": "HOLD",
            "scanner_call_status": "TRAILING_HOLD",
            "lifecycle_state": "TRAILING_HOLD",
            "reason_for_exit_decision": "T1 was achieved; hold the remaining position while the trailing stop remains intact.",
        }
    if expired:
        return payload | {
            "hold_or_exit": "EXPIRED",
            "scanner_call_status": "EXPIRED",
            "lifecycle_state": "EXPIRED",
            "reason_for_exit_decision": "Timeframe expired without enough directional progress.",
            "exit_reason": "no_follow_through",
        }
    if overextended and volume_fading:
        return payload | {
            "hold_or_exit": "PARTIAL_BOOK",
            "scanner_call_status": "PARTIAL_BOOK",
            "lifecycle_state": "PARTIAL_BOOK",
            "reason_for_exit_decision": "Momentum faded after an overextended move.",
            "exit_reason": "momentum_faded",
        }
    if structure_holds and volume_ok:
        return payload | {
            "hold_or_exit": "HOLD",
            "scanner_call_status": "ACTIVE",
            "lifecycle_state": prior_state or "ACTIVE",
            "reason_for_exit_decision": "Structure is holding with volume confirmation.",
        }
    return payload | {
        "hold_or_exit": "WAIT",
        "scanner_call_status": "ACTIVE",
        "lifecycle_state": prior_state or "ARMED",
        "reason_for_exit_decision": "No exit trigger yet, but confirmation is incomplete.",
    }
