"""Real-Time Entry Monitor
==========================
Watches live price ticks for all stocks that have an active entry level
(safe_entry_price / entry_trigger).  The moment a stock's live price
enters within ENTRY_TRIGGER_PCT of its planned entry, it is promoted to
the `live_entries` queue and (optionally) a Telegram alert is fired.

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
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ── Tuneable constants (all env-overridable) ───────────────────────────────────
import os
ENTRY_TRIGGER_PCT   = float(os.getenv("ENTRY_TRIGGER_PCT",   "1.5"))   # ±1.5% of entry = "at entry"
ALERT_COOLDOWN_SEC  = int(os.getenv("ENTRY_ALERT_COOLDOWN",  "60"))    # 60-sec gap between same-stock alerts
LIVE_ENTRIES_MAX    = int(os.getenv("LIVE_ENTRIES_MAX",       "100"))   # rolling window size
LIVE_ENTRIES_TTL    = int(os.getenv("LIVE_ENTRIES_TTL_SEC",   "1800"))  # keep entries visible for 30 min


def _safe_float(value: Any) -> float | None:
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _entry_level(item: Dict) -> float | None:
    """Return the actionable entry price for a scanned item."""
    return (
        _safe_float(item.get("safe_entry_price"))
        or _safe_float(item.get("entry_trigger"))
        or _safe_float(item.get("entryTrigger"))
        or _safe_float(item.get("breakoutTrigger"))
    )


def _stop_loss(item: Dict) -> float | None:
    return (
        _safe_float(item.get("invalidation_level"))
        or _safe_float(item.get("stop_loss"))
        or _safe_float(item.get("invalidation"))
    )


def _target(item: Dict) -> float | None:
    return (
        _safe_float(item.get("new_target"))
        or _safe_float(item.get("target_1"))
        or _safe_float(item.get("target_price"))
    )


class EntryMonitor:
    """Thread-safe singleton that tracks when live prices reach entry levels."""

    def __init__(self):
        self._lock = threading.Lock()
        # symbol -> { entry, stop, target, item_snapshot, setup_type, direction }
        self._watched: Dict[str, Dict] = {}
        # Rolling deque of fired alerts { symbol, price, entry, ... }
        self._live_entries: deque = deque(maxlen=LIVE_ENTRIES_MAX)
        # symbol -> last alert epoch
        self._last_alerted: Dict[str, float] = {}
        self._running = False
        self._subscribers: list[Callable] = []

    # ── Public API ─────────────────────────────────────────────────────────────

    def subscribe(self, callback: Callable[[Dict], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def start(self, scan_payload: Dict) -> None:
        """Register all eligible stocks from the latest scan result.

        Called after every background scan.  Rebuilds the watch-list.
        """
        self._register_watchlist(scan_payload)
        if not self._running:
            self._attach_live_feed()
            self._running = True

    def update_price(self, symbol: str, price: float) -> None:
        """Called by the live price feed callback for every tick."""
        with self._lock:
            watched = self._watched.get(symbol.upper())
        if not watched:
            return
        self._check_entry(symbol.upper(), price, watched)

    def get_live_entries(self) -> List[Dict]:
        """Return the most recent stocks that are *at their entry right now*."""
        now = time.time()
        with self._lock:
            return [
                e for e in list(self._live_entries)
                if now - e["detected_at_epoch"] < LIVE_ENTRIES_TTL
            ]

    def get_watched_count(self) -> int:
        with self._lock:
            return len(self._watched)

    def get_watched_symbols(self) -> List[str]:
        with self._lock:
            return list(self._watched.keys())

    # ── Internal ───────────────────────────────────────────────────────────────

    def _register_watchlist(self, scan_payload: Dict) -> None:
        """Build watch-list from every item in the scan results."""
        # Flatten all results from the scan payload
        all_results: List[Dict] = []
        for key in ("all_entry_levels", "results", "top_opportunities", "pre_breakout_setups",
                    "alert_above_setups", "retest_entry", "momentum_continuation",
                    "candidates", "pbsItems"):
            items = scan_payload.get(key) or []
            if isinstance(items, list):
                all_results.extend(items)

        new_watched: Dict[str, Dict] = {}
        for item in all_results:
            sym = (item.get("symbol") or "").upper()
            if not sym:
                continue
            entry = _entry_level(item)
            if not entry:
                continue
            # Skip stocks that have already gone way too far (> 6% above entry)
            current = _safe_float(item.get("current_price") or item.get("price"))
            if current and current > entry * 1.06:
                continue
            new_watched[sym] = {
                "entry": entry,
                "stop": _stop_loss(item),
                "target": _target(item),
                "direction": item.get("direction", "bullish"),
                "setup_type": item.get("setup_type") or item.get("signal_stage", ""),
                "signal_stage": item.get("signal_stage", ""),
                "confidence": item.get("confidence") or item.get("score"),
                "rr": item.get("risk_reward") or item.get("rr"),
                "time_horizon": item.get("pre_breakout_timeframe") or item.get("timeHorizon") or "",
            }

        with self._lock:
            self._watched = new_watched
        logger.info("[ENTRY_MONITOR] Watching %d symbols for entry triggers", len(new_watched))

    def _check_entry(self, symbol: str, price: float, watched: Dict) -> None:
        entry = watched["entry"]
        if not entry:
            return

        distance_pct = ((price - entry) / entry) * 100
        
        # We only care if it's at entry (down to -1.5%) or flying away (up to +5%)
        if distance_pct < -ENTRY_TRIGGER_PCT or distance_pct > 5.0:
            return

        label = "🔴 AT ENTRY NOW"
        if distance_pct > ENTRY_TRIGGER_PCT:
            label = "🚀 FLYING / CHASE"


        now = time.time()
        last = self._last_alerted.get(symbol, 0)
        if now - last < ALERT_COOLDOWN_SEC:
            return  # Cooldown — don't spam

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
            "rr": watched["rr"],
            "timeHorizon": watched["time_horizon"],
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "detected_at_epoch": now,
            "label": label,
        }

        with self._lock:
            # Remove duplicate if same symbol already queued
            existing = [e for e in self._live_entries if e["symbol"] != symbol]
            self._live_entries.clear()
            self._live_entries.extend(existing)
            self._live_entries.appendleft(alert)  # newest first
            self._last_alerted[symbol] = now

        logger.info(
            "[ENTRY_MONITOR] %s hit entry ₹%.2f | live ₹%.2f | distance %.2f%%",
            symbol, entry, price, distance_pct,
        )
        self._send_telegram(alert)

        # Notify event subscribers (e.g. WebSockets)
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(alert)
            except Exception as exc:
                logger.debug("Failed to notify entry subscriber: %s", exc)

    def _attach_live_feed(self) -> None:
        """Register callback with the AngelOne live price feed."""
        try:
            from app.services.angelone_live import live_price_feed
            if live_price_feed is not None:
                live_price_feed.subscribe(self._on_tick)
                logger.info("[ENTRY_MONITOR] Attached to AngelOne live feed")
        except Exception as exc:
            logger.warning("[ENTRY_MONITOR] Could not attach to live feed: %s", exc)

    def _on_tick(self, symbol: str, tick: Dict) -> None:
        price = (
            tick.get("ltp")
            or tick.get("price")
            or tick.get("last_traded_price")
        )
        if price:
            self.update_price(symbol, float(price))

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

                lines = [
                    f"{direction_emoji} *{alert['label']}* — `{sym}`",
                    f"📍 Live Price: ₹{price}",
                    f"🎯 Safe Entry: ₹{entry} ({dist:+.1f}%)",
                    f"🛑 Stop Loss: ₹{sl}" if sl else "",
                    f"🏆 Target: ₹{tgt}" if tgt else "",
                    f"⚖️ R:R = {rr}" if rr else "",
                    f"⏳ {horizon}" if horizon else "",
                    f"📊 {alert.get('setupType', '')}",
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
