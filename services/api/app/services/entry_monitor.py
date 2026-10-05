"""Real-Time Entry Monitor
==========================
Watches live ticks for stocks that have an active entry level.  Price reaching
the planned level is necessary but, when full ticks are available, volume
velocity, observed VWAP, persistent depth and price response must confirm it
before an alert is fired.

The monitor is a lightweight in-process singleton that piggy-backs on
the existing LivePriceFeed WebSocket.  No extra threads are created —
it simply registers a callback.

Usage:
    from app.services.entry_monitor import get_entry_monitor
    monitor = get_entry_monitor()
    monitor.start(scan_payload)        # called after every background scan
    alerts = monitor.get_live_entries()
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from app.services.realtime_features import RealtimeFeatureEngine, classify_live_trigger

logger = logging.getLogger(__name__)

# ── Tuneable constants (all env-overridable) ───────────────────────────────────
import os
ENTRY_TRIGGER_PCT   = float(os.getenv("ENTRY_TRIGGER_PCT",   "1.5"))   # ±1.5% of entry = "at entry"
ALERT_COOLDOWN_SEC  = max(0, int(os.getenv("ENTRY_ALERT_COOLDOWN", "60")))
LIVE_ENTRIES_MAX    = max(1, int(os.getenv("LIVE_ENTRIES_MAX", "100")))
LIVE_WATCH_MAX      = max(1, int(os.getenv("LIVE_WATCH_MAX", str(LIVE_ENTRIES_MAX))))
LIVE_ENTRIES_TTL    = max(1, int(os.getenv("LIVE_ENTRIES_TTL_SEC", "1800")))
LIVE_ENTRY_FRESH_SEC = max(1.0, float(os.getenv("LIVE_TICK_STALE_SEC", "15")))
LIVE_NOTIFICATION_QUEUE_MAX = max(1, int(os.getenv("LIVE_NOTIFICATION_QUEUE_MAX", "256")))


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def _entry_level(item: Dict) -> float | None:
    """Return the actionable entry price for a scanned item."""
    candle = item.get("candle_setup")
    if isinstance(candle, dict) and candle.get("status") == "READY" and "trigger" in candle:
        return _safe_float(candle.get("trigger"))
    return (
        _safe_float(item.get("safe_entry_price"))
        or _safe_float(item.get("entry_trigger"))
        or _safe_float(item.get("entryTrigger"))
        or _safe_float(item.get("breakoutTrigger"))
    )


def _stop_loss(item: Dict) -> float | None:
    candle = item.get("candle_setup")
    if isinstance(candle, dict) and candle.get("status") == "READY" and "invalidation" in candle:
        return _safe_float(candle.get("invalidation"))
    return (
        _safe_float(item.get("structural_invalidation"))
        or _safe_float(item.get("invalidation_level"))
        or _safe_float(item.get("stop_loss"))
        or _safe_float(item.get("stopLoss"))
        or _safe_float(item.get("stoploss"))
        or _safe_float(item.get("invalidation"))
    )


def _target(item: Dict) -> float | None:
    candle = item.get("candle_setup")
    if isinstance(candle, dict) and candle.get("status") == "READY" and "target" in candle:
        return _safe_float(candle.get("target"))
    return (
        _safe_float(item.get("new_target"))
        or _safe_float(item.get("target_1"))
        or _safe_float(item.get("target_price"))
        or _safe_float(item.get("target"))
    )


def _normalise_direction(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"bearish", "sell", "short", "down"}:
        return "bearish"
    if normalized in {"bullish", "buy", "long", "up"}:
        return "bullish"
    return "neutral"


def _plan_direction(item: Dict, entry: float, stop: float | None, target: float | None) -> str:
    """Infer only missing legacy directions from an unambiguous trade plan."""

    direction = _normalise_direction(item.get("direction"))
    if direction != "neutral":
        return direction
    # An explicit neutral direction is never promoted.
    if "direction" in item and str(item.get("direction") or "").strip():
        return "neutral"
    action = str(item.get("action") or item.get("recommended_action") or "").upper()
    if action in {"BUY", "LONG", "REENTRY_BUY", "ALERT_ABOVE_LEVEL"}:
        return "bullish"
    if action in {"SELL", "SHORT", "ALERT_BELOW_LEVEL"}:
        return "bearish"
    if stop is not None and target is not None:
        if stop < entry < target:
            return "bullish"
        if target < entry < stop:
            return "bearish"
    return "neutral"


def _structure_gate(item: Dict, entry: float, stop: float | None, target: float | None) -> tuple[bool, List[str]]:
    """Reject invalidated and impossible plans; candle-watch rows stay observed."""
    direction = _normalise_direction(item.get("direction"))
    side = -1 if direction == "bearish" else 1
    reasons: List[str] = []
    if direction == "neutral":
        reasons.append("Neutral setup has no actionable trade direction.")
    candle = item.get("candle_setup")
    candle_pending = isinstance(candle, dict) and candle.get("status") in {"WAIT", "WATCH", "UNAVAILABLE"}
    if not candle_pending and (stop is None or target is None):
        reasons.append("A finite positive stop and target are mandatory before live entry.")
    state_values = {
        str(item.get(key) or "").strip().upper().replace(" ", "_")
        for key in ("status", "lifecycle_state", "tracking_status", "signal_stage", "action")
    }
    invalid_states = {
        "INVALID", "INVALIDATED", "EXPIRED", "CANCELLED", "FAILED",
        "AVOID", "NO_TRADE", "EXIT", "WAIT_FOR_PULLBACK",
        "AVOID_LATE_ENTRY", "AVOID_CHASE", "PROFIT_BOOKING_RISK",
        "WAIT_FOR_BETTER_ENTRY",
        "BLOCKED_BUY",
    }
    if state_values & invalid_states:
        reasons.append("Scanner lifecycle marks this setup as invalid/non-actionable.")
    requires_confirmation = bool(item.get("requires_live_confirmation"))
    if item.get("blockedBuyReason"):
        reasons.append(str(item.get("blockedBuyReason")))
    if item.get("overextended_fresh_entry") or item.get("chase_risk"):
        reasons.append("Scanner marks this plan as overextended/chase risk.")
    if item.get("entry_plan_blocked"):
        reasons.append(
            str(item.get("entry_plan_blocked_reason") or "Structural trade plan is watch-only.")
        )
    if item.get("attention_only") and not requires_confirmation:
        reasons.append("Attention-only row is not armed for live confirmation.")
    if direction == "bullish" and item.get("allow_buy_call") is False and not requires_confirmation:
        reasons.append("Fresh bullish entry is blocked by the scanner gate.")
    if stop is not None and side * (entry - stop) <= 0:
        reasons.append("Stop/invalidation is on the wrong side of entry.")
    if target is not None and side * (target - entry) <= 0:
        reasons.append("Target is on the wrong side of entry.")
    if item.get("enforce_structural_risk_cap") and stop is not None:
        risk_pct = abs(entry - stop) / entry * 100
        try:
            max_risk_pct = float(item.get("max_structural_risk_pct") or 4.0)
        except (TypeError, ValueError):
            max_risk_pct = 4.0
        if risk_pct > max_risk_pct:
            reasons.append(
                f"Structural stop is {risk_pct:.2f}% away, above the {max_risk_pct:.2f}% live-arm cap."
            )
    return not reasons, reasons


class EntryMonitor:
    """Thread-safe singleton that tracks when live prices reach entry levels."""

    def __init__(self, feature_engine: RealtimeFeatureEngine | None = None):
        self._lock = threading.Lock()
        # symbol -> scanner structure and trade plan
        self._watched: Dict[str, Dict] = {}
        # Ordered symbols plus O(1) symbol lookup keep the tick path bounded.
        self._live_entries: deque[str] = deque(maxlen=LIVE_ENTRIES_MAX)
        self._live_by_symbol: Dict[str, Dict] = {}
        self._latest_assessments: Dict[str, Dict] = {}
        # symbol -> last alert epoch
        self._last_alerted: Dict[str, float] = {}
        self._features = feature_engine or RealtimeFeatureEngine()
        self._running = False
        self._subscribers: list[Callable] = []
        # Confirmation consumers may perform network/database I/O.  Never run
        # them on the broker WebSocket callback thread.
        self._notification_queue: queue.Queue[Dict] = queue.Queue(
            maxsize=LIVE_NOTIFICATION_QUEUE_MAX
        )
        self._notification_worker_lock = threading.Lock()
        self._notification_worker: threading.Thread | None = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def subscribe(self, callback: Callable[[Dict], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def start(self, scan_payload: Dict) -> None:
        """Register all eligible stocks from the latest scan result.

        Called after every background scan.  Rebuilds the watch-list.
        """
        self._register_watchlist(scan_payload)
        with self._lock:
            should_attach = not self._running
            self._running = True
        if should_attach:
            self._attach_live_feed()

    def update_price(self, symbol: str, price: float) -> None:
        """Compatibility path for callers that only publish LTP."""
        self.update_tick(symbol, {"ltp": price})

    def update_tick(self, symbol: str, tick: Dict) -> Dict | None:
        """Consume the complete broker tick and evaluate the live entry gate."""
        symbol = symbol.upper()
        with self._lock:
            watched = self._watched.get(symbol)
        if not watched:
            return None

        features = self._features.update(symbol, tick)
        assessment = classify_live_trigger(
            features,
            {
                **watched,
                "trigger_tolerance_pct": ENTRY_TRIGGER_PCT,
                "max_chase_pct": 5.0,
            },
        )
        assessment_snapshot = {
            "symbol": symbol,
            "entryLevel": watched["entry"],
            "entry_trigger": watched["entry"],
            "direction": watched["direction"],
            "stopLoss": watched.get("stop"),
            "stop_loss": watched.get("stop"),
            "target": watched.get("target"),
            "target_1": watched.get("target"),
            "setupType": watched.get("setup_type"),
            "signalStage": watched.get("signal_stage"),
            "confidence": watched.get("confidence"),
            "rr": assessment.get("liveRiskReward"),
            "risk_reward": assessment.get("liveRiskReward"),
            "plannedRiskReward": watched.get("rr"),
            "riskPct": assessment.get("liveRiskPct"),
            "riskPerShare": assessment.get("liveRiskPerShare"),
            "maxPositionPctAt1PctAccountRisk": self._position_cap(assessment),
            "timeHorizon": watched.get("time_horizon"),
            "marketMood": watched.get("market_mood"),
            "counterRegime": watched.get("counter_regime", False),
            "requiresFullTick": watched.get("requires_full_tick", False),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "updatedAtEpoch": time.time(),
            "lastTickAtEpoch": time.time(),
            **assessment,
        }
        with self._lock:
            if self._watched.get(symbol) is not watched:
                # A scan replaced this plan during feature computation.
                # Only a tick evaluated against the current plan may publish.
                return self._latest_assessments.get(symbol)
            self._latest_assessments[symbol] = assessment_snapshot

        price = _safe_float(features.get("price"))
        self._check_entry(symbol, price, watched, assessment)
        return assessment_snapshot

    def get_live_entries(self) -> List[Dict]:
        """Return currently confirmed entries; WAIT/REJECT rows stay hidden."""
        now = time.time()
        with self._lock:
            self._expire_stale_confirmations_locked(now)
            return [
                dict(self._live_by_symbol[symbol])
                for symbol in list(self._live_entries)
                if symbol in self._live_by_symbol
                and self._live_by_symbol[symbol].get("liveTriggerStatus", "CONFIRMED") == "CONFIRMED"
                and now - self._live_by_symbol[symbol]["detected_at_epoch"] < LIVE_ENTRIES_TTL
            ]

    def get_live_assessments(self) -> List[Dict]:
        """Expose the latest CONFIRMED/WAIT/REJECT decision for diagnostics/UI."""
        with self._lock:
            self._expire_stale_confirmations_locked(time.time())
            return [dict(value) for value in self._latest_assessments.values()]

    def get_symbol_assessment(self, symbol: str) -> Dict | None:
        with self._lock:
            self._expire_stale_confirmations_locked(time.time())
            value = self._latest_assessments.get(symbol.upper())
            return dict(value) if value else None

    def get_watched_count(self) -> int:
        with self._lock:
            return len(self._watched)

    def get_watched_symbols(self) -> List[str]:
        with self._lock:
            return list(self._watched.keys())

    # ── Internal ───────────────────────────────────────────────────────────────

    def _expire_stale_confirmations_locked(self, now: float) -> None:
        """Downgrade every decision when its broker evidence is no longer fresh."""

        for assessment in self._latest_assessments.values():
            last_tick = float(
                assessment.get("lastTickAtEpoch")
                or assessment.get("updatedAtEpoch")
                or 0
            )
            age = max(0.0, now - last_tick)
            assessment["lastTickAgeSec"] = round(age, 1)
            if age <= LIVE_ENTRY_FRESH_SEC:
                continue
            if assessment.get("confirmationMode") == "STALE_AFTER_CONFIRMATION":
                continue
            risks = list(assessment.get("risks") or [])
            stale_risk = f"No fresh broker tick for {age:.1f}s; live decision is stale."
            if not any("live decision is stale" in str(risk) for risk in risks):
                risks.append(stale_risk)
            assessment.update(
                {
                    "status": "REJECT",
                    "confirmationMode": "STALE_FEED",
                    "risks": risks,
                    "isFresh": False,
                    "staleDetectedAtEpoch": assessment.get("staleDetectedAtEpoch") or now,
                }
            )

        for symbol, alert in self._live_by_symbol.items():
            if alert.get("liveTriggerStatus") != "CONFIRMED":
                continue
            last_tick = float(alert.get("lastTickAtEpoch") or alert.get("detected_at_epoch") or 0)
            age = now - last_tick
            if age <= LIVE_ENTRY_FRESH_SEC:
                continue
            confirmation = dict(alert.get("liveConfirmation") or {})
            risks = list(confirmation.get("risks") or [])
            stale_risk = f"No fresh broker tick for {age:.1f}s; confirmation is no longer actionable."
            if not any("confirmation is no longer actionable" in str(risk) for risk in risks):
                risks.append(stale_risk)
            confirmation.update(
                {
                    "status": "REJECT",
                    "confirmationMode": "STALE_AFTER_CONFIRMATION",
                    "risks": risks,
                }
            )
            alert.update(
                {
                    "liveTriggerStatus": "REJECT",
                    "label": "STALE - WAIT FOR FRESH CONFIRMATION",
                    "liveConfirmation": confirmation,
                    "isFresh": False,
                }
            )
            assessment = dict(self._latest_assessments.get(symbol) or {})
            assessment.update(confirmation)
            assessment["lastTickAgeSec"] = round(age, 1)
            assessment["staleDetectedAtEpoch"] = assessment.get("staleDetectedAtEpoch") or now
            self._latest_assessments[symbol] = assessment

    def _register_watchlist(self, scan_payload: Dict) -> None:
        """Build watch-list from every item in the scan results."""
        # MarketHub may publish an explicitly capped live pool.  Treat its
        # presence as an allow-list so flattening other UI buckets cannot grow
        # the WebSocket subscription back to the whole scan universe.
        allow_symbols: set[str] | None = None
        if "live_candidate_symbols" in scan_payload or "live_candidate_pool" in scan_payload:
            allow_symbols = set()
            for value in scan_payload.get("live_candidate_symbols") or []:
                symbol = value.get("symbol") if isinstance(value, dict) else value
                if symbol:
                    allow_symbols.add(str(symbol).upper())
            for value in scan_payload.get("live_candidate_pool") or []:
                symbol = value.get("symbol") if isinstance(value, dict) else value
                if symbol:
                    allow_symbols.add(str(symbol).upper())

        # MarketHub's all_entry_levels is the authoritative post-gate arm list.
        # If it is present (even empty), never resurrect a rejected setup from
        # an older display bucket such as top_opportunities/blockedBuys.
        if "all_entry_levels" in scan_payload:
            source_keys = ("all_entry_levels",)
        else:
            source_keys = (
                "results", "top_opportunities", "pre_breakout_setups",
                "alert_above_setups", "retest_entry", "momentum_continuation",
                "candidates", "pbsItems", "pre_registered", "live_candidate_pool",
            )

        # Flatten only the selected source(s) from the scan payload.
        all_results: List[Dict] = []
        for key in source_keys:
            items = scan_payload.get(key) or []
            if isinstance(items, list):
                all_results.extend(item for item in items if isinstance(item, dict))

        new_watched: Dict[str, Dict] = {}
        for item in all_results:
            sym = (item.get("symbol") or "").upper()
            if not sym:
                continue
            if sym in new_watched:
                continue
            if allow_symbols is not None and sym not in allow_symbols:
                continue
            entry = _entry_level(item)
            if not entry:
                continue
            stop = _stop_loss(item)
            target = _target(item)
            direction = _plan_direction(item, entry, stop, target)
            if direction == "neutral":
                continue
            side = -1 if direction == "bearish" else 1
            # Skip plans already more than 6% past entry in their trade direction.
            current = _safe_float(item.get("current_price") or item.get("price"))
            if current and side * ((current - entry) / entry) > 0.06:
                continue
            plan_item = {**item, "direction": direction}
            structure_valid, structure_reasons = _structure_gate(plan_item, entry, stop, target)
            if item.get("entry_plan_blocked") or (
                item.get("enforce_structural_risk_cap")
                and any("live-arm cap" in reason for reason in structure_reasons)
            ):
                # Keep these rows visible in scanner watchlists, but do not
                # spend a broker subscription slot on a non-armable plan.
                continue
            raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
            new_watched[sym] = {
                "entry": entry,
                "stop": stop,
                "target": target,
                "direction": direction,
                "setup_type": item.get("setup_type") or item.get("signal_stage", ""),
                "signal_stage": item.get("signal_stage", ""),
                "confidence": item.get("confidence") or item.get("score"),
                "rr": item.get("risk_reward") or item.get("rr"),
                "risk_pct": item.get("riskPct"),
                "risk_per_share": item.get("riskPerShare"),
                "max_position_pct_at_1pct_risk": item.get("maxPositionPctAt1PctAccountRisk"),
                "time_horizon": item.get("pre_breakout_timeframe") or item.get("timeHorizon") or "",
                "relative_volume": item.get("relative_volume") or raw.get("relative_volume"),
                "structure_valid": structure_valid,
                "structure_reasons": structure_reasons,
                "requires_full_tick": bool(
                    item.get("requires_full_tick") or item.get("requires_live_confirmation")
                ),
                "live_min_confirmations": item.get("live_min_confirmations"),
                "counter_regime": bool(item.get("counterRegime")),
                "market_mood": item.get("marketMood") or item.get("market_mood"),
                "candle_setup": item.get("candle_setup"),
                "min_live_rr": item.get("min_live_rr") or item.get("min_risk_reward"),
                "enforce_structural_risk_cap": bool(item.get("enforce_structural_risk_cap")),
                "max_structural_risk_pct": item.get("max_structural_risk_pct") or 4.0,
            }

        # StockScanner's pre-registration path can contain hundreds of rows and
        # does not publish MarketHub's explicit allow-list.  Preserve its ranked
        # input order but keep the real-time path capped (100 by default).
        if len(new_watched) > LIVE_WATCH_MAX:
            new_watched = dict(list(new_watched.items())[:LIVE_WATCH_MAX])

        with self._lock:
            for symbol in list(self._live_by_symbol):
                previous_plan = self._watched.get(symbol) or {}
                new_plan = new_watched.get(symbol) or {}
                candle = new_plan.get("candle_setup")
                plan_changed = any(
                    previous_plan.get(key) != new_plan.get(key)
                    for key in ("entry", "stop", "target", "direction", "candle_setup")
                )
                candle_pending = isinstance(candle, dict) and (
                    candle.get("status") != "READY" or candle.get("entry_ready") is not True
                )
                if not new_plan or plan_changed or not new_plan.get("structure_valid") or candle_pending:
                    # A new scan must not leave the old plan actionable until
                    # another tick happens to arrive.
                    self._live_by_symbol.pop(symbol, None)
                    self._latest_assessments.pop(symbol, None)
            self._live_entries = deque(
                (symbol for symbol in self._live_entries if symbol in self._live_by_symbol),
                maxlen=LIVE_ENTRIES_MAX,
            )
            self._watched = new_watched
            self._latest_assessments = {
                symbol: value
                for symbol, value in self._latest_assessments.items()
                if symbol in new_watched
            }
            self._last_alerted = {
                symbol: value
                for symbol, value in self._last_alerted.items()
                if symbol in new_watched or symbol in self._live_by_symbol
            }
        self._features.retain_symbols(new_watched)
        logger.info("[ENTRY_MONITOR] Watching %d symbols for entry triggers", len(new_watched))

    @staticmethod
    def _position_cap(assessment: Dict) -> float | None:
        risk_pct = _safe_float(assessment.get("liveRiskPct"))
        return round(min(100.0, 100.0 / risk_pct), 2) if risk_pct else None

    def _check_entry(self, symbol: str, price: float | None, watched: Dict, assessment: Dict) -> None:
        entry = watched["entry"]
        if not entry:
            return

        distance_pct = ((price - entry) / entry) * 100 if price is not None else None
        now = time.time()
        live_status = assessment.get("status", "WAIT")
        features = assessment.get("features") or {}

        # Do not leave a previously confirmed row actionable after the live
        # evidence changes to WAIT/REJECT.
        if live_status != "CONFIRMED":
            with self._lock:
                if self._watched.get(symbol) is not watched:
                    return
                existing = self._live_by_symbol.get(symbol)
                if existing:
                    existing.update({
                        "livePrice": round(price, 2) if price is not None else None,
                        "distancePct": round(distance_pct, 2) if distance_pct is not None else None,
                        "liveTriggerStatus": live_status,
                        "liveTriggerScore": assessment.get("score"),
                        "liveConfirmationMode": assessment.get("confirmationMode"),
                        "rr": assessment.get("liveRiskReward"),
                        "risk_reward": assessment.get("liveRiskReward"),
                        "riskPct": assessment.get("liveRiskPct"),
                        "riskPerShare": assessment.get("liveRiskPerShare"),
                        "liveRiskReward": assessment.get("liveRiskReward"),
                        "liveRiskPct": assessment.get("liveRiskPct"),
                        "liveRiskPerShare": assessment.get("liveRiskPerShare"),
                        "minimumRiskReward": assessment.get("minimumRiskReward"),
                        "maxPositionPctAt1PctAccountRisk": self._position_cap(assessment),
                        "candle_setup": assessment.get("candle_setup"),
                        "liveEvidence": list(assessment.get("evidence") or []),
                        "liveRisks": list(assessment.get("risks") or []),
                        "liveDataQuality": assessment.get("dataQuality") or {},
                        "isFresh": features.get("isFresh"),
                        "tickAgeSec": features.get("tickAgeSec"),
                        "liveConfirmation": assessment,
                        "label": f"{live_status} — LIVE GATE",
                        "lastTickAtEpoch": now,
                    })
            return

        if price is None or distance_pct is None:
            return

        alert = {
            "symbol": symbol,
            "livePrice": round(price, 2),
            "entryLevel": round(entry, 2),
            "distancePct": round(distance_pct, 2),
            "stopLoss": watched["stop"],
            "target": watched["target"],
            "direction": watched["direction"],
            "setupType": watched["setup_type"],
            "signalStage": watched["signal_stage"],
            "confidence": watched["confidence"],
            "stop_loss": watched["stop"],
            "target_1": watched["target"],
            "rr": assessment.get("liveRiskReward"),
            "risk_reward": assessment.get("liveRiskReward"),
            "plannedRiskReward": watched.get("rr"),
            "riskPct": assessment.get("liveRiskPct"),
            "riskPerShare": assessment.get("liveRiskPerShare"),
            "liveRiskReward": assessment.get("liveRiskReward"),
            "liveRiskPct": assessment.get("liveRiskPct"),
            "liveRiskPerShare": assessment.get("liveRiskPerShare"),
            "minimumRiskReward": assessment.get("minimumRiskReward"),
            "candle_setup": watched.get("candle_setup"),
            "maxPositionPctAt1PctAccountRisk": self._position_cap(assessment),
            "timeHorizon": watched["time_horizon"],
            "relative_volume": watched["relative_volume"],
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "detected_at_epoch": now,
            "lastTickAtEpoch": now,
            "label": "🟢 LIVE ENTRY CONFIRMED",
            "liveTriggerStatus": live_status,
            "liveTriggerScore": assessment.get("score"),
            "liveTriggerScoreMeaning": assessment.get("scoreMeaning"),
            "liveConfirmationMode": assessment.get("confirmationMode"),
            "liveEvidence": list(assessment.get("evidence") or []),
            "liveRisks": list(assessment.get("risks") or []),
            "liveDataQuality": assessment.get("dataQuality") or {},
            "liveConfirmation": assessment,
            "cumulativeVolume": features.get("cumulativeVolume"),
            "sessionVwapApprox": features.get("sessionVwapApprox"),
            "vwapDistancePct": features.get("vwapDistancePct"),
            "observedVwapVolume": features.get("observedVwapVolume"),
            "volumeDelta": features.get("volumeDelta"),
            "volumeVelocityPerMin": features.get("volumeVelocityPerMin"),
            "volumeAcceleration": features.get("volumeAcceleration"),
            "bidAskRatio": features.get("bidAskRatio"),
            "imbalanceSide": features.get("imbalanceSide"),
            "imbalancePersistenceSec": features.get("imbalancePersistenceSec"),
            "imbalancePriceResponsePct": features.get("imbalancePriceResponsePct"),
            "priceChangePct": features.get("priceChangePct"),
            "tickAgeSec": features.get("tickAgeSec"),
            "isFresh": features.get("isFresh"),
            "liveSampleCount": features.get("sampleCount"),
            "participantIdentity": "UNKNOWN_FROM_MARKET_TICK",
        }

        with self._lock:
            if self._watched.get(symbol) is not watched:
                return
            previous = self._live_by_symbol.get(symbol)
            if previous is not None:
                previous.update(alert)
            else:
                if len(self._live_entries) >= LIVE_ENTRIES_MAX:
                    evicted = self._live_entries.pop()
                    self._live_by_symbol.pop(evicted, None)
                self._live_entries.appendleft(symbol)
                self._live_by_symbol[symbol] = alert
            last = self._last_alerted.get(symbol, 0)
            should_notify = now - last >= ALERT_COOLDOWN_SEC
            if should_notify:
                self._last_alerted[symbol] = now

        if not should_notify:
            return  # Keep the UI row fresh but suppress Telegram/WS spam.

        logger.info(
            "[ENTRY_MONITOR] %s live entry confirmed @ %.2f | plan %.2f | distance %.2f%% | mode=%s",
            symbol, price, entry, distance_pct, assessment.get("confirmationMode"),
        )
        self._enqueue_notification(alert)

    def _enqueue_notification(self, alert: Dict) -> None:
        """Queue slow Telegram/WebSocket/persistence work without blocking ticks."""

        try:
            self._notification_queue.put_nowait(dict(alert))
        except queue.Full:
            logger.error(
                "[ENTRY_MONITOR] Notification queue full; dropped alert for %s",
                alert.get("symbol"),
            )
            return

        with self._notification_worker_lock:
            worker = self._notification_worker
            if worker is None or not worker.is_alive():
                self._notification_worker = threading.Thread(
                    target=self._notification_loop,
                    name="entry-notification-worker",
                    daemon=True,
                )
                self._notification_worker.start()

    def _notification_loop(self) -> None:
        """Drain notifications serially and exit after an idle interval."""

        while True:
            try:
                alert = self._notification_queue.get(timeout=1.0)
            except queue.Empty:
                with self._notification_worker_lock:
                    if self._notification_queue.empty():
                        self._notification_worker = None
                        return
                continue
            try:
                self._send_telegram(alert)
                with self._lock:
                    subscribers = list(self._subscribers)
                for callback in subscribers:
                    try:
                        callback(alert)
                    except Exception as exc:
                        logger.debug("Failed to notify entry subscriber: %s", exc)
            finally:
                self._notification_queue.task_done()

    def _wait_for_notifications(self, timeout: float = 2.0) -> bool:
        """Bounded test/diagnostic wait; production tick handling never calls it."""

        deadline = time.monotonic() + max(0.0, timeout)
        while self._notification_queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._notification_queue.unfinished_tasks == 0

    def _attach_live_feed(self) -> None:
        """Register callback with the AngelOne live price feed."""
        try:
            from app.services.angelone_live import get_angelone_live
            live_service = get_angelone_live()
            if live_service is not None and live_service.feed is not None:
                live_service.feed.subscribe(self._on_tick)
                logger.info("[ENTRY_MONITOR] Attached to AngelOne live feed")
        except Exception as exc:
            logger.warning("[ENTRY_MONITOR] Could not attach to live feed: %s", exc)

    def _on_tick(self, symbol: str, tick: Dict) -> None:
        # Preserve every field; incremental processing remains O(1).
        self.update_tick(symbol, tick)

    def _send_telegram(self, alert: Dict) -> None:
        try:
            from app.services.telegram_market_alerts import get_telegram_market_alerts
            tg = get_telegram_market_alerts()
            if tg and tg.enabled:
                sym = alert["symbol"]
                price = alert["livePrice"]
                entry = alert["entryLevel"]
                dist = alert["distancePct"]
                sl = alert["stopLoss"]
                tgt = alert["target"]
                rr = alert["rr"]
                horizon = alert["timeHorizon"]
                direction_emoji = "🟢" if alert["direction"] == "bullish" else "🔴"
                quality = (alert.get("liveDataQuality") or {}).get("quality", "UNKNOWN")
                live_score = alert.get("liveTriggerScore")
                live_mode = alert.get("liveConfirmationMode") or "UNKNOWN"
                vwap_distance = alert.get("vwapDistancePct")
                volume_velocity = alert.get("volumeVelocityPerMin")
                volume_acceleration = alert.get("volumeAcceleration")
                bid_ask = alert.get("bidAskRatio")
                persistence = alert.get("imbalancePersistenceSec")
                price_response = alert.get("imbalancePriceResponsePct")
                evidence = list(alert.get("liveEvidence") or [])[:2]

                footprint_parts = []
                if vwap_distance is not None:
                    footprint_parts.append(f"VWAP {float(vwap_distance):+.2f}%")
                if volume_velocity is not None:
                    volume_text = f"Vol {float(volume_velocity):,.0f}/min"
                    if volume_acceleration is not None:
                        volume_text += f" ({float(volume_acceleration):.2f}x accel)"
                    footprint_parts.append(volume_text)
                if bid_ask is not None:
                    depth_text = f"Depth {float(bid_ask):.2f}x"
                    if persistence is not None:
                        depth_text += f" / {float(persistence):.0f}s"
                    if price_response is not None:
                        depth_text += f" / response {float(price_response):+.2f}%"
                    footprint_parts.append(depth_text)

                lines = [
                    f"{direction_emoji} *{alert['label']}* — `{sym}`",
                    f"📍 Live Price: ₹{price}",
                    f"🎯 Safe Entry: ₹{entry} ({dist:+.1f}%)",
                    f"🛑 Stop Loss: ₹{sl}" if sl else "",
                    f"🏆 Target: ₹{tgt}" if tgt else "",
                    f"⚖️ R:R = {rr}" if rr else "",
                    f"⏳ {horizon}" if horizon else "",
                    f"📊 {alert.get('setupType', '')}",
                    (
                        f"⚡ Live Gate: {live_score}/100 evidence score "
                        f"({quality}, {live_mode})"
                        if live_score is not None
                        else f"⚡ Live Gate: {quality} / {live_mode}"
                    ),
                    f"🔎 {' | '.join(footprint_parts)}" if footprint_parts else "",
                    *(f"✅ {reason}" for reason in evidence),
                    "👤 Participant identity unknown from market tick — not FII/DII confirmation.",
                ]
                message = "\n".join(l for l in lines if l)
                tg.send_message(message)
        except Exception as exc:
            logger.debug("[ENTRY_MONITOR] Telegram send failed: %s", exc)


# ── Singleton ─────────────────────────────────────────────────────────────────
_monitor: EntryMonitor | None = None
_monitor_lock = threading.Lock()


def get_entry_monitor() -> EntryMonitor:
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = EntryMonitor()
    return _monitor
