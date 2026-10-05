"""Small, closed-bar candle/context gate; not a calibrated prediction model.

Pattern names follow common candlestick terminology. All numeric thresholds below
are conservative *research heuristics*, not quotations or validated book rules.
This module does no I/O and cannot identify institutions, price option contracts,
check a live spread, or guarantee an executable stop. READY is a candle gate only.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


RULES_VERSION = "candle_context_v1"
EXCHANGE_TZ = "Asia/Kolkata"
MIN_RISK_REWARD = 1.5
MAX_CONFIRMATION_BARS = 3
_OHLCV = ["Open", "High", "Low", "Close", "Volume"]
_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15,
            "30m": 30, "60m": 60, "1h": 60, "90m": 90}


def _empty(reason: str) -> pd.DataFrame:
    result = pd.DataFrame(columns=_OHLCV)
    result.attrs["candle_error"] = reason
    return result


def completed_candles(
    frame: pd.DataFrame | None, *, timeframe: str, now: Any = None,
) -> pd.DataFrame:
    """Return at most 80 valid completed NSE regular-session OHLCV bars.

    Intraday index timestamps must denote bar OPEN time; timezone-naive timestamps
    are exchange-local. Daily bars close at 15:30 IST. The last partial intraday
    session bucket closes at 15:30, not after market close. An exchange holiday
    calendar is not inferred: callers must supply real exchange-session bars.
    Invalid/ambiguous timestamps or closed OHLCV fail closed, never get forward-filled.
    The returned attrs['candle_error'] describes invalid input when applicable.
    """
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return _empty("missing_history")
    if not set(_OHLCV).issubset(frame.columns):
        return _empty("missing_ohlcv")
    if not isinstance(frame.index, pd.DatetimeIndex):
        return _empty("datetime_index_required")
    if frame.index.hasnans or not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        return _empty("invalid_or_unordered_timestamps")
    interval = str(timeframe).lower().strip()
    daily = interval in {"1d", "d", "day", "daily"}
    if not daily and interval not in _MINUTES:
        return _empty("unsupported_timeframe")
    try:
        clock = pd.Timestamp.now(tz=EXCHANGE_TZ) if now is None else pd.Timestamp(now)
        if pd.isna(clock):
            return _empty("invalid_now")
        clock = clock.tz_localize(EXCHANGE_TZ) if clock.tz is None else clock.tz_convert(EXCHANGE_TZ)
        index = frame.index
        index = index.tz_localize(EXCHANGE_TZ) if index.tz is None else index.tz_convert(EXCHANGE_TZ)
        session_end = index.normalize() + pd.Timedelta(hours=15, minutes=30)
        if daily:
            if not index.normalize().is_unique:
                return _empty("duplicate_daily_sessions")
            closing = session_end
            session_ok = index.dayofweek < 5
        else:
            nominal_end = index + pd.Timedelta(minutes=_MINUTES[interval])
            closing = pd.DatetimeIndex(np.minimum(nominal_end.asi8, session_end.asi8), tz=EXCHANGE_TZ)
            session_start = index.normalize() + pd.Timedelta(hours=9, minutes=15)
            session_ok = (index >= session_start) & (index < session_end) & (index.dayofweek < 5)
        mask = session_ok & (closing <= clock)
        clean = frame.loc[mask, _OHLCV].tail(80).copy()
        clean.index = index[mask][-80:]
        if clean.empty:
            return _empty("no_completed_bars")
        clean = clean.apply(pd.to_numeric, errors="coerce")
        values = clean.to_numpy(dtype=float)
        valid = (
            np.isfinite(values).all()
            and (clean[["Open", "High", "Low", "Close"]] > 0).all().all()
            and (clean["Volume"] >= 0).all()
            and (clean["High"] >= clean[["Open", "Close", "Low"]].max(axis=1)).all()
            and (clean["Low"] <= clean[["Open", "Close", "High"]].min(axis=1)).all()
        )
        if not valid:
            return _empty("invalid_closed_ohlcv")
        clean.attrs["candle_error"] = None
        clean.attrs["excluded_bars"] = int(len(frame) - int(mask.sum()))
        clean.attrs["latest_close_time"] = closing[mask][-1].isoformat()
        clean.attrs["evaluated_at"] = clock.isoformat()
        return clean
    except (TypeError, ValueError, OverflowError):
        return _empty("invalid_timestamps_or_ohlcv")


def _trend(frame: pd.DataFrame) -> str:
    if len(frame) < 5:
        return "neutral"
    recent = frame.tail(5)
    scale = max(float((recent["High"] - recent["Low"]).median()), 0.000001)
    change = float(recent["Close"].iloc[-1] - recent["Close"].iloc[0])
    return "bullish" if change > scale * 0.4 else "bearish" if change < -scale * 0.4 else "neutral"


def _atr(frame: pd.DataFrame) -> float:
    previous = frame["Close"].shift()
    return float(pd.concat([
        frame["High"] - frame["Low"],
        (frame["High"] - previous).abs(),
        (frame["Low"] - previous).abs(),
    ], axis=1).max(axis=1).tail(14).mean())


def _levels(value: Any, prior: pd.DataFrame, side: str) -> list[float]:
    """Use supplied levels, or observed, already-confirmed historical extrema."""
    if value is not None:
        raw = value if isinstance(value, (list, tuple, np.ndarray, pd.Series)) else [value]
        levels = []
        for item in raw:
            try:
                number = float(item)
                if not isinstance(item, bool) and np.isfinite(number) and number > 0:
                    levels.append(number)
            except (TypeError, ValueError):
                pass
        return sorted(set(levels))
    series = prior["Low" if side == "support" else "High"].tail(40)
    levels = []
    for position in range(2, len(series) - 2):
        window = series.iloc[position - 2:position + 3]
        candidate = float(series.iloc[position])
        boundary = float(window.min() if side == "support" else window.max())
        if candidate == boundary and float(window.max()) > float(window.min()):
            levels.append(candidate)
    if len(series):
        levels.append(float(series.tail(20).min() if side == "support" else series.tail(20).max()))
    return sorted(set(levels))


def _phase(higher: str, local: str, direction: str | None = None) -> str:
    if direction and higher not in {"neutral", direction}:
        return "countertrend_reversal"
    if higher == "bullish" and local == "bearish":
        return "bullish_pullback"
    if higher == "bearish" and local == "bullish":
        return "bearish_pullback"
    if direction:
        return f"{direction}_reversal_watch"
    return "trend_continuation" if local == higher != "neutral" else "range_or_transition"


def _candidate(prior: pd.DataFrame, signal: pd.Series, support: Any, resistance: Any) -> dict[str, Any] | None:
    local = _trend(prior)
    atr = max(_atr(prior), float(signal["Close"]) * 0.0001)
    buffer = max(atr * 0.05, float(signal["Close"]) * 0.0001)
    supports, resistances = _levels(support, prior, "support"), _levels(resistance, prior, "resistance")
    o, h, low, close = (float(signal[key]) for key in ["Open", "High", "Low", "Close"])
    span, body = h - low, abs(close - o)
    if span <= 0:
        return None
    lower, upper = min(o, close) - low, h - max(o, close)
    near_support = any(abs(low - level) <= atr * 0.6 for level in supports)
    near_resistance = any(abs(h - level) <= atr * 0.6 for level in resistances)
    pattern, direction, context = None, None, False
    spring = any(low < level - buffer and close > level + buffer and o >= level - atr * 0.3 for level in supports)
    upthrust = any(h > level + buffer and close < level - buffer and o <= level + atr * 0.3 for level in resistances)
    prev = prior.iloc[-1]
    if spring and local == "bearish" and close >= low + span * 0.65:
        pattern, direction, context = "failed_breakdown_reclaim", "bullish", True
    elif upthrust and local == "bullish" and close <= low + span * 0.35:
        pattern, direction, context = "failed_breakout_rejection", "bearish", True
    elif lower >= max(body * 2, span * 0.55) and upper <= span * 0.2 and body <= span * 0.35:
        if local == "bearish":
            pattern, direction, context = "hammer", "bullish", near_support
        elif local == "bullish":
            pattern, direction, context = "hanging_man", "bearish", near_resistance
        else:
            pattern, context = "lower_wick_context_missing", False
    elif upper >= max(body * 2, span * 0.55) and lower <= span * 0.2 and body <= span * 0.35:
        if local == "bullish":
            pattern, direction, context = "shooting_star", "bearish", near_resistance
        elif local == "bearish":
            pattern, direction, context = "inverted_hammer", "bullish", near_support
        else:
            pattern, context = "upper_wick_context_missing", False
    elif (close > o and float(prev["Close"]) < float(prev["Open"])
          and o <= float(prev["Close"]) and close >= float(prev["Open"])
          and body > abs(float(prev["Close"] - prev["Open"]))):
        pattern, direction, context = "bullish_engulfing", "bullish", local == "bearish" and near_support
    elif (close < o and float(prev["Close"]) > float(prev["Open"])
          and o >= float(prev["Close"]) and close <= float(prev["Open"])
          and body > abs(float(prev["Close"] - prev["Open"]))):
        pattern, direction, context = "bearish_engulfing", "bearish", local == "bullish" and near_resistance
    if pattern is None:
        return None
    bullish = direction == "bullish"
    # Engulfing invalidation includes both candles, not just the smaller last wick.
    structure_low = min(low, float(prev["Low"])) if pattern == "bullish_engulfing" else low
    structure_high = max(h, float(prev["High"])) if pattern == "bearish_engulfing" else h
    trigger = h + buffer if bullish else low - buffer
    stop = structure_low - buffer if bullish else structure_high + buffer
    opposing = [level for level in resistances if level > trigger] if bullish else [level for level in supports if level < trigger]
    target = min(opposing) if bullish and opposing else max(opposing) if opposing else None
    return {"pattern": pattern, "direction": direction, "context": context,
            "local_trend": local, "trigger": trigger if direction else None,
            "invalidation": stop if direction else None, "target": target if direction else None,
            "atr": atr, "baseline_volume": float(prior["Volume"].tail(20).median())}


def analyze_candle_setup(
    frame: pd.DataFrame | None, *, timeframe: str, higher_timeframe_trend: str = "neutral",
    now: Any = None, support: Any = None, resistance: Any = None,
) -> dict[str, Any]:
    """Analyze a recent contextual reversal/pullback with non-repainting rules.

    A later completed directional close must confirm the signal, on meaningful
    relative volume, within three bars. Signal-only shapes NEVER produce READY.
    Stops/targets are observed structural levels; targets are never manufactured
    to meet an RR floor. RR uses the latest completed price, not the old trigger.
    Missing targets, counter-trend context, late moves, or low RR remain WAIT.
    Support/resistance may be positive numbers or sequences, known as of signal.
    """
    higher = str(higher_timeframe_trend).lower()
    higher = higher if higher in {"bullish", "bearish", "neutral"} else "neutral"
    result: dict[str, Any] = {
        "available": False, "status": "WATCH", "direction": None, "pattern": None,
        "phase": "unknown", "timeframe": timeframe, "higher_timeframe_trend": higher,
        "local_trend": "neutral", "trigger": None, "invalidation": None, "target": None,
        "entry_reference": None, "risk_reward": None, "entry_ready": False, "reasons": [],
        "signal_time": None, "confirmation_time": None, "latest_closed_time": None,
        "evaluated_at": None, "rules_version": RULES_VERSION,
        "basis": "research_heuristic", "min_risk_reward": MIN_RISK_REWARD,
    }
    bars = completed_candles(frame, timeframe=timeframe, now=now)
    error = bars.attrs.get("candle_error")
    if error:
        result["status"] = "UNAVAILABLE" if error in {"missing_history", "no_completed_bars", "missing_ohlcv"} else "INVALID"
        result["reasons"] = [error]
        return result
    if len(bars) < 9:
        result["status"] = "UNAVAILABLE"
        result["reasons"] = ["insufficient_completed_history"]
        return result
    result["available"] = True
    result["latest_closed_time"] = bars.attrs["latest_close_time"]
    result["evaluated_at"] = bars.attrs["evaluated_at"]
    result["local_trend"] = _trend(bars)
    result["phase"] = _phase(higher, result["local_trend"])
    candidate, index, rejected = None, None, None
    candidates = []
    for position in range(len(bars) - 1, max(7, len(bars) - MAX_CONFIRMATION_BARS - 2), -1):
        possible = _candidate(bars.iloc[:position], bars.iloc[position], support, resistance)
        if possible and possible["context"]:
            candidates.append((possible, position))
        if possible and rejected is None:
            rejected = possible
    if candidates:
        candidate, index = candidates[0]
        # A new reclaim shape must not erase an old setup's stop breach on the
        # very same bar. Report the breach now; the new shape can be evaluated
        # as a distinct setup only when a later completed bar confirms it.
        if index == len(bars) - 1:
            for older, older_index in candidates[1:]:
                bullish_older = older["direction"] == "bullish"
                invalid_now = (float(bars["Low"].iloc[-1]) <= older["invalidation"] if bullish_older
                               else float(bars["High"].iloc[-1]) >= older["invalidation"])
                if invalid_now:
                    candidate, index = older, older_index
                    break
    if candidate is None:
        if rejected:
            result.update({key: rejected[key] for key in ["pattern", "direction", "local_trend"]})
        result["reasons"] = ["pattern_context_missing" if rejected else "no_contextual_candle_setup"]
        return result
    result.update({key: candidate[key] for key in ["pattern", "direction", "local_trend", "trigger", "invalidation", "target"]})
    result["status"] = "WAIT"
    result["phase"] = _phase(higher, candidate["local_trend"], candidate["direction"])
    result["signal_time"] = bars.index[index].isoformat()
    bullish = candidate["direction"] == "bullish"
    trigger, stop, target = candidate["trigger"], candidate["invalidation"], candidate["target"]
    after = bars.iloc[index + 1:]
    price = float(bars["Close"].iloc[-1])
    entry = max(trigger, price) if bullish else min(trigger, price)
    result["entry_reference"] = entry
    risk = entry - stop if bullish else stop - entry
    reward = (target - entry if bullish else entry - target) if target is not None else None
    if target is not None and risk > 0:
        result["risk_reward"] = round(reward / risk, 4)
    if stop <= 0 or trigger <= 0 or risk <= 0:
        result.update(status="INVALID", reasons=["invalid_risk_geometry"])
        return result
    if len(after) and ((after["Low"] <= stop).any() if bullish else (after["High"] >= stop).any()):
        result.update(status="INVALID", reasons=["structure_invalidated_after_signal"])
        return result
    reasons = []
    clock = pd.Timestamp(result["evaluated_at"])
    interval = str(timeframe).lower().strip()
    market_minutes = clock.hour * 60 + clock.minute
    live_session = interval in _MINUTES and clock.dayofweek < 5 and 9 * 60 + 15 <= market_minutes < 15 * 60 + 30
    if live_session:
        last_close = pd.Timestamp(result["latest_closed_time"])
        if last_close.date() != clock.date():
            reasons.append("awaiting_current_session_closed_candle")
        elif clock - last_close > pd.Timedelta(minutes=_MINUTES[interval] * 3):
            reasons.append("stale_completed_candles")
    if higher not in {"neutral", candidate["direction"]}:
        reasons.append("higher_timeframe_opposes_setup")
    if candidate["baseline_volume"] <= 0 or float(bars["Volume"].iloc[index]) < max(1, candidate["baseline_volume"] * 0.5):
        reasons.append("signal_volume_insufficient")
    if target is None:
        reasons.append("no_known_opposing_target")
    elif reward <= 0 or result["risk_reward"] < MIN_RISK_REWARD:
        reasons.append("insufficient_room_at_current_price")
    if target is not None and len(after) and ((after["High"] >= target).any() if bullish else (after["Low"] <= target).any()):
        reasons.append("target_already_touched_wait_for_fresh_setup")
    initial_risk = abs(trigger - stop)
    if abs(entry - trigger) > initial_risk * 0.5:
        reasons.append("late_entry_wait_for_retest")
    confirmation = None
    for timestamp, bar in after.iterrows():
        if live_session and timestamp.date() != clock.date():
            continue
        span = float(bar["High"] - bar["Low"])
        location = float((bar["Close"] - bar["Low"]) / span) if span > 0 else 0.5
        crossed = float(bar["Close"]) > trigger if bullish else float(bar["Close"]) < trigger
        directional = float(bar["Close"]) > float(bar["Open"]) if bullish else float(bar["Close"]) < float(bar["Open"])
        strong_close = location >= 0.6 if bullish else location <= 0.4
        volume_ok = float(bar["Volume"]) >= max(1, candidate["baseline_volume"] * 0.75)
        if crossed and directional and strong_close and volume_ok:
            confirmation = timestamp
            break
    if confirmation is None:
        reasons.append("awaiting_closed_candle_confirmation")
    else:
        result["confirmation_time"] = confirmation.isoformat()
        if (price <= trigger if bullish else price >= trigger):
            reasons.append("confirmation_level_not_held")
    if not reasons:
        result.update(status="READY", entry_ready=True,
                      reasons=["contextual_pattern_confirmed_on_closed_bar", "structural_risk_and_opposing_target_valid",
                               "research_heuristic_not_win_probability"])
    else:
        result["reasons"] = reasons
    return result
