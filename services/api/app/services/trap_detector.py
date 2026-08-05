"""Trap and visible-footprint analysis.

Price, volume, delivery, order-book depth, and option open interest reveal market
behaviour; they do not reveal the identity or intent of the participant behind a
trade. The helpers in this module therefore report evidence-alignment and trap
risk. They must never be read as proof of FII, DII, institutional, or retail
activity.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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
    """Flag displayed bid depth that is not producing an upward response.

    Depth can be cancelled and it never identifies the participant. A mismatch
    is a reason to wait for executed-price confirmation, not proof of spoofing
    or of any participant's intent.

    Signs:
    - Bid quantity >> Ask quantity (buyers dikhte hain)
    - Lekin price neeche ja rahi hai ya flat hai
    - Volume high hai lekin close location low hai (close near low of candle)
    - Displayed depth is not confirmed by executed price
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
            f"({price_change_pct:.2f}%) — displayed demand is not producing a price response."
        )
    elif bid_ask_ratio >= 2.0 and price_change_pct <= 0:
        score += 30
        reasons.append(
            f"Heavy buy-side depth ({bid_ask_ratio:.1f}x) but price not moving up — "
            "displayed depth is unconfirmed by executed price."
        )

    # Close location: Candle ke neeche close = sellers in control
    if close_location <= 0.3 and volume_ratio >= 1.5:
        score += 25
        reasons.append(
            f"High volume ({volume_ratio:.1f}x) but candle closing near lows "
            f"(close location {close_location:.2f}) — visible selling pressure dominates."
        )

    # Volume high but price not moving = absorption
    if volume_ratio >= 2.0 and abs(price_change_pct) <= 0.5:
        score += 20
        reasons.append(
            f"Volume is {volume_ratio:.1f}x average but price barely moved — "
            "two-sided absorption is possible; direction needs price confirmation."
        )

    trap_detected = score >= 40
    if score >= 70:
        trap_type = "strong_sell_trap"
        action = "AVOID — Strong bid/price divergence; wait for executed-price confirmation."
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
    """Detect a failed breakout from observable price/volume behaviour.

    Kaise identify karein:
    - Price resistance ke upar gayi (breakout dikhta hai)
    - Lekin close resistance ke neeche aa gayi (failed breakout)
    - Ya upper wick bahut bada hai (rejection at resistance)
    - Volume high tha breakout pe, but price did not hold
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
                "strong visible selling pressure at this level."
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
            "supply overwhelmed demand; participant identity is unknown."
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
        action = "SELL/SHORT — Strong failed breakout. Exit longs or wait for a clean reclaim."
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
    Option-chain positioning aur crowding risk identify karo.

    Aggregate OI shows positioning/crowding, not whether contracts were bought
    or written and not participant identity. It is descriptive context only;
    price/volume structure must provide any directional vote.
    """
    reasons = []
    bias = "neutral"
    trap_warnings = []
    score = 0

    # Max Pain analysis
    if max_pain > 0 and spot_price > 0:
        distance_from_max_pain = (spot_price - max_pain) / spot_price * 100
        if distance_from_max_pain > 2.0:
            trap_warnings.append(
                f"Spot ({spot_price:.0f}) is {distance_from_max_pain:.1f}% ABOVE max pain ({max_pain:.0f}). "
                "This is an expiry payout reference, not a directional price magnet."
            )
        elif distance_from_max_pain < -2.0:
            reasons.append(
                f"Spot ({spot_price:.0f}) is {abs(distance_from_max_pain):.1f}% BELOW max pain ({max_pain:.0f}). "
                "This is an expiry payout reference, not a directional price magnet."
            )

    # PCR analysis
    if pcr >= 1.5:
        bias = "put_oi_heavy"
        reasons.append(
            f"PCR is {pcr:.2f} — put OI is heavy. Aggregate OI alone does not reveal "
            "buying versus writing or market direction."
        )
    elif pcr >= 1.2:
        bias = "put_oi_heavy"
        reasons.append(f"PCR {pcr:.2f} — put-side OI is moderately heavier; direction is unknown.")
    elif pcr <= 0.7:
        bias = "call_oi_heavy"
        trap_warnings.append(
            f"PCR is {pcr:.2f} — call OI is heavy. Aggregate OI alone does not reveal "
            "buying versus writing or market direction."
        )
    elif pcr <= 0.9:
        bias = "call_oi_heavy"
        trap_warnings.append(f"PCR {pcr:.2f} — call-side OI is moderately heavier; direction is unknown.")

    # Call OI at resistance = ceiling
    if call_oi_at_resistance > 0 and put_oi_at_support > 0:
        oi_ratio = call_oi_at_resistance / max(put_oi_at_support, 1)
        if oi_ratio >= 2.0:
            trap_warnings.append(
                f"Call OI at resistance is {oi_ratio:.1f}x put OI at support — "
                "call-side crowding is visible, but its trade side and direction are unknown."
            )
        elif oi_ratio <= 0.5:
            reasons.append(
                f"Put OI at support is {1/oi_ratio:.1f}x call OI at resistance — "
                "put-side crowding is visible, but its trade side and direction are unknown."
            )

    # OI change analysis (fresh positions)
    if call_oi_change > 0 and put_oi_change > 0:
        if call_oi_change > put_oi_change * 1.5:
            trap_warnings.append(
                f"Call OI increase ({call_oi_change:,.0f}) >> put OI increase ({put_oi_change:,.0f}) — "
                "buying versus writing cannot be identified from OI change alone."
            )
        elif put_oi_change > call_oi_change * 1.5:
            reasons.append(
                f"Put OI increase ({put_oi_change:,.0f}) >> call OI increase ({call_oi_change:,.0f}) — "
                "buying versus writing cannot be identified from OI change alone."
            )

    # IV analysis
    if iv_percentile >= 80:
        trap_warnings.append(
            f"IV percentile is {iv_percentile:.0f}% — options are expensive. "
            "Long-option entries carry IV-crush risk; use defined risk if a separate price signal confirms."
        )
    elif iv_percentile <= 20:
        reasons.append(
            f"IV percentile is {iv_percentile:.0f}% — implied volatility is relatively low, "
            "but that is not a directional entry signal."
        )

    overall_bias = "neutral"
    summary = "Options OI is positioning context only; these fields provide no directional inference."

    return {
        "options_bias": overall_bias,
        "options_score": score,
        "options_summary": summary,
        "positioning_label": bias,
        "directional_inference_available": False,
        "pcr": round(pcr, 2),
        "max_pain": round(max_pain, 2),
        "distance_from_max_pain_pct": round((spot_price - max_pain) / spot_price * 100, 2) if max_pain > 0 else 0,
        "iv_percentile": round(iv_percentile, 1),
        "bullish_reasons": reasons,
        "trap_warnings": trap_warnings,
        "recommended_strategy": (
            "No directional options trade from aggregate OI alone; wait for price/volume confirmation "
            "and prefer a defined-risk structure."
        ),
    }


def _suggest_options_strategy(bias: str, pcr: float, iv_pct: float) -> str:
    return (
        "Wait for a separate price/volume trigger. If trading options, use a defined-risk structure "
        "sized for the actual stop; PCR and IV alone are not entry signals."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. LARGE-MONEY VISIBLE-FOOTPRINT DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_large_money_footprint(
    price_change_pct: float,
    volume_ratio: float,
    delivery_ratio: float,
    delivery_spike: float,
    cmf: float,
    obv_slope: float,
    close_location: float,
    relative_strength: float,
    *,
    delivery_available: Optional[bool] = None,
    data_availability: Optional[Dict[str, bool]] = None,
) -> Dict:
    """Estimate directional large-money *footprints*, never participant identity.

    A high delivery ratio or volume spike only proves elevated participation.
    Direction comes from price response, close location and money-flow
    alignment. The signed score is an explainable evidence score, not a
    calibrated probability that an FII/DII/institution is trading.
    """
    availability = data_availability or {}
    if delivery_available is None:
        delivery_available = bool(delivery_ratio > 0 and availability.get("delivery", True))

    buying_score = 0.0
    selling_score = 0.0
    evidence: list[str] = []
    warnings: list[str] = []

    # Volume only becomes directional when executed price confirms it.
    if volume_ratio >= 2.0:
        if price_change_pct >= 1.0 and close_location >= 0.65:
            buying_score += 24
            evidence.append(
                f"{volume_ratio:.1f}x volume closed strongly with positive price response."
            )
        elif price_change_pct <= -1.0 and close_location <= 0.35:
            selling_score += 24
            warnings.append(
                f"{volume_ratio:.1f}x volume closed weakly with negative price response."
            )
        else:
            warnings.append(
                f"{volume_ratio:.1f}x volume has no clean directional price response yet."
            )

    # Delivery is useful only when the feed explicitly marks it as available.
    if delivery_available:
        if delivery_ratio >= 0.60:
            evidence.append(
                f"Delivery ratio is {delivery_ratio * 100:.0f}%, showing positional participation; "
                "participant identity is unavailable."
            )
            if price_change_pct >= 0.5 and close_location >= 0.60:
                buying_score += 12
            elif price_change_pct <= -0.5 and close_location <= 0.40:
                selling_score += 12
        if delivery_spike >= 1.5:
            if price_change_pct >= 0.5 and close_location >= 0.60:
                buying_score += 12
                evidence.append(
                    f"Delivery is {delivery_spike:.1f}x normal and price held the advance."
                )
            elif price_change_pct <= -0.5 and close_location <= 0.40:
                selling_score += 12
                warnings.append(
                    f"Delivery is {delivery_spike:.1f}x normal while price closed weakly."
                )
            else:
                warnings.append(
                    f"Delivery is {delivery_spike:.1f}x normal but directional response is mixed."
                )
        if delivery_ratio <= 0.25 and price_change_pct >= 2.0:
            warnings.append(
                f"Delivery is only {delivery_ratio * 100:.0f}% on the up move; follow-through is unconfirmed."
            )
    else:
        warnings.append("Verified delivery data is unavailable; delivery is excluded from the score.")

    if cmf >= 0.15:
        buying_score += 22
        evidence.append(f"CMF {cmf:.3f} shows strong positive money flow.")
    elif cmf >= 0.05:
        buying_score += 10
        evidence.append(f"CMF {cmf:.3f} is mildly positive.")
    elif cmf <= -0.15:
        selling_score += 22
        warnings.append(f"CMF {cmf:.3f} shows strong negative money flow.")
    elif cmf <= -0.05:
        selling_score += 10
        warnings.append(f"CMF {cmf:.3f} is mildly negative.")

    if obv_slope > 0:
        buying_score += 14
        evidence.append("OBV is rising, aligning volume with up sessions.")
    elif obv_slope < 0:
        selling_score += 14
        warnings.append("OBV is falling, aligning volume with down sessions.")

    if close_location >= 0.75:
        buying_score += 12
        evidence.append(f"Candle closed near its high ({close_location:.2f}).")
    elif close_location <= 0.25:
        selling_score += 12
        warnings.append(f"Candle closed near its low ({close_location:.2f}).")

    if relative_strength >= 3.0:
        buying_score += 8
        evidence.append(f"Price outperformed the benchmark by {relative_strength:.1f}%.")
    elif relative_strength <= -3.0:
        selling_score += 8
        warnings.append(f"Price underperformed the benchmark by {abs(relative_strength):.1f}%.")

    signed_score = float(_clip(buying_score - selling_score, -100, 100))
    if signed_score >= 45:
        bias = "strong_buying_pressure"
    elif signed_score >= 20:
        bias = "buying_pressure"
    elif signed_score <= -45:
        bias = "strong_selling_pressure"
    elif signed_score <= -20:
        bias = "selling_pressure"
    else:
        bias = "mixed_or_neutral"

    available = {
        "price_volume": availability.get("price_volume", True),
        "delivery": bool(delivery_available),
        "cmf": availability.get("cmf", True),
        "obv": availability.get("obv", True),
        "relative_strength": availability.get("relative_strength", True),
    }
    quality_score = (
        (25 if available["price_volume"] else 0)
        + (25 if available["delivery"] else 0)
        + (20 if available["cmf"] else 0)
        + (15 if available["obv"] else 0)
        + (15 if available["relative_strength"] else 0)
    )
    quality_grade = "high" if quality_score >= 80 else "medium" if quality_score >= 50 else "low"
    summary = (
        f"Visible footprint shows {bias.replace('_', ' ')}. "
        "This is behavioural evidence, not participant identification."
    )

    return {
        "bias": bias,
        "score": round(signed_score, 1),
        "strength": round(abs(signed_score), 1),
        "buying_evidence_score": round(min(100, buying_score), 1),
        "selling_evidence_score": round(min(100, selling_score), 1),
        "evidence": evidence[:5],
        "warnings": warnings[:5],
        "summary": summary,
        "data_quality": {
            "grade": quality_grade,
            "score": quality_score,
            "available": available,
            "delivery_verified": bool(delivery_available),
        },
        "buying_pressure": signed_score >= 20,
        "selling_pressure": signed_score <= -20,
        "identity_inference_supported": False,
        "score_is_calibrated_probability": False,
        "identity_note": (
            "Participant identity cannot be inferred from public tape data: FII, DII, "
            "institutional, and retail flow are not distinguishable here."
        ),
    }


def detect_institutional_footprint(
    price_change_pct: float,
    volume_ratio: float,
    delivery_ratio: float,
    delivery_spike: float,
    cmf: float,
    obv_slope: float,
    close_location: float,
    relative_strength: float,
    **kwargs,
) -> Dict:
    """Backward-compatible name; returns the truthful visible-footprint schema."""
    result = detect_large_money_footprint(
        price_change_pct=price_change_pct,
        volume_ratio=volume_ratio,
        delivery_ratio=delivery_ratio,
        delivery_spike=delivery_spike,
        cmf=cmf,
        obv_slope=obv_slope,
        close_location=close_location,
        relative_strength=relative_strength,
        **kwargs,
    )
    return {
        **result,
        # Legacy keys intentionally refuse to infer identity. New consumers
        # should use bias/score/evidence/data_quality above.
        "institutional_footprint": "not_inferable_from_public_tape",
        "institutional_score": None,
        "institutional_summary": result["identity_note"],
        "institutional_reasons": [],
        "institutional_warnings": [result["identity_note"]],
        "is_institutional_buying": False,
        "is_institutional_selling": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. COMPLETE TRAP ANALYSIS (combines all above)
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

    # Visible footprint. It deliberately makes no participant-identity claim.
    footprint = detect_large_money_footprint(
        price_change_pct=change_pct,
        volume_ratio=volume_ratio,
        delivery_ratio=delivery_ratio,
        delivery_spike=delivery_spike,
        cmf=cmf,
        obv_slope=obv_slope,
        close_location=close_location,
        relative_strength=relative_strength,
        delivery_available=bool(signal.get("delivery_available")),
        data_availability={
            "price_volume": "relative_volume" in signal,
            "delivery": bool(signal.get("delivery_available")),
            "cmf": "cmf" in signal and signal.get("cmf") is not None,
            "obv": "obv_slope" in signal and signal.get("obv_slope") is not None,
            "relative_strength": (
                "relative_strength_20d" in signal and signal.get("relative_strength_20d") is not None
            ),
        },
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

    # Aggregate trap risk
    trap_score = 0
    all_warnings = []

    if bull_trap["bull_trap_detected"]:
        trap_score += bull_trap["bull_trap_score"]
        all_warnings.extend(bull_trap["bull_trap_reason"])

    if footprint["selling_pressure"]:
        trap_score += abs(footprint["score"])
        all_warnings.extend(footprint["warnings"])

    if options_analysis and options_analysis["options_bias"] == "bearish":
        trap_score += 20
        all_warnings.extend(options_analysis["trap_warnings"])

    # Final risk level
    if trap_score >= 80:
        trap_risk = "high"
        safe_to_enter = False
        entry_advice = (
            "DO NOT ENTER — Multiple trap signals detected. "
            "Visible selling pressure is dominating this move. "
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

    if footprint["buying_pressure"] and trap_risk in ("none", "low"):
        entry_advice += " Buying-footprint evidence is aligned; participant identity remains unknown."

    return {
        "trap_risk": trap_risk,
        "trap_score": trap_score,
        "safe_to_enter": safe_to_enter,
        "entry_advice": entry_advice,
        "trap_warnings": all_warnings[:5],
        "bull_trap": bull_trap,
        "large_money_footprint": footprint,
        # Compatibility container for older clients. The nested payload itself
        # explicitly states that identity inference is unsupported.
        "institutional": {
            "institutional_footprint": "not_inferable_from_public_tape",
            "institutional_score": None,
            "institutional_summary": footprint["identity_note"],
            "institutional_reasons": [],
            "institutional_warnings": [footprint["identity_note"]],
            "is_institutional_buying": False,
            "is_institutional_selling": False,
            "identity_inference_supported": False,
            "visible_footprint": footprint,
        },
        "options": options_analysis,
    }
