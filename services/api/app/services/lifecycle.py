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
) -> dict[str, Any]:
    entry = float(entry_price or current_price or 0)
    current = float(current_price or entry or 0)
    target = float(target_1 or entry or 0)
    target_two = float(target_2 or 0)
    stop = float(stop_loss or entry or 0)
    trail = float(trailing_stop or stop or 0)
    pnl = directional_return(direction, entry, current)
    progress = target_progress(direction, entry, current, target)

    payload = {
        "hold_or_exit": "WAIT",
        "scanner_call_status": "ACTIVE",
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
        return payload | {
            "hold_or_exit": "EXIT",
            "scanner_call_status": "STOP_LOSS_HIT",
            "exit_signal": True,
            "reason_for_exit_decision": "Stop-loss hit.",
            "exit_reason": "stop_loss_hit",
        }
    if trail_broken:
        return payload | {
            "hold_or_exit": "EXIT",
            "scanner_call_status": "EXIT_SUGGESTED",
            "exit_signal": True,
            "reason_for_exit_decision": "Trailing stop broken.",
            "exit_reason": "trailing_stop_broken",
        }
    if target_2_hit:
        return payload | {
            "hold_or_exit": "PARTIAL_BOOK",
            "scanner_call_status": "TARGET_2_HIT",
            "reason_for_exit_decision": "Target 2 hit; protect remaining profit with a trailing stop.",
            "exit_reason": "target_2_hit",
        }
    if target_1_hit:
        return payload | {
            "hold_or_exit": "PARTIAL_BOOK",
            "scanner_call_status": "TARGET_1_HIT",
            "reason_for_exit_decision": "Target hit, book partial profit.",
            "exit_reason": "target_1_hit",
        }
    if expired:
        return payload | {
            "hold_or_exit": "EXPIRED",
            "scanner_call_status": "EXPIRED",
            "reason_for_exit_decision": "Timeframe expired without enough directional progress.",
            "exit_reason": "no_follow_through",
        }
    if overextended and volume_fading:
        return payload | {
            "hold_or_exit": "PARTIAL_BOOK",
            "scanner_call_status": "PARTIAL_BOOK",
            "reason_for_exit_decision": "Momentum faded after an overextended move.",
            "exit_reason": "momentum_faded",
        }
    if structure_holds and volume_ok:
        return payload | {
            "hold_or_exit": "HOLD",
            "scanner_call_status": "ACTIVE",
            "reason_for_exit_decision": "Structure is holding with volume confirmation.",
        }
    return payload | {
        "hold_or_exit": "WAIT",
        "scanner_call_status": "ACTIVE",
        "reason_for_exit_decision": "No exit trigger yet, but confirmation is incomplete.",
    }
