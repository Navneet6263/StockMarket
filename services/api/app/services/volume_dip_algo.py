"""
Volume-Dip-Rally Algo Engine
════════════════════════════

Yeh module woh pattern detect karta hai jo user ne observe kiya:
  "High volume aata hai → stock thoda neeche aata hai (gap fill / support test)
   → phir ek din mein sab recover karke blast karta hai"

Algo ki logic:
  1. HIGH VOLUME came today (3x+ relative volume)
  2. Structural SUPPORT zone identify karo (real support, not surface level)
  3. GAP FILL level detect karo (agar unfilled gap hai neeche)
  4. DIP TARGET = max(gap_fill_level, structural_support)
  5. SMART ENTRY = dip target zone pe (jab stock wahan aaye, tab lo)
  6. SMART STOP LOSS = real support se 1-2% NEECHE (not at surface)
  7. Chart zones generate karo for visual display

Key insight:
  - Jo stock surface support (e.g. 290) tod ke 270 aata hai, woh 270 pe REAL support hai
  - Entry 290 pe nahi, 270-275 pe leni chahiye
  - SL = 265 (270 se 2% neeche) — agar yahan aaya toh galat tha
  - Target = 350+ (original breakout level + measured move)
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _safe(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or isinstance(val, bool):
            return default
        out = float(val)
        if out != out:  # NaN check
            return default
        return out
    except (TypeError, ValueError):
        return default


def _pct_diff(a: float, b: float) -> float:
    """Percentage difference from b to a."""
    if not b:
        return 0.0
    return ((a - b) / b) * 100


def detect_volume_dip_setup(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main function — volume + dip pattern detect karo and algo plan return karo.

    Returns a dict with:
      - is_volume_dip_setup: bool
      - dip_scenario: str  (e.g. "gap_fill_then_rally", "support_test_then_rally")
      - smart_entry_zone: dict {low, high}   ← yahan se lena hai
      - smart_stop_loss: float               ← structural stop (1-2% below real support)
      - surface_stop_loss: float             ← jo signal pe dikh raha hai (risky)
      - dip_target: float                    ← kitna neeche aa sakta hai
      - rally_target: float                  ← phir kitna upar jayega
      - risk_reward: float
      - confidence_pct: int                  ← 0-100
      - algo_plan: str                       ← human readable plan
      - chart_zones: list                    ← frontend lines ke liye
      - wait_for_dip: bool                   ← kya abhi nahi lena, dip ka wait
      - dip_probability_pct: int             ← kitni probability mein dip aayega
    """

    # ── Input Extraction ────────────────────────────────────────────────────
    price = _safe(snapshot.get("current_price") or snapshot.get("price"))
    if not price:
        return {"is_volume_dip_setup": False}

    relative_volume = _safe(snapshot.get("relative_volume"), 1.0)
    intraday_volume = _safe(snapshot.get("intraday_volume_ratio"), 1.0)
    change_pct = _safe(snapshot.get("change_pct"))
    gap_pct = _safe(snapshot.get("gap_pct"))
    return_5d = _safe(snapshot.get("return_5d"))
    return_20d = _safe(snapshot.get("return_20d"))
    atr_pct = _safe(snapshot.get("atr_pct"), 2.0)
    atr_val = _safe(snapshot.get("atr")) or (price * atr_pct / 100)
    rsi = _safe(snapshot.get("rsi"), 50.0)
    cmf = _safe(snapshot.get("cmf"))
    obv_slope = _safe(snapshot.get("obv_slope"))

    support_20 = _safe(snapshot.get("support_20"))
    resistance_20 = _safe(snapshot.get("resistance_20"))
    ema_20 = _safe(snapshot.get("ema_20"))
    ema_50 = _safe(snapshot.get("ema_50"))
    rolling_vwap = _safe(snapshot.get("rolling_vwap"))

    breakout_20 = bool(snapshot.get("breakout_20") or snapshot.get("intraday_breakout"))
    breakdown_20 = bool(snapshot.get("breakdown_20"))
    above_vwap = bool(snapshot.get("above_vwap") or snapshot.get("intraday_above_vwap"))
    higher_lows = bool(snapshot.get("higher_lows"))
    bb_squeeze = bool(snapshot.get("bb_squeeze"))
    close_location = _safe(snapshot.get("close_location"), 0.5)

    # ── Step 1: Volume Check ─────────────────────────────────────────────────
    # Sirf high volume wale stocks pe yeh algo apply hota hai
    high_volume = relative_volume >= 3.0 or intraday_volume >= 2.5
    extreme_volume = relative_volume >= 7.0 or intraday_volume >= 5.0

    if not high_volume:
        return {"is_volume_dip_setup": False}

    # ── Step 2: Multi-Timeframe Support Confluence ─────────────────────────
    # Yeh main upgrade hai:
    # Har timeframe se support level uthao, phir dhundho kahan 2+ timeframes
    # ek saath agree karte hain (± 1% zone mein) → woh IRON SUPPORT hai
    #
    # Timeframes:
    #   20-day  = Daily chart ke 20 candles (~1 month)
    #   50-day  = Quarterly support
    #   60-day  = 3-month support
    #   120-day = 6-month / semi-annual support
    #   252-day = Yearly support
    #   52w_low = 52-week low (yearly floor)
    #   EMA levels = dynamic support

    all_supports = {}

    # Static support levels from indicators
    for label, key in [
        ("Daily(20d)",    "support_20"),
        ("Monthly(50d)",  "support_50"),
        ("Qtrly(60d)",    "support_60"),
        ("6Month(120d)",  "support_120"),
        ("Yearly(252d)",  "support_252"),
        ("52W_Low",       "low_52w"),
    ]:
        val = _safe(snapshot.get(key))
        if val and 0 < val < price:
            all_supports[label] = val

    # Dynamic EMA supports
    for label, key in [
        ("EMA20", "ema_20"),
        ("EMA50", "ema_50"),
        ("VWAP",  "rolling_vwap"),
    ]:
        val = _safe(snapshot.get(key))
        if val and 0 < val < price:
            all_supports[label] = val

    # ── Find Confluence Zones ────────────────────────────────────────────────
    # Group supports that are within 1% of each other
    # The group with most members = strongest support (most timeframes agree)
    confluence_groups: list[dict] = []
    visited = set()

    sorted_supports = sorted(all_supports.items(), key=lambda x: x[1])

    for i, (label_i, level_i) in enumerate(sorted_supports):
        if label_i in visited:
            continue
        group = {label_i: level_i}
        for label_j, level_j in sorted_supports[i+1:]:
            if label_j in visited:
                continue
            if abs(level_j - level_i) / level_i <= 0.012:  # within 1.2%
                group[label_j] = level_j
                visited.add(label_j)
        visited.add(label_i)
        avg_level = sum(group.values()) / len(group)
        confluence_groups.append({
            "labels": list(group.keys()),
            "count": len(group),
            "level": avg_level,
            "min_level": min(group.values()),
            "max_level": max(group.values()),
        })

    # Sort by strength: most timeframes > closest to price
    confluence_groups.sort(key=lambda g: (-g["count"], price - g["level"]))

    # Best support = highest confluence group (closest to price if tie)
    if confluence_groups:
        best_group = confluence_groups[0]
        primary_support = best_group["level"]
        primary_support_name = " + ".join(best_group["labels"][:3])
        support_strength = best_group["count"]  # how many timeframes agree
    elif all_supports:
        # Fallback: closest support to current price
        closest = min(all_supports.items(), key=lambda x: price - x[1])
        primary_support_name, primary_support = closest
        support_strength = 1
        confluence_groups = [{"labels": [primary_support_name], "count": 1, "level": primary_support}]
    else:
        primary_support_name = "ATR Support"
        primary_support = price * (1 - atr_pct * 1.5 / 100)
        support_strength = 0
        confluence_groups = []

    # Deep support = second confluence group (the next major level below)
    if len(confluence_groups) >= 2:
        deep_support = confluence_groups[1]["level"]
        deep_support_name = " + ".join(confluence_groups[1]["labels"][:2])
    else:
        deep_support = primary_support * 0.95
        deep_support_name = "Estimated"

    # ── Step 3: Gap Fill Detection ───────────────────────────────────────────
    # Agar aaj gap-up tha, toh previous close pe gap fill ka zone
    gap_fill_level = 0.0
    has_unfilled_gap = False
    if gap_pct >= 2.0:
        # Gap-up tha — gap fill level = aaj ke open se gap_pct neeche
        gap_fill_level = price * (1 - gap_pct / 100)
        has_unfilled_gap = True
    elif gap_pct <= -2.0:
        # Gap-down tha — gap fill resistance upar
        gap_fill_level = price * (1 - gap_pct / 100)
        has_unfilled_gap = True

    # ── Step 4: DIP TARGET Calculation ──────────────────────────────────────
    # Dip target = jahan tak neeche aa sakta hai before rally
    # Rule: max(gap_fill, deep_support) but not more than 15% below current price
    candidates = [primary_support]
    if has_unfilled_gap and gap_fill_level > 0:
        candidates.append(gap_fill_level)
    if deep_support > 0:
        candidates.append(deep_support)

    # Dip target = lowest of candidates lekin minimum 5% neeche
    dip_target = min(candidates)
    max_dip_pct = 15.0  # Never expect more than 15% dip in this pattern
    min_dip_pct = 1.0   # At least 1% dip expected (minor pullback)
    dip_target = max(dip_target, price * (1 - max_dip_pct / 100))
    dip_pct = _pct_diff(dip_target, price)  # negative number

    # ── Step 5: SMART ENTRY ZONE ─────────────────────────────────────────────
    # Entry = AT the dip target zone, not at current price
    # Zone = dip_target ± 0.5%
    entry_low = round(dip_target * 0.995, 2)
    entry_high = round(dip_target * 1.005, 2)

    # ── Step 6: SMART STOP LOSS ──────────────────────────────────────────────
    # Yahi main fix hai:
    # Surface stop = jo signal pe dikh raha hai (e.g. support level itself)
    # SMART stop = 1.5% BELOW real structural support
    # Logic: Agar support truly strong hai, toh 1.5% neeche jaana chahiye hi nahi
    smart_stop_loss = round(primary_support * 0.985, 2)   # 1.5% below structural support
    deep_stop_loss = round(deep_support * 0.98, 2)         # 2% below deep support (ultra-safe)
    surface_stop_loss = round(primary_support * 0.99, 2)   # 1% below (tighter, for aggressive)

    stop_distance_pct = abs(_pct_diff(smart_stop_loss, dip_target))

    # ── Step 7: RALLY TARGET ─────────────────────────────────────────────────
    # Rally target = current resistance or measured move from support
    if resistance_20 and resistance_20 > price:
        rally_target = resistance_20
    else:
        # Measured move: distance from support to current price, added above current
        measured_move = price - primary_support
        rally_target = price + (measured_move * 1.2)

    # Extended target (agar sab sahi hua)
    extended_target = rally_target * 1.15

    # ── Step 8: Risk/Reward Calculation ──────────────────────────────────────
    entry_mid = (entry_low + entry_high) / 2
    reward = rally_target - entry_mid
    risk = entry_mid - smart_stop_loss
    risk_reward = round(reward / risk, 2) if risk > 0 else 0.0

    # ── Step 9: Scenario Classification ─────────────────────────────────────
    dip_reasons = []
    dip_probability = 50  # base

    if has_unfilled_gap and gap_pct >= 3.0:
        scenario = "gap_fill_then_rally"
        dip_reasons.append(f"Gap-up of {gap_pct:.1f}% is unfilled — gaps tend to fill before the real move.")
        dip_probability += 20
    elif extreme_volume and return_5d > 8.0:
        scenario = "climax_volume_shakeout"
        dip_reasons.append("Extreme volume after a strong run = climax. Expect 5-10% shakeout first.")
        dip_probability += 25
    elif high_volume and close_location < 0.5 and not above_vwap:
        scenario = "volume_trap_reversal"
        dip_reasons.append("High volume but weak close — distribution likely. Wait for support test.")
        dip_probability += 30
    elif high_volume and above_vwap and cmf >= 0.05 and close_location >= 0.6:
        scenario = "volume_breakout_direct"
        dip_reasons.append("Clean volume breakout with strong close and positive CMF — may not dip much.")
        dip_probability = 25  # Less likely to dip deeply
    elif bb_squeeze and high_volume:
        scenario = "squeeze_explosion"
        dip_reasons.append("Bollinger squeeze + volume burst = explosive move starting. Minor dip only.")
        dip_probability = 35
    elif higher_lows and high_volume:
        scenario = "higher_lows_volume_confirmation"
        dip_reasons.append("Higher lows pattern + volume = accumulation complete. Dip to last higher low level.")
        dip_probability += 15
    else:
        scenario = "volume_support_test"
        dip_reasons.append("High volume — watch for a support test before the main move.")

    # Adjust for momentum
    if return_20d > 25:
        dip_probability = min(dip_probability + 15, 85)
        dip_reasons.append(f"Stock up {return_20d:.0f}% in 20 days — profit booking likely before next leg.")

    if rsi > 70:
        dip_probability = min(dip_probability + 10, 85)
        dip_reasons.append("RSI overextended — cooldown dip expected.")

    if cmf >= 0.10 and obv_slope > 0:
        dip_probability = max(dip_probability - 15, 20)
        dip_reasons.append("Strong CMF + OBV — institutions absorbing, dip may be shallow.")

    dip_probability = max(15, min(85, dip_probability))

    # ── Step 10: Confidence Score ─────────────────────────────────────────────
    confidence = 50
    if extreme_volume:
        confidence += 20
    elif high_volume:
        confidence += 12
    if higher_lows:
        confidence += 8
    if cmf >= 0.05:
        confidence += 8
    if bb_squeeze:
        confidence += 6
    if breakout_20:
        confidence += 8
    if rsi > 70 or rsi < 30:
        confidence -= 8  # Overextended = less reliable entry
    if risk_reward >= 2.0:
        confidence += 8
    # MULTI-TIMEFRAME BONUS: More timeframes agreeing = much higher confidence
    # 1 timeframe = no bonus, 2 timeframes = +8, 3+ timeframes = +15 (IRON SUPPORT)
    if support_strength >= 3:
        confidence += 15
        # Also tighten the SL since support is very strong
        smart_stop_loss = round(primary_support * 0.990, 2)   # tighten to 1% (was 1.5%)
    elif support_strength == 2:
        confidence += 8
        smart_stop_loss = round(primary_support * 0.985, 2)   # keep at 1.5%
    # else: support_strength <= 1 → keep SL at 1.5% (ATR fallback)
    confidence = max(40, min(92, confidence))

    # ── Step 11: Wait for Dip or Enter Now? ──────────────────────────────────
    # Agar aaj price already support ke paas hai (<2% above support), enter now
    # Agar price abhi high pe hai, wait for dip
    dist_to_support_pct = abs(_pct_diff(primary_support, price))
    wait_for_dip = (
        dist_to_support_pct > 2.5  # Price support se kaafi upar
        and dip_probability >= 50
        and scenario != "volume_breakout_direct"
    )

    # ── Step 12: Human Readable Algo Plan ────────────────────────────────────
    # Confluence description
    confluence_str = (
        f"🔒 IRON SUPPORT ({support_strength} timeframes agree)"
        if support_strength >= 3
        else f"💪 Strong ({support_strength} timeframes agree)"
        if support_strength == 2
        else "⚠️ Single timeframe only"
    )
    sl_pct_str = "1.0%" if support_strength >= 3 else "1.5%"
    wait_str = "WAIT FOR DIP" if wait_for_dip else "CURRENT PRICE VALID"

    algo_plan = (
        f"📊 ALGO PLAN [{scenario.upper().replace('_', ' ')}]\n"
        f"• Volume: {relative_volume:.1f}x — {'EXPLOSIVE' if extreme_volume else 'HIGH'}\n"
        f"• Structural Support: ₹{primary_support:.2f} ({confluence_str})\n"
        f"  Timeframes: {primary_support_name}\n"
        f"• Dip Probability: {dip_probability}% → Expected dip to ₹{dip_target:.2f}\n"
        f"• Action: {wait_str}\n"
        f"• Smart Entry Zone: ₹{entry_low:.2f} – ₹{entry_high:.2f}\n"
        f"• Smart Stop Loss: ₹{smart_stop_loss:.2f} ({sl_pct_str} below support ₹{primary_support:.2f})\n"
        f"• Rally Target: ₹{rally_target:.2f} | Extended: ₹{extended_target:.2f}\n"
        f"• Risk/Reward: 1:{risk_reward}\n"
        f"• Why Dip: {dip_reasons[0] if dip_reasons else 'Support test likely'}"
    )

    # ── Step 13: Chart Zones for Frontend ────────────────────────────────────
    # Yeh zones frontend pe lines/bands ke roop mein dikhenge
    chart_zones = [
        {
            "type": "entry_zone",
            "label": f"🎯 Smart Entry Zone",
            "low": entry_low,
            "high": entry_high,
            "color": "#22c55e",      # green
            "opacity": 0.25,
            "description": f"Enter here when dip completes ({primary_support_name} zone)",
        },
        {
            "type": "stop_zone",
            "label": f"🛑 Smart Stop Loss",
            "low": deep_stop_loss,
            "high": smart_stop_loss,
            "color": "#ef4444",      # red
            "opacity": 0.20,
            "description": f"Exit if closes below ₹{smart_stop_loss:.2f} (1.5% below {primary_support_name})",
        },
        {
            "type": "target_zone",
            "label": "🚀 Rally Target",
            "low": rally_target,
            "high": extended_target,
            "color": "#3b82f6",      # blue
            "opacity": 0.20,
            "description": f"Primary target ₹{rally_target:.2f}, extended ₹{extended_target:.2f}",
        },
        {
            "type": "dip_target",
            "label": f"⬇ Dip Zone ({dip_probability}% prob)",
            "low": round(dip_target * 0.99, 2),
            "high": round(dip_target * 1.01, 2),
            "color": "#f59e0b",      # amber
            "opacity": 0.20,
            "description": f"Expected pullback zone — {dip_probability}% probability",
        },
        {
            "type": "support_line",
            "label": (
                f"🔒 IRON SUPPORT ({support_strength} TF)"
                if support_strength >= 3
                else f"📍 Support ({support_strength} TF)"
            ),
            "value": round(primary_support, 2),
            "color": "#8b5cf6",      # purple (brighter when iron)
            "line_style": "solid" if support_strength >= 3 else "dashed",
            "description": f"{primary_support_name} at ₹{primary_support:.2f} — {support_strength} timeframe confluence",
        },
    ]

    # Add gap fill zone if exists
    if has_unfilled_gap and gap_fill_level > 0:
        chart_zones.append({
            "type": "gap_fill",
            "label": "🕳 Gap Fill Level",
            "value": round(gap_fill_level, 2),
            "color": "#ec4899",     # pink
            "line_style": "dotted",
            "description": f"Unfilled gap from {gap_pct:.1f}% move — magnets price",
        })

    return {
        "is_volume_dip_setup": True,
        "scenario": scenario,
        "dip_scenario": scenario,

        # Entry
        "smart_entry_zone": {"low": entry_low, "high": entry_high},
        "wait_for_dip": wait_for_dip,
        "dip_probability_pct": dip_probability,

        # Levels
        "dip_target": round(dip_target, 2),
        "dip_pct_expected": round(dip_pct, 2),
        "primary_support": round(primary_support, 2),
        "primary_support_name": primary_support_name,
        "deep_support": round(deep_support, 2),
        "gap_fill_level": round(gap_fill_level, 2) if has_unfilled_gap else None,
        "has_unfilled_gap": has_unfilled_gap,

        # Stop Loss (3 options — smart, surface, deep)
        "smart_stop_loss": smart_stop_loss,          # RECOMMENDED (tighter when strong confluence)
        "surface_stop_loss": surface_stop_loss,       # 1% below (aggressive)
        "deep_stop_loss": deep_stop_loss,             # 2% below (conservative)

        # Target
        "rally_target": round(rally_target, 2),
        "extended_target": round(extended_target, 2),

        # Risk metrics
        "risk_reward": risk_reward,
        "confidence_pct": confidence,

        # ── Multi-Timeframe Confluence Data ──────────────────────────────
        "support_confluence_count": support_strength,       # how many timeframes agree
        "support_timeframes": primary_support_name,         # e.g. "EMA50 + Monthly(50d) + Yearly(252d)"
        "confluence_groups": [
            {
                "level": round(g["level"], 2),
                "timeframes": g["labels"],
                "strength": g["count"],
            }
            for g in confluence_groups[:4]  # top 4 support levels
        ],

        # Chart
        "chart_zones": chart_zones,

        # Readable
        "algo_plan": algo_plan,
        "dip_reasons": dip_reasons[:3],
    }
