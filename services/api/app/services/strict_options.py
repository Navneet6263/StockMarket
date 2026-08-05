from __future__ import annotations

import os
import re
from typing import Any

from app.market_universe_config import FNO_STOCKS

# Strict options rules - IV-crush protection
IV_CRUSH_BLOCK_THRESHOLD = float(os.getenv("IV_CRUSH_BLOCK_THRESHOLD", "60.0"))
IV_CRUSH_WARNING_THRESHOLD = float(os.getenv("IV_CRUSH_WARNING_THRESHOLD", "40.0"))
MIN_DELTA_FOR_OPTION_BUY = float(os.getenv("MIN_DELTA_FOR_OPTION_BUY", "0.45"))
MAX_HV_PROXY_FOR_OPTION_BUY = float(os.getenv("MAX_HV_PROXY_FOR_OPTION_BUY", "60.0"))

STRICT_OPTIONS_DISCLAIMER = (
    "Options are high risk. Scanner shows research-only option ideas only when strict gates pass; "
    "avoid far OTM weekly options and size premium risk very small."
)

FNO_SYMBOLS = {symbol.upper() for symbol in FNO_STOCKS}

_dynamic_fno_symbols = None

def is_fno_symbol(symbol: str) -> bool:
    global _dynamic_fno_symbols
    if _dynamic_fno_symbols is None:
        try:
            from app.services.broker_adapter import get_broker_adapter
            adapter = get_broker_adapter()
            instruments = adapter._load_instruments()
            if instruments:
                fno = {row.get("name") for row in instruments if row.get("exch_seg") == "NFO"}
                if fno:
                    _dynamic_fno_symbols = {str(s).upper() for s in fno if s}
            if not _dynamic_fno_symbols:
                _dynamic_fno_symbols = FNO_SYMBOLS
        except Exception:
            _dynamic_fno_symbols = FNO_SYMBOLS
    return symbol.upper() in _dynamic_fno_symbols


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        if isinstance(value, str):
            value = value.replace("INR", "").replace("%", "").replace(",", "").strip()
            if not value or value == "-":
                return default
            match = re.search(r"-?\d+(?:\.\d+)?", value)
            if match:
                value = match.group(0)
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_price(value: Any) -> str:
    number = _safe_float(value)
    return f"INR {number:.2f}" if number else "-"


def _direction(item: dict[str, Any]) -> str:
    direction = str(item.get("direction") or "").lower()
    if direction in {"bullish", "up"}:
        return "bullish"
    if direction in {"bearish", "down"}:
        return "bearish"
    return "neutral"


def _raw(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("raw")
    return raw if isinstance(raw, dict) else {}


def _trigger(item: dict[str, Any]) -> float:
    raw = _raw(item)
    return _safe_float(
        raw.get("safe_entry_price")
        or raw.get("entry_trigger")
        or raw.get("trigger_price")
        or raw.get("alert_above_price")
        or raw.get("alert_price")
        or raw.get("breakoutTrigger")
        or item.get("triggerPrice")
        or item.get("entryTrigger")
    )


def _price(item: dict[str, Any]) -> float:
    raw = _raw(item)
    return _safe_float(raw.get("current_price") or raw.get("price") or item.get("currentPrice"))


def _fail(item: dict[str, Any]) -> float:
    raw = _raw(item)
    return _safe_float(
        raw.get("stop_loss")
        or raw.get("invalidation")
        or raw.get("invalidation_level")
        or raw.get("support")
        or item.get("stoploss")
        or item.get("invalidation")
    )


def _target(item: dict[str, Any]) -> float:
    raw = _raw(item)
    return _safe_float(raw.get("new_target") or raw.get("target_1") or raw.get("target_price") or item.get("targetZone") or item.get("target"))


def _relative_volume(item: dict[str, Any]) -> float:
    raw = _raw(item)
    return _safe_float(raw.get("relative_volume") or raw.get("volume_ratio") or raw.get("intraday_volume_ratio"), 1.0)


def _distance_to_trigger(direction: str, price: float, trigger: float) -> float | None:
    if not price or not trigger:
        return None
    if direction == "bearish":
        return round(((price - trigger) / price) * 100, 2)
    return round(((trigger - price) / price) * 100, 2)


def _trigger_crossed(direction: str, price: float, trigger: float) -> bool:
    if not price or not trigger:
        return False
    if direction == "bearish":
        return price <= trigger
    return price >= trigger


def build_nifty_option_gate(scan: dict[str, Any]) -> dict[str, Any]:
    nifty = scan.get("nifty_context") or {}
    breadth = scan.get("market_breadth") or {}
    summary = scan.get("summary") or {}
    bias_text = str(nifty.get("nifty_bias") or summary.get("nifty_bias") or "neutral").lower()
    regime = str(nifty.get("nifty_regime") or summary.get("nifty_regime") or "unknown").lower()
    market_score = _safe_float(nifty.get("market_score") or summary.get("market_score"), 50.0)
    benchmark_change = _safe_float(breadth.get("benchmark_change_pct"))
    pcr = nifty.get("pcr")
    pcr_signal = str(nifty.get("pcr_signal") or "unknown").lower()
    data_available = bool(nifty.get("data_available"))

    bullish = (
        "bullish" in bias_text
        and "bearish" not in bias_text
        and market_score >= 65
        and regime not in {"trending_down", "downtrend", "strong_downtrend"}
        and benchmark_change >= 0
    )
    bearish = (
        "bearish" in bias_text
        and "bullish" not in bias_text
        and market_score <= 35
        and regime not in {"trending_up", "uptrend", "strong_uptrend"}
        and benchmark_change <= 0
    )

    if bullish:
        side = "bullish"
        status = "BULLISH_CONFIRMED"
        message = "Nifty gate supports CE-only ideas, but stock trigger and volume must still confirm."
    elif bearish:
        side = "bearish"
        status = "BEARISH_CONFIRMED"
        message = "Nifty gate supports PE-only ideas, but stock trigger and volume must still confirm."
    else:
        side = "neutral"
        status = "BLOCKED"
        message = "Nifty gate is not strict enough for fresh option buying."

    return {
        "status": status,
        "side": side,
        "message": message,
        "niftyBias": bias_text,
        "niftyRegime": regime,
        "marketScore": round(market_score, 1),
        "benchmarkChangePct": round(benchmark_change, 2),
        "pcr": pcr,
        "pcrSignal": pcr_signal,
        "dataAvailable": data_available,
    }


def build_strict_option_idea(item: dict[str, Any], nifty_gate: dict[str, Any], bucket: str) -> dict[str, Any]:
    symbol = str(item.get("symbol") or "").upper()
    direction = _direction(item)
    option_side = "CE" if direction == "bullish" else "PE" if direction == "bearish" else "NO_TRADE"
    price = _price(item)
    trigger = _trigger(item)
    fail = _fail(item)
    target = _target(item)
    distance = _distance_to_trigger(direction, price, trigger)
    score = _safe_float(item.get("score") or item.get("confidence") or item.get("accumulationScore"))
    risk_level = str(item.get("riskLevel") or item.get("risk") or "medium").lower()
    status_text = str(item.get("entryStatus") or "").lower()
    bias_text = str(item.get("biasLabel") or "").lower()
    relative_volume = _relative_volume(item)
    raw = _raw(item)
    risk_reward = _safe_float(raw.get("risk_reward") or raw.get("continuation_risk_reward"))
    blockers: list[str] = []

    if not symbol:
        blockers.append("Missing symbol.")
    elif not is_fno_symbol(symbol):
        blockers.append("Not in F&O universe; no stock option idea.")
    if direction not in {"bullish", "bearish"}:
        blockers.append("No clear direction.")
    if nifty_gate.get("side") != direction:
        blockers.append(f"Nifty gate is {nifty_gate.get('side', 'neutral')}, not {direction}.")
    if "late-entry" in bias_text or "pullback" in status_text or raw.get("chase_risk") or raw.get("next_day_profit_booking_risk"):
        blockers.append("Late-entry or profit-booking risk.")
    if risk_level == "high":
        blockers.append("High-risk underlying setup.")
    if not price or not trigger or not fail:
        blockers.append("Price, trigger, or invalidation missing.")
    if raw.get("tradePlanGateBlocked") or raw.get("entry_plan_blocked") or item.get("entry_plan_blocked"):
        blockers.append("Underlying structural trade plan is watch-only.")
    if score < 72:
        blockers.append("Setup score below strict option threshold.")
    if not target or not risk_reward:
        blockers.append("Observed target or risk/reward is unavailable.")
    elif risk_reward < 1.5:
        blockers.append("Underlying risk/reward below 1:1.5.")
    if price and fail and direction == "bullish" and fail >= price:
        blockers.append("Bullish invalidation is not below the underlying price.")
    if price and target and direction == "bullish" and target <= price:
        blockers.append("Bullish target is not above the underlying price.")
    if price and fail and direction == "bearish" and fail <= price:
        blockers.append("Bearish invalidation is not above the underlying price.")
    if price and target and direction == "bearish" and target >= price:
        blockers.append("Bearish target is not below the underlying price.")

    trigger_crossed = _trigger_crossed(direction, price, trigger)
    abs_distance = abs(distance) if distance is not None else 99.0
    if not trigger_crossed and (distance is None or distance < 0 or distance > 1.0):
        blockers.append("Underlying has not confirmed trigger and is not within 1% alert zone.")
    if trigger_crossed and abs_distance > 1.25:
        blockers.append("Trigger is already too far; option entry would be chasing.")

    volume_confirmed = relative_volume >= 1.25 or bool((item.get("chart") or {}).get("volumeSpike"))
    if trigger_crossed and not volume_confirmed:
        blockers.append("Volume confirmation below strict option threshold.")

    stop_pct = abs(price - fail) / price * 100 if price and fail else 99.0
    if stop_pct > 4.0:
        blockers.append("Underlying stop is too wide for option buying.")

    # IV-crush protection - block option buying when IV/HV is too high.
    iv_percentile_verified = bool(item.get("iv_percentile_verified_historical"))
    iv_percentile = _safe_float(item.get("iv_percentile")) if iv_percentile_verified else 0.0
    if iv_percentile and iv_percentile > IV_CRUSH_BLOCK_THRESHOLD:
        blockers.append(f"IV Percentile ({iv_percentile:.0f}%) > {IV_CRUSH_BLOCK_THRESHOLD}% — SKIP option buying. IV crush risk.")
    elif iv_percentile and iv_percentile > IV_CRUSH_WARNING_THRESHOLD:
        blockers.append(f"IV Percentile ({iv_percentile:.0f}%) > {IV_CRUSH_WARNING_THRESHOLD}% — CAUTION: High IV crush risk.")

    atr_pct = _safe_float(raw.get("atr_pct"))
    hv_proxy = (atr_pct * 15.87) if atr_pct else 0.0
    # Additional HV proxy check
    if hv_proxy > MAX_HV_PROXY_FOR_OPTION_BUY:
        blockers.append(f"HV Proxy ({hv_proxy:.1f}%) is too high. High risk of IV Crush.")

    ready_blockers = [reason for reason in blockers if "within 1% alert zone" not in reason]
    if not ready_blockers and trigger_crossed and volume_confirmed:
        # This radar sees the underlying setup, not a verified live option
        # contract.  It may nominate a side but must never claim order-readiness.
        status = "WATCH_CONTRACT"
        action_label = f"{option_side} contract check"
        entry_rule = (
            f"Underlying confirmed beyond {_fmt_price(trigger)}. No option order until an exact listed "
            f"{option_side} has fresh two-sided quotes, liquidity, bounded spread and verified IV."
        )
        confidence = round(score, 1)
    elif not blockers or (
        len(blockers) == 1 and "within 1% alert zone" in blockers[0] and nifty_gate.get("side") == direction
    ):
        status = "WATCH_TRIGGER"
        action_label = f"{option_side} Watch"
        entry_rule = f"No option entry yet. Set alert at {_fmt_price(trigger)} and enter only after trigger + volume confirmation."
        confidence = min(88, round(score, 1))
        blockers = []
    else:
        status = "NO_TRADE"
        action_label = "No option trade"
        entry_rule = "Strict option gate blocked this setup."
        confidence = round(score, 1)

    expiry_rule = "No expiry is assumed here; use only an actual listed contract with enough sessions remaining."

    target_rule = f"First underlying target near {_fmt_price(target)}." if target else "Book by price action; target unavailable."
    invalidation_rule = f"Underlying research invalidates at {_fmt_price(fail)}; no broker-native option stop is implied."
    risk_rule = "Contract verification and premium-risk sizing are still required. Never average a losing option."
    instrument_rule = (
        f"{option_side} candidate only—not an executable contract. Require Delta > {MIN_DELTA_FOR_OPTION_BUY}, "
        "positive volume/OI and a fresh two-sided quote."
    )

    return {
        "symbol": symbol,
        "bucket": bucket,
        "side": option_side,
        "status": status,
        "actionLabel": action_label,
        "confidence": confidence,
        "underlyingPrice": _fmt_price(price),
        "underlyingTrigger": _fmt_price(trigger),
        "underlyingInvalidation": _fmt_price(fail),
        "distanceToTriggerPct": distance,
        "instrumentRule": instrument_rule,
        "entryRule": entry_rule,
        "targetRule": target_rule,
        "invalidationRule": invalidation_rule,
        "expiryRule": expiry_rule,
        "riskRule": risk_rule,
        "niftyGate": nifty_gate.get("status"),
        "tradeGatePassed": False,
        "contractVerificationRequired": True,
        "blockers": blockers[:5],
    }


def build_strict_options_response(
    scan: dict[str, Any],
    buckets: list[tuple[str, list[dict[str, Any]]]],
    *,
    max_results: int = 8,
) -> dict[str, Any]:
    nifty_gate = build_nifty_option_gate(scan)
    ideas: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    seen: set[str] = set()

    for bucket, items in buckets:
        for item in items:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            idea = build_strict_option_idea(item, nifty_gate, bucket)
            if idea["status"] == "NO_TRADE":
                if idea["confidence"] >= 70:
                    blocked.append(idea)
                continue
            ideas.append(idea)

    priority = {"WATCH_CONTRACT": 3, "WATCH_TRIGGER": 2, "NO_TRADE": 1}
    ideas.sort(key=lambda item: (priority.get(item["status"], 0), item["confidence"]), reverse=True)
    blocked.sort(key=lambda item: item["confidence"], reverse=True)

    return {
        "niftyGate": nifty_gate,
        "radar": ideas[:max_results],
        "blocked": blocked[:10],
        "summary": {
            "strictReady": 0,
            "watchOnly": len([item for item in ideas if item["status"] in {"WATCH_CONTRACT", "WATCH_TRIGGER"}]),
            "blockedHighScore": len(blocked),
        },
        "warnings": [STRICT_OPTIONS_DISCLAIMER],
    }
