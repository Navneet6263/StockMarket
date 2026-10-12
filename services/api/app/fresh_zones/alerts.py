"""Alert records, duplicate control and delivery.

An alert is built from one event-lifecycle transition.  Control rules:
  * duplicate prevention — one alert per event ID (events have deterministic IDs);
  * cooldown — a repeat of the *same* state for the same zone inside
    ``cooldown_minutes`` is suppressed; a NEAR → IMMINENT upgrade and any
    different state are genuine changes and are not suppressed;
  * daily cap — at most ``daily_alert_cap`` delivered alerts per IST date.
    Within a batch, lifecycle events on zones already being followed rank
    first, then approach alerts by IMMINENT/NEAR, score and ATR_D distance.

Suppressed alerts are still logged with the reason.  Alerts describe
technical conditions only; they are not trade recommendations.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Iterable, Protocol

import requests

from app.fresh_zones.models import EventState, Proximity, Zone
from app.fresh_zones.params import ScannerParams
from app.fresh_zones.proximity import rank_key
from app.fresh_zones.store import FreshZoneStore


logger = logging.getLogger(__name__)

DISCLAIMER = (
    "Technical zone classification from explicit rules. Not a trade recommendation, "
    "not a guaranteed opportunity; the quality score is not a probability of success."
)


class AlertSink(Protocol):
    name: str

    def deliver(self, alert: dict[str, Any]) -> bool: ...


class LogSink:
    name = "log"

    def deliver(self, alert: dict[str, Any]) -> bool:
        logger.info("[FRESH-ZONE ALERT] %s", format_text(alert))
        return True


class TelegramSink:
    """Telegram delivery; enabled only when the owner opts in."""

    name = "telegram"

    def __init__(self, token: str, chat_id: str, timeout: int = 8):
        self.token, self.chat_id, self.timeout = token, chat_id, timeout

    @classmethod
    def from_env(cls) -> "TelegramSink | None":
        if os.getenv("FRESH_ZONE_TELEGRAM_ENABLED", "false").strip().lower() not in {"1", "true", "yes"}:
            return None
        token, chat = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
        return cls(token, chat) if token and chat else None

    def deliver(self, alert: dict[str, Any]) -> bool:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": format_text(alert), "disable_web_page_preview": True},
                timeout=self.timeout,
            )
            return response.ok
        except requests.RequestException as exc:
            logger.warning("fresh-zone telegram delivery failed: %s", exc)
            return False


def default_sinks() -> list[AlertSink]:
    sinks: list[AlertSink] = [LogSink()]
    telegram = TelegramSink.from_env()
    if telegram:
        sinks.append(telegram)
    return sinks


def format_text(alert: dict[str, Any]) -> str:
    zone, dist, data = alert["zone"], alert["distance"] or {}, alert["data"]
    lines = [
        f"{alert['states']['current']} · {alert['stock']['symbol']} ({alert['stock']['exchange']})",
        f"{zone['timeframe']} {zone['type']} {zone['pattern']} · proximal {zone['proximal']} / distal {zone['distal']}",
        f"Price {alert['price']['current']} at {alert['price']['timestamp']} [{data['mode']}]",
    ]
    if dist.get("distance_atr_d") is not None:
        lines.append(
            f"Distance ₹{dist['distance_rupees']} · {dist['distance_pct']}% · {dist['distance_atr_d']}×ATR_D ({dist['category']})"
        )
    lines.append(
        f"Freshness {alert['zone_status']['freshness']} · tests {alert['zone_status']['test_count']} · score {alert['quality']['score']}/80"
    )
    if alert.get("path"):
        lines.append(f"Path {alert['path']['classification']} ({alert['path']['barrier_count']} barrier(s))")
    lines.append(f"Reason: {alert['reason']}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _conditions(zone: Zone, params: ScannerParams, event: dict[str, Any]) -> dict[str, list[str]]:
    passed, failed = [], []

    def check(ok: bool, text: str) -> None:
        (passed if ok else failed).append(text)

    if zone.parent_zone_id is None:
        check(params.base_min_candles <= len(zone.base_candles) <= params.base_max_candles,
              f"base of {len(zone.base_candles)} candle(s) within {params.base_min_candles}–{params.base_max_candles}")
        check(all((c["body_ratio"] or 1) <= params.base_body_max_ratio for c in zone.base_candles),
              f"every base body ≤ {params.base_body_max_ratio:.0%} of range")
        check(zone.leg_in_atr >= params.leg_in_min_atr, f"leg-in {zone.leg_in_atr:.2f}×ATR_Z ≥ {params.leg_in_min_atr}")
        check(zone.departure_atr >= params.departure_min_atr,
              f"departure {zone.departure_atr:.2f}×ATR_Z ≥ {params.departure_min_atr}")
        check(zone.width_atr_z <= params.max_width_atr, f"width {zone.width_atr_z:.2f}×ATR_Z ≤ {params.max_width_atr}")
        check(zone.width_pct <= params.width_pct_cap(zone.timeframe),
              f"width {zone.width_pct:.2f}% ≤ {params.width_pct_cap(zone.timeframe)}%")
    check(zone.freshness == "FRESH", f"freshness {zone.freshness}")
    check(zone.status != "INVALIDATED", f"zone status {zone.status}")
    details = event.get("details") or {}
    if details.get("category"):
        check(details["category"] in {Proximity.IMMINENT, Proximity.NEAR}, f"proximity {details['category']}")
    return {"passed": passed, "failed": failed}


def build_alert(event: dict[str, Any], zone: Zone, context: dict[str, Any], params: ScannerParams) -> dict[str, Any]:
    observed = event["observed"]
    at = observed["at"]
    alert_id = "A-" + hashlib.sha256(f"{event['event_id']}|{params.version}".encode()).hexdigest()[:16]
    details = event.get("details") or {}
    distance = {k: details.get(k) for k in ("distance_rupees", "distance_pct", "distance_atr_d", "category", "approach_direction", "position")} \
        if "distance_rupees" in details else context.get("proximity")
    return {
        "alert_id": alert_id,
        "event_id": event["event_id"],
        "zone_id": zone.zone_id,
        "param_version": params.version,
        "ist_date": at[:10],
        "stock": {"name": context.get("name") or zone.symbol, "symbol": zone.symbol, "exchange": zone.exchange},
        "price": {"current": observed.get("price") if observed.get("price") is not None else context.get("price"),
                  "timestamp": at, "timing": observed["timing"]},
        "zone": {
            "timeframe": zone.timeframe,
            "type": str(zone.zone_type),
            "pattern": str(zone.pattern),
            "construction_method": zone.construction_method,
            "proximal": zone.proximal,
            "distal": zone.distal,
            "width": round(zone.width, 4),
            "width_atr_z": round(zone.width_atr_z, 3),
            "width_pct": round(zone.width_pct, 3),
            "formation_date": zone.formation_date,
            "flipped_from": zone.parent_zone_id,
        },
        "zone_status": {
            "status": str(zone.status),
            "freshness": str(zone.freshness),
            "freshness_reason": zone.freshness_reason,
            "test_count": zone.test_count,
            "invalidated": zone.invalidation is not None,
            "invalidation": zone.invalidation,
        },
        "quality": {"score": zone.score, "max": 80, "breakdown": zone.score_breakdown},
        "distance": distance,
        "states": {"previous": event["from_state"], "current": event["to_state"]},
        "reason": event["reason"],
        "event_details": details,
        "unobserved_prior_events": event.get("unobserved_prior_events", []),
        "measurements": {
            "atr_z": zone.atr_z,
            "atr_z_period": zone.atr_z_period,
            "atr_d": context.get("atr_d"),
            "leg_in_candles": zone.leg_in_candles,
            "base_candles": zone.base_candles,
            "departure_candles": zone.departure_candles,
        },
        "departure": {"body": zone.departure_body, "atr_z_multiple": round(zone.departure_atr, 3)},
        "fvg": zone.fvg,
        "path": context.get("path"),
        "conditions": _conditions(zone, params, event),
        "invalidation_level": zone.distal,
        "next_opposing_zone": context.get("next_opposing_zone"),
        "upcoming_corporate_event": context.get("corporate_event"),
        "data": observed["data"],
        "limitations": [
            *observed["data"].get("limitations", []),
            *(["corporate-event dates not available from a reliable source"] if context.get("corporate_event") is None else []),
        ],
        "disclaimer": DISCLAIMER,
        "delivery": {"delivered": False, "suppressed_reason": None, "channels": []},
    }


def _priority(alert: dict[str, Any]) -> tuple:
    if alert["states"]["current"] != EventState.APPROACHING:
        return (0, alert["price"]["timestamp"])
    distance = alert["distance"] or {}
    category = distance.get("category")
    return (1, *rank_key(Proximity(category) if category else None, alert["quality"]["score"], distance.get("distance_atr_d")))


class AlertManager:
    def __init__(self, store: FreshZoneStore, params: ScannerParams, sinks: Iterable[AlertSink] | None = None):
        self.store = store
        self.params = params
        self.sinks = list(sinks) if sinks is not None else default_sinks()

    def _suppression(self, alert: dict[str, Any]) -> str | None:
        if self.store.alert_exists(alert["event_id"]):
            return "duplicate"
        previous = self.store.last_delivered(alert["zone_id"], alert["states"]["current"])
        if previous is None:
            return None
        is_upgrade = (
            alert["states"]["current"] == EventState.APPROACHING
            and (alert["distance"] or {}).get("category") == Proximity.IMMINENT
            and (previous.get("distance") or {}).get("category") == Proximity.NEAR
        )
        elapsed = datetime.fromisoformat(alert["price"]["timestamp"]) - datetime.fromisoformat(previous["price"]["timestamp"])
        if not is_upgrade and elapsed < timedelta(minutes=self.params.cooldown_minutes):
            return f"cooldown ({self.params.cooldown_minutes} min) for this zone and state"
        return None

    def submit(self, alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rank, filter, deliver and log a batch.  Returns every processed alert."""
        processed: list[dict[str, Any]] = []
        for alert in sorted(alerts, key=_priority):
            if self.store.alert_exists(alert["event_id"]):
                continue  # already logged; nothing new to record
            reason = self._suppression(alert)
            if reason is None and self.store.delivered_count(alert["ist_date"]) >= self.params.daily_alert_cap:
                reason = f"daily cap of {self.params.daily_alert_cap} reached"
            if reason is None:
                channels = [sink.name for sink in self.sinks if _safe_deliver(sink, alert)]
                alert["delivery"] = {"delivered": bool(channels), "suppressed_reason": None if channels else "delivery failed",
                                     "channels": channels}
            else:
                alert["delivery"] = {"delivered": False, "suppressed_reason": reason, "channels": []}
            self.store.save_alert(alert)
            processed.append(alert)
        return processed


def _safe_deliver(sink: AlertSink, alert: dict[str, Any]) -> bool:
    try:
        return bool(sink.deliver(alert))
    except Exception:  # a broken channel must not stop the monitor
        logger.exception("alert sink %s failed", getattr(sink, "name", sink))
        return False

