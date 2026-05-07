from __future__ import annotations

import html
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ALERT_COOLDOWN_SEC = int(os.getenv("TELEGRAM_MARKET_ALERT_COOLDOWN_SEC", "1800"))
MAX_ALERTS_PER_SCAN = int(os.getenv("TELEGRAM_MARKET_ALERT_MAX_PER_SCAN", "5"))
BUY_ZONE_TOLERANCE_PCT = float(os.getenv("TELEGRAM_BUY_ZONE_TOLERANCE_PCT", "1.0"))
ACCUMULATION_MIN_SCORE = float(os.getenv("TELEGRAM_ACCUMULATION_MIN_SCORE", "6"))
BREAKOUT_DISTANCE_MAX_PCT = float(os.getenv("TELEGRAM_BREAKOUT_DISTANCE_MAX_PCT", "2.5"))
_SEND_TIMEOUT = int(os.getenv("TELEGRAM_SEND_TIMEOUT_SEC", "8"))
_QUEUE_MAX = int(os.getenv("TELEGRAM_QUEUE_MAX", "50"))


class _TelegramSendWorker:
    """Background worker — scanner never blocks on Telegram sends."""

    def __init__(self):
        self._q: queue.Queue[str] = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread = threading.Thread(target=self._run, daemon=True, name="telegram-send-worker")
        self._thread.start()

    def enqueue(self, text: str) -> bool:
        try:
            self._q.put_nowait(text)
            return True
        except queue.Full:
            return False

    def _run(self):
        while True:
            try:
                text = self._q.get(timeout=5)
                self._do_send(text)
                self._q.task_done()
            except queue.Empty:
                continue
            except Exception:
                pass

    @staticmethod
    def _do_send(text: str):
        config = _telegram_config()
        if not config["configured"]:
            return
        try:
            requests.post(
                f"{config['api']}/sendMessage",
                json={"chat_id": config["chat_id"], "text": text, "parse_mode": "HTML"},
                timeout=_SEND_TIMEOUT,
            )
        except Exception:
            pass


_send_worker = _TelegramSendWorker()


def _default_watch_state_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "telegram_market_watch_state.json"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _telegram_config() -> dict[str, Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return {
        "configured": bool(token and chat_id),
        "chat_id": chat_id,
        "api": f"https://api.telegram.org/bot{token}" if token else "",
    }


class TelegramMarketAlertService:
    """Sends scanner discovery alerts from the market overview scan payload."""

    def __init__(self):
        self.enabled = os.getenv("TELEGRAM_MARKET_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()
        self._watch_state_path = Path(os.getenv("TELEGRAM_MARKET_WATCH_STATE_PATH", str(_default_watch_state_path())))
        self._watch_state: dict[str, dict[str, Any]] = self._load_watch_state()
        self.last_status: dict[str, Any] = {"ok": False, "message": "No market alert sent yet."}

    @staticmethod
    def _safe(value: Any) -> str:
        return html.escape(str(value), quote=False) if value is not None else ""

    def _is_duplicate(self, key: str) -> bool:
        with self._lock:
            last = self._last_sent.get(key, 0)
            return time.time() - last < ALERT_COOLDOWN_SEC

    def _record_sent(self, key: str) -> None:
        with self._lock:
            self._last_sent[key] = time.time()

    def _load_watch_state(self) -> dict[str, dict[str, Any]]:
        try:
            if not self._watch_state_path.exists():
                return {}
            raw = json.loads(self._watch_state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(symbol).upper(): state for symbol, state in raw.items() if isinstance(state, dict)}
        except Exception:
            return {}
        return {}

    def _save_watch_state(self) -> None:
        try:
            self._watch_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._watch_state_path.write_text(json.dumps(self._watch_state, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return

    def _send(self, text: str) -> bool:
        """Non-blocking send via background queue worker."""
        config = _telegram_config()
        if not self.enabled:
            self.last_status = {"ok": False, "message": "Telegram market alerts disabled."}
            return False
        if not config["configured"]:
            self.last_status = {"ok": False, "message": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."}
            return False
        queued = _send_worker.enqueue(text)
        if queued:
            self.last_status = {"ok": True, "message": "Market alert queued", "queued_at": datetime.now(timezone.utc).isoformat()}
        else:
            self.last_status = {"ok": False, "message": "Send queue full — alert dropped."}
        return queued

    def _entry_zone_text(self, item: dict[str, Any]) -> str:
        zone = item.get("entry_zone") or item.get("re_entry_zone") or {}
        low = _safe_float(zone.get("low"))
        high = _safe_float(zone.get("high"))
        if low and high:
            return f"{min(low, high):.2f} - {max(low, high):.2f}"
        trigger = _safe_float(
            item.get("safe_entry_price")
            or item.get("entry_trigger")
            or item.get("trigger_price")
            or item.get("breakoutTrigger")
            or item.get("resistance_level")
        )
        return f"near {trigger:.2f}" if trigger else "-"

    def _entry_text(self, item: dict[str, Any]) -> str:
        price = _safe_float(item.get("current_price") or item.get("price"))
        zone = item.get("entry_zone") or item.get("re_entry_zone") or {}
        low = _safe_float(zone.get("low"))
        high = _safe_float(zone.get("high"))
        trigger = self._trigger_price(item)

        if low and high:
            lower = min(low, high)
            upper = max(low, high)
            zone_text = f"{lower:.2f} - {upper:.2f}"
            if price and lower <= price <= upper:
                return f"{price:.2f} (inside zone {zone_text})"
            if price and price < lower:
                return f"wait for {zone_text}"
            if trigger:
                return f"above {trigger:.2f} or pullback to {zone_text}"
            return zone_text

        if trigger:
            if price and price >= trigger:
                return f"{price:.2f} if it holds above {trigger:.2f}"
            return f"above {trigger:.2f}"

        return f"{price:.2f}" if price and self._is_buy_zone(item) else "-"

    def _trigger_price(self, item: dict[str, Any]) -> float:
        return _safe_float(
            item.get("safe_entry_price")
            or item.get("entry_trigger")
            or item.get("trigger_price")
            or item.get("alert_above_price")
            or item.get("resistance_level")
            or item.get("resistance")
        )

    def _direction(self, item: dict[str, Any]) -> str:
        direction = str(item.get("direction") or "").lower()
        if direction in {"bullish", "up"}:
            return "bullish"
        if direction in {"bearish", "down"}:
            return "bearish"
        return "neutral"

    def _fail_price(self, item: dict[str, Any]) -> float:
        return _safe_float(
            item.get("stop_loss")
            or item.get("invalidation")
            or item.get("invalidation_level")
            or item.get("invalidationLevel")
            or item.get("support_level")
            or item.get("support")
        )

    def _trigger_distance_pct(self, item: dict[str, Any]) -> float | None:
        sentinel = -999999.0
        explicit = _safe_float(
            item.get("distance_to_trigger_pct")
            or item.get("distanceToTriggerPct")
            or item.get("distanceToResistancePct"),
            sentinel,
        )
        if explicit != sentinel:
            return explicit
        price = _safe_float(item.get("current_price") or item.get("price"))
        trigger = self._trigger_price(item)
        if not price or not trigger:
            return None
        if self._direction(item) == "bearish":
            return round(((price - trigger) / price) * 100, 2)
        return round(((trigger - price) / price) * 100, 2)

    def _pattern_text(self, item: dict[str, Any]) -> str:
        pattern = (
            item.get("biasLabel")
            or item.get("pattern")
            or item.get("basePatternType")
            or item.get("base_basePatternType")
            or item.get("setup_type")
            or item.get("breakout_readiness_label")
            or item.get("setup_label")
        )
        text = str(pattern or "").strip().replace("_", " ")
        return text if text else "pattern not classified"

    def _is_buy_zone(self, item: dict[str, Any]) -> bool:
        if (item.get("direction") or "").lower() not in {"bullish", "up"}:
            return False

        action = str(item.get("action") or item.get("effectiveAction") or item.get("recommended_action") or "").upper()
        if action and action not in {"BUY", "REENTRY_BUY", "ALERT", "WATCH", "ALERT_ABOVE_LEVEL"}:
            return False

        price = _safe_float(item.get("current_price") or item.get("price"))
        if not price:
            return False

        zone = item.get("entry_zone") or item.get("re_entry_zone") or {}
        low = _safe_float(zone.get("low"))
        high = _safe_float(zone.get("high"))
        if low and high and min(low, high) <= price <= max(low, high):
            return True

        trigger = self._trigger_price(item)
        if trigger:
            distance_pct = abs(price - trigger) / trigger * 100
            return distance_pct <= BUY_ZONE_TOLERANCE_PCT
        return False

    def _is_fresh_hot_pick(self, item: dict[str, Any]) -> bool:
        if (item.get("direction") or "").lower() != "bullish":
            return False

        labels = {str(label).upper() for label in item.get("trade_labels", [])}
        setup_stage = str(item.get("setup_stage") or "").upper()
        action = str(item.get("action") or item.get("effectiveAction") or item.get("recommended_action") or "").upper()
        if (
            item.get("attention_only")
            or item.get("chase_risk")
            or item.get("overextended_fresh_entry")
            or item.get("next_day_profit_booking_risk")
            or labels.intersection({"CHASE_RISK", "AVOID_LATE_ENTRY", "PROFIT_BOOKING_RISK", "WAIT_FOR_PULLBACK"})
            or setup_stage in {"CHASE_RISK", "AVOID_LATE_ENTRY", "PROFIT_BOOKING_RISK"}
            or action in {"WAIT_FOR_PULLBACK", "AVOID", "EXIT", "SELL"}
        ):
            return False

        rr = _safe_float(item.get("risk_reward") or item.get("rr"))
        if rr and rr < 1.2:
            return False

        return self._is_buy_zone(item) or bool(self._trigger_price(item))

    def _trade_instruction(self, category: str, item: dict[str, Any]) -> str:
        if item.get("tradeDecision"):
            return str(item["tradeDecision"])

        price = _safe_float(item.get("current_price") or item.get("price"))
        trigger = self._trigger_price(item)
        fail = self._fail_price(item)
        in_zone = self._is_buy_zone(item)
        direction = self._direction(item)

        if category == "SILENT ACCUMULATION":
            if direction == "bearish":
                return f"BEARISH WATCH: avoid fresh long. Weakness confirms below {trigger:.2f}." if trigger else "BEARISH WATCH: avoid fresh long until structure improves."
            if fail and price and price <= fail:
                return "FAILED: price broke the fail level. Do not enter."
            if trigger and price and price >= trigger:
                return "ENTRY ACTIVE: breakout trigger crossed. Buy only if it holds above trigger."
            if in_zone:
                return "BUY ZONE NEAR: wait for confirmation candle/volume before entry."
            if trigger:
                return f"WATCH ONLY: no buy yet. Buy only above {trigger:.2f}; fail below {fail:.2f}." if fail else f"WATCH ONLY: no buy yet. Buy only above {trigger:.2f}."
            return "WATCH ONLY: trigger is not available, so no fresh entry call."

        if category == "BUY ZONE":
            return "BUY ZONE: entry is active only with risk managed below SL/fail level."
        if category == "HOT PICK":
            return "HIGH PRIORITY: entry allowed only inside zone or after trigger confirmation."
        return "MOMENTUM WATCH: do not chase far above entry zone; wait for pullback/trigger hold."

    def _format_alert(self, category: str, item: dict[str, Any]) -> str:
        symbol = self._safe(item.get("symbol"))
        price = _safe_float(item.get("current_price") or item.get("price"))
        confidence = _safe_float(
            item.get("smartScore")
            or item.get("confidence")
            or item.get("breakout_readiness_score")
            or item.get("accumulationScore")
            or item.get("baseQualityScore")
            or item.get("base_baseQualityScore")
        )
        volume = _safe_float(item.get("relative_volume") or item.get("volume_ratio") or item.get("intraday_volume_ratio"), 1.0)
        rr = _safe_float(item.get("risk_reward") or item.get("rr"))
        trigger = self._trigger_price(item)
        stop = self._fail_price(item)
        t1 = _safe_float(item.get("target_1") or item.get("target_price") or item.get("target"))
        distance = self._trigger_distance_pct(item)
        reasons = item.get("reasons") or []
        if isinstance(item.get("reason"), str):
            reasons = [item["reason"]]
        if isinstance(item.get("whyInteresting"), str):
            reasons = [item["whyInteresting"], *reasons]

        lines = [
            f"<b>{symbol} | {self._safe(category)}</b>",
            "------------------------------",
            f"Action: <b>{self._safe(self._trade_instruction(category, item))}</b>",
            f"Bias: <b>{self._safe(self._pattern_text(item))}</b>",
            f"Status: <b>{self._safe(item.get('entryStatus') or item.get('recommended_action') or item.get('action') or 'WATCH')}</b>",
            f"Price: <code>{price:.2f}</code>" if price else "Price: -",
            f"Entry: <code>{self._safe(self._entry_text(item))}</code>",
            f"Entry zone: <code>{self._safe(self._entry_zone_text(item))}</code>",
            f"Buy above: <code>{trigger:.2f}</code>" if trigger else "Buy above: -",
            f"SL: <code>{stop:.2f}</code>" if stop else "SL: -",
            f"Fail below: <code>{stop:.2f}</code>" if stop else "Fail below: -",
            f"T1: <code>{t1:.2f}</code>" if t1 else "T1: -",
            "------------------------------",
            f"Score: {confidence:.1f}" if confidence else "Score: -",
            f"Volume: {volume:.2f}x",
            f"RR: 1:{rr:.2f}" if rr else "RR: -",
            f"Distance to trigger: {distance:.2f}%" if distance is not None else "Distance to trigger: -",
        ]
        if item.get("breakout_readiness_label"):
            lines.append(f"Setup: {self._safe(item.get('breakout_readiness_label'))}")
        if reasons:
            lines.append("")
            lines.append("<b>Why</b>")
            lines.extend(f"- {self._safe(reason)}" for reason in reasons[:3])
        lines.append("")
        lines.append("<i>Research alert only. Confirm price action and risk before trade.</i>")
        return "\n".join(lines)

    def _remember_watch(self, category: str, item: dict[str, Any]) -> None:
        if category != "SILENT ACCUMULATION":
            return
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            return
        trigger = self._trigger_price(item)
        fail = self._fail_price(item)
        if not trigger and not fail:
            return
        with self._lock:
            self._watch_state[symbol] = {
                "category": category,
                "trigger": trigger,
                "fail": fail,
                "target_1": _safe_float(item.get("target_1") or item.get("target_price") or item.get("target")),
                "last_status": "WATCH",
                "updated_at": time.time(),
            }
            self._save_watch_state()

    def _current_items_by_symbol(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        buckets = (
            "hotPicks",
            "top_opportunities",
            "breakout_radar",
            "baseFormationRadar",
            "momentumRadar",
            "momentum_continuation",
            "retest_entry",
            "unusual_volume",
            "fast_movers_missed_moves",
        )
        current: dict[str, dict[str, Any]] = {}
        for bucket in buckets:
            for item in payload.get(bucket) or []:
                symbol = str(item.get("symbol") or "").upper()
                if symbol and symbol not in current:
                    current[symbol] = item
        return current

    def _format_watch_update(self, symbol: str, status: str, state: dict[str, Any], item: dict[str, Any] | None = None) -> str:
        price = _safe_float((item or {}).get("current_price") or (item or {}).get("price"))
        trigger = _safe_float((item or {}).get("trigger_price") or (item or {}).get("entry_trigger"), state.get("trigger") or 0)
        fail = _safe_float((item or {}).get("stop_loss") or (item or {}).get("invalidation"), state.get("fail") or 0)
        target = _safe_float((item or {}).get("target_1") or (item or {}).get("target_price"), state.get("target_1") or 0)

        if status == "ENTRY_ACTIVE":
            action = "ENTRY ACTIVE: trigger crossed. Buy only if price holds above trigger with volume."
        elif status == "FAILED":
            action = "FAILED: fail level broke. Previous watch is cancelled."
        else:
            action = "INACTIVE: no longer visible in the current radar. Wait for fresh scan confirmation."

        lines = [
            f"<b>{self._safe(symbol)} | SILENT ACCUMULATION UPDATE</b>",
            "------------------------------",
            f"Action: <b>{self._safe(action)}</b>",
            f"Price: <code>{price:.2f}</code>" if price else "Price: -",
            f"Buy above: <code>{trigger:.2f}</code>" if trigger else "Buy above: -",
            f"Fail below: <code>{fail:.2f}</code>" if fail else "Fail below: -",
            f"T1: <code>{target:.2f}</code>" if target else "T1: -",
        ]
        return "\n".join(lines)

    def _send_watch_updates(self, payload: dict[str, Any]) -> tuple[int, set[str]]:
        current = self._current_items_by_symbol(payload)
        sent = 0
        updated_symbols: set[str] = set()
        with self._lock:
            watched = {symbol: dict(state) for symbol, state in self._watch_state.items()}

        for symbol, state in watched.items():
            item = current.get(symbol)
            status = ""
            if item is None:
                status = "INACTIVE"
            else:
                price = _safe_float(item.get("current_price") or item.get("price"))
                trigger = self._trigger_price(item) or _safe_float(state.get("trigger"))
                fail = self._fail_price(item) or _safe_float(state.get("fail"))
                if fail and price and price <= fail:
                    status = "FAILED"
                elif trigger and price and price >= trigger:
                    status = "ENTRY_ACTIVE"

            if not status or status == state.get("last_status"):
                continue
            key = f"SILENT_ACCUMULATION_UPDATE:{status}:{symbol}"
            if self._is_duplicate(key):
                continue
            if self._send(self._format_watch_update(symbol, status, state, item)):
                self._record_sent(key)
                sent += 1
                updated_symbols.add(symbol)
                with self._lock:
                    if status in {"FAILED", "INACTIVE"}:
                        self._watch_state.pop(symbol, None)
                    elif symbol in self._watch_state:
                        self._watch_state[symbol]["last_status"] = status
                    self._save_watch_state()
        return sent, updated_symbols

    def _add_candidate(
        self,
        candidates: list[tuple[int, str, dict[str, Any]]],
        seen: set[str],
        priority: int,
        category: str,
        item: dict[str, Any],
    ) -> None:
        symbol = item.get("symbol")
        if not symbol:
            return
        key = str(symbol).upper()
        if key in seen:
            return
        seen.add(key)
        candidates.append((priority, category, item))

    def collect_alerts(self, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        seen: set[str] = set()

        for item in [*(payload.get("hotPicks") or []), *(payload.get("top_opportunities") or [])]:
            if self._is_fresh_hot_pick(item):
                self._add_candidate(candidates, seen, 100, "HOT PICK", item)

        for item in payload.get("top_opportunities") or []:
            if self._is_buy_zone(item):
                self._add_candidate(candidates, seen, 95, "BUY ZONE", item)

        for item in [*(payload.get("breakout_radar") or []), *(payload.get("baseFormationRadar") or [])]:
            accumulation = _safe_float(
                item.get("accumulation_score")
                or item.get("accumulationScore")
                or item.get("baseQualityScore")
                or item.get("base_baseQualityScore")
                or item.get("breakout_readiness_score")
            )
            distance = self._trigger_distance_pct(item)
            if distance is None:
                distance = 99.0
            if accumulation >= ACCUMULATION_MIN_SCORE and distance <= BREAKOUT_DISTANCE_MAX_PCT:
                self._add_candidate(candidates, seen, 85, "SILENT ACCUMULATION", item)

        for bucket in ("momentumRadar", "momentum_continuation", "retest_entry", "unusual_volume", "fast_movers_missed_moves"):
            for item in payload.get(bucket) or []:
                direction = (item.get("direction") or "").lower()
                volume = _safe_float(item.get("relative_volume") or item.get("volume_ratio") or item.get("intraday_volume_ratio"), 1.0)
                if direction == "bullish" and (volume >= 1.4 or self._is_buy_zone(item)):
                    self._add_candidate(candidates, seen, 70, "VOLUME/MOMENTUM CANDIDATE", item)

        candidates.sort(key=lambda row: row[0], reverse=True)
        return [(category, item) for _, category, item in candidates[:MAX_ALERTS_PER_SCAN]]

    def send_payload_alerts(self, payload: dict[str, Any]) -> dict[str, Any]:
        sent, updated_symbols = self._send_watch_updates(payload)
        skipped = 0
        for category, item in self.collect_alerts(payload):
            symbol = str(item.get("symbol", "")).upper()
            if symbol in updated_symbols:
                skipped += 1
                continue
            key = f"{category}:{symbol}"
            if self._is_duplicate(key):
                skipped += 1
                continue
            if self._send(self._format_alert(category, item)):
                self._record_sent(key)
                self._remember_watch(category, item)
                sent += 1
        return {"sent": sent, "skipped_duplicates": skipped, "last_status": self.last_status}


_alert_service: TelegramMarketAlertService | None = None
_alert_lock = threading.Lock()


def get_telegram_market_alerts() -> TelegramMarketAlertService:
    global _alert_service
    if _alert_service is None:
        with _alert_lock:
            if _alert_service is None:
                _alert_service = TelegramMarketAlertService()
    return _alert_service
