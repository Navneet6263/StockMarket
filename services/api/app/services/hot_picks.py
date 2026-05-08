from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.services.strict_options import build_strict_options_response


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


def _display_label(value: Any, fallback: str = "watch") -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "unavailable", "none", "nan"}:
        return fallback
    return text.replace("_", " ")


def _direction(signal: dict[str, Any]) -> str:
    direction = str(signal.get("direction") or "neutral").lower()
    if direction in {"up", "bullish"}:
        return "bullish"
    if direction in {"down", "bearish"}:
        return "bearish"
    return "neutral"


def _signal_trigger(signal: dict[str, Any]) -> float:
    return _safe_float(
        signal.get("safe_entry_price")
        or signal.get("entry_trigger")
        or signal.get("trigger_price")
        or signal.get("alert_above_price")
        or signal.get("alert_price")
        or signal.get("breakoutTrigger")
        or signal.get("breakout_level")
        or signal.get("resistance_level")
        or signal.get("resistance")
    )


def _signal_fail_level(signal: dict[str, Any]) -> float:
    return _safe_float(
        signal.get("stop_loss")
        or signal.get("invalidation")
        or signal.get("invalidation_level")
        or signal.get("invalidationLevel")
        or signal.get("support")
        or signal.get("support_level")
    )


def _entry_context(signal: dict[str, Any], *, trigger: float | None = None) -> dict[str, Any]:
    direction = _direction(signal)
    price = _safe_float(signal.get("current_price") or signal.get("price"))
    trigger = _safe_float(trigger) or _signal_trigger(signal)
    if not price or not trigger or direction not in {"bullish", "bearish"}:
        return {
            "price": price,
            "trigger": trigger,
            "distance_pct": None,
            "near": False,
            "active": False,
            "missed": False,
            "entry_label": "Entry",
            "retest_zone": "-",
        }

    if direction == "bullish":
        distance_pct = ((price - trigger) / trigger) * 100
    else:
        distance_pct = ((trigger - price) / trigger) * 100

    active = 0 <= distance_pct <= 0.8
    near = -1.5 <= distance_pct <= 0.8
    missed = distance_pct > 0.8

    zone = signal.get("re_entry_zone") or {}
    low = _safe_float(zone.get("low"))
    high = _safe_float(zone.get("high"))
    if not low or not high:
        low = trigger * 0.995
        high = trigger * 1.006

    return {
        "price": price,
        "trigger": trigger,
        "distance_pct": round(distance_pct, 2),
        "near": near,
        "active": active,
        "missed": missed,
        "entry_label": "Retest" if missed else "Entry",
        "retest_zone": _fmt_zone(low, high),
    }


def _action_value(signal: dict[str, Any]) -> str:
    return str(
        signal.get("effectiveAction")
        or signal.get("actionOverride")
        or signal.get("recommended_action")
        or signal.get("action")
        or ""
    ).upper()


def _has_late_entry_risk(signal: dict[str, Any]) -> bool:
    labels = {str(label).upper() for label in signal.get("trade_labels", [])}
    setup_stage = str(signal.get("setup_stage") or "").upper()
    action = _action_value(signal)
    entry_quality = str(signal.get("entry_quality") or "").lower()
    seller_pressure = str(signal.get("seller_pressure") or "").lower()
    profit_booking_risk = str(signal.get("profit_booking_risk") or "").lower()
    return bool(
        signal.get("chase_risk")
        or signal.get("overextended_fresh_entry")
        or signal.get("next_day_profit_booking_risk")
        or entry_quality in {"poor", "avoid"}
        or seller_pressure in {"high", "very_high"}
        or profit_booking_risk in {"high", "very_high"}
        or labels.intersection(
            {
                "CHASE_RISK",
                "AVOID_LATE_ENTRY",
                "PROFIT_BOOKING_RISK",
                "WAIT_FOR_PULLBACK",
                "PROFIT_BOOKING_ZONE",
                "SELLER_REJECTION",
                "CLIMACTIC_MOVE",
            }
        )
        or setup_stage in {"CHASE_RISK", "AVOID_LATE_ENTRY", "PROFIT_BOOKING_RISK", "PROFIT_BOOKING_ZONE"}
        or action in {"WAIT_FOR_PULLBACK", "AVOID", "AVOID_CHASE", "EXIT", "SELL"}
    )


def _is_watch_only(signal: dict[str, Any]) -> bool:
    action = _action_value(signal)
    return bool(
        signal.get("attention_only")
        or signal.get("is_pre_breakout")
        or action in {"WATCH", "ALERT", "ALERT_ABOVE_LEVEL", "WAIT_FOR_CONFIRMATION"}
    )


def _guidance(signal: dict[str, Any], *, trigger: float | None = None, fail: float | None = None) -> dict[str, str]:
    direction = _direction(signal)
    price = _safe_float(signal.get("current_price") or signal.get("price"))
    trigger = _safe_float(trigger) or _signal_trigger(signal)
    fail = _safe_float(fail) or _signal_fail_level(signal)
    action = _action_value(signal)
    entry = _entry_context(signal, trigger=trigger)
    distance_pct = entry["distance_pct"]

    trigger_text = _fmt_price(trigger)
    fail_text = _fmt_price(fail)

    if direction == "bearish":
        if trigger:
            return {
                "biasLabel": "Bearish pattern",
                "entryStatus": "Avoid fresh long",
                "tradeDecision": f"Bearish risk. Avoid fresh long; downside confirms below {trigger_text}.",
                "confirmationText": f"Bearish view is active only below {trigger_text}. Invalid above {fail_text}.",
            }
        return {
            "biasLabel": "Bearish pattern",
            "entryStatus": "Avoid fresh long",
            "tradeDecision": "Bearish risk, but confirmation level is not clean.",
            "confirmationText": "Wait for a clean breakdown level before taking action.",
        }

    if direction != "bullish":
        return {
            "biasLabel": "Neutral range",
            "entryStatus": "No trade",
            "tradeDecision": "No fresh entry. Direction is not confirmed yet.",
            "confirmationText": "Wait for price to break the range before deciding up or down.",
        }

    if entry["missed"]:
        distance_text = f"{distance_pct:.2f}%" if distance_pct is not None else "above trigger"
        return {
            "biasLabel": "Bullish trigger already crossed",
            "entryStatus": "Wait for retest",
            "tradeDecision": (
                f"Trigger was {trigger_text}, but price is already {distance_text} beyond it. "
                f"Do not treat the old trigger as a fresh entry; wait near {entry['retest_zone']} "
                "or for a fresh 5/15-min base."
            )[:260],
            "confirmationText": f"Fresh entry only after retest/hold near {entry['retest_zone']}. Risk below {fail_text}.",
        }

    if _has_late_entry_risk(signal):
        seller_pressure = _display_label(signal.get("seller_pressure"), "elevated")
        profit_risk = _display_label(signal.get("profit_booking_risk"), "elevated")
        reentry_plan = str(signal.get("reentry_plan") or "").strip()
        decision = (
            f"Late entry risk. Seller pressure is {seller_pressure} and "
            f"profit-booking risk is {profit_risk}; wait for pullback/retest."
        )
        if reentry_plan:
            decision = f"{decision} {reentry_plan}"
        return {
            "biasLabel": "Bullish late-entry risk",
            "entryStatus": "Wait for pullback",
            "tradeDecision": decision[:260],
            "confirmationText": f"Only reconsider on pullback/retest or a strong hold above {trigger_text}. Risk below {fail_text}.",
        }

    if entry["active"] or (action in {"BUY", "REENTRY_BUY"} and price and trigger and price >= trigger):
        return {
            "biasLabel": "Bullish trigger active",
            "entryStatus": "Active near trigger",
            "tradeDecision": f"Trigger active near {trigger_text}. Entry only if price sustains with volume; risk below {fail_text}.",
            "confirmationText": f"Breakout/re-entry is active above {trigger_text}. Failed below {fail_text}.",
        }

    if trigger:
        if distance_pct is not None and -1.0 <= distance_pct < 0:
            return {
                "biasLabel": "Bullish near trigger",
                "entryStatus": "Alert above trigger",
                "tradeDecision": f"Near trigger. No buy until price breaks above {trigger_text} with volume.",
                "confirmationText": f"Upside confirms above {trigger_text}. Failed below {fail_text}.",
            }
        if _is_watch_only(signal):
            return {
                "biasLabel": "Bullish setup forming",
                "entryStatus": "Watch only",
                "tradeDecision": f"Setup forming. Set alert above {trigger_text}; no entry before confirmation.",
                "confirmationText": f"Upside confirms above {trigger_text}. Failed below {fail_text}.",
            }
        return {
            "biasLabel": "Bullish setup forming",
            "entryStatus": "Watch only",
            "tradeDecision": f"No buy yet. Buy only above {trigger_text} with volume confirmation.",
            "confirmationText": f"Upside confirms above {trigger_text}. Failed below {fail_text}.",
        }

    return {
        "biasLabel": "Bullish watch",
        "entryStatus": "Wait",
        "tradeDecision": "Bullish evidence exists, but trigger is not clean yet.",
        "confirmationText": "Wait for scanner to provide a clean entry trigger and invalidation.",
    }


def _entry_zone(signal: dict[str, Any]) -> str:
    entry = _entry_context(signal)
    if entry["missed"]:
        return f"Retest {entry['retest_zone']}"
    zone = signal.get("entry_zone") or {}
    low = _safe_float(zone.get("low"))
    high = _safe_float(zone.get("high"))
    trigger = _signal_trigger(signal) or _safe_float(signal.get("current_price"))
    if low and high:
        return _fmt_zone(low, high)
    if trigger:
        return f"Watch near INR {trigger:.2f}"
    return "Entry trigger unavailable"


def _setup_type(signal: dict[str, Any]) -> str:
    if signal.get("setup_type"):
        return _display_label(signal.get("setup_type"), "momentum candidate")
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
    relative_volume = _safe_float(signal.get("relative_volume") or signal.get("volume_ratio"), 1.0)
    intraday_volume = _safe_float(signal.get("intraday_volume_ratio"), 1.0)
    rsi = _safe_float(signal.get("rsi"), 50.0)
    stop = _signal_fail_level(signal)
    price = _safe_float(signal.get("current_price") or signal.get("price"))
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
    direction = _direction(signal)
    relative_volume = _safe_float(signal.get("relative_volume") or signal.get("volume_ratio"), 1.0)
    intraday_volume = _safe_float(signal.get("intraday_volume_ratio"), 1.0)
    volume = _safe_float(signal.get("volume"))
    rsi = _safe_float(signal.get("rsi"), 50.0)
    risk_reward = _safe_float(signal.get("risk_reward"))
    invalidation = _signal_fail_level(signal)
    price = _safe_float(signal.get("current_price") or signal.get("price"))
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


def _is_fresh_hot_pick(item: dict[str, Any]) -> bool:
    raw = item.get("raw") or {}
    if item.get("direction") != "bullish":
        return False
    if item.get("rejectionReasons"):
        return False
    if _has_late_entry_risk(raw):
        return False
    if raw.get("attention_only") or raw.get("allow_buy_call") is False:
        return False

    risk_reward = _safe_float(item.get("risk_reward"))
    if risk_reward and risk_reward < 1.2:
        return False

    score = _safe_float(item.get("score"))
    if score < 72:
        return False

    trigger = _signal_trigger(raw)
    entry = _entry_context(raw, trigger=trigger)
    if entry["missed"]:
        return False
    entry_quality = str(raw.get("entry_quality") or item.get("entryQuality") or "").lower()
    action = _action_value(raw)
    ready_action = action in {"BUY", "REENTRY_BUY", "BUY_ONLY_ON_TRIGGER_HOLD", "BUY_ONLY_ON_RETEST_HOLD"}

    return bool(
        (ready_action and entry["near"])
        or (entry["active"] and item.get("cleanRiskSetup"))
        or (entry["near"] and item.get("cleanRiskSetup") and entry_quality in {"good", "watch", ""})
    )


def _prediction_wording(signal: dict[str, Any], score: float) -> str:
    direction = _direction(signal)
    trigger = _signal_trigger(signal)
    invalidation = _signal_fail_level(signal)
    entry = _entry_context(signal, trigger=trigger)
    if direction == "bullish" and entry["missed"]:
        prefix = "High score watch, trigger already crossed" if score >= 75 else "Watchlist setup, trigger already crossed"
    elif _has_late_entry_risk(signal):
        prefix = "High score watch, but late-entry risk" if score >= 75 else "Watchlist setup with late-entry risk"
    elif _is_watch_only(signal) and direction == "bullish":
        prefix = "Pre-breakout watch setup" if score >= 60 else "Early bullish watch setup"
    elif score >= 75:
        prefix = "High probability setup"
    elif score >= 60:
        prefix = "Watchlist setup"
    else:
        prefix = "Momentum candidate"

    if direction == "bullish" and entry["missed"]:
        action = f"Wait for retest near {entry['retest_zone']}"
    elif _has_late_entry_risk(signal):
        action = "Avoid chasing; wait for pullback or retest"
    elif _is_watch_only(signal) and direction == "bullish":
        action = "No buy until trigger confirms"
    else:
        action = "May move if breakout sustains" if direction == "bullish" else "May move lower if weakness sustains" if direction == "bearish" else "Needs confirmation"

    trigger_text = f" Watch above {_fmt_price(trigger)}." if trigger and direction != "bearish" else f" Watch below {_fmt_price(trigger)}." if trigger else ""
    invalid_text = f" Invalid near {_fmt_price(invalidation)}." if invalidation else " Invalidation level unavailable."
    return f"{prefix}. {action}.{trigger_text}{invalid_text}"


def map_pick(signal: dict[str, Any], last_updated: str) -> dict[str, Any]:
    scored = score_signal(signal)
    catalyst = scored["catalyst"]
    chart = scored["chart"]
    score = scored["score"]
    direction = _direction(signal)
    trigger = _signal_trigger(signal)
    stop = _signal_fail_level(signal)
    entry = _entry_context(signal, trigger=trigger)
    guidance = _guidance(signal, trigger=trigger, fail=stop)
    reason_parts = [
        _prediction_wording(signal, score),
        signal.get("pattern_reason") or signal.get("pre_breakout_reason") or signal.get("continuation_reason") or signal.get("signal_summary"),
    ]
    if catalyst["catalystReason"] != "No fresh catalyst found":
        reason_parts.append(catalyst["catalystReason"])
    reason = " ".join(part for part in reason_parts if part) or "Scanner found aligned technical evidence."
    risk_items = signal.get("risk_factors") or []
    target = signal.get("new_target") or signal.get("target_1") or signal.get("target_price")
    price_value = _safe_float(signal.get("current_price") or signal.get("price"))
    return {
        "symbol": signal.get("symbol"),
        "currentPrice": _fmt_price(signal.get("current_price") or signal.get("price")),
        "current_price": round(price_value, 2) if price_value else None,
        "price": round(price_value, 2) if price_value else None,
        "change_pct": round(_safe_float(signal.get("change_pct")), 2),
        "relative_volume": round(_safe_float(signal.get("relative_volume") or signal.get("volume_ratio"), 1.0), 2),
        "risk_reward": _safe_float(signal.get("risk_reward")),
        "setupType": _setup_type(signal),
        "chartPattern": _setup_type(signal),
        "patternLabels": signal.get("pattern_labels") or [],
        "patternReason": signal.get("pattern_reason") or signal.get("pre_breakout_reason") or signal.get("continuation_reason"),
        "direction": direction,
        "confidence": score,
        "score": score,
        "scoreBreakdown": scored["breakdown"],
        "entryZone": _entry_zone(signal),
        "entryTrigger": _fmt_price(trigger),
        "entryLabel": entry["entry_label"],
        "entryDistancePct": entry["distance_pct"],
        "entryMissed": entry["missed"],
        "retestZone": entry["retest_zone"],
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
        **guidance,
        "entryQuality": signal.get("entry_quality"),
        "entryTiming": signal.get("entry_timing"),
        "sellerPressure": signal.get("seller_pressure"),
        "sellerPressureScore": signal.get("seller_pressure_score"),
        "profitBookingRisk": signal.get("profit_booking_risk"),
        "bestAction": signal.get("best_action"),
        "reentryPlan": signal.get("reentry_plan"),
        "chart": chart,
        "cleanRiskSetup": scored["cleanRiskSetup"],
        "entry_quality": signal.get("entry_quality"),
        "entry_timing": signal.get("entry_timing"),
        "seller_pressure": signal.get("seller_pressure"),
        "seller_pressure_score": signal.get("seller_pressure_score"),
        "profit_booking_risk": signal.get("profit_booking_risk"),
        "best_action": signal.get("best_action"),
        "reentry_plan": signal.get("reentry_plan"),
        "allow_buy_call": signal.get("allow_buy_call"),
        "attention_only": signal.get("attention_only"),
        "action": signal.get("action"),
        "recommended_action": signal.get("recommended_action"),
        "raw": signal,
        "rejectionReasons": scored["rejectionReasons"],
        "softReasons": scored["softReasons"],
    }


def _raw_base_range(signal: dict[str, Any]) -> tuple[float, float, float]:
    raw = signal.get("baseRange") or signal.get("base_baseRange") or {}
    if not isinstance(raw, dict):
        return 0.0, 0.0, 0.0
    low = _safe_float(raw.get("low"))
    high = _safe_float(raw.get("high"))
    range_pct = _safe_float(raw.get("rangePct"))
    return low, high, range_pct


def _base_bias(signal: dict[str, Any], labels: set[str], tags: set[str]) -> str:
    direction = _direction(signal)
    if direction != "neutral":
        return direction
    bullish_evidence = {"accumulation", "higher_lows", "support_respect", "resistance_pressure", "ema_hold", "trend_up"}
    bearish_evidence = {"distribution", "trend_down", "breakdown"}
    if labels.intersection(bullish_evidence) or tags.intersection(bullish_evidence):
        return "bullish"
    if labels.intersection(bearish_evidence) or tags.intersection(bearish_evidence):
        return "bearish"
    return "neutral"


def _base_formation(signal: dict[str, Any], last_updated: str) -> dict[str, Any] | None:
    chart = _chart_features(signal)
    price = _safe_float(signal.get("current_price") or signal.get("price"))
    base_low, base_high, base_range_pct = _raw_base_range(signal)
    support = _safe_float(
        signal.get("support")
        or signal.get("support_level")
        or base_low
        or signal.get("invalidation")
        or signal.get("invalidation_level")
        or signal.get("invalidationLevel")
        or signal.get("base_invalidationLevel")
    )
    resistance = _safe_float(
        signal.get("resistance")
        or signal.get("resistance_level")
        or signal.get("breakout_level")
        or base_high
        or signal.get("breakoutTrigger")
        or signal.get("base_breakoutTrigger")
    )
    if not price or not support or not resistance or support >= resistance:
        return None

    base_labels = {str(label).lower() for label in (signal.get("baseLabels") or signal.get("base_baseLabels") or [])}
    labels = {str(label).lower() for label in (signal.get("pattern_labels") or [])} | base_labels
    tags = {str(tag).lower() for tag in (signal.get("tags") or [])}
    base_direction = _base_bias(signal, labels, tags)

    tight_source = chart.get("tightConsolidationPct")
    if tight_source is None and base_range_pct:
        tight_source = base_range_pct
    tight = _safe_float(tight_source, 99.0)
    atr_expansion = _safe_float(chart.get("atrExpansion"), 1.0)
    bb_width = _safe_float(chart.get("bbWidthRatio"), 1.0)
    distance_to_resistance = _safe_float(chart.get("distanceToResistancePct"), 99.0)
    distance_to_support = _safe_float(chart.get("distanceToSupportPct"), 99.0)
    relative_volume = _safe_float(signal.get("relative_volume") or signal.get("volume_ratio"), 1.0)
    intraday_volume = _safe_float(signal.get("intraday_volume_ratio"), 1.0)
    delivery_signal = str(signal.get("deliverySignal") or signal.get("delivery_deliverySignal") or "").lower()

    range_pct = ((resistance - support) / price) * 100
    range_score = 20 if tight <= 4 or range_pct <= 6 else 14 if tight <= 7 or range_pct <= 9 else 8
    support_score = 15 if chart["pullbackToSupport"] or distance_to_support <= 2.0 or "support_respect" in labels else 10 if support < price else 5
    resistance_score = 15 if distance_to_resistance <= 2.0 or chart["nearBreakout"] or "resistance_pressure" in labels else 10 if distance_to_resistance <= 4.0 else 5
    volume_score = 0
    if chart["volumeDryup"] or "volume_dryup" in labels:
        volume_score += 10
    if relative_volume >= 1.3 or intraday_volume >= 1.25 or "accumulation" in tags or "accumulation" in labels or delivery_signal == "accumulation":
        volume_score += 10
    elif relative_volume <= 1.0:
        volume_score += 5
    trend_score = 0
    if chart["higherLows"] or "higher_lows" in labels:
        trend_score += 8
    if "trend_up" in tags or "ema_hold" in labels:
        trend_score += 7
    squeeze_score = 15 if chart["bbSqueeze"] or bb_width <= 0.85 or atr_expansion <= 0.9 or "vcp_contraction" in labels else 8 if atr_expansion <= 1.05 else 0
    computed_score = round(min(100, range_score + support_score + resistance_score + min(20, volume_score) + min(15, trend_score) + squeeze_score), 1)
    source_score = _safe_float(signal.get("baseQualityScore") or signal.get("base_baseQualityScore"))
    score = round(max(computed_score, source_score), 1)
    if score < 50 or signal.get("symbol") is None:
        return None

    breakout_trigger = _safe_float(
        signal.get("alert_above_price")
        or signal.get("alert_price")
        or signal.get("breakoutTrigger")
        or signal.get("base_breakoutTrigger")
        or signal.get("entry_trigger")
    )
    invalidation_level = _safe_float(
        signal.get("invalidation")
        or signal.get("invalidation_level")
        or signal.get("invalidationLevel")
        or signal.get("base_invalidationLevel")
    )
    if base_direction == "bearish":
        trade_trigger = _safe_float(signal.get("entry_trigger")) or round(support * 0.998, 2)
        fail_level = invalidation_level or round(resistance * 1.01, 2)
        trigger_text = f"below {_fmt_price(trade_trigger)} with volume"
        invalidation_text = f"above {_fmt_price(fail_level)}"
        distance_to_trigger = ((price - trade_trigger) / price) * 100 if price and trade_trigger else None
    else:
        trade_trigger = breakout_trigger or round(resistance * 1.002, 2)
        fail_level = invalidation_level or round(support * 0.99, 2)
        trigger_text = f"above {_fmt_price(trade_trigger)} with volume"
        invalidation_text = f"below {_fmt_price(fail_level)}"
        distance_to_trigger = ((trade_trigger - price) / price) * 100 if price and trade_trigger else None

    guidance = _guidance({**signal, "direction": base_direction}, trigger=trade_trigger, fail=fail_level)
    strength = "strong base" if score >= 75 else "building base" if score >= 60 else "loose consolidation"
    if base_direction in {"bullish", "bearish"}:
        stage = f"{strength} - {base_direction}"
    else:
        stage = strength
    raw_pattern = signal.get("basePatternType") or signal.get("base_basePatternType") or signal.get("setup_type")
    fallback_pattern = "higher lows" if chart["higherLows"] or "higher_lows" in labels else "tight range" if tight <= 6 or range_pct <= 8 else "flat base"
    pattern = _display_label(raw_pattern, fallback_pattern)
    if pattern == "no clear base":
        pattern = fallback_pattern

    if delivery_signal == "accumulation":
        volume_behavior = "delivery accumulation"
    elif (chart["volumeDryup"] or "volume_dryup" in labels) and (relative_volume >= 1.3 or intraday_volume >= 1.25):
        volume_behavior = "dry-up then volume spike"
    elif chart["volumeDryup"] or "volume_dryup" in labels:
        volume_behavior = "volume dry-up"
    elif relative_volume >= 1.3 or intraday_volume >= 1.25:
        volume_behavior = "volume spike"
    else:
        volume_behavior = "quiet volume"

    base_reason = (
        signal.get("baseReason")
        or signal.get("base_baseReason")
        or signal.get("pattern_reason")
        or signal.get("pre_breakout_reason")
        or "Base is forming through consolidation, support respect, and improving structure."
    )
    range_width = max(resistance - support, 0)
    target_level = 0.0
    if base_direction == "bullish" and trade_trigger and range_width:
        target_level = trade_trigger + range_width * 0.75
    elif base_direction == "bearish" and trade_trigger and range_width:
        target_level = max(0, trade_trigger - range_width * 0.75)

    return {
        "symbol": signal.get("symbol"),
        "direction": base_direction,
        "currentPrice": _fmt_price(price),
        "stage": stage,
        "biasLabel": guidance["biasLabel"],
        "entryStatus": guidance["entryStatus"],
        "tradeDecision": guidance["tradeDecision"],
        "confirmationText": guidance["confirmationText"],
        "accumulationScore": score,
        "range": _fmt_zone(support, resistance),
        "keyResistance": _fmt_price(resistance),
        "supportZone": _fmt_price(support),
        "breakoutTrigger": trigger_text,
        "triggerPrice": _fmt_price(trade_trigger),
        "invalidation": invalidation_text,
        "targetZone": _fmt_price(target_level),
        "volumeBehavior": volume_behavior,
        "pattern": pattern,
        "whyInteresting": f"{guidance['biasLabel']}. {base_reason}"[:420],
        "risk": signal.get("risk_level", "medium"),
        "timeHorizon": signal.get("pre_breakout_timeframe") or signal.get("timeframe_label") or ("positional" if score >= 75 else "swing"),
        "lastUpdated": last_updated,
        "distanceToTriggerPct": round(distance_to_trigger, 2) if distance_to_trigger is not None else None,
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
        *(scan.get("breakout_radar") or []),
        *(scan.get("baseFormationRadar") or []),
        *(scan.get("momentumRadar") or []),
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
    hot = [item for item in eligible if _is_fresh_hot_pick(item)][:8]
    hot_symbols = {item["symbol"] for item in hot}

    def watch_sort_key(item: dict[str, Any]) -> tuple[int, int, float, float]:
        distance = item.get("entryDistancePct")
        near_or_early = distance is not None and -3.0 <= float(distance) <= 0.8
        return (
            1 if near_or_early else 0,
            0 if item.get("entryMissed") else 1,
            _safe_float(item.get("score")),
            _safe_float(item.get("relative_volume")),
        )

    watchlist = sorted(
        [item for item in eligible if item["score"] >= 60 and item["symbol"] not in hot_symbols],
        key=watch_sort_key,
        reverse=True,
    )[:16]
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
    def base_sort_key(item: dict[str, Any]) -> tuple[int, float]:
        status = str(item.get("entryStatus") or "")
        direction = item.get("direction")
        if direction == "bullish" and status == "Ready above trigger":
            priority = 50
        elif direction == "bullish" and status == "Alert above trigger":
            priority = 40
        elif direction == "bullish" and status == "Watch only":
            priority = 30
        elif direction == "bullish" and status == "Wait for pullback":
            priority = 20
        elif direction == "bearish":
            priority = 10
        else:
            priority = 0
        return priority, _safe_float(item.get("accumulationScore"))

    base_items.sort(key=base_sort_key, reverse=True)
    strict_options = build_strict_options_response(
        scan,
        [
            ("Hot Picks", hot),
            ("Watchlist", watchlist),
            ("Momentum Radar", momentum),
            ("Silent Accumulation", base_items),
        ],
    )

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
            "lateEntryExcludedFromHotPicks": len([item for item in eligible if item["score"] >= 72 and _has_late_entry_risk(item.get("raw") or {})]),
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
            "highConfidence": len(hot),
            "bullish": breadth.get("bullish_setups", 0),
            "bearish": breadth.get("bearish_setups", 0),
            "breakouts": summary.get("breakout_count", 0),
            "watchlist": len(watchlist),
            "momentumRadar": len(momentum),
            "baseFormation": len(base_items),
            "strictOptionsReady": strict_options["summary"]["strictReady"],
            "strictOptionsWatch": strict_options["summary"]["watchOnly"],
        },
        "hotPicks": hot,
        "watchlist": watchlist,
        "momentumRadar": momentum,
        "catalystRadar": catalyst_radar,
        "baseFormationRadar": base_items[:12],
        "strictOptions": strict_options,
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
