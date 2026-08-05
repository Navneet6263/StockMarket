"""Incremental features for the live entry gate.

The scanner supplies the structural setup.  This module only reads the market
footprint that is visible in a quote tick (price, cumulative traded volume and
market depth).  It deliberately does *not* infer the identity of a participant:
an FII, DII and a retail order are indistinguishable in this feed.

All state updates are O(1) per symbol and are protected by one small lock.  No
data frames or historical-data calls are used on the WebSocket callback path.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable


STALE_TICK_SEC = float(os.getenv("LIVE_TICK_STALE_SEC", "15"))
IMBALANCE_RATIO = max(1.05, float(os.getenv("LIVE_IMBALANCE_RATIO", "1.25")))
IMBALANCE_MIN_SEC = max(0.0, float(os.getenv("LIVE_IMBALANCE_MIN_SEC", "8")))
VWAP_TOLERANCE_PCT = max(0.0, float(os.getenv("LIVE_VWAP_TOLERANCE_PCT", "0.15")))
VOLUME_ACCELERATION_MIN = max(1.0, float(os.getenv("LIVE_VOLUME_ACCELERATION", "1.20")))
VOLUME_EWMA_ALPHA = min(1.0, max(0.05, float(os.getenv("LIVE_VOLUME_EWMA_ALPHA", "0.25"))))


_IDENTITY_DISCLAIMER = (
    "Market-footprint proxy only; participant identity (FII/DII/retail) "
    "is not available from this tick."
)


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _first_number(source: Dict[str, Any], names: Iterable[str], *, positive: bool = True) -> float | None:
    parser = _positive_float if positive else _nonnegative_float
    for name in names:
        if name in source:
            value = parser(source.get(name))
            if value is not None:
                return value
    return None


def _timestamp_epoch(value: Any) -> float | None:
    """Parse seconds/milliseconds/ISO timestamps without raising."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        epoch = float(value)
        if not math.isfinite(epoch):
            return None
        while epoch > 10_000_000_000:  # milliseconds or microseconds
            epoch /= 1000.0
        return epoch if epoch > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return _timestamp_epoch(float(raw))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _tick_epoch(tick: Dict[str, Any]) -> tuple[float | None, str]:
    for name in (
        "exchange_timestamp",
        "exchange_time",
        "last_traded_timestamp",
        "last_traded_time",
        "timestamp",
    ):
        if name in tick:
            parsed = _timestamp_epoch(tick.get(name))
            if parsed is not None:
                return parsed, name
    return None, "received_at"


@dataclass(slots=True)
class _SymbolState:
    session_key: str = ""
    samples: int = 0
    last_received_at: float | None = None
    last_price: float | None = None
    cumulative_volume: float | None = None
    last_volume_at: float | None = None
    volume_rate_ewma: float | None = None
    observed_notional: float = 0.0
    observed_volume: float = 0.0
    imbalance_side: int = 0
    imbalance_since: float | None = None
    imbalance_anchor_price: float | None = None


class RealtimeFeatureEngine:
    """Thread-safe, constant-work live feature accumulator."""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._states: Dict[str, _SymbolState] = {}

    def retain_symbols(self, symbols: Iterable[str]) -> None:
        """Bound memory to the current armed universe."""
        keep = {str(symbol).upper() for symbol in symbols}
        with self._lock:
            self._states = {symbol: state for symbol, state in self._states.items() if symbol in keep}

    def reset(self, symbol: str | None = None) -> None:
        with self._lock:
            if symbol is None:
                self._states.clear()
            else:
                self._states.pop(symbol.upper(), None)

    def update(
        self,
        symbol: str,
        tick: Dict[str, Any],
        *,
        received_at: float | None = None,
    ) -> Dict[str, Any]:
        now = float(self._clock() if received_at is None else received_at)
        symbol = symbol.upper()
        price = _first_number(tick, ("ltp", "price", "last_traded_price", "lastTradedPrice"))
        event_epoch, timestamp_source = _tick_epoch(tick)
        session_epoch = event_epoch or now
        session_key = datetime.fromtimestamp(session_epoch, tz=timezone.utc).date().isoformat()
        tick_age = abs(now - event_epoch) if event_epoch is not None else 0.0
        is_fresh = tick_age <= STALE_TICK_SEC

        cumulative_volume = _first_number(
            tick,
            ("volume", "cumulative_volume", "volume_trade_for_the_day", "total_traded_volume"),
            positive=False,
        )
        buy_qty = _first_number(
            tick,
            ("total_buy_qty", "total_buy_quantity", "buy_quantity", "best_buy_quantity"),
            positive=False,
        )
        sell_qty = _first_number(
            tick,
            ("total_sell_qty", "total_sell_quantity", "sell_quantity", "best_sell_quantity"),
            positive=False,
        )
        explicit_ratio = _first_number(tick, ("bid_ask_ratio", "bidAskRatio"))
        quantities_present = buy_qty is not None or sell_qty is not None
        depth_available = bool(
            (buy_qty is not None and buy_qty > 0)
            or (sell_qty is not None and sell_qty > 0)
            or (not quantities_present and explicit_ratio is not None)
        )
        if buy_qty is not None and sell_qty is not None and (buy_qty > 0 or sell_qty > 0):
            if sell_qty > 0:
                bid_ask_ratio = buy_qty / sell_qty
            else:
                bid_ask_ratio = 10.0
        elif not quantities_present:
            bid_ask_ratio = explicit_ratio
        else:
            bid_ask_ratio = None
        if bid_ask_ratio is not None:
            bid_ask_ratio = min(10.0, max(0.01, bid_ask_ratio))

        with self._lock:
            state = self._states.get(symbol)
            if state is None or state.session_key != session_key:
                state = _SymbolState(session_key=session_key)
                self._states[symbol] = state

            previous_price = state.last_price
            price_change_pct = (
                ((price - previous_price) / previous_price) * 100
                if price is not None and previous_price
                else None
            )

            volume_delta: float | None = None
            volume_velocity: float | None = state.volume_rate_ewma
            volume_acceleration: float | None = None
            if cumulative_volume is not None:
                if state.cumulative_volume is None or cumulative_volume < state.cumulative_volume:
                    # First observation, exchange reset, reconnect, or a new session.
                    state.cumulative_volume = cumulative_volume
                    state.last_volume_at = now
                    state.volume_rate_ewma = None
                    state.observed_notional = 0.0
                    state.observed_volume = 0.0
                    volume_velocity = None
                elif cumulative_volume > state.cumulative_volume:
                    volume_delta = cumulative_volume - state.cumulative_volume
                    elapsed = max(0.10, now - (state.last_volume_at or now))
                    instant_rate = volume_delta * 60.0 / elapsed
                    previous_rate = state.volume_rate_ewma
                    if previous_rate and previous_rate > 0:
                        volume_acceleration = instant_rate / previous_rate
                        state.volume_rate_ewma = (
                            VOLUME_EWMA_ALPHA * instant_rate
                            + (1.0 - VOLUME_EWMA_ALPHA) * previous_rate
                        )
                    else:
                        state.volume_rate_ewma = instant_rate
                    volume_velocity = state.volume_rate_ewma
                    state.cumulative_volume = cumulative_volume
                    state.last_volume_at = now
                    if price is not None:
                        state.observed_notional += price * volume_delta
                        state.observed_volume += volume_delta

            session_vwap = (
                state.observed_notional / state.observed_volume
                if state.observed_volume > 0
                else None
            )
            vwap_distance_pct = (
                ((price - session_vwap) / session_vwap) * 100
                if price is not None and session_vwap
                else None
            )

            imbalance_side = 0
            if bid_ask_ratio is not None:
                if bid_ask_ratio >= IMBALANCE_RATIO:
                    imbalance_side = 1
                elif bid_ask_ratio <= (1.0 / IMBALANCE_RATIO):
                    imbalance_side = -1
            if imbalance_side == 0:
                state.imbalance_side = 0
                state.imbalance_since = None
                state.imbalance_anchor_price = None
            elif imbalance_side != state.imbalance_side:
                state.imbalance_side = imbalance_side
                state.imbalance_since = now
                state.imbalance_anchor_price = price

            imbalance_persistence = (
                max(0.0, now - state.imbalance_since)
                if state.imbalance_since is not None and state.imbalance_side
                else 0.0
            )
            imbalance_price_response = (
                ((price - state.imbalance_anchor_price) / state.imbalance_anchor_price) * 100
                if price is not None and state.imbalance_anchor_price
                else None
            )

            state.samples += 1
            state.last_received_at = now
            if price is not None:
                state.last_price = price

            samples = state.samples
            observed_volume = state.observed_volume
            persistent_imbalance_side = state.imbalance_side

        available_fields = []
        if price is not None:
            available_fields.append("price")
        if cumulative_volume is not None:
            available_fields.append("cumulative_volume")
        if session_vwap is not None:
            available_fields.append("observed_vwap")
        if depth_available:
            available_fields.append("market_depth")
        if event_epoch is not None:
            available_fields.append("exchange_timestamp")

        quality_score = 35 if price is not None else 0
        quality_score += 20 if cumulative_volume is not None else 0
        quality_score += 25 if depth_available else 0
        quality_score += 15 if event_epoch is not None else 0
        quality_score += 5 if session_vwap is not None else 0
        if not is_fresh:
            quality_score = min(quality_score, 20)
        quality = "HIGH" if quality_score >= 90 else "MEDIUM" if quality_score >= 55 else "LOW" if quality_score >= 35 else "NONE"
        missing_fields = []
        if cumulative_volume is None:
            missing_fields.append("cumulative_volume")
        if not depth_available:
            missing_fields.append("market_depth")
        if event_epoch is None:
            missing_fields.append("exchange_timestamp")

        return {
            "symbol": symbol,
            "price": round(price, 4) if price is not None else None,
            "previousPrice": round(previous_price, 4) if previous_price is not None else None,
            "priceChangePct": round(price_change_pct, 4) if price_change_pct is not None else None,
            "cumulativeVolume": int(cumulative_volume) if cumulative_volume is not None else None,
            "volumeDelta": int(volume_delta) if volume_delta is not None else None,
            "volumeVelocityPerMin": round(volume_velocity, 2) if volume_velocity is not None else None,
            "volumeAcceleration": round(volume_acceleration, 3) if volume_acceleration is not None else None,
            "sessionVwapApprox": round(session_vwap, 4) if session_vwap is not None else None,
            "vwapDistancePct": round(vwap_distance_pct, 4) if vwap_distance_pct is not None else None,
            "observedVwapVolume": int(observed_volume),
            "bidAskRatio": round(bid_ask_ratio, 3) if bid_ask_ratio is not None else None,
            "imbalanceSide": "BUY" if persistent_imbalance_side > 0 else "SELL" if persistent_imbalance_side < 0 else "NEUTRAL",
            "imbalancePersistenceSec": round(imbalance_persistence, 3),
            "imbalancePriceResponsePct": (
                round(imbalance_price_response, 4) if imbalance_price_response is not None else None
            ),
            "tickAgeSec": round(tick_age, 3),
            "isFresh": is_fresh,
            "sampleCount": samples,
            "dataQuality": {
                "quality": quality,
                "score": quality_score,
                "availableFields": available_fields,
                "missingFields": missing_fields,
                "timestampSource": timestamp_source,
                "footprintAvailable": bool(cumulative_volume is not None or depth_available),
                "identityDisclaimer": _IDENTITY_DISCLAIMER,
            },
        }


def _direction_side(direction: Any) -> int:
    normalized = str(direction or "bullish").strip().lower()
    return -1 if normalized in {"bearish", "sell", "short", "down"} else 1


def classify_live_trigger(features: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Return CONFIRMED, WAIT or REJECT for a structure-approved entry.

    ``score`` is an evidence score, not a probability.  When the broker only
    supplies LTP, the function preserves the previous price-proximity behavior
    but labels it explicitly as a low-quality fallback.
    """
    evidence: list[str] = []
    risks: list[str] = []
    side = _direction_side(context.get("direction"))
    direction = "bearish" if side < 0 else "bullish"
    price = _positive_float(features.get("price"))
    entry = _positive_float(context.get("entry"))
    stop = _positive_float(context.get("stop"))
    target = _positive_float(context.get("target"))
    trigger_tolerance = max(0.1, float(context.get("trigger_tolerance_pct") or 1.5))
    max_chase = max(trigger_tolerance, float(context.get("max_chase_pct") or 5.0))
    requires_full_tick = bool(context.get("requires_full_tick"))
    min_confirmations = max(2, min(4, int(context.get("live_min_confirmations") or 2)))
    structure_valid = context.get("structure_valid") is not False
    structure_reasons = list(context.get("structure_reasons") or [])
    quality = dict(features.get("dataQuality") or {})
    score = 20

    directional_distance: float | None = None
    if price is not None and entry is not None:
        directional_distance = side * ((price - entry) / entry) * 100

    def result(status: str, mode: str) -> Dict[str, Any]:
        return {
            "status": status,
            "score": max(0, min(100, round(score))),
            "scoreMeaning": "Evidence strength, not probability",
            "direction": direction,
            "confirmationMode": mode,
            "directionalDistancePct": (
                round(directional_distance, 3) if directional_distance is not None else None
            ),
            "evidence": evidence,
            "risks": risks,
            "dataQuality": quality,
            "features": {
                key: features.get(key)
                for key in (
                    "price",
                    "previousPrice",
                    "cumulativeVolume",
                    "sessionVwapApprox",
                    "vwapDistancePct",
                    "observedVwapVolume",
                    "volumeDelta",
                    "volumeVelocityPerMin",
                    "volumeAcceleration",
                    "bidAskRatio",
                    "imbalanceSide",
                    "imbalancePersistenceSec",
                    "imbalancePriceResponsePct",
                    "priceChangePct",
                    "tickAgeSec",
                    "isFresh",
                    "sampleCount",
                )
            },
            "identityDisclaimer": _IDENTITY_DISCLAIMER,
            "requiresFullTick": requires_full_tick,
            "minimumIndependentConfirmations": min_confirmations,
            "counterRegime": bool(context.get("counter_regime")),
        }

    if not structure_valid:
        risks.extend(structure_reasons or ["Scanner structure is invalid or no longer actionable."])
        return result("REJECT", "STRUCTURE_GATE")
    score += 15
    evidence.append("Scanner structure remains valid.")

    if price is None or entry is None:
        risks.append("Live price or planned entry is unavailable.")
        return result("REJECT", "INVALID_INPUT")
    if not features.get("isFresh", True):
        risks.append(f"Tick is stale ({features.get('tickAgeSec')}s old).")
        return result("REJECT", "STALE_TICK")
    if stop is not None and side * (price - stop) <= 0:
        risks.append("Price has breached the setup invalidation level.")
        return result("REJECT", "STRUCTURE_GATE")
    if target is not None and side * (price - target) >= 0:
        risks.append("Price has already reached/passed the planned target; fresh entry is late.")
        return result("REJECT", "CHASE_GATE")
    if directional_distance is None:
        risks.append("Entry distance could not be calculated.")
        return result("REJECT", "INVALID_INPUT")
    if directional_distance > trigger_tolerance:
        risks.append(
            "Price is beyond the live entry band; wait for a retest."
            if directional_distance <= max_chase
            else "Price is far beyond the entry and chasing has poor risk/reward."
        )
        return result("REJECT", "CHASE_GATE")
    if directional_distance < -trigger_tolerance:
        evidence.append("Price has not reached the planned trigger yet.")
        return result("WAIT", "PRICE_GATE")
    score += 15
    evidence.append("Price is inside the planned entry band.")

    available = set(quality.get("availableFields") or [])
    has_volume = "cumulative_volume" in available
    has_depth = "market_depth" in available
    has_vwap = features.get("sessionVwapApprox") is not None
    footprint_available = has_volume or has_depth

    # Safe compatibility path for feeds/tests that only publish LTP.  This is
    # visibly marked as low-quality and must never be described as large-money
    # or participant confirmation.
    if not footprint_available:
        if requires_full_tick:
            risks.append(
                "This setup requires fresh volume/depth evidence; price-only data cannot confirm it."
            )
            return result("WAIT", "FULL_TICK_REQUIRED")
        score += 20
        evidence.append("Price reached a structure-approved entry.")
        risks.append("Volume/depth unavailable; confirmation is price-only, not participant-flow evidence.")
        return result("CONFIRMED", "PRICE_ONLY_FALLBACK")

    vwap_distance = features.get("vwapDistancePct")
    vwap_aligned = bool(
        has_vwap
        and vwap_distance is not None
        and side * float(vwap_distance) >= -VWAP_TOLERANCE_PCT
    )
    vwap_opposed = bool(
        has_vwap
        and vwap_distance is not None
        and side * float(vwap_distance) <= -0.35
    )
    if vwap_aligned:
        score += 20
        evidence.append("Price is holding on the actionable side of observed VWAP.")
    elif vwap_opposed:
        score -= 15
        risks.append("Price is materially on the wrong side of observed VWAP.")

    expected_imbalance = "BUY" if side > 0 else "SELL"
    opposite_imbalance = "SELL" if side > 0 else "BUY"
    imbalance_side = features.get("imbalanceSide")
    persistence = float(features.get("imbalancePersistenceSec") or 0)
    imbalance_response = features.get("imbalancePriceResponsePct")
    directional_imbalance_response = (
        side * float(imbalance_response) if imbalance_response is not None else None
    )
    flow_persistent = persistence >= IMBALANCE_MIN_SEC
    flow_aligned = bool(
        has_depth
        and imbalance_side == expected_imbalance
        and flow_persistent
        and directional_imbalance_response is not None
        and directional_imbalance_response >= 0.01
    )
    flow_opposed = bool(
        has_depth and imbalance_side == opposite_imbalance and flow_persistent
    )
    no_price_response = bool(
        has_depth
        and imbalance_side == expected_imbalance
        and flow_persistent
        and directional_imbalance_response is not None
        and directional_imbalance_response <= -0.15
    )
    if no_price_response:
        risks.append("Displayed depth is not producing price response; absorption/spoof risk is elevated.")
        score -= 30
        return result("REJECT", "PRICE_RESPONSE_GATE")
    if flow_opposed and directional_imbalance_response is not None and directional_imbalance_response < -0.05:
        risks.append("Persistent opposing depth is moving price against the setup.")
        score -= 25
        return result("REJECT", "ORDER_FLOW_GATE")
    if flow_aligned:
        score += 25
        evidence.append("Persistent market-depth imbalance is supported by price response.")
    elif has_depth:
        evidence.append("Waiting for market-depth imbalance to persist and produce price response.")

    acceleration = features.get("volumeAcceleration")
    volume_impulse = bool(
        features.get("volumeDelta") is not None
        and acceleration is not None
        and float(acceleration) >= VOLUME_ACCELERATION_MIN
    )
    if volume_impulse:
        score += 20
        evidence.append("Executed-volume velocity is accelerating.")
    elif has_volume:
        evidence.append("Waiting for executed-volume velocity to strengthen.")

    tick_change = features.get("priceChangePct")
    directional_tick_change = side * float(tick_change) if tick_change is not None else None
    price_responding = bool(
        (directional_tick_change is not None and directional_tick_change >= 0.02)
        or (directional_imbalance_response is not None and directional_imbalance_response >= 0.03)
    )
    if price_responding:
        score += 10
        evidence.append("Live price is responding in the setup direction.")

    # Two independent pieces of live evidence are required whenever the feed
    # supplies footprint fields.  A persistent depth signal must show response;
    # a volume-only signal must hold observed VWAP and show acceleration/response.
    confirmation_count = sum((vwap_aligned, flow_aligned, volume_impulse, price_responding))
    if confirmation_count >= min_confirmations and (vwap_aligned or flow_aligned):
        return result("CONFIRMED", "FULL_TICK")

    if vwap_opposed and flow_opposed:
        risks.append("VWAP and persistent depth both oppose the setup.")
        return result("REJECT", "ORDER_FLOW_GATE")

    risks.append(
        f"Live footprint has not produced {min_confirmations} independent confirmations yet."
    )
    return result("WAIT", "FULL_TICK")
