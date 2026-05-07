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
from app.services.sector_strength import compute_sector_strength
from app.services.swing_ml import compute_swing_5d

logger = logging.getLogger(__name__)

# ── Score weights ─────────────────────────────────────────────────────────────
W_CHART = float(os.getenv("SMART_W_CHART", "0.25"))
W_BASE = float(os.getenv("SMART_W_BASE", "0.20"))
W_VOLUME = float(os.getenv("SMART_W_VOLUME", "0.20"))
W_MARKET = float(os.getenv("SMART_W_MARKET", "0.20"))
W_ML = float(os.getenv("SMART_W_ML", "0.15"))

# ── Classification thresholds ─────────────────────────────────────────────────
HOT_PICK_MIN = int(os.getenv("HOT_PICK_MIN_SCORE", "75"))
WATCHLIST_MIN = int(os.getenv("WATCHLIST_MIN_SCORE", "60"))
BASE_RADAR_MIN_BASE = int(os.getenv("BASE_RADAR_MIN_BASE_SCORE", "60"))


def enrich_signal(
    signal: Dict,
    frame: pd.DataFrame,
    market_context: Dict,
    all_signals: List[Dict],
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
    sector = compute_sector_strength(
        symbol,
        signal.get("return_20d"),
        all_signals,
    )

    # ── Final smart score ─────────────────────────────────────────────────────
    chart_score = float(signal.get("move_quality") or signal.get("quality_score") or 50)
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
    elif abs(float(signal.get("change_pct") or 0)) >= 3 or float(signal.get("relative_volume") or 1) >= 1.8:
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
    all_results: List[Dict] = (
        scan_payload.get("top_opportunities", [])
        + scan_payload.get("breakout_candidates", [])
        + scan_payload.get("candidates", [])
        + scan_payload.get("unusual_volume", [])
    )

    # ── A: Market breadth (once for all) ─────────────────────────────────────
    market_context = compute_market_breadth(benchmark_frame, all_results)
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
        symbol = signal.get("symbol", "")
        frame = daily_frames.get(symbol)
        if frame is None or frame.empty:
            continue

        enriched = enrich_signal(
            signal,
            frame,
            market_context,
            all_results,
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
    enriched_list = list(enriched_map.values())

    hot_picks = sorted(
        [s for s in enriched_list if s.get("smartClassification") == "hot_pick"],
        key=lambda s: s.get("smartScore", 0), reverse=True,
    )
    watchlist = sorted(
        [s for s in enriched_list if s.get("smartClassification") == "watchlist"],
        key=lambda s: s.get("smartScore", 0), reverse=True,
    )
    base_radar = sorted(
        [s for s in enriched_list if s.get("smartClassification") == "base_formation_radar"],
        key=lambda s: s.get("baseQualityScore") or 0, reverse=True,
    )
    momentum_radar = [s for s in enriched_list if s.get("smartClassification") == "momentum_radar"]
    blocked_buys = [s for s in enriched_list if s.get("smartClassification") == "blocked_buy"]

    return {
        **scan_payload,
        "marketContext": market_context,
        "hotPicks": hot_picks[:12],
        "smartWatchlist": watchlist[:20],
        "baseFormationRadar": base_radar[:15],
        "momentumRadar": momentum_radar[:10],
        "blockedBuys": blocked_buys[:10],
        "smartDebug": {
            "totalEnriched": len(enriched_list),
            "blockedByMarketGate": blocked_count,
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
    if buy_blocked or smart_score < HOT_PICK_MIN:
        return False
    if signal.get("direction") != "bullish":
        return False
    if (
        signal.get("attention_only")
        or signal.get("chase_risk")
        or signal.get("overextended_fresh_entry")
        or signal.get("next_day_profit_booking_risk")
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
    return max(base, 0)


def _ml_score(signal: Dict, swing: Dict) -> float:
    prob = swing.get("mlSwing5dProbability") or float(signal.get("probability") or 0.5)
    return round(float(prob) * 100, 1)
