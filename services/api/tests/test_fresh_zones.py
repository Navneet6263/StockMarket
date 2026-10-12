from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd


API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fresh_zone_fixtures import DBR_WEEKS, chop, week_days  # noqa: E402

from app.fresh_zones.alerts import AlertManager, build_alert  # noqa: E402
from app.fresh_zones.data import FrameProvider, Quote, eligibility, normalise_symbols  # noqa: E402
from app.fresh_zones.detector import ZoneDetector, round_to_tick  # noqa: E402
from app.fresh_zones.engine import FreshZoneEngine, analyse_symbol  # noqa: E402
from app.fresh_zones.events import Observation, TransitionError, ZoneEventTracker, ZoneWatch  # noqa: E402
from app.fresh_zones.freshness import GAP_BEYOND, GAP_INTO, TOUCH, classify_entry  # noqa: E402
from app.fresh_zones.models import (  # noqa: E402
    DataMode,
    DataQuality,
    EventState,
    Freshness,
    Pattern,
    Proximity,
    TimingConfidence,
    Zone,
    ZoneStatus,
    ZoneType,
)
from app.fresh_zones.params import ScannerParams, params_from_overrides  # noqa: E402
from app.fresh_zones.path_clarity import CLEAR, HEAVILY_OBSTRUCTED, PARTIALLY_OBSTRUCTED, assess, swing_points  # noqa: E402
from app.fresh_zones.proximity import APPROACHING, BEYOND, INSIDE, measure, position_of  # noqa: E402
from app.fresh_zones.scoring import score_zone  # noqa: E402
from app.fresh_zones.store import FreshZoneStore, merge_zone  # noqa: E402
from app.fresh_zones.timeframes import IST, SessionCalendar, atr_series, build_candles, validate_daily  # noqa: E402


PARAMS = ScannerParams()
LIVE = DataQuality("test-feed", DataMode.LIVE, 0)
CAL = SessionCalendar()


def frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Positional higher-timeframe candles with synthetic weekly labels."""
    start = date(2020, 1, 6)
    records = []
    for i, (o, h, l, c) in enumerate(rows):
        monday = start + timedelta(weeks=i)
        records.append({
            "label": f"W{i:03d}", "start": monday, "end": monday + timedelta(days=4),
            "period_end": monday + timedelta(days=6), "open": o, "high": h, "low": l, "close": c,
            "volume": 1.0, "complete": True,
        })
    return pd.DataFrame(records)


def detect(rows, params=PARAMS, timeframe="W"):
    return ZoneDetector(params).detect(frame(rows), symbol="TEST", timeframe=timeframe)


def ist(*args) -> datetime:
    return datetime(*args, tzinfo=IST)


def demand_zone(**overrides) -> Zone:
    zone = detect(DBR_WEEKS)[0]
    for key, value in overrides.items():
        setattr(zone, key, value)
    return zone


def fresh(zone: Zone) -> Zone:
    zone.status, zone.freshness = ZoneStatus.FRESH, Freshness.FRESH
    return zone


# ── Parameters ──────────────────────────────────────────────────────────────

class ParamsTests(unittest.TestCase):
    def test_version_is_content_hash(self):
        self.assertEqual(ScannerParams().version, ScannerParams().version)
        self.assertNotEqual(ScannerParams().version, replace(ScannerParams(), near_atr_d=1.2).version)

    def test_overrides_are_validated(self):
        params = params_from_overrides({"year_definition": "financial", "max_width_pct": {"W": 5}})
        self.assertEqual(params.width_pct_cap("W"), 5.0)
        with self.assertRaises(ValueError):
            params_from_overrides({"not_a_param": 1})
        with self.assertRaises(ValueError):
            params_from_overrides({"imminent_atr_d": 2.0, "near_atr_d": 1.0})


# ── Timeframes ──────────────────────────────────────────────────────────────

class TimeframeTests(unittest.TestCase):
    def setUp(self):
        idx = pd.bdate_range("2023-01-02", "2024-05-10")
        self.daily, _ = validate_daily(pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
                                                     "Volume": 1000}, index=idx))

    def test_unfinished_candles_are_not_completed(self):
        weekly = build_candles(self.daily, "W", params=PARAMS, now=ist(2024, 5, 10, 16, 0))
        self.assertFalse(bool(weekly["complete"].iloc[-1]))  # week ends Sunday 12 May
        monthly = build_candles(self.daily, "M", params=PARAMS, now=ist(2024, 5, 10, 16, 0))
        self.assertEqual(monthly["label"].iloc[-1], "2024-05")
        self.assertFalse(bool(monthly["complete"].iloc[-1]))
        self.assertTrue(bool(monthly["complete"].iloc[-2]))

    def test_session_close_controls_daily_completion(self):
        self.assertFalse(CAL.session_closed(date(2024, 5, 10), ist(2024, 5, 10, 15, 29)))
        self.assertTrue(CAL.session_closed(date(2024, 5, 10), ist(2024, 5, 10, 15, 30)))
        self.assertEqual(CAL.last_completed_session(ist(2024, 5, 11, 10, 0)), date(2024, 5, 10))
        self.assertEqual(CAL.last_completed_session(ist(2024, 5, 13, 9, 0)), date(2024, 5, 10))

    def test_financial_year_and_partial_first_candle(self):
        fy = replace(PARAMS, year_definition="financial")
        yearly = build_candles(self.daily, "Y", params=fy, now=ist(2024, 5, 10, 16, 0))
        # Jan–Mar 2023 is a partial FY2022-23 candle and must be dropped.
        self.assertEqual(list(yearly["label"]), ["FY2023-24", "FY2024-25"])
        self.assertEqual(list(yearly["complete"]), [True, False])

    def test_validate_drops_malformed_and_duplicate_rows(self):
        raw = pd.DataFrame({"Open": [10, 10, 10], "High": [11, 9, 11], "Low": [9, 9, 9], "Close": [10, 10, 10]},
                           index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-02"]))
        clean, issues = validate_daily(raw)
        self.assertEqual(len(clean), 2)
        self.assertTrue(any("duplicate" in i for i in issues))

    def test_wilder_atr(self):
        candles = frame([(10, 12, 8, 11)] * 3)  # TR = 4 each
        atr = atr_series(candles, 2)
        self.assertTrue(pd.isna(atr.iloc[0]))
        self.assertAlmostEqual(atr.iloc[2], 4.0)


# ── Detection ───────────────────────────────────────────────────────────────

class DetectorTests(unittest.TestCase):
    def test_dbr_demand_boundaries(self):
        zones = detect(DBR_WEEKS)
        self.assertEqual(len(zones), 1)
        zone = zones[0]
        self.assertEqual(zone.zone_type, ZoneType.DEMAND)
        self.assertEqual(zone.pattern, Pattern.DBR)
        self.assertEqual(zone.proximal, 86.0)  # highest body edge of base
        self.assertEqual(zone.distal, 83.0)    # lowest low of base
        self.assertEqual([c["label"] for c in zone.base_candles], ["W021"])
        self.assertEqual([c["label"] for c in zone.leg_in_candles], ["W020"])
        self.assertEqual([c["label"] for c in zone.departure_candles], ["W022"])
        self.assertGreaterEqual(zone.departure_atr, 1.5)
        self.assertEqual(zone.formation_date, zone.departure_candles[-1]["end"])

    def test_rbd_resistance_mirror(self):
        rows = [*chop(20), (100, 116, 99, 115), (115, 117, 112, 114), (114, 114, 94, 95)]
        zone = detect(rows)[0]
        self.assertEqual((zone.zone_type, zone.pattern), (ZoneType.RESISTANCE, Pattern.RBD))
        self.assertEqual(zone.proximal, 114.0)  # lowest body edge
        self.assertEqual(zone.distal, 117.0)    # highest high

    def test_rbr_and_dbd_patterns(self):
        rbr = [*chop(20), (100, 116, 99, 115), (115, 118, 113, 116), (116, 136, 116, 135)]
        self.assertEqual(detect(rbr)[0].pattern, Pattern.RBR)
        dbd = [*chop(20), (100, 101, 84, 85), (85, 87, 82, 84), (84, 84, 64, 65)]
        self.assertEqual(detect(dbd)[0].pattern, Pattern.DBD)

    def test_weak_departure_or_no_close_beyond_base_rejects(self):
        weak = [*DBR_WEEKS[:22], (86.0, 98.0, 86.0, 97.0)]  # body 11 < 1.5×ATR
        self.assertEqual(detect(weak), [])
        no_close = [*DBR_WEEKS[:21], (85.0, 110.0, 83.0, 86.0), (86.0, 106.0, 86.0, 87.9)]
        self.assertFalse(any(z.zone_type == ZoneType.DEMAND for z in detect(no_close)))

    def test_leg_in_required(self):
        rows = [*chop(20), (100, 102, 98, 99), (99, 101, 97, 100), (100, 120, 100, 119)]
        self.assertEqual(detect(rows), [])

    def test_smallest_qualifying_base_is_selected(self):
        # Two base-like candles before the departure: the earlier one cannot
        # serve as leg-in, so the smallest qualifying base has two candles.
        rows = [*chop(20), (100, 101, 84, 85), (85, 88, 83, 86), (86, 88.5, 84, 85.5), (85.5, 106, 85, 105)]
        zone = detect(rows)[0]
        self.assertEqual(len(zone.base_candles), 2)
        self.assertEqual(zone.proximal, 86.0)
        self.assertEqual(zone.distal, 83.0)

    def test_two_candle_departure(self):
        rows = [*DBR_WEEKS[:22], (86.0, 96.0, 86.0, 95.0), (95.0, 104.0, 94.0, 103.0)]
        zone = detect(rows)[0]
        self.assertEqual(len(zone.departure_candles), 2)

    def test_zero_range_base_candle_rejected(self):
        rows = [*DBR_WEEKS[:21], (85.0, 85.0, 85.0, 85.0), (85.0, 106.0, 85.0, 105.0)]
        self.assertEqual(detect(rows), [])

    def test_width_limits_reject_or_review(self):
        wide = [*DBR_WEEKS[:21], (85.0, 92.0, 78.0, 86.0), (86.0, 110.0, 86.0, 109.0)]  # width 8 > 6% of 86
        self.assertEqual(detect(wide), [])
        review = detect(wide, replace(PARAMS, width_violation_action="review"))[0]
        self.assertTrue(review.review_required)
        self.assertTrue(any("%" in r for r in review.review_reasons))
        self.assertFalse(fresh(review).is_alert_eligible)

    def test_fvg_detection(self):
        zone = detect([*DBR_WEEKS[:23], (105.0, 112.0, 95.0, 110.0)])[0]
        self.assertEqual(zone.fvg["direction"], "bullish")
        self.assertAlmostEqual(zone.fvg["size"], 95.0 - 88.0)
        self.assertGreater(zone.fvg["size_atr_z"], 0.5)

    def test_tick_rounding_is_outward(self):
        self.assertEqual(round_to_tick(86.03, 0.05, "up"), 86.05)
        self.assertEqual(round_to_tick(83.02, 0.05, "down"), 83.0)
        self.assertEqual(round_to_tick(86.05, 0.05, "up"), 86.05)

    def test_yearly_needs_six_completed_candles(self):
        rows = DBR_WEEKS[-5:]
        self.assertEqual(detect(rows, timeframe="Y"), [])

    def test_insufficient_atr_history_yields_no_zone(self):
        self.assertEqual(detect(DBR_WEEKS[18:]), [])

    def test_zone_id_is_stable(self):
        self.assertEqual(detect(DBR_WEEKS)[0].zone_id, detect(DBR_WEEKS)[0].zone_id)


# ── Freshness ───────────────────────────────────────────────────────────────

class FreshnessTests(unittest.TestCase):
    def analyse(self, extra_weeks, now):
        return analyse_symbol("TEST", week_days([*DBR_WEEKS, *extra_weeks]), params=PARAMS, now=now, calendar=CAL)

    def demand(self, analysis):
        return next(z for z in analysis.zones if z.zone_type == ZoneType.DEMAND)

    def test_untouched_zone_is_fresh(self):
        zone = self.demand(self.analyse([(112, 113, 95, 96)], ist(2020, 7, 3, 18, 0)))
        self.assertEqual((zone.status, zone.freshness), (ZoneStatus.FRESH, Freshness.FRESH))
        self.assertTrue(zone.is_alert_eligible)

    def test_touch_ends_freshness_permanently(self):
        zone = self.demand(self.analyse([(112, 113, 85, 96), (96, 120, 95, 118)], ist(2020, 7, 10, 18, 0)))
        self.assertEqual(zone.status, ZoneStatus.TOUCHED)
        self.assertEqual(zone.freshness, Freshness.TESTED)
        self.assertEqual(zone.touches[0]["kind"], TOUCH)
        self.assertFalse(zone.is_alert_eligible)

    def test_test_counting_requires_excursion(self):
        # Two entries separated by a big rally (new test), then an immediate re-entry (same test).
        zone = self.demand(self.analyse(
            [(112, 113, 85, 96), (96, 120, 95, 118), (118, 119, 85.5, 90)], ist(2020, 7, 17, 18, 0)))
        self.assertEqual(zone.test_count, 2)
        self.assertGreater(len(zone.touches), zone.test_count - 1)

    def test_gap_classification(self):
        zone = demand_zone()
        self.assertEqual(classify_entry(zone, 85.0), GAP_INTO)
        self.assertEqual(classify_entry(zone, 80.0), GAP_BEYOND)
        self.assertEqual(classify_entry(zone, 90.0), TOUCH)

    def test_invalidation_needs_zone_timeframe_close(self):
        # Daily trade below the distal without a weekly close below: touched, not invalidated.
        zone = self.demand(self.analyse([(112, 113, 80, 96)], ist(2020, 7, 6, 18, 0)))
        self.assertEqual(zone.status, ZoneStatus.TOUCHED)
        zone = self.demand(self.analyse([(112, 113, 80, 81)], ist(2020, 7, 6, 18, 0)))
        self.assertEqual(zone.status, ZoneStatus.INVALIDATED)
        self.assertEqual(zone.invalidation["close"], 81)

    def test_stale_data_is_unknown(self):
        zone = self.demand(self.analyse([(112, 113, 95, 96)], ist(2020, 7, 20, 18, 0)))
        self.assertEqual(zone.freshness, Freshness.UNKNOWN)
        self.assertEqual(zone.status, ZoneStatus.UNVERIFIED)
        self.assertFalse(zone.is_alert_eligible)


# ── Proximity, scoring, path ────────────────────────────────────────────────

class ProximityScoringPathTests(unittest.TestCase):
    def test_positions_and_categories(self):
        zone = demand_zone()
        self.assertEqual(position_of(zone, 90), APPROACHING)
        self.assertEqual(position_of(zone, 86), INSIDE)  # at the proximal boundary is a touch
        self.assertEqual(position_of(zone, 82.9), BEYOND)
        self.assertEqual(measure(zone, 88, 5, PARAMS).category, Proximity.IMMINENT)   # 0.4 ATR
        self.assertEqual(measure(zone, 92, 5, PARAMS).category, Proximity.NEAR)       # 1.2 ATR
        self.assertEqual(measure(zone, 95, 5, PARAMS).category, Proximity.NOT_PRIORITY)
        reading = measure(zone, 92, 5, PARAMS)
        self.assertAlmostEqual(reading.distance, 6.0)
        self.assertAlmostEqual(reading.distance_pct, 6 / 92 * 100)
        self.assertIsNone(measure(zone, 85, 5, PARAMS).distance)

    def test_score_breakdown(self):
        zone = score_zone(demand_zone(), [], PARAMS)
        bd = zone.score_breakdown
        self.assertEqual(bd["base_quality"]["points"], 10 + 8 + 7)
        self.assertEqual(bd["timeframe_and_clarity"]["points"], 3 + 4 + 4)
        self.assertEqual(bd["fvg"]["points"], 10)  # week after departure gaps above the base
        expected_departure = 15 + 15 * (zone.departure_atr - 1.5) / 1.5
        self.assertAlmostEqual(bd["departure_strength"]["points"], round(expected_departure, 2))
        self.assertLessEqual(zone.score, 80)

    def test_opposite_zone_nearby_costs_clarity(self):
        zone = demand_zone()
        rbd = detect([*chop(20), (100, 116, 99, 115), (115, 117, 112, 114), (114, 114, 94, 95)])[0]
        rbd.proximal, rbd.distal = 88.0, 90.0
        rbd.departure_candles = [{**rbd.departure_candles[0], "end": "2019-01-01"}]
        score_zone(zone, [rbd], PARAMS)
        self.assertEqual(zone.score_breakdown["timeframe_and_clarity"]["no_conflicting_zones"]["points"], 0)

    def test_swings_have_no_lookahead(self):
        highs = [10.0] * 30
        highs[15] = 20.0
        daily = pd.DataFrame({"open": 9.0, "high": highs, "low": 8.0, "close": 9.0},
                             index=pd.bdate_range("2024-01-01", periods=30))
        self.assertEqual([p["type"] for p in swing_points(daily, PARAMS)], ["swing_high"])
        self.assertEqual(swing_points(daily.iloc[:25], PARAMS), [])  # only 9 bars after the high

    def test_path_classification(self):
        zone = demand_zone()
        self.assertEqual(assess(zone, 100, [], [])["classification"], CLEAR)
        swings = [{"type": "swing_low", "date": "2020-01-01", "price": 95.0},
                  {"type": "swing_high", "date": "2020-01-01", "price": 97.0}]
        self.assertEqual(assess(zone, 100, [], swings)["classification"], PARTIALLY_OBSTRUCTED)
        other = demand_zone()
        other.zone_id, other.proximal, other.distal = "Z-other", 92.0, 90.0
        result = assess(zone, 100, [other], swings)
        self.assertEqual(result["classification"], HEAVILY_OBSTRUCTED)
        self.assertEqual(result["barriers"][0]["kind"], "swing_low")  # nearest first


# ── Event lifecycle ─────────────────────────────────────────────────────────

class EventTests(unittest.TestCase):
    def setUp(self):
        self.zone = fresh(demand_zone())  # proximal 86, distal 83
        self.tracker = ZoneEventTracker(self.zone, ZoneWatch(self.zone.zone_id), PARAMS)
        self.t = ist(2020, 7, 13, 9, 30)

    def price(self, p, minutes=0, timing=TimingConfidence.LIVE_TRADE):
        return self.tracker.on_price(Observation(self.t + timedelta(minutes=minutes), timing, LIVE, price=p), 5.0)

    def candle(self, low, high, close, minutes, confirmation=True):
        obs = Observation(self.t + timedelta(minutes=minutes), TimingConfidence.CANDLE_INFERRED, LIVE,
                          low=low, high=high, interval=("a", "b"))
        return self.tracker.on_intraday_candle(obs, close, 5.0, confirmation=confirmation)

    def daily_close(self, close, minutes):
        obs = Observation(self.t + timedelta(minutes=minutes), TimingConfidence.EOD_INFERRED, LIVE, price=close)
        return self.tracker.on_daily_close(obs, close, 5.0)

    def states(self):
        return [e["to_state"] for e in self.tracker.watch.history]

    def test_approach_near_then_imminent_without_duplicates(self):
        self.assertEqual(self.price(100), [])                       # NOT_PRIORITY
        self.assertEqual(self.price(92)[0]["details"]["category"], "NEAR")
        self.assertEqual(self.price(91.5), [])                      # still NEAR: duplicate suppressed
        upgrade = self.price(88)
        self.assertEqual(upgrade[0]["details"]["category"], "IMMINENT")
        self.assertEqual(upgrade[0]["details"]["previous_category"], "NEAR")
        self.assertEqual(self.price(87.5), [])

    def test_rearm_does_not_restore_freshness(self):
        self.price(92)
        rearm = self.price(97)  # 2.2 ATR away
        self.assertEqual(rearm[0]["to_state"], EventState.FRESH)
        self.assertEqual(self.price(92)[0]["to_state"], EventState.APPROACHING)
        self.price(85)   # touch
        self.price(100)  # far away
        self.assertEqual(self.zone.status, ZoneStatus.TOUCHED)
        self.assertEqual(self.price(92), [])  # no fresh-zone approach after a touch

    def test_touch_starts_reaction_observation(self):
        self.price(92)
        events = self.price(85.5)
        self.assertEqual([e["to_state"] for e in events], [EventState.TOUCH_DETECTED, EventState.REACTION_UNDER_OBSERVATION])
        self.assertEqual(self.zone.status, ZoneStatus.TOUCHED)
        self.assertEqual(self.zone.test_count, 1)

    def test_sudden_touch_marks_approach_unobserved(self):
        events = self.price(85.0)
        self.assertTrue(events[0]["unobserved_prior_events"])

    def test_candle_inferred_touch_between_polls(self):
        events = self.candle(85.0, 90.0, 89.0, 15, confirmation=False)
        self.assertEqual(events[0]["to_state"], EventState.TOUCH_DETECTED)
        self.assertEqual(events[0]["observed"]["timing"], "CANDLE_INFERRED")

    def test_reaction_confirmed_on_completed_15m_close(self):
        self.price(85.5)
        self.assertEqual(self.candle(85.0, 88.0, 87.9, 15), [])      # 0.38 ATR < 0.5
        events = self.candle(87.0, 89.0, 88.6, 30)                   # 0.52 ATR
        self.assertEqual(events[0]["to_state"], EventState.REACTION_CONFIRMED)

    def test_under_pressure_then_consolidating(self):
        self.price(85.5)
        self.assertEqual(self.price(84.4)[0]["to_state"], EventState.UNDER_PRESSURE)  # 53% penetration
        self.daily_close(85.0, 400)
        events = self.daily_close(85.2, 1800)
        self.assertEqual(events[0]["to_state"], EventState.CONSOLIDATING)

    def test_failed_reaction(self):
        self.price(85.5)
        self.daily_close(86.5, 400)
        events = self.daily_close(87.0, 1800)  # outside, but only 0.2 ATR away
        self.assertEqual(events[0]["to_state"], EventState.FAILED_REACTION)

    def test_gap_through_into_zone(self):
        obs = Observation(self.t, TimingConfidence.SESSION_OPEN, LIVE, price=85.0)
        events = self.tracker.on_session_open(obs, 5.0)
        self.assertEqual(events[0]["details"]["touch_kind"], GAP_INTO)
        self.assertEqual(self.zone.touches[0]["kind"], GAP_INTO)

    def test_gap_beyond_then_confirmed_breakdown_and_flip(self):
        obs = Observation(self.t, TimingConfidence.SESSION_OPEN, LIVE, price=80.0)
        events = self.tracker.on_session_open(obs, 5.0)
        self.assertEqual(events[0]["to_state"], EventState.PROVISIONAL_BREACH)
        self.assertEqual(events[0]["details"]["breach_kind"], GAP_BEYOND)
        candle = {"label": "W030", "start": "2020-07-13", "end": "2020-07-17", "open": 80, "high": 82, "low": 75, "close": 76}
        events = self.tracker.on_zone_candle_close(Observation(self.t, TimingConfidence.EOD_INFERRED, LIVE, price=76), candle, 5.0)
        self.assertEqual(events[0]["to_state"], EventState.CONFIRMED_BREAK)
        self.assertEqual(self.zone.status, ZoneStatus.INVALIDATED)
        flipped = self.tracker.flipped[0]
        self.assertEqual(flipped.zone_type, ZoneType.RESISTANCE)
        self.assertEqual((flipped.proximal, flipped.distal), (83.0, 86.0))
        self.assertEqual(flipped.parent_zone_id, self.zone.zone_id)
        self.assertFalse(flipped.is_alert_eligible)

    def test_retest_continuation_and_false_break(self):
        self.price(82.0)  # breach (touch unobserved)
        self.assertIn("TOUCH_DETECTED", " ".join(self.tracker.watch.history[0]["unobserved_prior_events"]))
        candle = {"label": "W", "start": "s", "end": "e", "close": 80.0}
        self.tracker.on_zone_candle_close(Observation(self.t, TimingConfidence.EOD_INFERRED, LIVE, price=80), candle, 5.0)
        self.assertEqual(self.price(83.5, 60)[0]["to_state"], EventState.RETEST)
        self.assertEqual(self.daily_close(78.5, 400), [])  # 0.9 ATR beyond: not yet
        self.assertEqual(self.daily_close(77.5, 1800)[0]["to_state"], EventState.CONTINUATION_CONFIRMED)
        self.assertEqual(self.daily_close(84.0, 3200)[0]["to_state"], EventState.FALSE_BREAK)
        self.assertTrue(self.tracker.watch.breach["closed_back_inside_after_confirmation"])

    def test_false_break_before_confirmation(self):
        self.price(82.0)
        events = self.daily_close(84.0, 400)
        self.assertEqual(events[0]["to_state"], EventState.FALSE_BREAK)
        self.assertFalse(events[0]["details"]["confirmed"])

    def test_disallowed_transition_raises(self):
        with self.assertRaises(TransitionError):
            self.tracker._transition(EventState.CONTINUATION_CONFIRMED, Observation(self.t, TimingConfidence.LIVE_TRADE, LIVE), "x")


# ── Alerts and storage ──────────────────────────────────────────────────────

class ListSink:
    name = "list"

    def __init__(self):
        self.items = []

    def deliver(self, alert):
        self.items.append(alert)
        return True


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.store = FreshZoneStore(":memory:")
        self.sink = ListSink()
        self.zone = score_zone(fresh(demand_zone()), [], PARAMS)
        self.store.save_zones([self.zone])

    def approach_event(self, at, category="NEAR", key="1"):
        return {"event_id": f"E-{key}", "zone_id": self.zone.zone_id, "symbol": "TEST", "from_state": "FRESH",
                "to_state": "APPROACHING", "at": at.isoformat(), "reason": "r",
                "observed": Observation(at, TimingConfidence.LIVE_TRADE, LIVE, price=92.0).to_dict(),
                "details": {"category": category, "distance_rupees": 6.0, "distance_pct": 6.5, "distance_atr_d": 1.2},
                "unobserved_prior_events": []}

    def test_alert_record_has_required_fields(self):
        alert = build_alert(self.approach_event(ist(2020, 7, 13, 10)), self.zone, {"atr_d": 5.0}, PARAMS)
        for key in ("alert_id", "zone_id", "param_version", "stock", "price", "zone", "zone_status", "quality",
                    "distance", "states", "measurements", "departure", "fvg", "conditions", "invalidation_level",
                    "data", "limitations", "disclaimer"):
            self.assertIn(key, alert)
        self.assertEqual(alert["invalidation_level"], 83.0)
        self.assertTrue(alert["data"]["is_live"])
        self.assertIn("not a trade recommendation", alert["disclaimer"].lower())

    def test_duplicate_cooldown_upgrade_and_cap(self):
        manager = AlertManager(self.store, replace(PARAMS, daily_alert_cap=2), [self.sink])
        t = ist(2020, 7, 13, 10)
        first = build_alert(self.approach_event(t), self.zone, {}, PARAMS)
        manager.submit([first])
        manager.submit([build_alert(self.approach_event(t), self.zone, {}, PARAMS)])  # same event id
        self.assertEqual(len(self.sink.items), 1)
        repeat = build_alert(self.approach_event(t + timedelta(minutes=10), key="2"), self.zone, {}, PARAMS)
        self.assertIn("cooldown", manager.submit([repeat])[0]["delivery"]["suppressed_reason"])
        upgrade = build_alert(self.approach_event(t + timedelta(minutes=11), "IMMINENT", key="3"), self.zone, {}, PARAMS)
        self.assertTrue(manager.submit([upgrade])[0]["delivery"]["delivered"])
        later = build_alert(self.approach_event(t + timedelta(hours=3), key="4"), self.zone, {}, PARAMS)
        self.assertIn("daily cap", manager.submit([later])[0]["delivery"]["suppressed_reason"])
        self.assertEqual(len(self.store.list_alerts(delivered=False)), 2)

    def test_merge_never_restores_freshness(self):
        stored = demand_zone(status=ZoneStatus.TOUCHED, freshness=Freshness.TESTED,
                             touches=[{"date": "2020-07-13", "kind": TOUCH}], tests=[{"test_number": 1}])
        merged = merge_zone(stored, fresh(demand_zone()))
        self.assertEqual(merged.status, ZoneStatus.TOUCHED)
        self.assertEqual(len(merged.touches), 1)

    def test_zone_roundtrip(self):
        loaded = self.store.get_zone(self.zone.zone_id)
        self.assertEqual(loaded.to_dict(), self.zone.to_dict())


# ── Universe and end-to-end ─────────────────────────────────────────────────

class EngineTests(unittest.TestCase):
    PARAMS = replace(PARAMS, min_history_sessions=50, min_avg_traded_value_cr=1.0)

    def setUp(self):
        self.daily = week_days([*DBR_WEEKS, (112, 113, 95, 96), (96, 97, 88, 89)])
        self.store = FreshZoneStore(":memory:")
        self.sink = ListSink()
        self.provider = FrameProvider({"TEST": self.daily})
        self.engine = FreshZoneEngine(self.provider, self.store, self.PARAMS, CAL,
                                      AlertManager(self.store, self.PARAMS, [self.sink]))

    def test_universe_helpers(self):
        self.assertEqual(normalise_symbols(["infy.ns", "INFY.BO", "TCS", "^NSEI"]), ["INFY", "TCS"])
        clean, _ = validate_daily(self.daily)
        ok, reasons, stats = eligibility("TEST", clean, self.PARAMS, {"TEST"})
        self.assertFalse(ok)
        self.assertIn("excluded", reasons[0])

    def test_nightly_then_live_monitor(self):
        summary = self.engine.run_nightly(["TEST"], ist(2020, 7, 10, 18, 0))
        self.assertEqual(summary["skipped"], {})
        self.assertGreaterEqual(summary["fresh_zones"], 1)
        approach = [a for a in self.sink.items if a["states"]["current"] == "APPROACHING"]
        self.assertEqual(approach[0]["distance"]["category"], "NEAR")
        self.assertEqual(approach[0]["data"]["mode"], "END_OF_DAY")
        self.assertEqual(approach[0]["price"]["timing"], "EOD_INFERRED")

        self.provider.quotes = {"TEST": Quote("TEST", 85.5, ist(2020, 7, 13, 10, 5), LIVE, session_open=88.5)}
        result = self.engine.monitor_cycle(ist(2020, 7, 13, 10, 6))
        self.assertEqual(result["symbols"], 1)
        states = [a["states"]["current"] for a in self.sink.items]
        self.assertIn("TOUCH_DETECTED", states)
        demand = next(z for z in self.store.list_zones(symbol="TEST") if z.zone_type == ZoneType.DEMAND)
        self.assertEqual(demand.status, ZoneStatus.TOUCHED)

        # Re-running the nightly engine must not make the touched zone fresh again.
        self.engine.run_nightly(["TEST"], ist(2020, 7, 13, 18, 0))
        demand = self.store.get_zone(demand.zone_id)
        self.assertEqual(demand.status, ZoneStatus.TOUCHED)
        ranked = self.engine.ranked_opportunities(prices={"TEST": 88.0})
        self.assertNotIn(demand.zone_id, [row["zone"]["zone_id"] for row in ranked])

    def test_monitor_skips_outside_session_and_stale_quotes(self):
        self.assertIn("skipped", self.engine.monitor_cycle(ist(2020, 7, 13, 9, 0)))
        self.engine.run_nightly(["TEST"], ist(2020, 7, 10, 18, 0))
        self.provider.quotes = {"TEST": Quote("TEST", 85.5, ist(2020, 7, 10, 15, 0), LIVE)}
        self.assertEqual(self.engine.monitor_cycle(ist(2020, 7, 13, 10, 0))["stale_quote"], ["TEST"])

    def test_replay_lifecycle(self):
        from app.fresh_zones.replay import replay

        weeks = [*chop(60), *DBR_WEEKS[20:], (112, 113, 95, 96), (96, 97, 88, 89), (89, 90, 84.5, 87),
                 (87, 88, 80, 81), (81, 82, 70, 71)]
        report = replay("TEST", week_days(weeks), "2021-04-01", params=PARAMS)
        states = [e["to_state"] for e in report["events"]]
        for expected in ("APPROACHING", "TOUCH_DETECTED", "REACTION_UNDER_OBSERVATION", "PROVISIONAL_BREACH",
                         "CONFIRMED_BREAK", "CONTINUATION_CONFIRMED"):
            self.assertIn(expected, states)
        self.assertTrue(any(z["parent_zone_id"] for z in report["zones"]))


if __name__ == "__main__":
    unittest.main()
