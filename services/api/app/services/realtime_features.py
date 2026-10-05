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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable


STALE_TICK_SEC = float(os.getenv("LIVE_TICK_STALE_SEC", "15"))
IMBALANCE_RATIO = max(1.05, float(os.getenv("LIVE_IMBALANCE_RATIO", "1.25")))
IMBALANCE_MIN_SEC = max(0.0, float(os.getenv("LIVE_IMBALANCE_MIN_SEC", "8")))
VWAP_TOLERANCE_PCT = max(0.0, float(os.getenv("LIVE_VWAP_TOLERANCE_PCT", "0.15")))
VOLUME_ACCELERATION_MIN = max(1.0, float(os.getenv("LIVE_VOLUME_ACCELERATION", "1.20")))
VOLUME_EWMA_ALPHA = min(1.0, max(0.05, float(os.getenv("LIVE_VOLUME_EWMA_ALPHA", "0.25"))))
# These are minimum evidence safeguards, not calibrated probability estimates.
MIN_LIVE_RR = max(1.5, float(os.getenv("LIVE_MIN_RISK_REWARD", "1.5")))
MIN_OBSERVATION_SEC = max(1.0, float(os.getenv("LIVE_MIN_OBSERVATION_SEC", "8")))
MIN_VALID_SAMPLES = max(3, int(os.getenv("LIVE_MIN_VALID_SAMPLES", "3")))
MIN_OBSERVED_VOLUME = max(1.0, float(os.getenv("LIVE_MIN_OBSERVED_VOLUME", "100")))
MIN_OBSERVED_NOTIONAL = max(1.0, float(os.getenv("LIVE_MIN_OBSERVED_NOTIONAL", "10000")))
_NSE_TZ = timezone(timedelta(hours=5, minutes=30))
_CANDLE_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15,
    "30m": 30, "60m": 60, "1h": 60, "90m": 90,
    "1d": 1440, "d": 1440, "day": 1440, "daily": 1440,
}


_IDENTITY_DISCLAIMER = (
    "Market-footprint proxy only; participant identity (FII/DII/retail) "
    "is not available from this tick."
)


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
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
    return None, "unavailable"


def _candle_timing_risks(candle: Dict[str, Any], received_at: float) -> list[str]:
    """Expire a scanner snapshot even when broker quotes continue arriving.

    Production candle plans include all three timing fields.  Completely
    untimed legacy plans remain compatible, but present malformed/naive dates
    fail closed instead of being interpreted in the host machine's timezone.
    This validates an NSE regular-session date, not an exchange holiday calendar.
    """
    present = [key for key in ("latest_closed_time", "evaluated_at", "confirmation_time") if key in candle]
    if not present:
        return []
    minutes = _CANDLE_MINUTES.get(str(candle.get("timeframe") or "15m").strip().lower())
    if minutes is None:
        return ["Candle timeframe is unsupported; snapshot freshness cannot be verified."]
    clock = datetime.fromtimestamp(received_at, tz=_NSE_TZ)
    clock_minutes = clock.hour * 60 + clock.minute
    if clock.weekday() >= 5 or not 9 * 60 + 15 <= clock_minutes < 15 * 60 + 30:
        return ["Timed candle entry is outside the current NSE regular session; wait for a fresh session setup."]
    epochs = {}
    for key in present:
        value = candle.get(key)
        if isinstance(value, bool):
            return [f"Candle {key} is malformed; snapshot freshness cannot be verified."]
        try:
            if isinstance(value, datetime):
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("ambiguous timezone")
            elif isinstance(value, str):
                try:
                    float(value)
                except ValueError:
                    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError("ambiguous timezone")
            epoch = _timestamp_epoch(value)
            if epoch is None or epoch <= 0:
                raise ValueError("invalid timestamp")
            moment = datetime.fromtimestamp(epoch, tz=_NSE_TZ)
        except (ValueError, TypeError, OverflowError, OSError):
            return [f"Candle {key} is malformed or timezone-ambiguous; refresh the scanner setup."]
        if moment.date() != clock.date():
            return [f"Candle {key} belongs to a different NSE session; fresh ticks cannot revive it."]
        if epoch > received_at:
            return [f"Candle {key} is in the future; a completed-candle setup is required."]
        if key in {"latest_closed_time", "evaluated_at"} and received_at - epoch > minutes * 3 * 60:
            return [f"Candle {key} is older than three {minutes:g}-minute bars; wait for a scanner refresh."]
        session_minutes = moment.hour * 60 + moment.minute
        if not 9 * 60 + 15 <= session_minutes <= 15 * 60 + 30:
            return [f"Candle {key} is outside the NSE regular session."]
        epochs[key] = epoch
    if epochs.get("latest_closed_time", 0) > epochs.get("evaluated_at", received_at):
        return ["The latest candle closes after the scanner evaluation; timing is inconsistent."]
    if epochs.get("confirmation_time", 0) > epochs.get("latest_closed_time", received_at):
        return ["Candle confirmation is later than the latest completed candle; timing is inconsistent."]
    return []


@dataclass(slots=True)
class _SymbolState:
    session_key: str = ""
    samples: int = 0
    first_received_at: float | None = None
    last_received_at: float | None = None
    first_event_at: float | None = None
    last_event_at: float | None = None
    last_tick_fingerprint: tuple[Any, ...] | None = None
    last_price: float | None = None
    cumulative_volume: float | None = None
    last_volume_at: float | None = None
    volume_rate_ewma: float | None = None
    observed_notional: float = 0.0
    observed_volume: float = 0.0
    imbalance_side: int = 0
    imbalance_since: float | None = None
    imbalance_received_since: float | None = None
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
        tick_age = abs(now - event_epoch) if event_epoch is not None else None
        is_fresh = tick_age is not None and tick_age <= STALE_TICK_SEC

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
            fingerprint = (price, cumulative_volume, buy_qty, sell_qty, explicit_ratio)
            rejected_reason = None
            if price is None:
                rejected_reason = "INVALID_PRICE"
            elif event_epoch is None:
                rejected_reason = "MISSING_TIMESTAMP"
            elif not is_fresh:
                rejected_reason = "STALE_TICK"
            elif state and state.last_event_at is not None and event_epoch < state.last_event_at:
                rejected_reason = "OUT_OF_ORDER_TICK"
            elif (
                state and event_epoch == state.last_event_at
                and fingerprint == state.last_tick_fingerprint
            ):
                rejected_reason = "DUPLICATE_TICK"
            if rejected_reason:
                # A malformed/replayed packet must never reset the session,
                # contribute volume, or manufacture imbalance persistence.
                return {
                    "symbol": symbol,
                    "price": price,
                    "receivedAtEpoch": now,
                    "tickAgeSec": round(tick_age, 3) if tick_age is not None else None,
                    "isFresh": False,
                    "tickRejectedReason": rejected_reason,
                    "sampleCount": state.samples if state else 0,
                    "observationSpanSec": 0.0,
                    "dataQuality": {
                        "quality": "LOW" if price is not None else "NONE",
                        "score": 20 if price is not None else 0,
                        "availableFields": ["price"] if price is not None else [],
                        "missingFields": ["verified_fresh_tick"],
                        "timestampSource": timestamp_source,
                        "footprintAvailable": False,
                        "identityDisclaimer": _IDENTITY_DISCLAIMER,
                    },
                }
            session_key = datetime.fromtimestamp(event_epoch, tz=timezone.utc).date().isoformat()
            if (
                state is None or state.session_key != session_key
                or (state.last_event_at is not None and event_epoch - state.last_event_at > STALE_TICK_SEC)
                or (
                    cumulative_volume is not None and state.cumulative_volume is not None
                    and cumulative_volume < state.cumulative_volume
                )
            ):
                state = _SymbolState(session_key=session_key)
                self._states[symbol] = state
            if state.first_event_at is None:
                state.first_event_at = event_epoch
                state.first_received_at = now

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
                    state.last_volume_at = event_epoch
                    state.volume_rate_ewma = None
                    state.observed_notional = 0.0
                    state.observed_volume = 0.0
                    volume_velocity = None
                elif cumulative_volume > state.cumulative_volume:
                    volume_delta = cumulative_volume - state.cumulative_volume
                    elapsed = max(0.10, event_epoch - (state.last_volume_at or event_epoch))
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
                    state.last_volume_at = event_epoch
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
                state.imbalance_received_since = None
                state.imbalance_anchor_price = None
            elif imbalance_side != state.imbalance_side:
                state.imbalance_side = imbalance_side
                state.imbalance_since = event_epoch
                state.imbalance_received_since = now
                state.imbalance_anchor_price = price

            imbalance_persistence = (
                max(0.0, min(
                    event_epoch - state.imbalance_since,
                    now - state.imbalance_received_since,
                ))
                if state.imbalance_since is not None and state.imbalance_received_since is not None and state.imbalance_side
                else 0.0
            )
            imbalance_price_response = (
                ((price - state.imbalance_anchor_price) / state.imbalance_anchor_price) * 100
                if price is not None and state.imbalance_anchor_price
                else None
            )

            state.samples += 1
            state.last_received_at = now
            state.last_event_at = event_epoch
            state.last_tick_fingerprint = fingerprint
            if price is not None:
                state.last_price = price

            samples = state.samples
            observed_volume = state.observed_volume
            persistent_imbalance_side = state.imbalance_side
            observed_notional = state.observed_notional
            observation_span = max(0.0, min(
                event_epoch - state.first_event_at,
                now - state.first_received_at,
            ))

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
            "receivedAtEpoch": now,
            # Keep raw precision for risk gates; round only display fields.
            "price": price,
            "previousPrice": previous_price,
            "priceChangePct": round(price_change_pct, 4) if price_change_pct is not None else None,
            "cumulativeVolume": int(cumulative_volume) if cumulative_volume is not None else None,
            "volumeDelta": int(volume_delta) if volume_delta is not None else None,
            "volumeVelocityPerMin": round(volume_velocity, 2) if volume_velocity is not None else None,
            "volumeAcceleration": round(volume_acceleration, 3) if volume_acceleration is not None else None,
            "sessionVwapApprox": round(session_vwap, 4) if session_vwap is not None else None,
            "vwapDistancePct": round(vwap_distance_pct, 4) if vwap_distance_pct is not None else None,
            "observedVwapVolume": int(observed_volume),
            "observedNotional": round(observed_notional, 2),
            "observationSpanSec": round(observation_span, 3),
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

    ``score`` is an evidence score, not a probability.  Price-only observations
    remain visible as WAIT, never as executable footprint confirmation.
    """
    evidence: list[str] = []
    risks: list[str] = []
    side = _direction_side(context.get("direction"))
    direction = "bearish" if side < 0 else "bullish"
    price = _positive_float(features.get("price"))
    entry = _positive_float(context.get("entry"))
    stop = _positive_float(context.get("stop"))
    target = _positive_float(context.get("target"))
    candle = context.get("candle_setup")
    if isinstance(candle, dict) and str(candle.get("status") or "").upper() == "READY":
        # A book-derived plan owns its actual trigger/invalidation; a cached
        # scanner plan must not quietly bypass them.
        if "trigger" in candle:
            entry = _positive_float(candle.get("trigger"))
        if "invalidation" in candle:
            stop = _positive_float(candle.get("invalidation"))
        if "target" in candle:
            target = _positive_float(candle.get("target"))
    trigger_tolerance = max(0.1, float(context.get("trigger_tolerance_pct") or 1.5))
    max_chase = max(trigger_tolerance, float(context.get("max_chase_pct") or 5.0))
    requires_full_tick = bool(context.get("requires_full_tick"))
    min_confirmations = max(2, min(4, int(context.get("live_min_confirmations") or 2)))
    structure_valid = context.get("structure_valid") is not False
    structure_reasons = list(context.get("structure_reasons") or [])
    quality = dict(features.get("dataQuality") or {})
    score = 20
    minimum_rr = max(
        MIN_LIVE_RR,
        _positive_float(context.get("min_live_rr") or context.get("min_risk_reward")) or MIN_LIVE_RR,
    )
    live_risk = side * (price - stop) if price is not None and stop is not None else None
    live_reward = side * (target - price) if price is not None and target is not None else None
    live_rr = (
        live_reward / live_risk
        if live_reward is not None and live_risk is not None and live_risk > 0
        else None
    )
    live_risk_pct = live_risk / price * 100 if live_risk is not None and price else None

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
            "liveEntryPrice": price,
            "plannedEntry": entry,
            "invalidation": stop,
            "target": target,
            "liveRiskReward": round(live_rr, 4) if live_rr is not None else None,
            "liveRiskPct": round(live_risk_pct, 4) if live_risk_pct is not None else None,
            "liveRiskPerShare": round(live_risk, 4) if live_risk is not None else None,
            "minimumRiskReward": minimum_rr,
            "candle_setup": candle,
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
                    "observedNotional",
                    "observationSpanSec",
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
                    "receivedAtEpoch",
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

    if candle is not None:
        if not isinstance(candle, dict):
            risks.append("Candle setup is malformed; a valid closed-candle plan is required.")
            return result("REJECT", "CANDLE_GATE")
        candle_status = str(candle.get("status") or "").upper()
        if candle_status == "INVALID":
            risks.extend(candle.get("reasons") or ["The candle setup is invalidated."])
            return result("REJECT", "CANDLE_GATE")
        if candle_status != "READY" or candle.get("entry_ready") is not True:
            risks.extend(candle.get("reasons") or ["Waiting for a completed-candle setup and follow-through."])
            return result("WAIT", "CANDLE_GATE")
        if (
            candle.get("confirmation_required") is True
            and candle.get("confirmed") is not True
        ):
            risks.append("The candle setup still requires closed-candle confirmation.")
            return result("WAIT", "CANDLE_GATE")
        if candle.get("direction") not in {"bullish", "bearish"} or candle["direction"] != direction:
            risks.append("Candle direction is missing or conflicts with the scanner plan.")
            return result("REJECT", "CANDLE_GATE")
        if "trigger" not in candle or "invalidation" not in candle:
            risks.append("A ready candle setup must define its trigger and invalidation.")
            return result("REJECT", "CANDLE_GATE")
        candle_timing_risks = _candle_timing_risks(
            candle, _positive_float(features.get("receivedAtEpoch")) or time.time(),
        )
        if candle_timing_risks:
            risks.extend(candle_timing_risks)
            return result("REJECT", "CANDLE_FRESHNESS_GATE")

    if context.get("direction") not in {"bullish", "bearish"}:
        risks.append("An explicit bullish or bearish trade direction is required.")
        return result("REJECT", "INVALID_INPUT")
    if price is None or entry is None or stop is None or target is None:
        risks.append("Finite positive live price, planned entry, stop and target are mandatory.")
        return result("REJECT", "INVALID_INPUT")
    if side * (entry - stop) <= 0 or side * (target - entry) <= 0:
        risks.append("Entry, stop and target do not form a valid directional trade plan.")
        return result("REJECT", "STRUCTURE_GATE")
    rejection = features.get("tickRejectedReason")
    if rejection == "MISSING_TIMESTAMP":
        risks.append("Waiting for a verifiable quote timestamp; receipt time cannot prove quote freshness.")
        return result("WAIT", "MISSING_TIMESTAMP")
    if rejection == "DUPLICATE_TICK":
        risks.append("Repeated quote contains no new evidence; waiting for a fresh tick.")
        return result("WAIT", "DUPLICATE_TICK")
    if rejection == "OUT_OF_ORDER_TICK":
        risks.append("Out-of-order quote ignored; it cannot update live evidence.")
        return result("REJECT", "OUT_OF_ORDER_TICK")
    if features.get("isFresh") is not True:
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
    if candle is not None and directional_distance < 0:
        evidence.append("Waiting for price to reclaim/cross the completed-candle trigger.")
        return result("WAIT", "CANDLE_TRIGGER_GATE")
    if live_rr is None or live_rr < minimum_rr:
        risks.append(
            f"Current-price reward:risk is {live_rr:.2f}, below {minimum_rr:.2f}; wait for a better entry."
            if live_rr is not None else "Current-price reward:risk is unavailable."
        )
        return result("WAIT", "LIVE_RISK_GATE")
    risk_cap = _positive_float(context.get("max_structural_risk_pct"))
    if context.get("enforce_structural_risk_cap") and risk_cap and live_risk_pct > risk_cap:
        risks.append(f"Current-price risk {live_risk_pct:.2f}% exceeds the {risk_cap:.2f}% plan cap.")
        return result("WAIT", "LIVE_RISK_GATE")
    score += 15
    evidence.append("Price is inside the planned entry band.")
    evidence.append(f"Current-price reward:risk is {live_rr:.2f}; stop is unchanged.")

    available = set(quality.get("availableFields") or [])
    has_volume = "cumulative_volume" in available
    has_depth = "market_depth" in available
    has_vwap = features.get("sessionVwapApprox") is not None
    footprint_available = has_volume or has_depth

    if not footprint_available:
        if requires_full_tick:
            risks.append(
                "This setup requires fresh volume/depth evidence; price-only data cannot confirm it."
            )
            return result("WAIT", "FULL_TICK_REQUIRED")
        risks.append("Price reached the entry area, but executed-volume evidence is unavailable.")
        return result("WAIT", "PRICE_ONLY_WATCH")
    if requires_full_tick and not (has_volume and has_depth):
        risks.append("This setup requires both executed volume and market depth; partial quotes remain watch-only.")
        return result("WAIT", "FULL_TICK_REQUIRED")

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

    # Presence of fields is not evidence of a tradable move.  Require multiple
    # fresh samples across time and actual volume, not one-share VWAP or depth
    # alone.  These conservative engineering defaults require empirical tuning.
    observation_span = _nonnegative_float(features.get("observationSpanSec")) or 0.0
    sample_count = _nonnegative_float(features.get("sampleCount")) or 0.0
    observed_volume = _nonnegative_float(features.get("observedVwapVolume")) or 0.0
    observed_notional = _nonnegative_float(features.get("observedNotional")) or 0.0
    timestamp_verified = "exchange_timestamp" in available
    if not timestamp_verified:
        risks.append("A verifiable market timestamp is required for footprint confirmation.")
        return result("WAIT", "MISSING_TIMESTAMP")
    if sample_count < MIN_VALID_SAMPLES or observation_span < MIN_OBSERVATION_SEC:
        risks.append(
            f"Live evidence warming up: requires {MIN_VALID_SAMPLES} fresh samples over "
            f"at least {MIN_OBSERVATION_SEC:g}s."
        )
        return result("WAIT", "EVIDENCE_WARMUP")
    if not has_volume or observed_volume < MIN_OBSERVED_VOLUME or observed_notional < MIN_OBSERVED_NOTIONAL:
        risks.append("Waiting for sufficient executed-volume evidence; displayed depth alone cannot confirm entry.")
        return result("WAIT", "EXECUTED_VOLUME_REQUIRED")

    # Two independent pieces of live evidence are required whenever the feed
    # supplies footprint fields.  A persistent depth signal must show response;
    # a volume-only signal must hold observed VWAP and show acceleration/response.
    confirmation_count = sum((vwap_aligned, flow_aligned, volume_impulse, price_responding))
    # A one-tick price change and its own tiny VWAP are correlated, so require
    # actual accelerating trades or persistent depth response as well.
    if (
        confirmation_count >= min_confirmations
        and (vwap_aligned or flow_aligned)
        and (volume_impulse or flow_aligned)
    ):
        return result("CONFIRMED", "FULL_TICK" if has_depth else "EXECUTED_VOLUME")

    if vwap_opposed and flow_opposed:
        risks.append("VWAP and persistent depth both oppose the setup.")
        return result("REJECT", "ORDER_FLOW_GATE")

    risks.append(
        f"Live footprint has not produced {min_confirmations} independent confirmations yet."
    )
    return result("WAIT", "FULL_TICK" if has_depth else "EXECUTED_VOLUME")
