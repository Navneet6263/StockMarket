from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        out = float(value)
        if np.isnan(out) or np.isinf(out):
            return default
        return out
    except (TypeError, ValueError):
        return default


def _pct(part: float, whole: float) -> float:
    return (part / whole) * 100 if whole else 0.0


def _clean_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(set(frame.columns)):
        return pd.DataFrame()
    clean = frame.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    return clean.tail(120)


def _latest(feature_frame: pd.DataFrame | None, key: str, default: float = 0.0) -> float:
    if feature_frame is None or feature_frame.empty or key not in feature_frame.columns:
        return default
    return _num(feature_frame[key].iloc[-1], default)


def _true_range(frame: pd.DataFrame) -> pd.Series:
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    close = frame["Close"].astype(float)
    return pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _risk_reward(trigger: float, stop: float, target: float) -> float:
    risk = trigger - stop
    reward = target - trigger
    return round(reward / risk, 2) if risk > 0 and reward > 0 else 0.0


def _distance_to_trigger(price: float, trigger: float) -> float:
    return round(((price - trigger) / trigger) * 100, 2) if trigger else 0.0


def _stage(price: float, trigger: float, late: bool) -> str:
    distance = _distance_to_trigger(price, trigger)
    if late:
        return "LATE_MOVE"
    if 0 <= distance <= 0.8:
        return "BREAKOUT_ACTIVE"
    if -0.8 <= distance < 0:
        return "READY_TO_BREAK"
    if -3.0 <= distance < -0.8:
        return "BUILDING_NEAR_TRIGGER"
    return "FORMING"


def _target_from_height(trigger: float, stop: float, height: float, atr: float, price: float) -> tuple[float, float, float]:
    min_move = max(atr * 1.4, trigger * 0.018)
    measured = max(height * 0.75, min_move)
    measured = min(measured, trigger * 0.14)
    target_1 = round(trigger + measured, 2)
    target_2 = round(trigger + min(measured * 1.6, trigger * 0.22), 2)
    expected_pct = round(_pct(target_1 - max(price, trigger), max(price, trigger)), 2)
    if target_1 <= trigger and stop < trigger:
        target_1 = round(trigger + (trigger - stop) * 1.5, 2)
        target_2 = round(trigger + (trigger - stop) * 2.3, 2)
        expected_pct = round(_pct(target_1 - max(price, trigger), max(price, trigger)), 2)
    return target_1, target_2, expected_pct


def _late_entry_risk(
    *,
    price: float,
    trigger: float,
    change_pct: float,
    return_5d: float,
    rsi: float,
    ema20_distance_pct: float,
    vwap_distance_pct: float,
) -> bool:
    distance = _distance_to_trigger(price, trigger)
    return bool(
        distance > 0.8
        or change_pct >= 4.5
        or return_5d >= 10.0
        or rsi >= 76.0
        or ema20_distance_pct >= 7.0
        or vwap_distance_pct >= 4.5
    )


def detect_chart_pattern_setup(
    frame: pd.DataFrame | None,
    feature_frame: pd.DataFrame | None = None,
    intraday_frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Detect high-confluence bullish chart patterns and measured-move levels.

    The detector is intentionally early-entry focused. A pattern can score well only
    when price is near a trigger, risk is definable, and the move is not already
    extended beyond the trigger.
    """

    df = _clean_frame(frame)
    if df.empty or len(df) < 30:
        return {"available": False, "score": 0, "reason": "insufficient_history"}

    close = df["Close"].astype(float)
    open_ = df["Open"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float).replace(0, np.nan)

    price = float(close.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close) > 1 else price
    change_pct = _latest(feature_frame, "change_pct", _pct(price - prev_close, prev_close))
    gap_pct = _latest(feature_frame, "gap_pct", 0.0)
    return_5d = _latest(feature_frame, "return_5d", _pct(price - float(close.iloc[-6]), float(close.iloc[-6])) if len(close) > 6 else 0.0)
    return_20d = _latest(feature_frame, "return_20d", 0.0)
    return_60d = _latest(feature_frame, "return_60d", 0.0)
    rsi = _latest(feature_frame, "rsi", 50.0)
    relative_volume = _latest(feature_frame, "relative_volume", _num(volume.iloc[-1] / volume.rolling(20).mean().iloc[-1], 1.0))
    atr = _latest(feature_frame, "atr", _num(_true_range(df).tail(14).mean(), price * 0.02))
    atr_pct = _latest(feature_frame, "atr_pct", _pct(atr, price))
    atr_expansion = _latest(feature_frame, "atr_expansion", 1.0)
    bb_width_ratio = _latest(feature_frame, "bb_width_ratio", 1.0)
    distance_to_resistance = _latest(feature_frame, "distance_to_resistance_pct", 99.0)
    distance_to_support = _latest(feature_frame, "distance_to_support_pct", 99.0)
    cmf = _latest(feature_frame, "cmf", 0.0)
    obv_slope = _latest(feature_frame, "obv_slope", 0.0)
    ema_20 = _latest(feature_frame, "ema_20", 0.0)
    ema_50 = _latest(feature_frame, "ema_50", 0.0)
    rolling_vwap = _latest(feature_frame, "rolling_vwap", 0.0)
    ema20_distance_pct = _pct(price - ema_20, ema_20) if ema_20 else 0.0
    vwap_distance_pct = _pct(price - rolling_vwap, rolling_vwap) if rolling_vwap else 0.0
    close_location = _latest(feature_frame, "close_location", 0.5)
    upper_wick_pct = _latest(feature_frame, "upper_wick_pct", 0.0)
    lower_wick_pct = _latest(feature_frame, "lower_wick_pct", 0.0)

    prior_high_20 = float(high.tail(21).iloc[:-1].max()) if len(high) >= 21 else float(high.iloc[:-1].max())
    prior_high_10 = float(high.tail(11).iloc[:-1].max()) if len(high) >= 11 else prior_high_20
    prior_high_40 = float(high.tail(41).iloc[:-1].max()) if len(high) >= 41 else prior_high_20
    resistance_20 = _latest(feature_frame, "resistance_20", prior_high_20)
    support_20 = _latest(feature_frame, "support_20", float(low.tail(21).iloc[:-1].min()))
    recent_high = max(resistance_20, prior_high_20)
    recent_low = float(low.tail(20).min())
    base_height = max(recent_high - recent_low, atr * 1.5)
    tight_10 = _pct(float(high.tail(10).max() - low.tail(10).min()), price)
    tight_20 = _pct(float(high.tail(20).max() - low.tail(20).min()), price)
    range_40 = _pct(float(high.tail(40).max() - low.tail(40).min()), price) if len(df) >= 40 else tight_20
    higher_lows = bool(_latest(feature_frame, "higher_lows", 0.0)) or (
        len(low) >= 12 and float(low.tail(5).min()) > float(low.tail(12).head(7).min())
    )
    volume_dryup = bool(_latest(feature_frame, "volume_dryup", 0.0)) or relative_volume <= 0.9
    accumulation = cmf >= 0.04 or obv_slope > 0
    squeeze = bb_width_ratio <= 0.9 or atr_expansion <= 1.0

    intraday_breakout = False
    intraday_volume_ratio = 1.0
    if intraday_frame is not None and not intraday_frame.empty and len(intraday_frame) >= 20:
        intraday = _clean_frame(intraday_frame).tail(80)
        if not intraday.empty:
            i_close = intraday["Close"].astype(float)
            i_high = intraday["High"].astype(float)
            i_volume = intraday["Volume"].astype(float).replace(0, np.nan)
            prior_high = _num(i_high.rolling(20).max().shift(1).iloc[-1])
            intraday_breakout = bool(prior_high and float(i_close.iloc[-1]) > prior_high)
            intraday_volume_ratio = _num(i_volume.iloc[-1] / i_volume.rolling(20).mean().iloc[-1], 1.0)

    candidates: list[dict[str, Any]] = []

    def add_candidate(
        *,
        key: str,
        name: str,
        family: str,
        base_score: float,
        trigger: float,
        stop: float,
        height: float,
        labels: list[str],
        reasons: list[str],
        target_method: str,
    ) -> None:
        if not trigger or not stop or stop >= trigger or trigger <= 0:
            return
        late = _late_entry_risk(
            price=price,
            trigger=trigger,
            change_pct=change_pct,
            return_5d=return_5d,
            rsi=rsi,
            ema20_distance_pct=ema20_distance_pct,
            vwap_distance_pct=vwap_distance_pct,
        )
        distance = _distance_to_trigger(price, trigger)
        freshness = 0
        if -0.8 <= distance <= 0.35:
            freshness = 20
        elif -2.0 <= distance < -0.8:
            freshness = 15
        elif 0.35 < distance <= 0.8:
            freshness = 12
        elif -4.0 <= distance < -2.0:
            freshness = 8
        elif distance > 0.8:
            freshness = -20
        else:
            freshness = 2

        target_1, target_2, expected_pct = _target_from_height(trigger, stop, height, atr, price)
        rr = _risk_reward(trigger, stop, target_1)
        score = base_score + freshness
        if rr >= 1.8:
            score += 8
        elif rr >= 1.4:
            score += 4
        elif rr < 1.1:
            score -= 10
        if accumulation:
            score += 5
        if intraday_breakout or intraday_volume_ratio >= 1.4:
            score += 5
        if late:
            score -= 25

        score = round(float(np.clip(score, 0, 100)), 1)
        candidates.append(
            {
                "key": key,
                "pattern_name": name,
                "pattern_family": family,
                "direction": "bullish",
                "score": score,
                "trigger_price": round(trigger, 2),
                "invalidation_level": round(stop, 2),
                "target_1": target_1,
                "target_2": target_2,
                "risk_reward": rr,
                "expected_move_pct": expected_pct,
                "measured_height_pct": round(_pct(height, price), 2),
                "distance_to_trigger_pct": distance,
                "freshness_score": freshness,
                "late_entry_risk": late,
                "labels": list(dict.fromkeys(labels)),
                "reasons": reasons[:5],
                "target_method": target_method,
            }
        )

    resistance_tests = int((_pct((high.tail(40).iloc[:-1] - recent_high).abs(), price) <= 1.5).sum()) if len(high) >= 41 else 0
    highs_tail = high.tail(8)
    flat_highs = bool(price and highs_tail.std() / price * 100 <= 1.8)
    support_candidates = [value for value in (support_20, ema_20, ema_50, rolling_vwap) if value and value < price]
    base_stop = max(support_candidates) if support_candidates else price - max(atr * 1.6, price * 0.025)

    if tight_20 <= max(6.5, atr_pct * 2.6) and (squeeze or volume_dryup) and (higher_lows or accumulation):
        add_candidate(
            key="vcp_squeeze",
            name="VCP / Bollinger squeeze base",
            family="continuation",
            base_score=48 + (8 if higher_lows else 0) + (7 if volume_dryup else 0) + (7 if squeeze else 0),
            trigger=recent_high * 1.002,
            stop=base_stop,
            height=base_height,
            labels=["VCP", "BOLLINGER_SQUEEZE", "VOLATILITY_CONTRACTION"],
            reasons=[
                "Volatility is compressed while price stays near the breakout level.",
                "Higher lows or accumulation suggest buyers are absorbing supply.",
            ],
            target_method="base height measured move",
        )

    if flat_highs and higher_lows and distance_to_resistance <= 3.5 and resistance_tests >= 1:
        add_candidate(
            key="ascending_triangle",
            name="Ascending triangle",
            family="continuation",
            base_score=58 + (8 if accumulation else 0) + (6 if relative_volume >= 1.1 else 0),
            trigger=recent_high * 1.002,
            stop=base_stop,
            height=max(recent_high - recent_low, atr * 1.8),
            labels=["ASCENDING_TRIANGLE", "HIGHER_LOWS", "RESISTANCE_PRESSURE"],
            reasons=[
                "Repeated resistance with higher lows shows pressure building under supply.",
                "Best entry is near the trigger, not after a wide candle.",
            ],
            target_method="triangle height projection",
        )

    if range_40 <= 9.0 and resistance_tests >= 1 and (volume_dryup or relative_volume <= 1.15):
        add_candidate(
            key="flat_base",
            name="Flat base",
            family="continuation",
            base_score=52 + (8 if range_40 <= 6.5 else 0) + (6 if accumulation else 0),
            trigger=max(resistance_20, prior_high_40) * 1.002,
            stop=base_stop,
            height=max(high.tail(40).max() - low.tail(40).min(), atr * 1.8),
            labels=["FLAT_BASE", "TIGHT_RANGE", "VOLUME_DRYUP"],
            reasons=[
                "Price is moving in a controlled range with a defined breakout level.",
                "Volume is quiet enough for a fresh expansion setup.",
            ],
            target_method="flat-base range projection",
        )

    if return_60d >= 8 and -9 <= return_20d <= 8 and tight_20 <= 9.0:
        add_candidate(
            key="cup_handle",
            name="Cup with handle candidate",
            family="continuation",
            base_score=54 + (6 if close_location >= 0.55 else 0) + (6 if volume_dryup else 0),
            trigger=max(resistance_20, prior_high_20) * 1.002,
            stop=max([value for value in (support_20, ema_20) if value and value < price] or [price - atr * 2.0]),
            height=max(high.tail(60).max() - low.tail(60).min(), atr * 2.0),
            labels=["CUP_HANDLE", "HANDLE_BASE", "PRIOR_UPTREND"],
            reasons=[
                "Prior uptrend is pausing in a handle-like base.",
                "Trigger is the handle high; avoid entries far above it.",
            ],
            target_method="cup/handle measured move",
        )

    if return_20d >= 10 and tight_10 <= max(7.5, atr_pct * 2.5) and relative_volume <= 1.8:
        add_candidate(
            key="flag_pennant",
            name="Flag / pennant continuation",
            family="continuation",
            base_score=50 + (8 if volume_dryup else 0) + (5 if higher_lows else 0),
            trigger=max(prior_high_10, resistance_20) * 1.002,
            stop=max([value for value in (low.tail(10).min(), ema_20, rolling_vwap) if value and value < price] or [price - atr * 1.8]),
            height=max(price * min(return_20d, 18) / 100 * 0.45, atr * 1.8),
            labels=["FLAG_PENNANT", "MOMENTUM_PAUSE"],
            reasons=[
                "A strong prior move is digesting in a tighter range.",
                "Only a new range breakout keeps the continuation valid.",
            ],
            target_method="flag-pole conservative projection",
        )

    if len(df) >= 50:
        first_low = float(low.tail(50).head(25).min())
        second_low = float(low.tail(25).min())
        lows_match = abs(first_low - second_low) / max(price, 0.0001) * 100 <= 3.5
        neckline = float(high.tail(36).iloc[:-1].max())
        if lows_match and price > second_low * 1.04 and _distance_to_trigger(price, neckline * 1.002) <= 3.5:
            add_candidate(
                key="double_bottom",
                name="Double bottom reversal",
                family="reversal",
                base_score=50 + (8 if accumulation else 0) + (6 if lower_wick_pct >= 0.25 else 0),
                trigger=neckline * 1.002,
                stop=min(first_low, second_low) * 0.985,
                height=max(neckline - min(first_low, second_low), atr * 1.8),
                labels=["DOUBLE_BOTTOM", "REVERSAL_BASE"],
                reasons=[
                    "Two similar lows show a potential reversal base.",
                    "Confirmation requires a neckline break with risk below the second low.",
                ],
                target_method="neckline-to-low measured move",
            )

    if distance_to_support <= 1.8 and lower_wick_pct >= 0.25 and close_location >= 0.55 and (ema_20 or rolling_vwap):
        add_candidate(
            key="support_retest",
            name="Support retest / bounce",
            family="reversal",
            base_score=46 + (8 if accumulation else 0) + (6 if relative_volume >= 1.1 else 0),
            trigger=max(float(high.iloc[-1]), price + atr * 0.25) * 1.002,
            stop=min(support_20 or price, float(low.iloc[-1])) * 0.99,
            height=max(atr * 2.2, price * 0.025),
            labels=["SUPPORT_RETEST", "PIN_BAR" if lower_wick_pct >= 0.35 else "BOUNCE_SETUP"],
            reasons=[
                "Price is retesting support with lower-wick demand.",
                "Entry needs the bounce trigger to clear, otherwise support can fail.",
            ],
            target_method="ATR bounce projection",
        )

    if len(df) >= 2:
        prev_bear = close.iloc[-2] < open_.iloc[-2]
        curr_bull = close.iloc[-1] > open_.iloc[-1]
        bullish_engulf = bool(prev_bear and curr_bull and close.iloc[-1] >= open_.iloc[-2] and open_.iloc[-1] <= close.iloc[-2])
        inside_bar = bool(high.iloc[-1] < high.iloc[-2] and low.iloc[-1] > low.iloc[-2])
        hammer = bool(lower_wick_pct >= 0.45 and close_location >= 0.6)
        if bullish_engulf or inside_bar or hammer:
            candle_labels = []
            candle_reasons = []
            if bullish_engulf:
                candle_labels.append("BULLISH_ENGULFING")
                candle_reasons.append("Bullish engulfing candle shows demand after a weak candle.")
            if inside_bar:
                candle_labels.append("INSIDE_BAR")
                candle_reasons.append("Inside bar compression gives a clean high/low trigger.")
            if hammer:
                candle_labels.append("PIN_BAR")
                candle_reasons.append("Lower-wick rejection shows buyers defended the dip.")
            add_candidate(
                key="candlestick_trigger",
                name="Candlestick trigger",
                family="candlestick",
                base_score=42 + (8 if distance_to_support <= 2.5 else 0) + (5 if accumulation else 0),
                trigger=max(float(high.iloc[-1]), float(high.iloc[-2])) * 1.002,
                stop=min(float(low.iloc[-1]), support_20 or float(low.iloc[-1])) * 0.99,
                height=max(atr * 1.8, price * 0.018),
                labels=candle_labels,
                reasons=candle_reasons,
                target_method="candle range plus ATR",
            )

    if not candidates:
        return {
            "available": True,
            "score": 0,
            "pattern_name": "No high-confluence pattern",
            "direction": "neutral",
            "late_entry_risk": False,
            "reason": "No strong chart pattern is close enough to a clean trigger.",
        }

    candidates.sort(key=lambda item: item["score"], reverse=True)
    best = candidates[0]
    stage = _stage(price, best["trigger_price"], bool(best["late_entry_risk"]))
    action = "WATCH"
    if best["score"] >= 78 and stage in {"READY_TO_BREAK", "BREAKOUT_ACTIVE"} and best["risk_reward"] >= 1.3:
        action = "BUY_ONLY_ON_TRIGGER_HOLD"
    elif best["score"] >= 60 and stage in {"READY_TO_BREAK", "BUILDING_NEAR_TRIGGER", "BREAKOUT_ACTIVE"}:
        action = "ALERT_ABOVE_LEVEL"

    if stage == "LATE_MOVE":
        timeframe = "wait for fresh base/retest"
    elif stage in {"READY_TO_BREAK", "BREAKOUT_ACTIVE"}:
        timeframe = "live / same session"
    elif stage == "BUILDING_NEAR_TRIGGER":
        timeframe = "1-3 sessions"
    else:
        timeframe = "forming"

    return {
        "available": True,
        **best,
        "stage": stage,
        "action": action,
        "entry_quality": "poor" if best["late_entry_risk"] else "good" if action == "BUY_ONLY_ON_TRIGGER_HOLD" else "watch",
        "timeframe": timeframe,
        "intraday_breakout": intraday_breakout,
        "intraday_volume_ratio": round(intraday_volume_ratio, 2),
        "price": round(price, 2),
        "change_pct": round(change_pct, 2),
        "gap_pct": round(gap_pct, 2),
        "return_5d": round(return_5d, 2),
        "return_20d": round(return_20d, 2),
        "rsi": round(rsi, 1),
        "alternates": [
            {
                "pattern_name": item["pattern_name"],
                "score": item["score"],
                "trigger_price": item["trigger_price"],
                "late_entry_risk": item["late_entry_risk"],
            }
            for item in candidates[1:4]
        ],
        "score_breakdown": {
            "relative_volume": round(relative_volume, 2),
            "tight_10_pct": round(tight_10, 2),
            "tight_20_pct": round(tight_20, 2),
            "range_40_pct": round(range_40, 2),
            "atr_pct": round(atr_pct, 2),
            "bb_width_ratio": round(bb_width_ratio, 2),
            "accumulation": accumulation,
            "higher_lows": higher_lows,
            "volume_dryup": volume_dryup,
            "squeeze": squeeze,
        },
    }
