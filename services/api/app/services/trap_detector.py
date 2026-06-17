"""
Trap Detector - Institutional Trap & Smart Money Analysis

Ek experienced trader jaanta hai:
1. Jab buy orders zyada hain lekin price neeche ja rahi hai = institutions sell kar rahe hain
2. Jab resistance toot ke bada candle se neeche aata hai = bull trap
3. Jab OI build-up resistance pe zyada hai = institutions ne ceiling bana di hai
4. Jab delivery low hai lekin price upar hai = retail speculation, not real buying

Yeh module in sab cheezein detect karta hai aur trader ko bachata hai.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


def _safe(val, default: float = 0.0) -> float:
    try:
        if val is None or (isinstance(val, float) and val != val):
            return default
        return float(val)
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────────────────────
# 1. ORDER BOOK TRAP DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_order_book_trap(
    bid_quantity: float,
    ask_quantity: float,
    price_change_pct: float,
    volume_ratio: float,
    close_location: float,
) -> Dict:
    """
    Order book trap: Buy orders zyada hain lekin price neeche ja rahi hai.

    Yeh tab hota hai jab institutions:
    - Retail ko lure karne ke liye fake buy orders dikhate hain
    - Khud quietly sell karte rehte hain
    - Jab enough retail buy ho jaata hai, orders cancel karke price crash karte hain

    Signs:
    - Bid quantity >> Ask quantity (buyers dikhte hain)
    - Lekin price neeche ja rahi hai ya flat hai
    - Volume high hai lekin close location low hai (close near low of candle)
    - Delivery ratio low hai (no real buying)
    """
    if bid_quantity <= 0 or ask_quantity <= 0:
        return {"trap_detected": False, "trap_type": None, "trap_score": 0, "trap_reason": []}

    bid_ask_ratio = bid_quantity / max(ask_quantity, 1)
    reasons = []
    score = 0

    # Core signal: Buyers dikhte hain lekin price neeche
    if bid_ask_ratio >= 1.5 and price_change_pct <= -0.5:
        score += 40
        reasons.append(
            f"Buy orders {bid_ask_ratio:.1f}x sell orders but price is falling "
            f"({price_change_pct:.2f}%) — classic institutional sell trap."
        )
    elif bid_ask_ratio >= 2.0 and price_change_pct <= 0:
        score += 30
        reasons.append(
            f"Heavy buy-side depth ({bid_ask_ratio:.1f}x) but price not moving up — "
            "fake depth to attract retail buyers."
        )

    # Close location: Candle ke neeche close = sellers in control
    if close_location <= 0.3 and volume_ratio >= 1.5:
        score += 25
        reasons.append(
            f"High volume ({volume_ratio:.1f}x) but candle closing near lows "
            f"(close location {close_location:.2f}) — distribution happening."
        )

    # Volume high but price not moving = absorption
    if volume_ratio >= 2.0 and abs(price_change_pct) <= 0.5:
        score += 20
        reasons.append(
            f"Volume is {volume_ratio:.1f}x average but price barely moved — "
            "institutions absorbing supply before a move."
        )

    trap_detected = score >= 40
    if score >= 70:
        trap_type = "strong_sell_trap"
        action = "AVOID — Strong institutional selling disguised as buying."
    elif score >= 40:
        trap_type = "possible_sell_trap"
        action = "CAUTION — Possible trap. Wait for price confirmation before entry."
    else:
        trap_type = None
        action = "No trap signal detected."

    return {
        "trap_detected": trap_detected,
        "trap_type": trap_type,
        "trap_score": score,
        "trap_action": action,
        "trap_reason": reasons,
        "bid_ask_ratio": round(bid_ask_ratio, 2),
        "close_location": round(close_location, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. BULL TRAP DETECTION (Chart Pattern)
# ─────────────────────────────────────────────────────────────────────────────

def detect_bull_trap(
    price: float,
    resistance_level: float,
    prev_close: float,
    current_high: float,
    current_close: float,
    volume_ratio: float,
    candle_body_pct: float,
    upper_wick_pct: float,
) -> Dict:
    """
    Bull Trap: Resistance tod ke bada candle se neeche aana.

    Yeh institutions ka sabse common trap hai:
    1. Price resistance ke upar jaata hai (retail buyers excited ho jaate hain)
    2. Bada green candle banta hai (FOMO entry)
    3. Phir usi session mein ya next session mein bada red candle
    4. Retail trapped, institutions ne exit kar liya

    Kaise identify karein:
    - Price resistance ke upar gayi (breakout dikhta hai)
    - Lekin close resistance ke neeche aa gayi (failed breakout)
    - Ya upper wick bahut bada hai (rejection at resistance)
    - Volume high tha breakout pe (retail FOMO)
    """
    reasons = []
    score = 0

    if resistance_level <= 0 or price <= 0:
        return {"bull_trap_detected": False, "bull_trap_score": 0, "bull_trap_reason": []}

    # Did price break above resistance?
    broke_resistance = current_high > resistance_level * 1.001

    if not broke_resistance:
        return {
            "bull_trap_detected": False,
            "bull_trap_score": 0,
            "bull_trap_reason": ["Price has not broken resistance yet — no bull trap possible."],
        }

    # Core: Broke resistance but closed below it
    if current_close < resistance_level:
        penetration_pct = (current_high - resistance_level) / resistance_level * 100
        rejection_pct = (current_high - current_close) / current_high * 100
        score += 50
        reasons.append(
            f"Price broke resistance at {resistance_level:.2f} (high: {current_high:.2f}) "
            f"but closed BELOW at {current_close:.2f} — failed breakout / bull trap."
        )
        if rejection_pct >= 2.0:
            score += 20
            reasons.append(
                f"Rejection from resistance was {rejection_pct:.1f}% — "
                "strong institutional selling at this level."
            )

    # Large upper wick = rejection
    if upper_wick_pct >= 0.4:
        score += 25
        reasons.append(
            f"Upper wick is {upper_wick_pct * 100:.0f}% of candle range — "
            "sellers aggressively rejected the breakout."
        )

    # High volume on failed breakout = distribution
    if volume_ratio >= 1.5 and current_close < resistance_level:
        score += 20
        reasons.append(
            f"High volume ({volume_ratio:.1f}x) on a failed breakout — "
            "institutions were selling into retail buying."
        )

    # Price now below prev close = reversal confirmed
    if current_close < prev_close and current_high > resistance_level:
        score += 15
        reasons.append(
            "Price closed below previous close after touching resistance — "
            "reversal is confirmed."
        )

    bull_trap_detected = score >= 50

    if score >= 80:
        severity = "high"
        action = "SELL/SHORT — Strong bull trap. Institutions sold into breakout. Exit longs immediately."
        entry_note = f"Short entry below {resistance_level:.2f}. Stop above {current_high:.2f}."
    elif score >= 50:
        severity = "medium"
        action = "CAUTION — Possible bull trap. Do not buy the breakout. Wait for re-test."
        entry_note = f"Watch for re-test of {resistance_level:.2f} from below."
    else:
        severity = "low"
        action = "Monitor — Weak trap signal. Breakout may still be valid."
        entry_note = "Wait for volume confirmation."

    return {
        "bull_trap_detected": bull_trap_detected,
        "bull_trap_score": score,
        "bull_trap_severity": severity if bull_trap_detected else "none",
        "bull_trap_action": action,
        "bull_trap_entry_note": entry_note,
        "bull_trap_reason": reasons,
        "resistance_level": round(resistance_level, 2),
        "breakout_high": round(current_high, 2),
        "close_vs_resistance": round(current_close - resistance_level, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. OPTION CHAIN TRAP ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyze_option_chain_traps(
    spot_price: float,
    pcr: float,
    max_pain: float,
    call_oi_at_resistance: float,
    put_oi_at_support: float,
    call_oi_change: float,
    put_oi_change: float,
    iv_percentile: float = 50.0,
) -> Dict:
    """
    Option chain se institutional trap identify karo.

    Key concepts:
    - Max Pain: Jis price pe maximum options expire worthless — institutions yahan price le jaate hain
    - PCR > 1.5: Bahut zyada puts = market makers bullish (contrarian signal)
    - PCR < 0.7: Bahut zyada calls = market makers bearish (contrarian signal)
    - Heavy Call OI at resistance = ceiling bana hua hai, price wahan se neeche aayegi
    - Heavy Put OI at support = floor bana hua hai, price wahan se upar aayegi
    - OI change: Fresh positions build ho rahi hain = strong signal
    """
    reasons = []
    bias = "neutral"
    trap_warnings = []
    score = 0

    # Max Pain analysis
    if max_pain > 0 and spot_price > 0:
        distance_from_max_pain = (spot_price - max_pain) / spot_price * 100
        if distance_from_max_pain > 2.0:
            score -= 20
            trap_warnings.append(
                f"Spot ({spot_price:.0f}) is {distance_from_max_pain:.1f}% ABOVE max pain ({max_pain:.0f}). "
                "Institutions will try to pull price down to max pain by expiry."
            )
        elif distance_from_max_pain < -2.0:
            score += 20
            reasons.append(
                f"Spot ({spot_price:.0f}) is {abs(distance_from_max_pain):.1f}% BELOW max pain ({max_pain:.0f}). "
                "Institutions will try to push price up to max pain by expiry."
            )

    # PCR analysis
    if pcr >= 1.5:
        score += 25
        bias = "bullish"
        reasons.append(
            f"PCR is {pcr:.2f} — heavy put writing means market makers are bullish. "
            "Puts are being sold = floor is being built."
        )
    elif pcr >= 1.2:
        score += 10
        bias = "mildly_bullish"
        reasons.append(f"PCR {pcr:.2f} — slightly bullish bias from options market.")
    elif pcr <= 0.7:
        score -= 25
        bias = "bearish"
        trap_warnings.append(
            f"PCR is {pcr:.2f} — heavy call writing means market makers are bearish. "
            "Calls are being sold = ceiling is being built."
        )
    elif pcr <= 0.9:
        score -= 10
        bias = "mildly_bearish"
        trap_warnings.append(f"PCR {pcr:.2f} — slightly bearish bias from options market.")

    # Call OI at resistance = ceiling
    if call_oi_at_resistance > 0 and put_oi_at_support > 0:
        oi_ratio = call_oi_at_resistance / max(put_oi_at_support, 1)
        if oi_ratio >= 2.0:
            score -= 20
            trap_warnings.append(
                f"Call OI at resistance is {oi_ratio:.1f}x put OI at support — "
                "strong ceiling built by institutions. Price likely to face heavy resistance."
            )
        elif oi_ratio <= 0.5:
            score += 20
            reasons.append(
                f"Put OI at support is {1/oi_ratio:.1f}x call OI at resistance — "
                "strong floor built. Price likely to bounce from support."
            )

    # OI change analysis (fresh positions)
    if call_oi_change > 0 and put_oi_change > 0:
        if call_oi_change > put_oi_change * 1.5:
            score -= 15
            trap_warnings.append(
                f"Fresh call writing ({call_oi_change:,.0f}) >> put writing ({put_oi_change:,.0f}) — "
                "institutions building bearish positions."
            )
        elif put_oi_change > call_oi_change * 1.5:
            score += 15
            reasons.append(
                f"Fresh put writing ({put_oi_change:,.0f}) >> call writing ({call_oi_change:,.0f}) — "
                "institutions building bullish floor."
            )

    # IV analysis
    if iv_percentile >= 80:
        trap_warnings.append(
            f"IV percentile is {iv_percentile:.0f}% — options are expensive. "
            "Avoid buying options; sell premium instead."
        )
    elif iv_percentile <= 20:
        reasons.append(
            f"IV percentile is {iv_percentile:.0f}% — options are cheap. "
            "Good time to buy options for directional bets."
        )

    # Final classification
    if score >= 30:
        overall_bias = "bullish"
        summary = "Options market is bullish. Institutions are building a floor."
    elif score <= -30:
        overall_bias = "bearish"
        summary = "Options market is bearish. Institutions are building a ceiling."
    else:
        overall_bias = "neutral"
        summary = "Options market is neutral. No strong institutional bias."

    return {
        "options_bias": overall_bias,
        "options_score": score,
        "options_summary": summary,
        "pcr": round(pcr, 2),
        "max_pain": round(max_pain, 2),
        "distance_from_max_pain_pct": round((spot_price - max_pain) / spot_price * 100, 2) if max_pain > 0 else 0,
        "iv_percentile": round(iv_percentile, 1),
        "bullish_reasons": reasons,
        "trap_warnings": trap_warnings,
        "recommended_strategy": _suggest_options_strategy(overall_bias, pcr, iv_percentile),
    }


def _suggest_options_strategy(bias: str, pcr: float, iv_pct: float) -> str:
    if bias == "bullish":
        if iv_pct <= 30:
            return "Buy ATM Call OR Bull Call Spread (IV is cheap — buy options)"
        else:
            return "Sell OTM Put (IV is high — sell premium, collect theta)"
    elif bias == "bearish":
        if iv_pct <= 30:
            return "Buy ATM Put OR Bear Put Spread (IV is cheap — buy options)"
        else:
            return "Sell OTM Call (IV is high — sell premium, collect theta)"
    else:
        if iv_pct >= 70:
            return "Iron Condor — Sell both OTM Call and Put (high IV, range-bound)"
        else:
            return "Wait for directional clarity before entering options"


# ─────────────────────────────────────────────────────────────────────────────
# 4. INSTITUTIONAL FOOTPRINT DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_institutional_footprint(
    price_change_pct: float,
    volume_ratio: float,
    delivery_ratio: float,
    delivery_spike: float,
    cmf: float,
    obv_slope: float,
    close_location: float,
    relative_strength: float,
) -> Dict:
    """
    Institutional buying/selling ka footprint detect karo.

    Real institutional buying ke signs:
    - High delivery ratio (real shares changing hands, not intraday speculation)
    - CMF positive (money flowing in)
    - OBV rising (volume on up days > volume on down days)
    - Price closing near highs (close location > 0.7)
    - Relative strength vs Nifty positive

    Institutional selling ke signs:
    - High volume but price not moving (absorption)
    - Delivery high but price falling (forced selling or distribution)
    - CMF negative
    - Close location low (closing near lows)
    """
    reasons = []
    warnings = []
    score = 0

    # Delivery analysis (most reliable signal)
    if delivery_ratio > 0:
        if delivery_ratio >= 0.6 and price_change_pct >= 0:
            score += 30
            reasons.append(
                f"Delivery ratio is {delivery_ratio * 100:.0f}% — real institutional buying. "
                "Not just intraday speculation."
            )
        elif delivery_ratio >= 0.6 and price_change_pct < 0:
            score -= 20
            warnings.append(
                f"High delivery ({delivery_ratio * 100:.0f}%) on a down day — "
                "institutions may be selling/distributing."
            )
        elif delivery_ratio <= 0.25 and price_change_pct >= 2.0:
            score -= 15
            warnings.append(
                f"Low delivery ({delivery_ratio * 100:.0f}%) on a big up move — "
                "retail speculation, not institutional buying. Move may not sustain."
            )

    if delivery_spike >= 1.5 and price_change_pct >= 0:
        score += 20
        reasons.append(
            f"Delivery spike {delivery_spike:.1f}x normal — unusual institutional activity."
        )

    # CMF (Chaikin Money Flow)
    if cmf >= 0.15:
        score += 25
        reasons.append(f"CMF is {cmf:.3f} — strong money inflow. Institutions are accumulating.")
    elif cmf >= 0.05:
        score += 10
        reasons.append(f"CMF is {cmf:.3f} — mild accumulation.")
    elif cmf <= -0.15:
        score -= 25
        warnings.append(f"CMF is {cmf:.3f} — strong money outflow. Institutions are distributing.")
    elif cmf <= -0.05:
        score -= 10
        warnings.append(f"CMF is {cmf:.3f} — mild distribution.")

    # OBV slope
    if obv_slope > 0:
        score += 15
        reasons.append("OBV is rising — volume on up days is greater than down days.")
    elif obv_slope < 0:
        score -= 15
        warnings.append("OBV is falling — volume on down days is greater than up days.")

    # Close location
    if close_location >= 0.75:
        score += 10
        reasons.append(f"Candle closing near highs ({close_location:.2f}) — buyers in control.")
    elif close_location <= 0.25:
        score -= 10
        warnings.append(f"Candle closing near lows ({close_location:.2f}) — sellers in control.")

    # Relative strength
    if relative_strength >= 3.0:
        score += 10
        reasons.append(f"Outperforming Nifty by {relative_strength:.1f}% — institutional interest.")
    elif relative_strength <= -3.0:
        score -= 10
        warnings.append(f"Underperforming Nifty by {abs(relative_strength):.1f}% — institutions avoiding.")

    # Classification
    if score >= 50:
        footprint = "strong_institutional_buying"
        summary = "Strong institutional buying detected. High conviction setup."
    elif score >= 25:
        footprint = "mild_institutional_buying"
        summary = "Mild institutional interest. Setup is constructive but not high conviction."
    elif score <= -50:
        footprint = "strong_institutional_selling"
        summary = "Strong institutional selling/distribution. Avoid fresh longs."
    elif score <= -25:
        footprint = "mild_institutional_selling"
        summary = "Mild distribution. Be cautious with new entries."
    else:
        footprint = "neutral"
        summary = "No clear institutional footprint. Retail-driven move."

    return {
        "institutional_footprint": footprint,
        "institutional_score": score,
        "institutional_summary": summary,
        "institutional_reasons": reasons[:4],
        "institutional_warnings": warnings[:4],
        "is_institutional_buying": score >= 25,
        "is_institutional_selling": score <= -25,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. SMART SHAKEOUT (FOMO PULLBACK) DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_smart_shakeout(
    price: float,
    high: float,
    prev_close: float,
    volume_ratio: float,
    vwap: float,
    ema_20: float,
    bid_ask_ratio: float,
) -> Dict:
    """
    Operator Shakeout (Liquidity Grab) detect karta hai.
    
    Conditions:
    1. FOMO Surge: Intraday high > 5% se upar gaya tha.
    2. Rejection Wick: High se price 2.5% ya zyada gir chuka hai.
    3. Volume: Volume bhayankar high hai (> 2.0x).
    4. Support Hold: Price abhi bhi VWAP aur 20-EMA ke upar safe zone me hai.
    5. Smart Logic: Girte hue market me Bid/Ask ratio acha hai (yani institutional buyers are catching the dip).
    """
    reasons = []
    
    if price <= 0 or prev_close <= 0 or high <= 0:
        return {"is_shakeout": False, "shakeout_reasons": []}
        
    fomo_surge_pct = ((high - prev_close) / prev_close) * 100
    pullback_from_high_pct = ((high - price) / price) * 100
    
    is_fomo_surge = fomo_surge_pct >= 5.0
    is_deep_pullback = pullback_from_high_pct >= 2.5
    is_high_volume = volume_ratio >= 1.8
    is_holding_support = price >= vwap * 0.995 and (ema_20 == 0 or price >= ema_20)
    is_smart_bidding = bid_ask_ratio >= 1.2  # More buyers stepping in on the dip
    
    if is_fomo_surge and is_deep_pullback and is_high_volume and is_holding_support:
        if is_smart_bidding:
            reasons.append(
                f"Smart Shakeout: Stock surged {fomo_surge_pct:.1f}% but pulled back {pullback_from_high_pct:.1f}%. "
                f"Volume is high ({volume_ratio:.1f}x) and Bid/Ask is bullish ({bid_ask_ratio:.1f}x) while holding VWAP support. "
                "Institutions are absorbing weak hands."
            )
            return {"is_shakeout": True, "shakeout_reasons": reasons, "shakeout_confidence": "high"}
        else:
            reasons.append(
                f"Possible Shakeout: Stock pulled back {pullback_from_high_pct:.1f}% after a {fomo_surge_pct:.1f}% surge on high volume. "
                "Holding VWAP, but awaiting stronger bid-depth confirmation."
            )
            return {"is_shakeout": True, "shakeout_reasons": reasons, "shakeout_confidence": "medium"}
            
    return {"is_shakeout": False, "shakeout_reasons": []}


# ─────────────────────────────────────────────────────────────────────────────
# 6. COMPLETE TRAP ANALYSIS (combines all above)
# ─────────────────────────────────────────────────────────────────────────────

def full_trap_analysis(signal: Dict, options_data: Optional[Dict] = None) -> Dict:
    """
    Ek signal ke liye complete trap analysis karo.
    Yeh function scoring.py ke output pe run hota hai.

    Returns:
    - trap_risk: "high", "medium", "low", "none"
    - trap_summary: plain language explanation
    - safe_to_enter: True/False
    - entry_advice: what to do
    """
    price = _safe(signal.get("price") or signal.get("current_price"))
    resistance = _safe(signal.get("resistance_20"))
    prev_close = _safe(signal.get("prev_close") or signal.get("close"))
    high = _safe(signal.get("high"))
    close = _safe(signal.get("close") or signal.get("price"))
    volume_ratio = _safe(signal.get("relative_volume"), 1.0)
    close_location = _safe(signal.get("close_location"), 0.5)
    upper_wick_pct = _safe(signal.get("upper_wick_pct"), 0.0)
    candle_body_pct = 1.0 - upper_wick_pct - _safe(signal.get("lower_wick_pct"), 0.0)
    change_pct = _safe(signal.get("change_pct"))
    delivery_ratio = _safe(signal.get("delivery_ratio"), 0.0)
    delivery_spike = _safe(signal.get("delivery_spike"), 1.0)
    cmf = _safe(signal.get("cmf"), 0.0)
    obv_slope = _safe(signal.get("obv_slope"), 0.0)
    relative_strength = _safe(signal.get("relative_strength_20d"), 0.0)

    # Bull trap check
    bull_trap = detect_bull_trap(
        price=price,
        resistance_level=resistance,
        prev_close=prev_close,
        current_high=high,
        current_close=close,
        volume_ratio=volume_ratio,
        candle_body_pct=candle_body_pct,
        upper_wick_pct=upper_wick_pct,
    )

    # Institutional footprint
    inst = detect_institutional_footprint(
        price_change_pct=change_pct,
        volume_ratio=volume_ratio,
        delivery_ratio=delivery_ratio,
        delivery_spike=delivery_spike,
        cmf=cmf,
        obv_slope=obv_slope,
        close_location=close_location,
        relative_strength=relative_strength,
    )

    # Options analysis (if available)
    options_analysis = None
    if options_data and options_data.get("spot_price", 0) > 0:
        options_analysis = analyze_option_chain_traps(
            spot_price=_safe(options_data.get("spot_price")),
            pcr=_safe(options_data.get("pcr"), 1.0),
            max_pain=_safe(options_data.get("max_pain")),
            call_oi_at_resistance=_safe(options_data.get("call_oi_at_resistance")),
            put_oi_at_support=_safe(options_data.get("put_oi_at_support")),
            call_oi_change=_safe(options_data.get("call_oi_change")),
            put_oi_change=_safe(options_data.get("put_oi_change")),
            iv_percentile=_safe(options_data.get("iv_percentile"), 50.0),
        )

    # Smart Shakeout Check
    shakeout = detect_smart_shakeout(
        price=price,
        high=high,
        prev_close=prev_close,
        volume_ratio=volume_ratio,
        vwap=_safe(signal.get("rolling_vwap") or signal.get("vwap")),
        ema_20=_safe(signal.get("ema_20")),
        bid_ask_ratio=_safe(signal.get("bid_ask_ratio"), 1.0),
    )

    # Aggregate trap risk
    trap_score = 0
    all_warnings = []

    if bull_trap["bull_trap_detected"]:
        if shakeout["is_shakeout"]:
            # OVERRIDE: The bull trap is actually a smart shakeout!
            bull_trap["bull_trap_detected"] = False
            bull_trap["bull_trap_reason"] = ["Bull Trap signal overridden: Identified as a Smart Money Shakeout / Retest."]
            all_warnings.extend(shakeout["shakeout_reasons"])
        else:
            trap_score += bull_trap["bull_trap_score"]
            all_warnings.extend(bull_trap["bull_trap_reason"])

    if inst["is_institutional_selling"]:
        # Soften institutional selling warning if it's a shakeout
        if shakeout["is_shakeout"] and shakeout["shakeout_confidence"] == "high":
            trap_score += abs(inst["institutional_score"]) * 0.3
            all_warnings.append("Institutional selling footprint detected, but overridden by Shakeout Absorption.")
        else:
            trap_score += abs(inst["institutional_score"])
            all_warnings.extend(inst["institutional_warnings"])

    if options_analysis and options_analysis["options_bias"] == "bearish":
        trap_score += 20
        all_warnings.extend(options_analysis["trap_warnings"])

    # Final risk level
    if trap_score >= 80:
        trap_risk = "high"
        safe_to_enter = False
        entry_advice = (
            "DO NOT ENTER — Multiple trap signals detected. "
            "Institutions are likely selling into this move. "
            "Wait for price to stabilize and re-test support."
        )
    elif trap_score >= 40:
        trap_risk = "medium"
        safe_to_enter = False
        entry_advice = (
            "WAIT — Trap risk is elevated. "
            "If you must enter, use very tight stop loss. "
            "Reduce position size by 50%."
        )
    elif trap_score >= 20:
        trap_risk = "low"
        safe_to_enter = True
        entry_advice = (
            "PROCEED WITH CAUTION — Minor trap signals present. "
            "Normal position sizing with defined stop loss."
        )
    else:
        trap_risk = "none"
        safe_to_enter = True
        entry_advice = "CLEAR — No significant trap signals. Setup looks genuine."

    # Institutional confirmation
    if inst["is_institutional_buying"] and trap_risk in ("none", "low"):
        entry_advice += " Institutional buying confirmed — higher conviction entry."

    if shakeout["is_shakeout"]:
        entry_advice = "🎯 Operator Shakeout detected. Weak hands are being flushed. BUY on support hold (VWAP/EMA) when volume dries up."

    return {
        "trap_risk": trap_risk,
        "trap_score": trap_score,
        "safe_to_enter": safe_to_enter,
        "entry_advice": entry_advice,
        "trap_warnings": all_warnings[:5],
        "bull_trap": bull_trap,
        "institutional": inst,
        "options": options_analysis,
        "smart_shakeout": shakeout,
    }
