from __future__ import annotations

from typing import Any


def classify_chase_risk(signal: dict[str, Any]) -> dict[str, Any]:
    direction = signal.get("direction")
    change_pct = float(signal.get("change_pct") or 0)
    gap_pct = float(signal.get("gap_pct") or 0)
    return_5d = float(signal.get("return_5d") or 0)
    return_20d = float(signal.get("return_20d") or 0)
    risk_reward = float(signal.get("risk_reward") or 0)
    relative_volume = float(signal.get("relative_volume") or 1)
    intraday_volume_ratio = float(signal.get("intraday_volume_ratio") or 1)
    rsi = float(signal.get("rsi") or 50)
    current_price = float(signal.get("current_price") or 0)
    ema_20 = float(signal.get("ema_20") or 0)
    rolling_vwap = float(signal.get("rolling_vwap") or 0)
    close_location = float(signal.get("close_location") or 0.5)
    trailing_stop = float(signal.get("trailing_stop") or signal.get("stop_loss") or signal.get("invalidation") or 0)
    above_vwap = bool(rolling_vwap and current_price >= rolling_vwap)
    strong_volume = relative_volume >= 1.6 or intraday_volume_ratio >= 1.4
    stop_nearby = bool(current_price and trailing_stop and abs((current_price - trailing_stop) / current_price) <= 0.035)
    ema20_distance_pct = ((current_price - ema_20) / ema_20 * 100) if current_price and ema_20 else 0.0
    vwap_distance_pct = ((current_price - rolling_vwap) / rolling_vwap * 100) if current_price and rolling_vwap else 0.0
    extended_candle = abs(change_pct) >= 3.0 and (close_location >= 0.78 or close_location <= 0.22)
    overextended = (direction == "bullish" and rsi >= 74) or (direction == "bearish" and rsi <= 26)
    strong_move = abs(change_pct) >= 5 or abs(gap_pct) >= 3
    multi_day_overextension = abs(return_20d) >= 28 or abs(return_5d) >= 14
    far_from_mean = abs(ema20_distance_pct) >= 8 or abs(vwap_distance_pct) >= 5
    exhaustion_risk = overextended and extended_candle
    
    obv_slope = float(signal.get("obv_slope") or 0)
    cmf = float(signal.get("cmf") or 0)
    
    is_distribution_dump = strong_volume and close_location < 0.45 and (obv_slope <= 0 or cmf < 0)
    is_institutional_absorption = (change_pct < 1.5) and (relative_volume < 1.5) and (obv_slope > 0 or cmf > 0.1) and (abs(ema20_distance_pct) < 3.0 or abs(vwap_distance_pct) < 2.0)
    genuine_fresh_breakout = strong_volume and above_vwap and close_location >= 0.70 and not multi_day_overextension and not exhaustion_risk
    
    hard_chase_risk = (strong_move or multi_day_overextension or far_from_mean or exhaustion_risk)

    next_day_profit_booking_risk = abs(change_pct) >= 8 or abs(return_20d) >= 30 or abs(return_5d) >= 15

    labels: list[str] = []
    action = "EARLY_ENTRY"
    reason = "Setup is early enough for normal scanner evaluation."
    attention_only = False
    allow_buy_call = direction in {"bullish", "bearish"}
    
    if is_distribution_dump:
        labels = ["DISTRIBUTION_DUMP"]
        action = "AVOID"
        attention_only = True
        allow_buy_call = False
        reason = "Massive volume but price rejected from the top (long upper wick). High probability of institutional profit booking/dump."
        hard_chase_risk = True
    elif is_institutional_absorption:
        labels = ["INSTITUTIONAL_ABSORPTION"]
        action = "BUY"
        attention_only = False
        allow_buy_call = True
        reason = "Price is pulling back/flat with dry volume, but OBV/CMF is rising. Institutions are stealthily absorbing supply."
        hard_chase_risk = False
    elif genuine_fresh_breakout:
        labels = ["VALID_BREAKOUT"]
        action = "BUY"
        attention_only = False
        allow_buy_call = True
        reason = "Strong fresh breakout with high volume and solid close. Valid entry."
        hard_chase_risk = False
    elif hard_chase_risk:
        labels.append("CHASE_RISK")
        labels.append("WAIT_FOR_PULLBACK")
        action = "WAIT_FOR_PULLBACK"
        attention_only = True
        allow_buy_call = False
        if multi_day_overextension:
            reason = "Stock is already extended over 5-20 days; avoid fresh entry and wait for base or pullback."
        elif far_from_mean:
            reason = "Price is far above 20 EMA/VWAP; fresh entry has poor risk/reward."
        elif exhaustion_risk:
            reason = "RSI and candle stretch show exhaustion risk after a fast move."
        else:
            reason = "Stock already moved strongly; avoid late entry unless it builds a fresh base."

    if next_day_profit_booking_risk:
        labels.append("PROFIT_BOOKING_RISK")
        reason = "High chance of profit booking tomorrow. Use trailing stop if already holding."
        
    # Do NOT override hard_chase_risk just because volume is strong. 
    # Strong volume on extended candles usually leads to a 10-15 minute shakeout (pullback).
    valid_after_move = bool(strong_volume and above_vwap and not exhaustion_risk)
    if valid_after_move and hard_chase_risk:
        labels.append("VALID_BREAKOUT_BUT_EXTENDED")
        action = "WAIT_FOR_PULLBACK"
        attention_only = True
        allow_buy_call = False
        reason = "Valid breakout with strong volume, but price is extended. Wait for a pullback to VWAP/EMA to avoid stoploss hunting."

    if next_day_profit_booking_risk:
        labels = [l for l in labels if l != "PROFIT_BOOKING_RISK"]
        labels.append("PROFIT_BOOKING_RISK")
        if "VALID_BREAKOUT_BUT_EXTENDED" in labels:
            reason = "Valid breakout but extended, AND high chance of profit booking tomorrow. Definitely wait for pullback or avoid."
    elif hard_chase_risk:
        labels.append("AVOID_LATE_ENTRY")

    if not labels:
        labels.append("EARLY_ENTRY")

    return {
        "setup_stage": labels[0],
        "trade_labels": list(dict.fromkeys(labels)),
        "recommended_action": action,
        "attention_only": attention_only,
        "allow_buy_call": allow_buy_call,
        "chase_risk": hard_chase_risk,
        "overextended_fresh_entry": hard_chase_risk,
        "return_5d": round(return_5d, 2),
        "return_20d": round(return_20d, 2),
        "ema20_distance_pct": round(ema20_distance_pct, 2),
        "vwap_distance_pct": round(vwap_distance_pct, 2),
        "next_day_profit_booking_risk": next_day_profit_booking_risk,
        "chase_risk_reason": reason,
    }
