"""Smart Layer Integrator — combines A-E layers into final classification.
Zero changes to scoring.py / indicators.py / ml_predictor.py.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List

import pandas as pd

from app.services.base_quality import score_base_quality
from app.services.delivery_analysis import analyse_delivery
from app.services.market_breadth import compute_market_breadth, should_block_buy
from app.services.sector_strength import build_sector_strength_map, compute_sector_strength, rank_global_sectors
from app.services.swing_ml import compute_swing_5d

logger = logging.getLogger(__name__)

# ── Score weights ─────────────────────────────────────────────────────────────
W_CHART = float(os.getenv("SMART_W_CHART", "0.25"))
W_BASE = float(os.getenv("SMART_W_BASE", "0.20"))
W_VOLUME = float(os.getenv("SMART_W_VOLUME", "0.20"))
W_MARKET = float(os.getenv("SMART_W_MARKET", "0.20"))
W_ML = float(os.getenv("SMART_W_ML", "0.15"))

# ── Classification thresholds ─────────────────────────────────────────────────
HOT_PICK_MIN = int(os.getenv("HOT_PICK_MIN_SCORE", "80"))  # Strict 80+ evidence alignment
WATCHLIST_MIN = int(os.getenv("WATCHLIST_MIN_SCORE", "60"))
BASE_RADAR_MIN_BASE = int(os.getenv("BASE_RADAR_MIN_BASE_SCORE", "60"))


def enrich_signal(
    signal: Dict,
    frame: pd.DataFrame,
    market_context: Dict,
    all_signals: List[Dict],
    global_sectors: Dict[str, Dict],
    sector_strength_map: Dict[str, Dict] | None = None,
    ml_model=None,
) -> Dict:
    """
    Enrich a single signal dict with all 5 new layers.
    Returns the signal with new keys added — existing keys untouched.
    """
    symbol = signal.get("symbol", "")

    # ── B: Base quality ───────────────────────────────────────────────────────
    base = score_base_quality(frame)

    # ── C: Delivery ───────────────────────────────────────────────────────────
    delivery = analyse_delivery(frame)

    # ── D: Swing 5d ───────────────────────────────────────────────────────────
    swing = compute_swing_5d(frame, ml_model)

    # ── E: Sector strength ────────────────────────────────────────────────────
    sector = (sector_strength_map or {}).get(str(symbol).upper())
    if sector is None:
        sector = compute_sector_strength(
            symbol,
            signal.get("return_20d"),
            all_signals,
        )
    
    # Apply global sector ranking
    sec_name = sector.get("sector")
    if sec_name and sec_name in global_sectors:
        sector["globalSectorRank"] = global_sectors[sec_name]["rank"]
        sector["globalSectorCategory"] = global_sectors[sec_name]["category"]
    else:
        sector["globalSectorCategory"] = "neutral"

    # ── Final smart score ─────────────────────────────────────────────────────
    chart_score = max(
        float(signal.get("move_quality") or signal.get("quality_score") or 50),
        float(signal.get("chart_pattern_score") or 0),
    )
    if signal.get("live_pattern_ready"):
        chart_score = min(chart_score + 6, 100)
    base_score = float(base.get("baseQualityScore") or 50)
    volume_score = _volume_score(signal, delivery)
    market_score = _market_score(market_context, sector)
    ml_score = _ml_score(signal, swing)

    smart_score = int(round(
        chart_score * W_CHART
        + base_score * W_BASE
        + volume_score * W_VOLUME
        + market_score * W_MARKET
        + ml_score * W_ML
    ))

    # ── Classification ────────────────────────────────────────────────────────
    buy_blocked = should_block_buy(market_context)
    direction = signal.get("direction", "neutral")
    action = signal.get("action", "WATCH")

    if buy_blocked and action in {"BUY", "REENTRY_BUY"}:
        classification = "blocked_buy"
        blocked_reason = market_context.get("blockedReason", "Market breadth weak; fresh buy calls blocked.")
        action_override = "WATCH"
    elif _is_hot_pick_candidate(signal, smart_score, buy_blocked):
        classification = "hot_pick"
        blocked_reason = None
        action_override = None
    elif smart_score >= WATCHLIST_MIN:
        classification = "watchlist"
        blocked_reason = None
        action_override = None
    elif base.get("baseQualityScore") and base["baseQualityScore"] >= BASE_RADAR_MIN_BASE:
        classification = "base_formation_radar"
        blocked_reason = None
        action_override = None
    elif (
        float(signal.get("change_pct") or 0) >= 3
        or float(signal.get("relative_volume") or 1) >= 1.8
    ) and signal.get("direction") != "bearish":
        # Only positive/bullish momentum — avoid labelling crashes as momentum
        classification = "momentum_radar"
        blocked_reason = None
        action_override = None
    else:
        classification = "watch"
        blocked_reason = None
        action_override = None

    enriched = {
        **signal,
        # New layer outputs
        **{f"base_{k}": v for k, v in base.items()},
        **{f"delivery_{k}": v for k, v in delivery.items()},
        **{f"swing_{k}": v for k, v in swing.items()},
        **{f"sector_{k}": v for k, v in sector.items()},
        # Flat convenience keys
        "baseQualityScore": base.get("baseQualityScore"),
        "basePatternType": base.get("basePatternType"),
        "breakoutTrigger": base.get("breakoutTrigger"),
        "deliverySignal": delivery.get("deliverySignal"),
        "mlSwing5dProbability": swing.get("mlSwing5dProbability"),
        "sectorStrength": sector.get("sectorStrength"),
        "sectorRank": sector.get("sectorRank"),
        # Smart score + classification
        "smartScore": smart_score,
        "smartClassification": classification,
        "blockedBuyReason": blocked_reason,
        "actionOverride": action_override,
        "effectiveAction": action_override or action,
    }

    if blocked_reason:
        enriched.setdefault("risk_factors", [])
        if isinstance(enriched["risk_factors"], list):
            enriched["risk_factors"] = list(enriched["risk_factors"]) + [blocked_reason]

    return enriched


def build_smart_scan_payload(
    scan_payload: Dict,
    daily_frames: Dict[str, pd.DataFrame],
    benchmark_frame: pd.DataFrame,
    ml_models: Dict = None,
) -> Dict:
    """
    Post-process the existing scan payload with all 5 new layers.
    Called after _shape_scan_payload — does not replace it.
    """
    raw_results: List[Dict] = (
        scan_payload.get("top_opportunities", [])
        + scan_payload.get("breakout_candidates", [])
        + scan_payload.get("candidates", [])
        + scan_payload.get("unusual_volume", [])
        + scan_payload.get("candle_watch_setups", [])
    )
    all_results: List[Dict] = []
    seen_symbols: set[str] = set()
    for signal in raw_results:
        if not isinstance(signal, dict):
            continue
        symbol = str(signal.get("symbol") or "").upper()
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        all_results.append(signal)

    # ── A: Market breadth (once for all) ─────────────────────────────────────
    try:
        expected_breadth_universe = int(
            (scan_payload.get("market_discovery") or {}).get("total_scanned")
            or scan_payload.get("universe_size")
            or len(daily_frames)
        )
    except (TypeError, ValueError):
        expected_breadth_universe = len(daily_frames)
    market_context = compute_market_breadth(
        benchmark_frame,
        all_results,
        daily_frames=daily_frames,
        expected_universe_count=expected_breadth_universe,
    )
    
    # Global Sector Ranking
    global_sectors = rank_global_sectors(all_results)
    sector_strength_map = build_sector_strength_map(all_results)
    
    logger.info(
        "[SMART] marketMood=%s freshBuyBlocked=%s totalSignals=%d",
        market_context.get("marketMood"),
        market_context.get("freshBuyBlocked"),
        len(all_results),
    )

    ml_models = ml_models or {}
    enriched_map: Dict[str, Dict] = {}
    blocked_count = 0
    base_candidates = 0
    delivery_unknown = 0
    sector_unknown = 0
    swing_unavailable = 0

    for signal in all_results:
        symbol = str(signal.get("symbol") or "").upper()
        frame = daily_frames.get(symbol)
        if frame is None or frame.empty:
            continue

        enriched = enrich_signal(
            signal,
            frame,
            market_context,
            all_results,
            global_sectors,
            sector_strength_map=sector_strength_map,
            ml_model=ml_models.get(symbol.upper()),
        )
        enriched_map[symbol] = enriched

        # Debug counters
        if enriched.get("blockedBuyReason"):
            blocked_count += 1
        if (enriched.get("baseQualityScore") or 0) >= BASE_RADAR_MIN_BASE:
            base_candidates += 1
        if enriched.get("deliverySignal") == "unknown":
            delivery_unknown += 1
        if not enriched.get("sector_sectorAvailable"):
            sector_unknown += 1
        if not enriched.get("swing_swing5dAvailable"):
            swing_unavailable += 1

    logger.info(
        "[SMART] blocked=%d base_candidates=%d delivery_unknown=%d sector_unknown=%d swing_unavailable=%d",
        blocked_count, base_candidates, delivery_unknown, sector_unknown, swing_unavailable,
    )

    # ── Build output buckets ──────────────────────────────────────────────────
    def number(value, fallback: float = 0.0) -> float:
        try:
            return float(value if value is not None else fallback)
        except (TypeError, ValueError):
            return fallback

    def final_gate(raw_signal: Dict) -> Dict:
        """Apply one authoritative eligibility decision to every consumer."""
        from app.services.book_strategy import apply_candle_gate

        symbol = str(raw_signal.get("symbol") or "").upper()
        enriched = enriched_map.get(symbol)
        signal = {**raw_signal, **enriched} if enriched else dict(raw_signal)
        direction = str(signal.get("direction") or "neutral").lower()
        stage = str(signal.get("signal_stage") or signal.get("setup_stage") or "").upper()
        action = str(
            signal.get("effectiveAction")
            or signal.get("action")
            or signal.get("recommended_action")
            or "WATCH"
        ).upper()
        entry = number(signal.get("safe_entry_price") or signal.get("entry_trigger"))
        stop = number(signal.get("invalidation_level") or signal.get("stop_loss") or signal.get("invalidation"))
        target = number(signal.get("target_1") or signal.get("target_price") or signal.get("target"))
        footprint = signal.get("large_money_footprint") or {}
        footprint_score = number(
            (
                footprint.get("score")
                if isinstance(footprint, dict)
                else None
            )
            or signal.get("large_money_footprint_score")
        )

        plan_blocked_reason = signal.get("entry_plan_blocked_reason")
        if signal.get("entry_plan_blocked"):
            plan_blocked_reason = plan_blocked_reason or (
                "Structural trade plan is watch-only; wait for a safer entry."
            )
        if signal.get("enforce_structural_risk_cap") and entry > 0 and stop > 0:
            risk_pct = abs(entry - stop) / entry * 100
            max_risk_pct = number(signal.get("max_structural_risk_pct"), 4.0)
            signal["riskPct"] = round(risk_pct, 3)
            if risk_pct > max_risk_pct:
                plan_blocked_reason = (
                    f"Structural stop is {risk_pct:.2f}% away, above the "
                    f"{max_risk_pct:.2f}% live-arm cap."
                )

        if plan_blocked_reason:
            signal.update(
                {
                    "tradePlanGateBlocked": True,
                    "entry_plan_blocked": True,
                    "entry_plan_blocked_reason": plan_blocked_reason,
                    "effectiveAction": "WATCH",
                    "actionOverride": "WATCH",
                    "allow_buy_call": False,
                    "allow_trade_call": False,
                    "attention_only": True,
                    "freshEntryAllowed": False,
                }
            )
            risks = list(signal.get("risk_factors") or [])
            if plan_blocked_reason not in risks:
                risks.append(plan_blocked_reason)
            signal["risk_factors"] = risks
            return apply_candle_gate(signal)

        coherent_bullish_reversal = bool(
            direction == "bullish"
            and stage in {"BULLISH_REVERSAL_CANDIDATE", "RETEST_ENTRY", "SUPPORT_BOUNCE"}
            and str(signal.get("reversal_bias") or "bullish").lower() == "bullish"
            and number(signal.get("bullish_reversal_score") or signal.get("reversal_risk_score")) >= 65
            and entry > 0
            and 0 < stop < entry < target
            and signal.get("requires_live_confirmation")
        )
        coherent_bearish_breakdown = bool(
            direction == "bearish"
            and entry > 0
            and 0 < target < entry < stop
            and (
                (stage == "BREAKDOWN_WATCH" and footprint_score <= -35)
                or (
                    action in {"SELL", "ALERT_BELOW_LEVEL", "WAIT_FOR_BREAKDOWN_CONFIRMATION"}
                    and footprint_score <= -50
                )
            )
        )

        blocked_reason = signal.get("blockedBuyReason")
        counter_regime = False
        mood = str(market_context.get("marketMood") or "unknown").lower()
        signal["marketMood"] = mood
        if direction == "bullish" and should_block_buy(market_context):
            if coherent_bullish_reversal:
                counter_regime = True
                blocked_reason = None
                if signal.get("smartClassification") == "blocked_buy":
                    signal["smartClassification"] = "watchlist"
            else:
                blocked_reason = blocked_reason or market_context.get(
                    "blockedReason", "Market breadth is bearish; fresh long entries are blocked."
                )
        elif direction == "bearish" and mood == "bullish":
            if coherent_bearish_breakdown:
                counter_regime = True
            else:
                blocked_reason = blocked_reason or (
                    "Market breadth is bullish; fresh short entries need a confirmed breakdown."
                )

        if blocked_reason:
            signal.update(
                {
                    "blockedBuyReason": blocked_reason,
                    "marketGateBlocked": True,
                    "effectiveAction": "WATCH",
                    "actionOverride": "WATCH",
                    "allow_buy_call": False,
                    "allow_trade_call": False,
                    "attention_only": True,
                    "smartClassification": "blocked_buy" if direction == "bullish" else "blocked_trade",
                    "marketGateDecision": "BLOCKED",
                }
            )
            risks = list(signal.get("risk_factors") or [])
            if blocked_reason not in risks:
                risks.append(blocked_reason)
            signal["risk_factors"] = risks
        elif counter_regime:
            signal.update(
                {
                    "counterRegime": True,
                    "requires_live_confirmation": True,
                    "requires_full_tick": True,
                    "live_min_confirmations": 3,
                    "allow_buy_call": False,
                    "allow_trade_call": False,
                    "attention_only": True,
                    "marketGateDecision": "COUNTER_REGIME_STRICT_CONFIRMATION",
                }
            )
        else:
            signal["marketGateDecision"] = "ALIGNED_OR_NEUTRAL"
        return apply_candle_gate(signal)

    authoritative_bucket_names = (
        "candle_watch_setups",
        "top_opportunities", "breakout_candidates", "bearish_risks", "unusual_volume",
        "candidates", "pre_breakout_setups", "pattern_forming_setups",
        "alert_above_setups", "retest_entry", "momentum_continuation",
        "re_entry_setups", "avoid_late_entry", "avoid_risky", "trap_signals",
        "breakout_radar", "fast_movers_missed_moves", "top_movers",
        "missed_moves_analysis",
    )
    authoritative_buckets = {
        name: [final_gate(item) for item in (scan_payload.get(name) or []) if isinstance(item, dict)]
        for name in authoritative_bucket_names
        if name in scan_payload
    }

    enriched_list = [final_gate(item) for item in enriched_map.values()]

    hot_picks = sorted(
        [
            signal for signal in enriched_list
            if signal.get("smartClassification") == "hot_pick"
            and not signal.get("marketGateBlocked")
            and not signal.get("tradePlanGateBlocked")
            and not signal.get("entry_plan_blocked")
        ],
        key=lambda signal: signal.get("smartScore", 0), reverse=True,
    )
    watchlist = sorted(
        [
            signal for signal in enriched_list
            if signal.get("smartClassification") == "watchlist" and not signal.get("marketGateBlocked")
        ],
        key=lambda signal: signal.get("smartScore", 0), reverse=True,
    )
    base_radar = sorted(
        [
            signal for signal in enriched_list
            if signal.get("smartClassification") == "base_formation_radar"
            and not signal.get("marketGateBlocked")
        ],
        key=lambda signal: signal.get("baseQualityScore") or 0, reverse=True,
    )
    momentum_radar = [
        signal for signal in enriched_list
        if signal.get("smartClassification") == "momentum_radar" and not signal.get("marketGateBlocked")
    ]
    blocked_buys = [signal for signal in enriched_list if signal.get("marketGateBlocked")]

    # all_entry_levels is the single post-gate arm list used by EntryMonitor.
    armed_entry_levels: List[Dict] = []
    live_gate_blocks: List[Dict] = []
    blocked_actions = {
        "AVOID", "EXIT", "NO_TRADE", "WAIT_FOR_PULLBACK",
        "AVOID_LATE_ENTRY", "AVOID_CHASE", "PROFIT_BOOKING_RISK",
        "WAIT_FOR_BETTER_ENTRY",
    }
    for raw_level in scan_payload.get("all_entry_levels", []) or []:
        if not isinstance(raw_level, dict):
            continue
        level = final_gate(raw_level)
        symbol = str(level.get("symbol") or "").upper()
        effective_action = str(
            level.get("effectiveAction") or level.get("action") or "WATCH"
        ).upper()
        blocked_reason = level.get("blockedBuyReason")
        if (
            level.get("marketGateBlocked")
            or level.get("tradePlanGateBlocked")
            or level.get("entry_plan_blocked")
            or blocked_reason
            or effective_action in blocked_actions
        ):
            live_gate_blocks.append(
                {
                    "symbol": symbol,
                    "reason": (
                        blocked_reason
                        or level.get("entry_plan_blocked_reason")
                        or f"Final action is {effective_action}."
                    ),
                }
            )
            continue
        armed_entry_levels.append(level)

    return {
        **scan_payload,
        **authoritative_buckets,
        "marketContext": market_context,
        "hotPicks": hot_picks[:12],
        "smartWatchlist": watchlist[:20],
        "baseFormationRadar": base_radar[:15],
        "momentumRadar": momentum_radar[:10],
        "blockedBuys": blocked_buys[:10],
        "all_entry_levels": armed_entry_levels,
        "liveEntryGate": {
            "armedCount": len(armed_entry_levels),
            "blockedCount": len(live_gate_blocks),
            "blocked": live_gate_blocks[:100],
            "authoritativeSource": "post_smart_gate_all_entry_levels",
        },
        "smartDebug": {
            "totalEnriched": len(enriched_list),
            "blockedByMarketGate": len(blocked_buys),
            "baseCandidatesFound": base_candidates,
            "deliveryUnknownCount": delivery_unknown,
            "sectorUnknownCount": sector_unknown,
            "mlSwingUnavailableCount": swing_unavailable,
            "marketMood": market_context.get("marketMood"),
            "weights": {"chart": W_CHART, "base": W_BASE, "volume": W_VOLUME, "market": W_MARKET, "ml": W_ML},
        },
        "summary": {
            **scan_payload.get("summary", {}),
            "hot_picks_count": len(hot_picks),
            "base_formation_radar_count": len(base_radar),
            "blocked_buy_count": len(blocked_buys),
            "market_mood": market_context.get("marketMood", "unknown"),
        },
    }


# ── Score helpers ─────────────────────────────────────────────────────────────

def _volume_score(signal: Dict, delivery: Dict) -> float:
    base = min(float(signal.get("relative_volume") or 1) * 25, 60)
    if delivery.get("deliverySignal") == "accumulation":
        base = min(base + 25, 100)
    elif delivery.get("deliverySignal") == "distribution_risk":
        base = max(base - 20, 0)
    return base


def _is_hot_pick_candidate(signal: Dict, smart_score: int, buy_blocked: bool) -> bool:
    if (
        buy_blocked
        or smart_score < HOT_PICK_MIN
        or signal.get("entry_plan_blocked")
        or signal.get("tradePlanGateBlocked")
    ):
        return False
    if signal.get("direction") != "bullish":
        return False
    if (
        signal.get("attention_only")
        or signal.get("chase_risk")
        or signal.get("overextended_fresh_entry")
        or signal.get("next_day_profit_booking_risk")
        or (signal.get("pattern_late_entry_risk") and not signal.get("is_momentum_continuation"))
        or not signal.get("allow_buy_call", True)
    ):
        return False

    labels = {str(label).upper() for label in signal.get("trade_labels", [])}
    setup_stage = str(signal.get("setup_stage") or "").upper()
    action = str(signal.get("action") or signal.get("recommended_action") or "").upper()
    if labels.intersection({"CHASE_RISK", "AVOID_LATE_ENTRY", "PROFIT_BOOKING_RISK", "WAIT_FOR_PULLBACK"}):
        return False
    if setup_stage in {"CHASE_RISK", "AVOID_LATE_ENTRY", "PROFIT_BOOKING_RISK"}:
        return False
    if action in {"WAIT_FOR_PULLBACK", "AVOID", "EXIT", "SELL"}:
        return False

    risk_reward = float(signal.get("risk_reward") or 0)
    return not risk_reward or risk_reward >= 1.2


def _market_score(market_context: Dict, sector: Dict) -> float:
    mood = market_context.get("marketMood", "unknown")
    base = {"bullish": 80, "neutral": 55, "bearish": 20, "unknown": 50}.get(mood, 50)
    rs = sector.get("relativeStrengthScore")
    if rs is not None:
        base = min(base + float(rs) * 8, 100)
        
    # Apply Global Sector Top 3 / Bottom 3 Bonus
    global_category = sector.get("globalSectorCategory", "neutral")
    if global_category == "top_3":
        base = min(base + 20, 100)
    elif global_category == "bottom_3":
        base = max(base - 20, 0)
        
    return max(base, 0)


def _ml_score(signal: Dict, swing: Dict) -> float:
    if swing.get("mlSwing5dModelScoreUp") is not None:
        return round(float(swing["mlSwing5dModelScoreUp"]), 1)
    if swing.get("mlSwing5dProbability") is not None:
        return round(float(swing["mlSwing5dProbability"]) * 100, 1)
    if signal.get("probability_available") and signal.get("probability") is not None:
        return round(float(signal["probability"]) * 100, 1)
    return 50.0
