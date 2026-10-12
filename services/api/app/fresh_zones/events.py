"""Event lifecycle for one zone, kept separate from the zone lifecycle.

Event states (``EventState``):

    FRESH → APPROACHING → TOUCH_DETECTED → REACTION_UNDER_OBSERVATION
    REACTION_UNDER_OBSERVATION → REACTION_CONFIRMED | CONSOLIDATING |
                                 UNDER_PRESSURE | FAILED_REACTION |
                                 PROVISIONAL_BREACH
    PROVISIONAL_BREACH → CONFIRMED_BREAK | FALSE_BREAK
    CONFIRMED_BREAK → RETEST | CONTINUATION_CONFIRMED | FALSE_BREAK

The zone lifecycle (FRESH → TOUCHED → INVALIDATED) is updated on the zone
object itself; a touch permanently ends freshness, and re-arming an approach
alert never restores it.

Every transition is timestamped, carries its reason, the observed data that
caused it and a timing-confidence label.  When monitoring missed an earlier
step (e.g. a touch inferred only after a breach is seen), the missing step is
listed as *unobserved*, never given an invented time or price.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.fresh_zones.freshness import GAP_BEYOND, GAP_INTO, TOUCH
from app.fresh_zones.models import (
    DataQuality,
    EventState as S,
    Freshness,
    Proximity,
    TimingConfidence,
    Zone,
    ZoneStatus,
    ZoneType,
)
from app.fresh_zones.params import ScannerParams
from app.fresh_zones.proximity import APPROACHING, BEYOND, INSIDE, measure, position_of
from app.fresh_zones.timeframes import to_ist


ALLOWED: dict[S, frozenset[S]] = {
    S.FRESH: frozenset({S.APPROACHING, S.TOUCH_DETECTED, S.PROVISIONAL_BREACH}),
    S.APPROACHING: frozenset({S.APPROACHING, S.FRESH, S.TOUCH_DETECTED, S.PROVISIONAL_BREACH}),
    S.TOUCH_DETECTED: frozenset({S.REACTION_UNDER_OBSERVATION, S.PROVISIONAL_BREACH}),
    S.REACTION_UNDER_OBSERVATION: frozenset(
        {S.REACTION_CONFIRMED, S.CONSOLIDATING, S.UNDER_PRESSURE, S.FAILED_REACTION, S.PROVISIONAL_BREACH}
    ),
    S.UNDER_PRESSURE: frozenset({S.REACTION_CONFIRMED, S.CONSOLIDATING, S.FAILED_REACTION, S.PROVISIONAL_BREACH}),
    S.CONSOLIDATING: frozenset({S.REACTION_CONFIRMED, S.UNDER_PRESSURE, S.FAILED_REACTION, S.PROVISIONAL_BREACH}),
    S.REACTION_CONFIRMED: frozenset({S.APPROACHING, S.TOUCH_DETECTED, S.PROVISIONAL_BREACH}),
    S.FAILED_REACTION: frozenset({S.APPROACHING, S.TOUCH_DETECTED, S.PROVISIONAL_BREACH}),
    S.PROVISIONAL_BREACH: frozenset({S.CONFIRMED_BREAK, S.FALSE_BREAK}),
    S.CONFIRMED_BREAK: frozenset({S.RETEST, S.CONTINUATION_CONFIRMED, S.FALSE_BREAK}),
    S.RETEST: frozenset({S.CONTINUATION_CONFIRMED, S.FALSE_BREAK}),
    S.CONTINUATION_CONFIRMED: frozenset({S.FALSE_BREAK}),
    S.FALSE_BREAK: frozenset({S.PROVISIONAL_BREACH, S.TOUCH_DETECTED, S.REACTION_UNDER_OBSERVATION}),
}

PRE_TOUCH = frozenset({S.FRESH, S.APPROACHING})
REACTION_STATES = frozenset({S.REACTION_UNDER_OBSERVATION, S.UNDER_PRESSURE, S.CONSOLIDATING})
POST_REACTION = frozenset({S.REACTION_CONFIRMED, S.FAILED_REACTION})
BREAK_STATES = frozenset({S.PROVISIONAL_BREACH, S.CONFIRMED_BREAK, S.RETEST, S.CONTINUATION_CONFIRMED})


class TransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Observation:
    """What the monitor actually saw, with its provenance."""

    at: datetime
    timing: TimingConfidence
    quality: DataQuality
    price: float | None = None
    low: float | None = None
    high: float | None = None
    interval: tuple[str, str] | None = None  # candle start/end when candle-inferred

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": to_ist(self.at).isoformat(),
            "timing": str(self.timing),
            "price": self.price,
            "low": self.low,
            "high": self.high,
            "interval": list(self.interval) if self.interval else None,
            "data": self.quality.to_dict(),
        }


@dataclass
class ZoneWatch:
    zone_id: str
    state: S = S.FRESH
    category: str | None = None          # last proximity category reached while approaching
    approach_alerted: bool = False       # an approach alert is outstanding until re-armed
    max_distance_atr_d: float = 0.0      # farthest distance since the last approach alert
    touch: dict[str, Any] | None = None  # current touch/observation episode
    excursion: float = 0.0               # farthest move away from proximal since last entry
    breach: dict[str, Any] | None = None
    last_daily_processed: str | None = None        # last completed session fed at end of day
    last_zone_candle_processed: str | None = None  # end date of last zone-timeframe candle fed
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = str(self.state)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ZoneWatch":
        payload = dict(data)
        payload["state"] = S(payload.get("state", S.FRESH))
        return cls(**payload)


def _event_id(zone_id: str, *parts: Any) -> str:
    digest = hashlib.sha256("|".join([zone_id, *map(str, parts)]).encode()).hexdigest()[:16]
    return f"E-{digest}"


class ZoneEventTracker:
    """Applies observations to a (zone, watch) pair and returns new events."""

    def __init__(self, zone: Zone, watch: ZoneWatch, params: ScannerParams):
        self.zone = zone
        self.watch = watch
        self.params = params
        self.flipped: list[Zone] = []

    # ── Core transition ─────────────────────────────────────────────────────
    def _transition(
        self,
        to: S,
        obs: Observation,
        reason: str,
        *,
        details: dict[str, Any] | None = None,
        unobserved: list[str] | None = None,
        key: Any = None,
    ) -> dict[str, Any]:
        frm = self.watch.state
        if to not in ALLOWED[frm]:
            raise TransitionError(f"{frm} → {to} is not an allowed transition ({self.zone.zone_id})")
        event = {
            "event_id": _event_id(self.zone.zone_id, frm, to, key if key is not None else obs.at.isoformat()),
            "zone_id": self.zone.zone_id,
            "symbol": self.zone.symbol,
            "from_state": str(frm),
            "to_state": str(to),
            "at": to_ist(obs.at).isoformat(),
            "reason": reason,
            "observed": obs.to_dict(),
            "details": details or {},
            "unobserved_prior_events": unobserved or [],
            "zone_status": str(self.zone.status),
            "param_version": self.params.version,
        }
        self.watch.state = to
        self.watch.history.append(event)
        return event

    # ── Helpers ─────────────────────────────────────────────────────────────
    @property
    def _demand(self) -> bool:
        return self.zone.zone_type == ZoneType.DEMAND

    def _penetration_fraction(self, extreme: float) -> float:
        width = self.zone.width
        if width <= 0:
            return 1.0
        depth = (self.zone.proximal - extreme) if self._demand else (extreme - self.zone.proximal)
        return max(0.0, depth / width)

    def _away_from_proximal(self, price: float) -> float:
        return price - self.zone.proximal if self._demand else self.zone.proximal - price

    def _beyond_distal(self, price: float) -> bool:
        return price < self.zone.distal if self._demand else price > self.zone.distal

    def _record_zone_touch(self, kind: str, obs: Observation, atr_d: float) -> dict[str, Any]:
        touch = {
            "date": to_ist(obs.at).date().isoformat(),
            "kind": kind,
            "observed": obs.to_dict(),
            "timing": str(obs.timing),
        }
        self.zone.touches.append(touch)
        new_test = not self.zone.tests or (
            atr_d > 0 and self.watch.excursion >= self.params.test_reset_atr_d * atr_d
        )
        if new_test:
            self.zone.tests.append(
                {
                    "test_number": len(self.zone.tests) + 1,
                    "date": touch["date"],
                    "kind": kind,
                    "excursion_before_atr_d": round(self.watch.excursion / atr_d, 3)
                    if self.zone.tests and atr_d > 0
                    else None,
                    "timing": touch["timing"],
                }
            )
        self.watch.excursion = 0.0
        if self.zone.status in {ZoneStatus.FRESH, ZoneStatus.UNVERIFIED}:
            self.zone.status = ZoneStatus.TOUCHED
            self.zone.freshness = Freshness.TESTED
            self.zone.freshness_reason = f"entered on {touch['date']} ({kind})"
        return touch

    def _start_touch(self, kind: str, obs: Observation, atr_d: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        prior = self.watch.state
        unobserved = ["APPROACHING (no approach alert was observed before this touch)"] if prior == S.FRESH else []
        touch = self._record_zone_touch(kind, obs, atr_d)
        test_no = self.zone.test_count
        events.append(
            self._transition(
                S.TOUCH_DETECTED,
                obs,
                "gap-through into the zone at the session open" if kind == GAP_INTO else "price reached the zone",
                details={"touch_kind": kind, "test_number": test_no, "previous_state": str(prior)},
                unobserved=unobserved,
                key=f"touch-{len(self.zone.touches)}",
            )
        )
        self.watch.touch = {
            "started_at": touch["observed"]["at"],
            "session": touch["date"],
            "kind": kind,
            "sessions_closed": 0,
            "deepest": obs.low if self._demand else obs.high,
            "atr_d": atr_d,
        }
        if self.watch.touch["deepest"] is None:
            self.watch.touch["deepest"] = obs.price
        events.append(
            self._transition(
                S.REACTION_UNDER_OBSERVATION,
                obs,
                "touch is not a confirmed reversal; observing the reaction",
                details={
                    "window_sessions": self.params.reaction_window_sessions,
                    "confirmation_candle_minutes": self.params.reaction_candle_minutes,
                    "threshold_atr_d": self.params.reaction_threshold_atr_d,
                },
                key=f"observe-{len(self.zone.touches)}",
            )
        )
        self.watch.approach_alerted = False
        self.watch.category = None
        return events

    def _start_breach(self, kind: str, obs: Observation, atr_d: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        unobserved: list[str] = []
        if self.watch.state in PRE_TOUCH:
            if kind == GAP_BEYOND:
                self._record_zone_touch(GAP_BEYOND, obs, atr_d)
            else:
                unobserved.append("TOUCH_DETECTED (price passed through the zone between observations)")
                self._record_zone_touch(TOUCH, obs, atr_d)
            if self.watch.state == S.FRESH:
                unobserved.insert(0, "APPROACHING (no approach alert was observed)")
        self.watch.breach = {
            "started_at": to_ist(obs.at).isoformat(),
            "kind": kind,
            "confirmed_at": None,
            "daily_close_beyond": None,
            "returned_inside_before_confirmation": None,
            "retest": None,
            "closed_back_inside_after_confirmation": False,
        }
        events.append(
            self._transition(
                S.PROVISIONAL_BREACH,
                obs,
                "session opened beyond the distal boundary" if kind == GAP_BEYOND
                else "price traded beyond the distal boundary",
                details={"breach_kind": kind, "distal": self.zone.distal},
                unobserved=unobserved,
                key=f"breach-{len(self.zone.touches)}-{obs.at.isoformat()}",
            )
        )
        self.watch.touch = None
        return events

    # ── Observations ────────────────────────────────────────────────────────
    def on_price(self, obs: Observation, atr_d: float) -> list[dict[str, Any]]:
        """A traded/quoted price (live or delayed).  Proximity is evaluated on
        the price itself, not on candle closes."""

        price = float(obs.price)
        position = position_of(self.zone, price)
        state = self.watch.state
        events: list[dict[str, Any]] = []

        if position == APPROACHING:
            self.watch.excursion = max(self.watch.excursion, self._away_from_proximal(price))
            if state in PRE_TOUCH or (state in POST_REACTION and self.params.allow_post_touch_approach):
                events += self._approach(obs, atr_d)
            elif state in BREAK_STATES or state == S.FALSE_BREAK:
                events += self._break_from_price(price, obs, atr_d)
            return events

        if position == INSIDE:
            if state in PRE_TOUCH or state in POST_REACTION:
                return self._start_touch(TOUCH, obs, atr_d)
            if state in REACTION_STATES:
                return self._pressure(price, obs)
            if state in BREAK_STATES or state == S.FALSE_BREAK:
                return self._break_from_price(price, obs, atr_d)
            return events

        # BEYOND the distal boundary
        if state in BREAK_STATES:
            return self._break_from_price(price, obs, atr_d)
        return self._start_breach(TOUCH, obs, atr_d)

    def on_session_open(self, obs: Observation, atr_d: float) -> list[dict[str, Any]]:
        """First regular-session print.  Gap-throughs are never treated as an
        untouched approach opportunity."""

        state = self.watch.state
        position = position_of(self.zone, float(obs.price))
        if state in PRE_TOUCH or state in POST_REACTION:
            if position == INSIDE:
                return self._start_touch(GAP_INTO, obs, atr_d)
            if position == BEYOND:
                return self._start_breach(GAP_BEYOND, obs, atr_d)
        return self.on_price(obs, atr_d)

    def on_intraday_candle(self, obs: Observation, close: float, atr_d: float, *, confirmation: bool) -> list[dict[str, Any]]:
        """A completed intraday candle since the previous check.

        Used to catch zone entries that happened between price polls (timing
        is candle-inferred) and, when ``confirmation`` is true (15-minute
        candles), to confirm a reaction on the candle's close.
        """

        low, high = float(obs.low), float(obs.high)
        state = self.watch.state
        events: list[dict[str, Any]] = []
        beyond = low < self.zone.distal if self._demand else high > self.zone.distal
        if state in PRE_TOUCH or state in POST_REACTION:
            if beyond:
                return self._start_breach(TOUCH, obs, atr_d)
            if self.zone.overlaps(low, high):
                events += self._start_touch(TOUCH, obs, atr_d)
                state = self.watch.state
            else:
                self.watch.excursion = max(self.watch.excursion, self._away_from_proximal(high if self._demand else low))
                return events
        if state in REACTION_STATES:
            if beyond:
                return events + self._start_breach(TOUCH, obs, atr_d)
            extreme = low if self._demand else high
            deepest = self.watch.touch.get("deepest") if self.watch.touch else None
            if deepest is None or (extreme < deepest if self._demand else extreme > deepest):
                self.watch.touch["deepest"] = extreme
            if confirmation and self._away_from_proximal(close) >= self.params.reaction_threshold_atr_d * self.watch.touch["atr_d"]:
                events.append(
                    self._transition(
                        S.REACTION_CONFIRMED,
                        obs,
                        "completed confirmation candle closed beyond the reaction threshold",
                        details=self._reaction_details(close, obs),
                        key=f"confirm-{len(self.zone.touches)}",
                    )
                )
                return events
            if self.watch.state != S.UNDER_PRESSURE and self._penetration_fraction(extreme) >= self.params.under_pressure_fraction:
                events += self._pressure(extreme, obs)
        elif state in BREAK_STATES:
            events += self._break_from_price(low if self._demand else high, obs, atr_d)
        elif state == S.FALSE_BREAK and beyond:
            events += self._start_breach(TOUCH, obs, atr_d)
        return events

    def on_daily_close(
        self, obs: Observation, close: float, atr_d: float, *, daily_confirmation_fallback: bool = False
    ) -> list[dict[str, Any]]:
        """A completed daily candle: resolves reaction windows and drives the
        daily-close parts of the breakout lifecycle.

        ``daily_confirmation_fallback`` is set only when no intraday
        confirmation candles exist (END_OF_DAY mode); the daily close may then
        confirm a reaction, and the event says so explicitly.
        """

        state = self.watch.state
        events: list[dict[str, Any]] = []
        position = position_of(self.zone, close)
        if state in REACTION_STATES and self.watch.touch:
            self.watch.touch["sessions_closed"] += 1
            threshold = self.params.reaction_threshold_atr_d * self.watch.touch["atr_d"]
            if daily_confirmation_fallback and self._away_from_proximal(close) >= threshold:
                details = self._reaction_details(close, obs)
                details["confirmation_basis"] = (
                    f"completed daily close ({self.params.reaction_candle_minutes}-minute candles unavailable)"
                )
                return [
                    self._transition(
                        S.REACTION_CONFIRMED,
                        obs,
                        "completed daily close beyond the reaction threshold (end-of-day fallback)",
                        details=details,
                        key=f"confirm-{len(self.zone.touches)}",
                    )
                ]
            if self.watch.touch["sessions_closed"] >= self.params.reaction_window_sessions:
                if position == INSIDE:
                    events.append(
                        self._transition(
                            S.CONSOLIDATING,
                            obs,
                            "observation window ended without confirmation; price remains inside the zone",
                            details=self._reaction_details(close, obs),
                            key=f"consolidating-{len(self.zone.touches)}",
                        )
                    )
                elif position == APPROACHING:
                    events.append(
                        self._transition(
                            S.FAILED_REACTION,
                            obs,
                            "observation window ended without a confirmed reaction; price is outside the zone",
                            details=self._reaction_details(close, obs),
                            key=f"failed-{len(self.zone.touches)}",
                        )
                    )
            return events

        breach = self.watch.breach
        if state == S.PROVISIONAL_BREACH and breach is not None:
            if position != BEYOND:
                breach["returned_inside_before_confirmation"] = to_ist(obs.at).isoformat()
                events.append(
                    self._transition(
                        S.FALSE_BREAK,
                        obs,
                        "completed daily candle closed back inside the zone (or on its original side) before confirmation",
                        details={"confirmed": False, "close": close},
                        key=f"false-pre-{breach['started_at']}",
                    )
                )
            elif self.params.enable_daily_close_provisional and breach["daily_close_beyond"] is None:
                breach["daily_close_beyond"] = to_ist(obs.at).isoformat()
                event = {
                    "event_id": _event_id(self.zone.zone_id, "daily-close-provisional", breach["started_at"]),
                    "zone_id": self.zone.zone_id,
                    "symbol": self.zone.symbol,
                    "from_state": str(state),
                    "to_state": str(state),
                    "at": to_ist(obs.at).isoformat(),
                    "reason": "daily close beyond the distal boundary (provisional confirmation; the zone's own timeframe close is still required)",
                    "observed": obs.to_dict(),
                    "details": {"close": close, "kind": "DAILY_CLOSE_PROVISIONAL"},
                    "unobserved_prior_events": [],
                    "zone_status": str(self.zone.status),
                    "param_version": self.params.version,
                }
                self.watch.history.append(event)
                events.append(event)
            return events

        if state in {S.CONFIRMED_BREAK, S.RETEST, S.CONTINUATION_CONFIRMED} and breach is not None:
            if position != BEYOND:
                breach["closed_back_inside_after_confirmation"] = True
                return [
                    self._transition(
                        S.FALSE_BREAK,
                        obs,
                        "completed daily candle closed back inside the zone (or on its original side) after confirmation",
                        details={"confirmed": True, "close": close},
                        key=f"false-post-{breach['started_at']}",
                    )
                ]
            if state != S.CONTINUATION_CONFIRMED:
                beyond_by = (self.zone.distal - close) if self._demand else (close - self.zone.distal)
                # Holding rule "no_daily_close_inside": any daily close back
                # inside has already turned the event into FALSE_BREAK above.
                if atr_d > 0 and beyond_by >= self.params.continuation_atr_d * atr_d:
                    events.append(
                        self._transition(
                            S.CONTINUATION_CONFIRMED,
                            obs,
                            f"daily close {beyond_by / atr_d:.2f}×ATR_D beyond the distal boundary with no daily close back inside",
                            details={"close": close, "beyond_atr_d": round(beyond_by / atr_d, 3), "retest": breach["retest"]},
                            key=f"continuation-{breach['started_at']}",
                        )
                    )
        return events

    def on_zone_candle_close(self, obs: Observation, candle: dict[str, Any], atr_d: float) -> list[dict[str, Any]]:
        """A completed candle of the zone's own timeframe."""

        close = float(candle["close"])
        if position_of(self.zone, close) != BEYOND or self.zone.status == ZoneStatus.INVALIDATED:
            return []
        events: list[dict[str, Any]] = []
        if self.watch.state in {S.CONFIRMED_BREAK, S.RETEST, S.CONTINUATION_CONFIRMED}:
            return []
        if self.watch.state != S.PROVISIONAL_BREACH:
            # The monitor never saw the breach itself; record it as unobserved.
            events += self._start_breach(TOUCH, Observation(obs.at, TimingConfidence.UNOBSERVED, obs.quality), atr_d)
        self.zone.status = ZoneStatus.INVALIDATED
        self.zone.invalidation = {
            "date": str(candle.get("end")),
            "candle": str(candle.get("label")),
            "close": close,
            "distal": self.zone.distal,
            "rule": f"completed {self.zone.timeframe} candle closed beyond the distal boundary",
        }
        self.watch.breach["confirmed_at"] = to_ist(obs.at).isoformat()
        direction = "breakout" if self.zone.zone_type == ZoneType.RESISTANCE else "breakdown"
        flipped = make_flipped_zone(self.zone, candle, self.params)
        self.zone.flipped_zone_ids.append(flipped.zone_id)
        self.flipped.append(flipped)
        events.append(
            self._transition(
                S.CONFIRMED_BREAK,
                obs,
                f"confirmed {direction}: completed {self.zone.timeframe} candle closed beyond the distal boundary",
                details={
                    "direction": direction,
                    "close": close,
                    "candle": candle.get("label"),
                    "flipped_zone_id": flipped.zone_id,
                },
                key=f"confirmed-{self.watch.breach['started_at']}",
            )
        )
        return events

    # ── State-specific handlers ─────────────────────────────────────────────
    def _approach(self, obs: Observation, atr_d: float) -> list[dict[str, Any]]:
        eligible = self.zone.is_alert_eligible or (
            self.params.allow_post_touch_approach and self.zone.status == ZoneStatus.TOUCHED
        )
        reading = measure(self.zone, float(obs.price), atr_d, self.params)
        if reading.distance_atr_d is not None:
            self.watch.max_distance_atr_d = max(self.watch.max_distance_atr_d, reading.distance_atr_d)
        events: list[dict[str, Any]] = []

        if self.watch.approach_alerted and self.params.rearm_enabled and (
            reading.distance_atr_d is not None and reading.distance_atr_d > self.params.rearm_distance_atr_d
        ):
            self.watch.approach_alerted = False
            self.watch.category = None
            if self.watch.state == S.APPROACHING:
                events.append(
                    self._transition(
                        S.FRESH,
                        obs,
                        f"price moved more than {self.params.rearm_distance_atr_d}×ATR_D away; approach alert re-armed",
                        details={**reading.to_dict(), "rearm": True, "zone_freshness": str(self.zone.freshness)},
                        key=f"rearm-{obs.at.isoformat()}",
                    )
                )
            return events

        if not eligible or reading.category not in {Proximity.IMMINENT, Proximity.NEAR}:
            return events
        previous = self.watch.category
        upgrade = previous == str(Proximity.NEAR) and reading.category == Proximity.IMMINENT
        if self.watch.approach_alerted and not upgrade:
            return events
        self.watch.category = str(reading.category)
        self.watch.approach_alerted = True
        self.watch.max_distance_atr_d = reading.distance_atr_d or 0.0
        events.append(
            self._transition(
                S.APPROACHING,
                obs,
                f"{'fresh ' if self.zone.is_alert_eligible else 'previously tested '}zone entered {reading.category}",
                details={**reading.to_dict(), "previous_category": previous, "fresh": self.zone.is_alert_eligible},
                key=f"approach-{reading.category}-{len(self.zone.touches)}-{obs.at.isoformat()}",
            )
        )
        return events

    def _reaction_details(self, price: float, obs: Observation) -> dict[str, Any]:
        touch = self.watch.touch or {}
        started = touch.get("started_at")
        elapsed = None
        if started:
            elapsed = round((obs.at - datetime.fromisoformat(started)).total_seconds() / 60, 1)
        return {
            "price": price,
            "move_away_from_proximal": round(self._away_from_proximal(price), 4),
            "move_away_atr_d": round(self._away_from_proximal(price) / touch["atr_d"], 3) if touch.get("atr_d") else None,
            "elapsed_minutes_since_touch": elapsed,
            "position": position_of(self.zone, price),
            "deepest_penetration_fraction": round(self._penetration_fraction(touch["deepest"]), 3)
            if touch.get("deepest") is not None
            else None,
            "confirmation_candle": obs.interval,
        }

    def _pressure(self, extreme: float, obs: Observation) -> list[dict[str, Any]]:
        if self.watch.touch is not None:
            deepest = self.watch.touch.get("deepest")
            if deepest is None or (extreme < deepest if self._demand else extreme > deepest):
                self.watch.touch["deepest"] = extreme
        if self.watch.state == S.UNDER_PRESSURE:
            return []
        if self._penetration_fraction(extreme) < self.params.under_pressure_fraction:
            return []
        return [
            self._transition(
                S.UNDER_PRESSURE,
                obs,
                f"price penetrated ≥{self.params.under_pressure_fraction:.0%} of the zone width without trading beyond the distal boundary",
                details=self._reaction_details(extreme, obs),
                key=f"pressure-{len(self.zone.touches)}",
            )
        ]

    def _break_from_price(self, price: float, obs: Observation, atr_d: float) -> list[dict[str, Any]]:
        state = self.watch.state
        breach = self.watch.breach
        if state == S.FALSE_BREAK and position_of(self.zone, price) == BEYOND:
            return self._start_breach(TOUCH, obs, atr_d)
        if state == S.CONFIRMED_BREAK and breach is not None:
            back_at_zone = price >= self.zone.distal if self._demand else price <= self.zone.distal
            if back_at_zone:
                breach["retest"] = {"at": to_ist(obs.at).isoformat(), "price": price, "timing": str(obs.timing)}
                return [
                    self._transition(
                        S.RETEST,
                        obs,
                        "price returned to the broken zone after confirmation",
                        details={"price": price, "distal": self.zone.distal},
                        key=f"retest-{breach['started_at']}",
                    )
                ]
        return []


def make_flipped_zone(zone: Zone, candle: dict[str, Any], params: ScannerParams) -> Zone:
    """Separate record for a broken zone that may later act from the other side.

    Boundaries: the broken zone's distal edge becomes the proximal edge (it is
    met first from the new side) and its proximal edge becomes the distal
    edge.  Nothing assumes the flip will hold; it is just a new candidate.
    """

    new_type = ZoneType.DEMAND if zone.zone_type == ZoneType.RESISTANCE else ZoneType.RESISTANCE
    confirmation = {
        "label": str(candle.get("label")),
        "start": str(candle.get("start")),
        "end": str(candle.get("end")),
        "open": float(candle.get("open", math.nan)),
        "high": float(candle.get("high", math.nan)),
        "low": float(candle.get("low", math.nan)),
        "close": float(candle["close"]),
    }
    flipped = Zone(
        symbol=zone.symbol,
        exchange=zone.exchange,
        timeframe=zone.timeframe,
        zone_type=new_type,
        pattern=zone.pattern,
        construction_method=f"flipped_from:{zone.zone_id}",
        proximal=zone.distal,
        distal=zone.proximal,
        proximal_raw=zone.distal_raw,
        distal_raw=zone.proximal_raw,
        atr_z=zone.atr_z,
        atr_z_period=zone.atr_z_period,
        base_candles=zone.base_candles,
        leg_in_candles=[],
        departure_candles=[confirmation],
        leg_in_body=0.0,
        departure_body=0.0,
        base_avg_body_ratio=zone.base_avg_body_ratio,
        fvg=None,
        param_version=params.version,
        status=ZoneStatus.FRESH,
        freshness=Freshness.FRESH,
        freshness_reason=f"created on confirmation {confirmation['end']}; no retest yet",
        data_source=zone.data_source,
        parent_zone_id=zone.zone_id,
        evaluated_through=confirmation["end"],
    )
    return flipped
