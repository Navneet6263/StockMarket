from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any


DISCLAIMER = "This is not financial advice. Use this only for research and paper trading."


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_price(value: Any) -> str:
    price = _safe_float(value)
    return f"INR {price:.2f}" if price else "-"


def _fmt_zone(low: Any, high: Any) -> str:
    low_value = _safe_float(low)
    high_value = _safe_float(high)
    if low_value and high_value:
        return f"INR {min(low_value, high_value):.2f} - {max(low_value, high_value):.2f}"
    if low_value or high_value:
        return _fmt_price(low_value or high_value)
    return "-"


def _entry_zone(signal: dict[str, Any]) -> str:
    zone = signal.get("entry_zone") or {}
    low = _safe_float(zone.get("low"))
    high = _safe_float(zone.get("high"))
    trigger = _safe_float(signal.get("safe_entry_price") or signal.get("entry_trigger") or signal.get("current_price"))
    if low and high:
        return _fmt_zone(low, high)
    if trigger:
        return f"Watch near INR {trigger:.2f}"
    return "Entry trigger unavailable"


def _setup_type(signal: dict[str, Any]) -> str:
    if signal.get("is_momentum_continuation"):
        return "momentum continuation"
    if signal.get("is_pre_breakout"):
        return "watch for breakout"
    tags = set(signal.get("tags") or [])
    labels = set(signal.get("pattern_labels") or [])
    if "breakdown" in tags or signal.get("direction") == "bearish":
        return "breakdown"
    if "breakout" in tags or signal.get("signal_stage") in {"CONFIRMED_BREAKOUT", "ALERT_ABOVE_LEVEL"}:
        return "breakout"
    if "support_bounce" in labels:
        return "pullback"
    if "distribution" in tags:
        return "reversal"
    if "tight_consolidation" in labels or "base_building" in tags:
        return "base formation"
    return "momentum candidate" if signal.get("direction") != "neutral" else "watch only"


def _catalyst(signal: dict[str, Any]) -> dict[str, Any]:
    raw = signal.get("catalyst_summary") or {}
    sentiment = raw.get("sentiment")
    if sentiment not in {"positive", "negative", "neutral"}:
        sentiment = "unknown"
    headlines = raw.get("headlines") or []
    summary = raw.get("summary") or ""
    if not summary or summary == "Catalyst scan not loaded for this request.":
        return {
            "catalystStatus": "unknown",
            "catalystReason": "No fresh catalyst found",
            "catalystSource": None,
            "catalystTime": None,
        }
    return {
        "catalystStatus": sentiment,
        "catalystReason": summary,
        "catalystSource": "news_feed" if headlines or raw.get("catalysts") else None,
        "catalystTime": raw.get("published_at") or raw.get("time"),
    }


def _chart_features(signal: dict[str, Any]) -> dict[str, Any]:
    features = signal.get("chart_features") or {}
    tags = set(signal.get("tags") or [])
    labels = set(signal.get("pattern_labels") or [])
    relative_volume = _safe_float(signal.get("relative_volume"), 1.0)
    intraday_volume = _safe_float(signal.get("intraday_volume_ratio"), 1.0)
    rsi = _safe_float(signal.get("rsi"), 50.0)
    stop = _safe_float(signal.get("stop_loss") or signal.get("invalidation") or signal.get("invalidation_level"))
    price = _safe_float(signal.get("current_price"))
    distance_from_stop = features.get("distance_from_stop_pct")
    if distance_from_stop is None and price and stop:
        distance_from_stop = round((abs(price - stop) / price) * 100, 2)
    return {
        "nearBreakout": bool(features.get("near_breakout")) or bool(signal.get("near_resistance")) or "pre_breakout" in tags,
        "breakoutConfirmed": bool(features.get("breakout_confirmed")) or "breakout" in tags,
        "pullbackToSupport": bool(features.get("pullback_to_support")) or "support_bounce" in labels,
        "volumeSpike": bool(features.get("volume_spike")) or relative_volume >= 1.5 or intraday_volume >= 1.4,
        "rsiMomentum": rsi,
        "movingAverageTrend": features.get("moving_average_trend") or ("uptrend" if "trend_up" in tags else "downtrend" if "trend_down" in tags else "range"),
        "riskReward": _safe_float(signal.get("risk_reward")),
        "distanceFromStopPct": distance_from_stop,
        "overextended": bool(features.get("overextended")) or bool(signal.get("overextended_fresh_entry")),
        "tightConsolidationPct": features.get("tight_consolidation_pct"),
        "distanceToResistancePct": features.get("distance_to_resistance_pct"),
        "distanceToSupportPct": features.get("distance_to_support_pct"),
        "higherLows": bool(features.get("higher_lows")) or "higher_lows" in labels,
        "volumeDryup": bool(features.get("volume_dryup")),
        "bbSqueeze": bool(features.get("bb_squeeze")) or "bollinger_squeeze" in labels,
        "atrExpansion": features.get("atr_expansion"),
        "bbWidthRatio": features.get("bb_width_ratio"),
    }


def score_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """100-point practical discovery score derived from available scanner evidence."""
    tags = set(signal.get("tags") or [])
    labels = set(signal.get("pattern_labels") or [])
    direction = signal.get("direction") or "neutral"
    relative_volume = _safe_float(signal.get("relative_volume"), 1.0)
    intraday_volume = _safe_float(signal.get("intraday_volume_ratio"), 1.0)
    volume = _safe_float(signal.get("volume"))
    rsi = _safe_float(signal.get("rsi"), 50.0)
    risk_reward = _safe_float(signal.get("risk_reward"))
    invalidation = _safe_float(signal.get("invalidation") or signal.get("stop_loss") or signal.get("invalidation_level"))
    price = _safe_float(signal.get("current_price"))
    confidence = _safe_float(signal.get("confidence"))
    move_quality = _safe_float(signal.get("move_quality"))
    relative_strength = _safe_float(signal.get("benchmark_relative_strength"))
    catalyst = _catalyst(signal)
    chart = _chart_features(signal)

    setup = 0.0
    if chart["breakoutConfirmed"] or "breakdown" in tags:
        setup += 13
    if chart["nearBreakout"] or signal.get("is_pre_breakout"):
        setup += 7
    if chart["pullbackToSupport"]:
        setup += 5
    if "tight_consolidation" in labels or "ema_hold" in labels:
        setup += 3
    if invalidation:
        setup += 2

    volume_score = 0.0
    if relative_volume >= 2.0 or intraday_volume >= 1.8:
        volume_score += 14
    elif relative_volume >= 1.4 or intraday_volume >= 1.25:
        volume_score += 11
    elif relative_volume >= 0.9:
        volume_score += 7
    elif volume:
        volume_score += 4
    if volume >= 100000:
        volume_score += 4
    if "accumulation" in tags or chart["volumeDryup"]:
        volume_score += 2

    trend = 0.0
    if direction != "neutral":
        trend += min(9, max(0, (move_quality - 40) / 6))
    if relative_strength > 0:
        trend += min(6, relative_strength)
    if "trend_up" in tags or "trend_down" in tags:
        trend += 4
    if chart["higherLows"]:
        trend += 2

    momentum = 0.0
    if direction == "bullish" and 48 <= rsi <= 72:
        momentum += 8
    elif direction == "bearish" and 28 <= rsi <= 52:
        momentum += 8
    elif 42 <= rsi <= 65:
        momentum += 5
    if "momentum" in tags:
        momentum += 4
    if abs(_safe_float(signal.get("return_5d"))) <= 8:
        momentum += 3

    catalyst_score = 8.0
    if catalyst["catalystStatus"] == "positive" and direction == "bullish":
        catalyst_score = 18.0
    elif catalyst["catalystStatus"] == "negative" and direction == "bearish":
        catalyst_score = 18.0
    elif catalyst["catalystStatus"] in {"positive", "negative", "neutral"}:
        catalyst_score = 12.0
    if (signal.get("discovered_by") or []) and catalyst_score < 14:
        catalyst_score += 2

    breakdown = {
        "chartSetup": round(min(25, setup), 1),
        "volumeLiquidity": round(min(20, volume_score), 1),
        "trendStrength": round(min(20, trend), 1),
        "momentumIndicators": round(min(15, momentum), 1),
        "newsSectorCatalyst": round(min(20, catalyst_score), 1),
    }
    raw_score = sum(breakdown.values())
    score = max(0, min(100, raw_score))
    if confidence:
        score = round((score * 0.72) + (min(100, confidence) * 0.28), 1)

    rejection_reasons: list[str] = []
    soft_reasons: list[str] = []
    if not price:
        rejection_reasons.append("missing_price")
    if direction == "neutral":
        rejection_reasons.append("unclear_direction")
    if volume and volume < 30000:
        soft_reasons.append("low_volume")
    if not invalidation:
        soft_reasons.append("missing_stoploss")
    if risk_reward and risk_reward < 1.0:
        soft_reasons.append("weak_risk_reward")
    if chart["overextended"] and not signal.get("is_momentum_continuation"):
        soft_reasons.append("overextended")
    if score < 50:
        rejection_reasons.append("score_below_50")

    clean_risk = bool(invalidation and (not risk_reward or risk_reward >= 1.0) and not chart["overextended"])
    return {
        "score": score,
        "breakdown": breakdown,
        "cleanRiskSetup": clean_risk,
        "rejected": bool(rejection_reasons),
        "rejectionReasons": rejection_reasons,
        "softReasons": soft_reasons,
        "chart": chart,
        "catalyst": catalyst,
    }


def _prediction_wording(signal: dict[str, Any], score: float) -> str:
    direction = signal.get("direction", "neutral")
    trigger = signal.get("alert_above_price") or signal.get("alert_price") or signal.get("entry_trigger") or signal.get("resistance")
    invalidation = signal.get("invalidation") or signal.get("stop_loss") or signal.get("support")
    if score >= 75:
        prefix = "High probability setup"
    elif score >= 60:
        prefix = "Watchlist setup"
    else:
        prefix = "Momentum candidate"
    action = "May move if breakout sustains" if direction == "bullish" else "May move lower if weakness sustains" if direction == "bearish" else "Needs confirmation"
    trigger_text = f" Watch above {_fmt_price(trigger)}." if trigger else ""
    invalid_text = f" Invalid below {_fmt_price(invalidation)}." if invalidation else " Invalidation level unavailable."
    return f"{prefix}. {action}.{trigger_text}{invalid_text}"


def map_pick(signal: dict[str, Any], last_updated: str) -> dict[str, Any]:
    scored = score_signal(signal)
    catalyst = scored["catalyst"]
    chart = scored["chart"]
    score = scored["score"]
    reason_parts = [
        _prediction_wording(signal, score),
        signal.get("pattern_reason") or signal.get("pre_breakout_reason") or signal.get("continuation_reason") or signal.get("signal_summary"),
    ]
    if catalyst["catalystReason"] != "No fresh catalyst found":
        reason_parts.append(catalyst["catalystReason"])
    reason = " ".join(part for part in reason_parts if part) or "Scanner found aligned technical evidence."
    risk_items = signal.get("risk_factors") or []
    target = signal.get("new_target") or signal.get("target_1") or signal.get("target_price")
    stop = signal.get("stop_loss") or signal.get("invalidation") or signal.get("invalidation_level")
    return {
        "symbol": signal.get("symbol"),
        "currentPrice": _fmt_price(signal.get("current_price")),
        "setupType": _setup_type(signal),
        "direction": signal.get("direction", "neutral"),
        "confidence": score,
        "score": score,
        "scoreBreakdown": scored["breakdown"],
        "entryZone": _entry_zone(signal),
        "entryTrigger": _fmt_price(signal.get("safe_entry_price") or signal.get("entry_trigger") or signal.get("alert_above_price") or signal.get("resistance")),
        "target": _fmt_price(target),
        "targetZone": _fmt_price(target),
        "stoploss": _fmt_price(stop),
        "invalidation": _fmt_price(signal.get("invalidation") or stop),
        "reason": reason[:520],
        "whyItCanMove": reason[:520],
        "risk": risk_items[0] if risk_items else f"{signal.get('risk_level', 'medium').title()} risk. Respect the invalidation level.",
        "riskLevel": signal.get("risk_level", "medium"),
        "timeHorizon": signal.get("timeframe_label") or "swing",
        "lastUpdated": last_updated,
        **catalyst,
        "chart": chart,
        "cleanRiskSetup": scored["cleanRiskSetup"],
        "raw": signal,
        "rejectionReasons": scored["rejectionReasons"],
        "softReasons": scored["softReasons"],
    }


def _base_formation(signal: dict[str, Any], last_updated: str) -> dict[str, Any] | None:
    chart = _chart_features(signal)
    price = _safe_float(signal.get("current_price"))
    support = _safe_float(signal.get("support") or signal.get("invalidation"))
    resistance = _safe_float(signal.get("resistance") or signal.get("breakout_level"))
    if not price or not support or not resistance or support >= resistance:
        return None

    tight = _safe_float(chart.get("tightConsolidationPct"), 99.0)
    atr_expansion = _safe_float(chart.get("atrExpansion"), 1.0)
    bb_width = _safe_float(chart.get("bbWidthRatio"), 1.0)
    distance_to_resistance = _safe_float(chart.get("distanceToResistancePct"), 99.0)
    distance_to_support = _safe_float(chart.get("distanceToSupportPct"), 99.0)
    relative_volume = _safe_float(signal.get("relative_volume"), 1.0)
    intraday_volume = _safe_float(signal.get("intraday_volume_ratio"), 1.0)
    tags = set(signal.get("tags") or [])
    labels = set(signal.get("pattern_labels") or [])

    range_pct = ((resistance - support) / price) * 100
    range_score = 20 if tight <= 4 or range_pct <= 6 else 14 if tight <= 7 or range_pct <= 9 else 8
    support_score = 15 if chart["pullbackToSupport"] or distance_to_support <= 2.0 else 10 if support < price else 5
    resistance_score = 15 if distance_to_resistance <= 2.0 or chart["nearBreakout"] else 10 if distance_to_resistance <= 4.0 else 5
    volume_score = 0
    if chart["volumeDryup"]:
        volume_score += 10
    if relative_volume >= 1.3 or intraday_volume >= 1.25 or "accumulation" in tags:
        volume_score += 10
    elif relative_volume <= 1.0:
        volume_score += 5
    trend_score = 0
    if chart["higherLows"] or "higher_lows" in labels:
        trend_score += 8
    if "trend_up" in tags or "ema_hold" in labels:
        trend_score += 7
    squeeze_score = 15 if chart["bbSqueeze"] or bb_width <= 0.85 or atr_expansion <= 0.9 else 8 if atr_expansion <= 1.05 else 0
    score = round(min(100, range_score + support_score + resistance_score + min(20, volume_score) + min(15, trend_score) + squeeze_score), 1)
    if score < 50:
        return None
    if signal.get("symbol") is None:
        return None
    stage = "strong base" if score >= 75 else "building base" if score >= 60 else "loose consolidation"
    pattern = "higher lows" if chart["higherLows"] else "tight range" if tight <= 6 or range_pct <= 8 else "flat base"
    volume_behavior = "drying up with spike observed" if chart["volumeDryup"] and (relative_volume >= 1.3 or intraday_volume >= 1.25) else "drying up" if chart["volumeDryup"] else "spike observed" if relative_volume >= 1.3 or intraday_volume >= 1.25 else "normal"
    return {
        "symbol": signal.get("symbol"),
        "stage": stage,
        "accumulationScore": score,
        "range": _fmt_zone(support, resistance),
        "keyResistance": _fmt_price(resistance),
        "supportZone": _fmt_price(support),
        "breakoutTrigger": f"above {_fmt_price(resistance)} with volume",
        "invalidation": f"below {_fmt_price(support)}",
        "volumeBehavior": volume_behavior,
        "pattern": pattern,
        "whyInteresting": "Early accumulation - wait for confirmation. Base is forming through consolidation, support respect, and improving structure.",
        "risk": signal.get("risk_level", "medium"),
        "timeHorizon": "positional" if score >= 75 else "swing",
        "lastUpdated": last_updated,
        "debug": {
            "avgRangePct": round(range_pct, 2),
            "resistanceTouches": 1 if distance_to_resistance <= 2 else 0,
            "supportTouches": 1 if distance_to_support <= 2 else 0,
            "volatilityCompressionPct": round(max(0.0, (1 - min(atr_expansion, 1.5)) * 100), 2),
            "tightConsolidationPct": None if tight == 99.0 else tight,
        },
    }


def _all_signals(scan: dict[str, Any]) -> list[dict[str, Any]]:
    pools = [
        *(scan.get("top_opportunities") or []),
        *(scan.get("breakout_candidates") or []),
        *(scan.get("bearish_risks") or []),
        *(scan.get("unusual_volume") or []),
        *(scan.get("candidates") or []),
        *(scan.get("pre_breakout_setups") or []),
        *(scan.get("momentum_continuation") or []),
        *(scan.get("retest_entry") or []),
        *(scan.get("avoid_late_entry") or []),
    ]
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for signal in pools:
        symbol = signal.get("symbol")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        unique.append(signal)
    return unique


def build_hot_picks_response(scan: dict[str, Any], tracker_dashboard: dict[str, Any] | None = None) -> dict[str, Any]:
    last_updated = scan.get("generated_at") or datetime.now(timezone.utc).isoformat()
    warnings = [DISCLAIMER]
    if scan.get("warning"):
        warnings.append(scan["warning"])
    if scan.get("cache_status") in {"stale", "refresh_failed_stale", "tracked_fallback", "refresh_in_progress"}:
        warnings.append("Showing cached or persisted scanner data while live scan refreshes.")

    mapped = [map_pick(signal, last_updated) for signal in _all_signals(scan)]
    mapped.sort(key=lambda item: item["score"], reverse=True)

    rejected = [item for item in mapped if item["rejectionReasons"]]
    eligible = [item for item in mapped if not item["rejectionReasons"]]
    hot = [item for item in eligible if item["score"] >= 75 and item["cleanRiskSetup"]][:8]
    hot_symbols = {item["symbol"] for item in hot}
    watchlist = [item for item in eligible if 60 <= item["score"] < 75 and item["symbol"] not in hot_symbols][:16]
    used = hot_symbols | {item["symbol"] for item in watchlist}
    momentum = [
        item for item in eligible
        if item["symbol"] not in used
        and (
            50 <= item["score"] < 60
            or item["chart"]["volumeSpike"]
            or item["setupType"] in {"momentum continuation", "momentum candidate", "watch for breakout"}
        )
    ][:16]
    used.update(item["symbol"] for item in momentum)
    catalyst_radar = [
        item for item in eligible
        if item["catalystStatus"] in {"positive", "negative", "neutral"}
        and item["catalystReason"] != "No fresh catalyst found"
    ][:12]

    base_items = []
    excluded_for_base = used | {item["symbol"] for item in catalyst_radar}
    for signal in _all_signals(scan):
        if signal.get("symbol") in excluded_for_base:
            continue
        base = _base_formation(signal, last_updated)
        if base:
            base_items.append(base)
    base_items.sort(key=lambda item: item["accumulationScore"], reverse=True)

    reason_counts = Counter()
    for item in mapped:
        for reason in item["rejectionReasons"] or item["softReasons"]:
            reason_counts[reason] += 1
    near_miss = [
        {
            "symbol": item["symbol"],
            "score": item["score"],
            "setupType": item["setupType"],
            "softReasons": item["softReasons"],
            "rejectionReasons": item["rejectionReasons"],
            "why": item["whyItCanMove"],
        }
        for item in mapped
        if item["score"] >= 45 and item["symbol"] not in {pick["symbol"] for pick in hot + watchlist + momentum}
    ][:10]

    summary = scan.get("summary") or {}
    breadth = scan.get("market_breadth") or {}
    journal = (tracker_dashboard or {}).get("summary") or {}
    resolved = int(journal.get("passed_calls", 0) or 0) + int(journal.get("failed_calls", 0) or 0)
    performance = {
        "label": "Paper trade performance",
        "last30Calls": (tracker_dashboard or {}).get("watchlists", {}).get("todays_top_10_scanner_calls", [])[:30],
        "activeCalls": journal.get("active_calls", 0),
        "winRate": journal.get("win_rate") if resolved else None,
        "averageReturn": journal.get("average_return") if resolved else None,
        "maxLoss": (journal.get("worst_call") or {}).get("result_pct") if resolved else None,
        "message": None if resolved else "Not enough data yet",
    }
    raw_mood = (summary.get("market_mood") or "sideways").replace("defensive", "bearish")
    market_mood = raw_mood if raw_mood in {"bullish", "bearish", "sideways"} else "sideways"
    debug = {
        "totalScanned": scan.get("market_discovery", {}).get("total_scanned") or summary.get("total_scanned_universe") or scan.get("universe_size", 0),
        "sourceCandidates": len(mapped),
        "candidatesByTier": {
            "hotPicks": len(hot),
            "watchlist": len(watchlist),
            "momentumRadar": len(momentum),
            "catalystRadar": len(catalyst_radar),
            "baseFormation": len(base_items),
            "rejected": len(rejected),
        },
        "rejectedCountsByReason": dict(reason_counts),
        "top10NearMissCandidates": near_miss,
    }
    return {
        "marketMood": market_mood,
        "lastUpdated": last_updated,
        "cacheStatus": scan.get("cache_status", "fresh"),
        "isStale": scan.get("cache_status") in {"stale", "refresh_failed_stale", "tracked_fallback", "refresh_in_progress"},
        "summary": {
            "totalScanned": debug["totalScanned"],
            "highConfidence": len([item for item in eligible if item["score"] >= 75]),
            "bullish": breadth.get("bullish_setups", 0),
            "bearish": breadth.get("bearish_setups", 0),
            "breakouts": summary.get("breakout_count", 0),
            "watchlist": len(watchlist),
            "momentumRadar": len(momentum),
            "baseFormation": len(base_items),
        },
        "hotPicks": hot,
        "watchlist": watchlist,
        "momentumRadar": momentum,
        "catalystRadar": catalyst_radar,
        "baseFormationRadar": base_items[:12],
        "rejectionSummary": {
            "totalRejected": len(rejected),
            "countsByReason": dict(reason_counts),
            "nearMissCandidates": near_miss,
        },
        "debug": debug,
        "marketExplanation": scan.get("macro_context", {}).get("summary") or "Market explanation unavailable from current data.",
        "sectorStrength": scan.get("market_discovery", {}).get("bucket_counts", {}),
        "performance": performance,
        "warnings": list(dict.fromkeys(warnings)),
    }
