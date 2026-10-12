"""Nightly zone engine and live price monitor.

Nightly (after the session closes):
  1. retrieve and validate daily OHLCV;
  2. feed the newly completed daily / zone-timeframe candles to every watched
     zone (end-of-day touch, reaction, breakout and invalidation handling);
  3. build W/M/Y candles, detect zones, evaluate freshness/tests/invalidation
     and score them; merge with stored zones (lifecycles only move forward);
  4. start watching newly detected FRESH zones;
  5. in END_OF_DAY mode, evaluate proximity on the completed close.

Live monitor (during the regular session, 09:15–15:30 IST):
  session-open gap checks → completed intraday candles since the previous
  check (so moves between polls are not missed) → latest quote.  Historical
  zones are never recomputed on a price update.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

import pandas as pd

from app.fresh_zones import path_clarity
from app.fresh_zones.alerts import AlertManager, build_alert
from app.fresh_zones.data import MarketDataProvider, Quote, eligibility, load_exclusions
from app.fresh_zones.detector import ZoneDetector
from app.fresh_zones.events import Observation, ZoneEventTracker, ZoneWatch
from app.fresh_zones.freshness import evaluate_zone_history
from app.fresh_zones.models import DataMode, DataQuality, EventState, TimingConfidence, Zone, ZoneStatus, ZoneType
from app.fresh_zones.params import ScannerParams, get_default_params
from app.fresh_zones.proximity import measure
from app.fresh_zones.scoring import score_zone
from app.fresh_zones.store import FreshZoneStore, merge_zone
from app.fresh_zones.timeframes import (
    IST,
    SessionCalendar,
    atr_series,
    build_candles,
    completed,
    to_ist,
    validate_daily,
)


logger = logging.getLogger(__name__)


@dataclass
class SymbolAnalysis:
    symbol: str
    daily: pd.DataFrame
    completed_daily: pd.DataFrame
    atr_d: pd.Series
    zone_candles: dict[str, pd.DataFrame]
    zones: list[Zone]
    swings: list[dict[str, Any]]
    issues: list[str] = field(default_factory=list)

    @property
    def latest_atr_d(self) -> float | None:
        values = self.atr_d.dropna()
        return float(values.iloc[-1]) if not values.empty else None


def session_close_at(day: date, calendar: SessionCalendar) -> datetime:
    hours = calendar.session_hours(day)
    close = hours[1] if hours else calendar.close_time
    return datetime.combine(day, close, tzinfo=IST)


def analyse_symbol(
    symbol: str,
    daily_raw: pd.DataFrame,
    *,
    params: ScannerParams,
    now: datetime,
    calendar: SessionCalendar,
    data_source: str = "",
) -> SymbolAnalysis:
    """Pure zone construction for one symbol (no storage, no side effects)."""

    daily, issues = validate_daily(daily_raw)
    asof = calendar.last_completed_session(now)
    completed_daily = daily[daily.index.date <= asof]
    # Anything dated after "now" cannot have been observed yet.
    daily = daily[daily.index.date <= to_ist(now).date()]
    atr_d = atr_series(completed_daily, params.atr_period, params.atr_method)
    detector = ZoneDetector(params)
    zones: list[Zone] = []
    zone_candles: dict[str, pd.DataFrame] = {}
    for timeframe in params.timeframes:
        candles = completed(build_candles(completed_daily, timeframe, params=params, now=now, calendar=calendar))
        zone_candles[timeframe] = candles
        for zone in detector.detect(candles, symbol=symbol, timeframe=timeframe, data_source=data_source):
            zones.append(evaluate_zone_history(zone, daily, candles, atr_d, params=params, asof=asof))
    for zone in zones:
        score_zone(zone, zones, params)
    swings = path_clarity.swing_points(completed_daily, params)
    return SymbolAnalysis(symbol, daily, completed_daily, atr_d, zone_candles, zones, swings, issues)


class FreshZoneEngine:
    def __init__(
        self,
        provider: MarketDataProvider,
        store: FreshZoneStore,
        params: ScannerParams | None = None,
        calendar: SessionCalendar | None = None,
        alert_manager: AlertManager | None = None,
        names: dict[str, str] | None = None,
    ):
        self.provider = provider
        self.store = store
        self.params = params or get_default_params()
        self.calendar = calendar or SessionCalendar.from_params(self.params)
        self.alerts = alert_manager or AlertManager(store, self.params)
        self.names = names or {}

    # ── Context for alerts ──────────────────────────────────────────────────
    def _context(self, zone: Zone, price: float | None, atr_d: float | None, zones: list[Zone],
                 swings: list[dict[str, Any]], event: dict[str, Any]) -> dict[str, Any]:
        context: dict[str, Any] = {"name": self.names.get(zone.symbol), "price": price, "atr_d": atr_d,
                                   "corporate_event": None}
        if price is None:
            return context
        if atr_d:
            context["proximity"] = measure(zone, price, atr_d, self.params).to_dict()
        if event["to_state"] == EventState.CONFIRMED_BREAK:
            direction = "up" if zone.zone_type == ZoneType.RESISTANCE else "down"
        else:
            direction = "up" if zone.zone_type == ZoneType.DEMAND else "down"
        context["next_opposing_zone"] = path_clarity.next_opposing_zone(zone, price, zones, direction)
        if event["to_state"] in {EventState.APPROACHING, EventState.FRESH}:
            context["path"] = path_clarity.assess(zone, price, zones, swings)
        return context

    def _alerts_for(self, events: list[dict[str, Any]], zones_by_id: dict[str, Zone], price: float | None,
                    atr_d: float | None, swings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        zones = list(zones_by_id.values())
        built = []
        for event in events:
            if event["to_state"] == EventState.FRESH:
                continue  # re-arming is logged as an event, not alerted
            zone = zones_by_id[event["zone_id"]]
            observed_price = event["observed"].get("price")
            ctx = self._context(zone, observed_price if observed_price is not None else price, atr_d, zones, swings, event)
            built.append(build_alert(event, zone, ctx, self.params))
        return built

    def _persist(self, symbol: str, zones: Iterable[Zone], trackers: Iterable[ZoneEventTracker],
                 events: list[dict[str, Any]]) -> None:
        self.store.save_zones(zones)
        for tracker in trackers:
            self.store.save_watch(symbol, tracker.watch)
        self.store.save_events(events)

    # ── Nightly ─────────────────────────────────────────────────────────────
    def run_nightly(self, symbols: Iterable[str], now: datetime | None = None, *,
                    enforce_universe: bool = True) -> dict[str, Any]:
        now = to_ist(now or datetime.now(timezone.utc))
        started = now.isoformat()
        exclusions = load_exclusions()
        quality = self.provider.quality()
        summary: dict[str, Any] = {"asof": self.calendar.last_completed_session(now).isoformat(),
                                   "param_version": self.params.version, "data": quality.to_dict(),
                                   "symbols": 0, "skipped": {}, "zones": 0, "fresh_zones": 0, "events": 0,
                                   "alerts_delivered": 0}
        pending_alerts: list[dict[str, Any]] = []
        for symbol in symbols:
            try:
                result = self._nightly_symbol(symbol, now, exclusions, quality, enforce_universe)
            except Exception as exc:  # one bad symbol must not stop the run
                logger.exception("fresh-zone nightly failed symbol=%s", symbol)
                summary["skipped"][symbol] = [f"error: {exc}"]
                continue
            if result.get("skipped"):
                summary["skipped"][symbol] = result["skipped"]
            summary["symbols"] += 1
            summary["zones"] += result.get("zones", 0)
            summary["fresh_zones"] += result.get("fresh_zones", 0)
            summary["events"] += len(result.get("events", []))
            pending_alerts += result.get("alerts", [])
        delivered = self.alerts.submit(pending_alerts)
        summary["alerts_delivered"] = sum(1 for a in delivered if a["delivery"]["delivered"])
        summary["alerts_suppressed"] = sum(1 for a in delivered if not a["delivery"]["delivered"])
        self.store.record_run("nightly", started, self.params.version, summary)
        return summary

    def _nightly_symbol(self, symbol: str, now: datetime, exclusions: set[str], quality: DataQuality,
                        enforce_universe: bool) -> dict[str, Any]:
        raw = self.provider.daily_history(symbol)
        analysis = analyse_symbol(symbol, raw, params=self.params, now=now, calendar=self.calendar,
                                  data_source=quality.source)
        eligible, reasons, stats = eligibility(symbol, analysis.completed_daily, self.params, exclusions,
                                               asof=pd.Timestamp(self.calendar.last_completed_session(now)))
        existing = {z.zone_id: z for z in self.store.list_zones(symbol=symbol)}
        result: dict[str, Any] = {"events": [], "alerts": []}

        # 1. End-of-day processing for zones already being watched.
        trackers, eod_events = self._eod_trackers(symbol, analysis, existing, quality)

        if not eligible and enforce_universe:
            result["skipped"] = reasons
            self._persist(symbol, existing.values(), trackers, eod_events)
            self.store.save_symbol(symbol, self._symbol_meta(analysis, stats, reasons, quality))
            result["events"] = eod_events
            result["alerts"] = self._alerts_for(eod_events, existing, None, analysis.latest_atr_d, analysis.swings)
            return result

        # 2. Refresh zones from history; lifecycles only move forward.
        tracked = {t.zone.zone_id: t for t in trackers}
        merged: dict[str, Zone] = dict(existing)
        for zone in analysis.zones:
            stored = tracked[zone.zone_id].zone if zone.zone_id in tracked else existing.get(zone.zone_id)
            if stored is not None:
                if abs(stored.proximal_raw / zone.proximal_raw - 1) > 0.002:
                    analysis.issues.append(f"{zone.zone_id}: price basis changed (corporate action?)")
                zone = merge_zone(stored, zone)
                if zone.zone_id in tracked:
                    tracked[zone.zone_id].zone = zone
            merged[zone.zone_id] = zone
            # 3. Watch newly detected fresh zones.
            if zone.zone_id not in tracked and zone.status == ZoneStatus.FRESH:
                watch = ZoneWatch(zone.zone_id, last_daily_processed=zone.evaluated_through,
                                  last_zone_candle_processed=zone.evaluated_through)
                tracker = ZoneEventTracker(zone, watch, self.params)
                trackers.append(tracker)
                tracked[zone.zone_id] = tracker

        # 4. End-of-day proximity (only when no intraday monitoring exists).
        events = list(eod_events)
        close = float(analysis.completed_daily["close"].iloc[-1]) if not analysis.completed_daily.empty else None
        atr_d = analysis.latest_atr_d
        if quality.mode == DataMode.END_OF_DAY and close is not None and atr_d:
            last_day = analysis.completed_daily.index[-1].date()
            obs = Observation(session_close_at(last_day, self.calendar), TimingConfidence.EOD_INFERRED, quality, price=close)
            for tracker in trackers:
                if tracker.watch.state in {EventState.FRESH, EventState.APPROACHING}:
                    events += tracker.on_price(obs, atr_d)

        self._persist(symbol, merged.values(), trackers, events)
        self.store.save_symbol(symbol, self._symbol_meta(analysis, stats, reasons, quality))
        result.update(
            zones=len(analysis.zones),
            fresh_zones=sum(1 for z in merged.values() if z.is_alert_eligible),
            events=events,
            alerts=self._alerts_for(events, merged, close, atr_d, analysis.swings),
        )
        return result

    def _eod_trackers(self, symbol: str, analysis: SymbolAnalysis, existing: dict[str, Zone],
                      quality: DataQuality) -> tuple[list[ZoneEventTracker], list[dict[str, Any]]]:
        """Feed newly completed daily and zone-timeframe candles to watched zones."""

        trackers: list[ZoneEventTracker] = []
        events: list[dict[str, Any]] = []
        eod_quality = DataQuality(quality.source, DataMode.END_OF_DAY, None, quality.limitations)
        records = {tf: frame.to_dict("records") for tf, frame in analysis.zone_candles.items()}
        for watch in self.store.watches_for(symbol):
            zone = existing.get(watch.zone_id)
            if zone is None:
                continue
            tracker = ZoneEventTracker(zone, watch, self.params)
            trackers.append(tracker)
            daily = analysis.completed_daily
            if watch.last_daily_processed:
                daily = daily[daily.index.date > date.fromisoformat(watch.last_daily_processed)]
            pending = [c for c in records.get(zone.timeframe, [])
                       if c["end"].isoformat() > (watch.last_zone_candle_processed or zone.formation_date)]
            fallback = quality.mode == DataMode.END_OF_DAY

            def close_zone_candles(before: date | None) -> None:
                # A zone-timeframe candle is fed once it is complete, in order.
                nonlocal events
                while pending and (before is None or pending[0]["period_end"] < before):
                    candle = pending.pop(0)
                    end = candle["end"]
                    at = session_close_at(end, self.calendar)
                    obs = Observation(at, TimingConfidence.EOD_INFERRED, eod_quality, price=float(candle["close"]))
                    events += tracker.on_zone_candle_close(obs, candle, _atr_before(analysis.atr_d, pd.Timestamp(end)))
                    watch.last_zone_candle_processed = end.isoformat()

            for ts, bar in daily.iterrows():
                day = ts.date()
                close_zone_candles(day)
                atr_d = _atr_before(analysis.atr_d, ts)
                at = session_close_at(day, self.calendar)
                close = float(bar["close"])
                candle_obs = Observation(at, TimingConfidence.EOD_INFERRED, eod_quality, low=float(bar["low"]),
                                         high=float(bar["high"]), interval=(day.isoformat(), day.isoformat()))
                close_obs = Observation(at, TimingConfidence.EOD_INFERRED, eod_quality, price=close)
                events += tracker.on_intraday_candle(candle_obs, close, atr_d, confirmation=False)
                events += tracker.on_daily_close(close_obs, close, atr_d, daily_confirmation_fallback=fallback)
                watch.last_daily_processed = day.isoformat()
            close_zone_candles(None)
        return trackers + self._adopt_flipped(trackers, existing), events

    def _adopt_flipped(self, trackers: list[ZoneEventTracker], zones: dict[str, Zone]) -> list[ZoneEventTracker]:
        """Start watching flipped zones created by confirmed breaks."""
        adopted = []
        for tracker in trackers:
            for flipped in tracker.flipped:
                zones[flipped.zone_id] = flipped
                watch = ZoneWatch(flipped.zone_id, last_daily_processed=flipped.formation_date,
                                  last_zone_candle_processed=flipped.formation_date)
                adopted.append(ZoneEventTracker(flipped, watch, self.params))
            tracker.flipped = []
        return adopted

    @staticmethod
    def _symbol_meta(analysis: SymbolAnalysis, stats: dict[str, Any], reasons: list[str],
                     quality: DataQuality) -> dict[str, Any]:
        daily = analysis.completed_daily
        return {
            "symbol": analysis.symbol,
            "atr_d": analysis.latest_atr_d,
            "last_session": daily.index[-1].date().isoformat() if not daily.empty else None,
            "last_close": float(daily["close"].iloc[-1]) if not daily.empty else None,
            "swings": analysis.swings,
            "universe": {"eligible": not reasons, "reasons": reasons, **stats},
            "data_issues": analysis.issues,
            "data": quality.to_dict(),
        }

    # ── Live monitor ────────────────────────────────────────────────────────
    def monitor_cycle(self, now: datetime | None = None, symbols: Iterable[str] | None = None) -> dict[str, Any]:
        now = to_ist(now or datetime.now(timezone.utc))
        if not self.calendar.is_open(now):
            return {"skipped": "outside the regular NSE session (09:15–15:30 IST)", "at": now.isoformat()}
        summary: dict[str, Any] = {"at": now.isoformat(), "symbols": 0, "events": 0, "no_quote": [], "stale_quote": []}
        pending: list[dict[str, Any]] = []
        for symbol in symbols or self.store.watched_symbols():
            try:
                outcome = self._monitor_symbol(symbol, now)
            except Exception:
                logger.exception("fresh-zone monitor failed symbol=%s", symbol)
                continue
            if outcome.get("no_quote"):
                summary["no_quote"].append(symbol)
                continue
            if outcome.get("stale_quote"):
                summary["stale_quote"].append(symbol)
                continue
            summary["symbols"] += 1
            summary["events"] += len(outcome["events"])
            pending += outcome["alerts"]
        processed = self.alerts.submit(pending)
        summary["alerts_delivered"] = sum(1 for a in processed if a["delivery"]["delivered"])
        summary["alerts_suppressed"] = sum(1 for a in processed if not a["delivery"]["delivered"])
        return summary

    def _monitor_symbol(self, symbol: str, now: datetime) -> dict[str, Any]:
        meta = self.store.get_symbol(symbol) or {}
        atr_d = meta.get("atr_d")
        quote: Quote | None = self.provider.quote(symbol)
        if quote is None or not atr_d:
            return {"no_quote": True}
        if to_ist(quote.at).date() != now.date():
            return {"stale_quote": True}  # e.g. an unannounced holiday or a stuck feed
        zones = {z.zone_id: z for z in self.store.list_zones(symbol=symbol)}
        monitor = meta.get("monitor", {})
        first_of_session = monitor.get("session_date") != now.date().isoformat()
        since = None if first_of_session else datetime.fromisoformat(monitor["last_check"])
        candles = self.provider.intraday_candles(symbol, self.params.reaction_candle_minutes, since)
        if first_of_session and not candles.empty:
            session_open = datetime.combine(now.date(), self.calendar.open_time, tzinfo=IST)
            candles = candles[candles.index >= pd.Timestamp(session_open)]
        quality = quote.quality
        live = quality.mode == DataMode.LIVE and not quote.timestamp_is_retrieval_time
        timing = TimingConfidence.LIVE_TRADE if live else TimingConfidence.DELAYED_QUOTE

        events: list[dict[str, Any]] = []
        trackers = []
        for watch in self.store.watches_for(symbol):
            zone = zones.get(watch.zone_id)
            if zone is None:
                continue
            tracker = ZoneEventTracker(zone, watch, self.params)
            trackers.append(tracker)
            if first_of_session:
                open_price = quote.session_open
                if open_price is None and not candles.empty and candles.index[0].time() == self.calendar.open_time:
                    open_price = float(candles["open"].iloc[0])
                if open_price is not None:
                    at = datetime.combine(now.date(), self.calendar.open_time, tzinfo=IST)
                    events += tracker.on_session_open(Observation(at, TimingConfidence.SESSION_OPEN, quality,
                                                                  price=open_price), atr_d)
            for ts, bar in candles.iterrows():
                end = ts + pd.Timedelta(minutes=self.params.reaction_candle_minutes)
                obs = Observation(end.to_pydatetime(), TimingConfidence.CANDLE_INFERRED, quality,
                                  low=float(bar["low"]), high=float(bar["high"]),
                                  interval=(ts.isoformat(), end.isoformat()))
                events += tracker.on_intraday_candle(obs, float(bar["close"]), atr_d, confirmation=True)
            events += tracker.on_price(Observation(quote.at, timing, quality, price=quote.price), atr_d)
        trackers += self._adopt_flipped(trackers, zones)

        self._persist(symbol, zones.values(), trackers, events)
        meta["monitor"] = {"session_date": now.date().isoformat(), "last_check": now.isoformat()}
        self.store.save_symbol(symbol, meta)
        return {"events": events,
                "alerts": self._alerts_for(events, zones, quote.price, atr_d, meta.get("swings", []))}

    # ── Queries ─────────────────────────────────────────────────────────────
    def ranked_opportunities(self, prices: dict[str, float] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Fresh, eligible zones ranked by proximity category, score and distance.

        Uses ``prices`` when given, else each symbol's last stored close (and
        labels it as such).  Only qualifying fresh zones are returned.
        """

        rows = []
        for zone in self.store.list_zones(statuses=[str(ZoneStatus.FRESH)]):
            if not zone.is_alert_eligible:
                continue
            meta = self.store.get_symbol(zone.symbol) or {}
            atr_d = meta.get("atr_d")
            price = (prices or {}).get(zone.symbol, meta.get("last_close"))
            if not atr_d or price is None:
                continue
            reading = measure(zone, price, atr_d, self.params)
            if reading.category is None:
                continue
            symbol_zones = self.store.list_zones(symbol=zone.symbol)
            rows.append({
                "zone": zone.to_dict(),
                "price": price,
                "price_basis": "supplied" if prices and zone.symbol in prices else f"last close {meta.get('last_session')}",
                "proximity": reading.to_dict(),
                "path": path_clarity.assess(zone, price, symbol_zones, meta.get("swings", [])),
                "rank": [str(reading.category), zone.score, reading.distance_atr_d],
            })
        order = {"IMMINENT": 0, "NEAR": 1, "NOT_PRIORITY": 2}
        rows.sort(key=lambda r: (order[r["proximity"]["category"]], -(r["zone"]["score"] or 0), r["proximity"]["distance_atr_d"]))
        return rows[:limit]


def _atr_before(atr_d: pd.Series, ts: pd.Timestamp) -> float:
    position = atr_d.index.searchsorted(ts, side="left")
    prior = atr_d.iloc[:position].dropna()
    if prior.empty:
        prior = atr_d.iloc[: position + 1].dropna()
    return float(prior.iloc[-1]) if not prior.empty else 0.0
