from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from app.services.entry_monitor import EntryMonitor
from app.services.realtime_features import RealtimeFeatureEngine, classify_live_trigger


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _context(**updates):
    base = {
        "entry": 100.0,
        "stop": 95.0,
        "target": 115.0,
        "direction": "bullish",
        "structure_valid": True,
        "trigger_tolerance_pct": 1.5,
    }
    base.update(updates)
    return base


def _tick(clock, price=100.0, volume=1_000, **updates):
    tick = {
        "ltp": price,
        "volume": volume,
        "timestamp": clock.value,
        "total_buy_qty": 3_000,
        "total_sell_qty": 1_000,
    }
    tick.update(updates)
    return tick


def _warm(update, clock, symbol, final_price=100.2):
    update(symbol, _tick(clock))
    clock.advance(4)
    update(symbol, _tick(clock, 100.1, 1_100))
    clock.advance(5)
    return update(symbol, _tick(clock, final_price, 1_300))


class RealtimeFeatureEngineTests(unittest.TestCase):
    def test_missing_timestamp_does_not_create_or_mutate_accumulator(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        bad = engine.update("FRESH", {"ltp": 1, "volume": 100_000})
        self.assertFalse(bad["isFresh"])
        self.assertEqual(bad["sampleCount"], 0)
        first = engine.update("FRESH", _tick(clock))
        self.assertEqual(first["sampleCount"], 1)
        clock.advance(4)
        engine.update("FRESH", {"ltp": 1, "volume": 900_000})
        second = engine.update("FRESH", _tick(clock, 100.1, 1_100))
        self.assertEqual(second["sampleCount"], 2)
        self.assertEqual(second["volumeDelta"], 100)
        self.assertEqual(second["previousPrice"], 100)

    def test_stale_out_of_order_and_duplicate_ticks_cannot_poison_features(self):
        for kind in ("STALE_TICK", "OUT_OF_ORDER_TICK", "DUPLICATE_TICK"):
            with self.subTest(kind=kind):
                clock = _Clock()
                engine = RealtimeFeatureEngine(clock=clock)
                previous = _warm(engine.update, clock, "SAFE")
                if kind == "STALE_TICK":
                    bad_tick = _tick(clock, 1, 9_000_000, timestamp=clock.value - 60)
                elif kind == "OUT_OF_ORDER_TICK":
                    bad_tick = _tick(clock, 1, 9_000_000, timestamp=clock.value - 2)
                else:
                    bad_tick = _tick(clock, 100.2, 1_300)
                bad = engine.update("SAFE", bad_tick)
                self.assertEqual(bad["tickRejectedReason"], kind)
                self.assertEqual(bad["sampleCount"], previous["sampleCount"])
                clock.advance(3)
                fresh = engine.update("SAFE", _tick(clock, 100.3, 1_400))
                self.assertEqual(fresh["sampleCount"], 4)
                self.assertEqual(fresh["volumeDelta"], 100)
                self.assertEqual(fresh["previousPrice"], 100.2)

    def test_long_gap_or_cumulative_volume_reset_requires_new_warmup(self):
        for reset_volume in (True, False):
            with self.subTest(reset_volume=reset_volume):
                clock = _Clock()
                engine = RealtimeFeatureEngine(clock=clock)
                _warm(engine.update, clock, "RESET")
                clock.advance(1 if reset_volume else 30)
                features = engine.update("RESET", _tick(clock, 100.3, 10 if reset_volume else 1_400))
                self.assertEqual(features["sampleCount"], 1)
                self.assertEqual(features["observationSpanSec"], 0)
                self.assertEqual(features["observedVwapVolume"], 0)
                self.assertEqual(classify_live_trigger(features, _context())["status"], "WAIT")

    def test_two_ticks_with_one_share_each_cannot_confirm(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        engine.update("TINY", _tick(clock, 100, 1))
        clock.advance(9)
        features = engine.update("TINY", _tick(clock, 100.2, 2))
        self.assertEqual(classify_live_trigger(features, _context())["confirmationMode"], "EVIDENCE_WARMUP")
        clock.advance(2)
        features = engine.update("TINY", _tick(clock, 100.3, 3))
        self.assertEqual(classify_live_trigger(features, _context())["confirmationMode"], "EXECUTED_VOLUME_REQUIRED")

    def test_buffered_ticks_delivered_together_do_not_fake_observation_time(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        for index, seconds_ago in enumerate((9, 5, 0)):
            features = engine.update("BUFFER", _tick(
                clock, 100 + index * 0.1, (1_000, 1_100, 1_400)[index],
                timestamp=clock.value - seconds_ago,
            ))
        self.assertEqual(features["sampleCount"], 3)
        self.assertEqual(features["observationSpanSec"], 0)
        self.assertEqual(features["imbalancePersistenceSec"], 0)
        self.assertEqual(classify_live_trigger(features, _context())["confirmationMode"], "EVIDENCE_WARMUP")

    def test_depth_only_after_warmup_still_needs_executed_volume(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        for i, step in enumerate((0, 4, 5)):
            clock.advance(step)
            tick = _tick(clock, 100 + 0.1 * i)
            tick.pop("volume")
            features = engine.update("DEPTH", tick)
        decision = classify_live_trigger(features, _context())
        self.assertEqual(decision["status"], "WAIT")
        self.assertEqual(decision["confirmationMode"], "EXECUTED_VOLUME_REQUIRED")

    def test_volume_only_confirmation_is_not_mislabeled_full_tick(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        for i, step in enumerate((0, 4, 5)):
            clock.advance(step)
            tick = {"ltp": 100 + 0.1 * i, "volume": (1_000, 1_100, 1_400)[i], "timestamp": clock.value}
            features = engine.update("VOLUME", tick)
        decision = classify_live_trigger(features, _context())
        self.assertEqual(decision["status"], "CONFIRMED")
        self.assertEqual(decision["confirmationMode"], "EXECUTED_VOLUME")
        strict = classify_live_trigger(features, _context(requires_full_tick=True))
        self.assertEqual(strict["status"], "WAIT")
        self.assertEqual(strict["confirmationMode"], "FULL_TICK_REQUIRED")

    def test_observed_vwap_uses_only_cumulative_volume_deltas(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)

        first = engine.update("TEST", _tick(clock, 100, 1_000))
        self.assertIsNone(first["sessionVwapApprox"])

        clock.advance(5)
        second = engine.update("TEST", _tick(clock, 102, 1_100))
        self.assertEqual(second["volumeDelta"], 100)
        self.assertEqual(second["sessionVwapApprox"], 102.0)

        clock.advance(5)
        third = engine.update("TEST", _tick(clock, 104, 1_300))
        self.assertEqual(third["volumeDelta"], 200)
        self.assertAlmostEqual(third["sessionVwapApprox"], 103.3333, places=4)
        self.assertGreater(third["volumeAcceleration"], 1.0)

    def test_persistent_depth_needs_supporting_price_response(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        first = engine.update(
            "FLOW",
            _tick(clock),
        )
        self.assertEqual(classify_live_trigger(first, _context())["status"], "WAIT")

        clock.advance(4)
        engine.update("FLOW", _tick(clock, 100.1, 1_100))
        clock.advance(5)
        second = engine.update(
            "FLOW",
            _tick(clock, 100.2, 1_300),
        )
        decision = classify_live_trigger(second, _context())
        self.assertEqual(decision["status"], "CONFIRMED")
        self.assertEqual(decision["confirmationMode"], "FULL_TICK")
        self.assertGreaterEqual(second["imbalancePersistenceSec"], 8)

    def test_displayed_buy_depth_with_falling_price_is_rejected(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        engine.update(
            "TRAP",
            _tick(clock, total_buy_qty=5_000),
        )
        clock.advance(9)
        features = engine.update(
            "TRAP",
            _tick(clock, 99.7, 1_100, total_buy_qty=5_500),
        )
        decision = classify_live_trigger(features, _context())
        self.assertEqual(decision["status"], "REJECT")
        self.assertEqual(decision["confirmationMode"], "PRICE_RESPONSE_GATE")
        self.assertTrue(any("price response" in risk for risk in decision["risks"]))

    def test_stale_exchange_tick_is_rejected(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        features = engine.update(
            "STALE",
            {"ltp": 100.0, "volume": 1_000, "timestamp": clock.value - 60},
        )
        decision = classify_live_trigger(features, _context())
        self.assertFalse(features["isFresh"])
        self.assertEqual(decision["status"], "REJECT")
        self.assertEqual(decision["confirmationMode"], "STALE_TICK")

    def test_price_only_feed_is_watch_only_not_actionable_confirmation(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        features = engine.update("LTPONLY", {"ltp": 100.0, "timestamp": clock.value})
        decision = classify_live_trigger(features, _context())

        self.assertEqual(decision["status"], "WAIT")
        self.assertEqual(decision["confirmationMode"], "PRICE_ONLY_WATCH")
        self.assertEqual(decision["dataQuality"]["quality"], "LOW")
        self.assertIn("not available", decision["identityDisclaimer"])

    def test_reversal_or_counter_regime_cannot_confirm_on_price_only_tick(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        features = engine.update("STRICT", {"ltp": 100.0, "timestamp": clock.value})
        decision = classify_live_trigger(
            features,
            _context(requires_full_tick=True, live_min_confirmations=3, counter_regime=True),
        )

        self.assertEqual(decision["status"], "WAIT")
        self.assertEqual(decision["confirmationMode"], "FULL_TICK_REQUIRED")
        self.assertEqual(decision["minimumIndependentConfirmations"], 3)
        self.assertTrue(decision["counterRegime"])


class LiveRiskGateTests(unittest.TestCase):
    def _ready_features(self, price=100.0, direction="bullish", clock=None):
        clock = clock or _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        side = 1 if direction == "bullish" else -1
        for index, step in enumerate((0, 4, 5)):
            clock.advance(step)
            features = engine.update("PLAN", _tick(
                clock, price - side * (2 - index) * 0.1, (1_000, 1_100, 1_400)[index],
                total_buy_qty=3_000 if side > 0 else 1_000,
                total_sell_qty=1_000 if side > 0 else 3_000,
            ))
        return features

    def _timed_candle(self, **updates):
        return {
            "status": "READY", "entry_ready": True, "direction": "bullish",
            "trigger": 100, "invalidation": 95, "target": 115, "timeframe": "15m",
            "latest_closed_time": "2026-09-15T10:15:00+05:30",
            "evaluated_at": "2026-09-15T10:29:00+05:30",
            "confirmation_time": "2026-09-15T10:00:00+05:30", **updates,
        }

    def _timed_features(self):
        return self._ready_features(clock=_Clock(datetime.fromisoformat("2026-09-15T10:30:00+05:30").timestamp()))

    def test_current_session_timed_candle_can_confirm(self):
        decision = classify_live_trigger(self._timed_features(), _context(candle_setup=self._timed_candle()))
        self.assertEqual(decision["status"], "CONFIRMED")

    def test_fresh_tick_cannot_revive_stale_latest_close_or_evaluation(self):
        features = self._timed_features()
        self.assertTrue(features["isFresh"])
        for key in ("latest_closed_time", "evaluated_at"):
            with self.subTest(key=key):
                candle = self._timed_candle(**{key: "2026-09-15T09:30:00+05:30"})
                decision = classify_live_trigger(features, _context(candle_setup=candle))
                self.assertEqual(decision["status"], "REJECT")
                self.assertEqual(decision["confirmationMode"], "CANDLE_FRESHNESS_GATE")
                self.assertTrue(any("older than" in reason for reason in decision["risks"]))

    def test_prior_session_confirmation_cannot_revive_with_current_quotes(self):
        for key in ("latest_closed_time", "evaluated_at", "confirmation_time"):
            with self.subTest(key=key):
                candle = self._timed_candle(**{key: "2026-09-14T14:30:00+05:30"})
                decision = classify_live_trigger(self._timed_features(), _context(candle_setup=candle))
                self.assertEqual(decision["status"], "REJECT")
                self.assertTrue(any("different NSE session" in reason for reason in decision["risks"]))

    def test_present_malformed_naive_or_future_candle_timing_fails_closed(self):
        for key in ("latest_closed_time", "evaluated_at", "confirmation_time"):
            for value in (None, "invalid", "2026-09-15T10:00:00", float("inf"), True, "2026-09-15T11:00:00+05:30"):
                with self.subTest(key=key, value=value):
                    decision = classify_live_trigger(self._timed_features(), _context(
                        candle_setup=self._timed_candle(**{key: value}),
                    ))
                    self.assertEqual(decision["status"], "REJECT")
                    self.assertEqual(decision["confirmationMode"], "CANDLE_FRESHNESS_GATE")

    def test_candle_expiry_uses_declared_timeframe(self):
        candle = self._timed_candle(timeframe="5m", latest_closed_time="2026-09-15T10:10:00+05:30")
        decision = classify_live_trigger(self._timed_features(), _context(candle_setup=candle))
        self.assertEqual(decision["status"], "REJECT")
        self.assertTrue(any("three 5-minute bars" in reason for reason in decision["risks"]))

    def test_mandatory_finite_plan_values(self):
        features = self._ready_features()
        for key in ("entry", "stop", "target"):
            for value in (None, 0, -10, float("nan"), float("inf"), True, "invalid"):
                with self.subTest(key=key, value=value):
                    decision = classify_live_trigger(features, _context(**{key: value}))
                    self.assertEqual(decision["status"], "REJECT")
                    self.assertEqual(decision["confirmationMode"], "INVALID_INPUT")

    def test_wrong_side_stop_or_target_rejected_for_both_directions(self):
        for direction, stop, target in (
            ("bullish", 101, 115), ("bullish", 95, 99),
            ("bearish", 99, 85), ("bearish", 105, 101),
        ):
            with self.subTest(direction=direction, stop=stop, target=target):
                decision = classify_live_trigger(
                    self._ready_features(direction=direction),
                    _context(direction=direction, stop=stop, target=target),
                )
                self.assertEqual(decision["status"], "REJECT")
                self.assertEqual(decision["confirmationMode"], "STRUCTURE_GATE")

    def test_cached_rr_cannot_override_worsened_live_rr(self):
        decision = classify_live_trigger(
            self._ready_features(101.5), _context(stop=97, target=106, rr=2),
        )
        self.assertEqual(decision["status"], "WAIT")
        self.assertEqual(decision["confirmationMode"], "LIVE_RISK_GATE")
        self.assertEqual(decision["liveRiskReward"], 1)
        self.assertEqual(decision["liveRiskPerShare"], 4.5)

    def test_bearish_current_price_rr_uses_short_geometry(self):
        decision = classify_live_trigger(
            self._ready_features(98.5, "bearish"),
            _context(direction="bearish", stop=103, target=94, rr=2),
        )
        self.assertEqual(decision["status"], "WAIT")
        self.assertEqual(decision["liveRiskReward"], 1)
        self.assertEqual(decision["liveRiskPerShare"], 4.5)

    def test_rr_cutoff_is_unrounded_and_cannot_be_lowered_below_floor(self):
        exactly = classify_live_trigger(self._ready_features(), _context(stop=98, target=103))
        self.assertEqual(exactly["status"], "CONFIRMED")
        just_below = classify_live_trigger(
            self._ready_features(100.00001),
            _context(stop=98, target=103, min_live_rr=0.5),
        )
        self.assertEqual(just_below["liveRiskReward"], 1.5)
        self.assertEqual(just_below["status"], "WAIT")
        self.assertEqual(just_below["minimumRiskReward"], 1.5)

    def test_candle_wait_watch_unavailable_never_bypassed_by_full_tick(self):
        for status in ("WAIT", "WATCH", "UNAVAILABLE"):
            with self.subTest(status=status):
                decision = classify_live_trigger(self._ready_features(), _context(candle_setup={
                    "status": status, "entry_ready": False, "reasons": ["Need a completed candle."],
                }))
                self.assertEqual(decision["status"], "WAIT")
                self.assertEqual(decision["confirmationMode"], "CANDLE_GATE")

    def test_ready_candle_requires_followthrough_trigger_not_proximity(self):
        candle = {
            "status": "READY", "entry_ready": True, "direction": "bullish",
            "trigger": 100.5, "invalidation": 95, "target": 115,
        }
        decision = classify_live_trigger(self._ready_features(), _context(candle_setup=candle))
        self.assertEqual(decision["status"], "WAIT")
        self.assertEqual(decision["confirmationMode"], "CANDLE_TRIGGER_GATE")
        self.assertEqual(decision["plannedEntry"], 100.5)

    def test_candle_invalidation_and_direction_are_enforced(self):
        for update in ({"invalidation": 100.1}, {"direction": "bearish"}, {"status": "INVALID"}):
            with self.subTest(update=update):
                candle = {
                    "status": "READY", "entry_ready": True, "direction": "bullish",
                    "trigger": 100, "invalidation": 95, "target": 115, **update,
                }
                decision = classify_live_trigger(self._ready_features(), _context(candle_setup=candle))
                self.assertEqual(decision["status"], "REJECT")

    def test_ready_candle_false_or_missing_entry_ready_cannot_confirm(self):
        for value in (None, False, "true", 1):
            with self.subTest(value=value):
                decision = classify_live_trigger(self._ready_features(), _context(candle_setup={
                    "status": "READY", "entry_ready": value, "direction": "bullish",
                    "trigger": 100, "invalidation": 95, "target": 115,
                }))
                self.assertEqual(decision["status"], "WAIT")


class EntryMonitorTests(unittest.TestCase):
    def _monitor(self, clock: _Clock | None = None) -> EntryMonitor:
        monitor = EntryMonitor(feature_engine=RealtimeFeatureEngine(clock=clock or _Clock()))
        monitor._send_telegram = Mock()
        return monitor

    def test_missing_or_nonfinite_stop_never_alerts(self):
        for stop in (None, 0, -1, float("nan"), float("inf"), True):
            with self.subTest(stop=stop):
                clock = _Clock()
                monitor = self._monitor(clock)
                monitor._register_watchlist({"results": [{
                    "symbol": "STOP", "direction": "bullish", "entry_trigger": 100,
                    "stop_loss": stop, "target_1": 115,
                }]})
                decision = _warm(monitor.update_tick, clock, "STOP")
                self.assertEqual(decision["status"], "REJECT")
                self.assertEqual(monitor.get_live_entries(), [])

    def test_alert_exposes_current_price_risk_and_candle_plan(self):
        clock = _Clock()
        monitor = self._monitor(clock)
        candle = {
            "status": "READY", "direction": "bullish", "entry_ready": True,
            "trigger": 100, "invalidation": 95, "target": 115,
        }
        monitor._register_watchlist({"results": [{
            "symbol": "RISK", "direction": "bullish", "entry_trigger": 99,
            "stop_loss": 94, "target_1": 120, "risk_reward": 5.2, "candle_setup": candle,
        }]})
        decision = _warm(monitor.update_tick, clock, "RISK")
        self.assertEqual(decision["status"], "CONFIRMED")
        alert = monitor.get_live_entries()[0]
        self.assertEqual(alert["entryLevel"], 100)
        self.assertEqual(alert["stop_loss"], 95)
        self.assertEqual(alert["plannedRiskReward"], 5.2)
        self.assertAlmostEqual(alert["rr"], (115 - 100.2) / (100.2 - 95), places=4)
        self.assertEqual(alert["rr"], alert["liveRiskReward"])
        self.assertEqual(alert["riskPerShare"], 5.2)
        self.assertEqual(alert["candle_setup"], candle)

    def test_rescan_candle_wait_revokes_previous_confirmation_immediately(self):
        clock = _Clock()
        monitor = self._monitor(clock)
        row = {"symbol": "PLAN", "entry_trigger": 100, "stop_loss": 95, "target_1": 115}
        monitor._register_watchlist({"results": [row]})
        _warm(monitor.update_tick, clock, "PLAN")
        self.assertEqual(len(monitor.get_live_entries()), 1)
        row["candle_setup"] = {"status": "WAIT", "entry_ready": False, "reasons": ["Need close."]}
        monitor._register_watchlist({"results": [row]})
        self.assertEqual(monitor.get_live_entries(), [])
        self.assertEqual(monitor.get_watched_count(), 1)
        clock.advance(1)
        decision = monitor.update_tick("PLAN", _tick(clock, 100.3, 1_500))
        self.assertEqual(decision["status"], "WAIT")
        self.assertEqual(decision["confirmationMode"], "CANDLE_GATE")

    def test_rescan_removal_revokes_previous_confirmation(self):
        clock = _Clock()
        monitor = self._monitor(clock)
        monitor._register_watchlist({"results": [{
            "symbol": "GONE", "entry_trigger": 100, "stop_loss": 95, "target_1": 115,
        }]})
        _warm(monitor.update_tick, clock, "GONE")
        monitor._register_watchlist({"all_entry_levels": []})
        self.assertEqual(monitor.get_live_entries(), [])

    def test_downgraded_alert_has_current_top_level_risk_not_old_confirmed_rr(self):
        clock = _Clock()
        monitor = self._monitor(clock)
        monitor._register_watchlist({"results": [{
            "symbol": "DRIFT", "direction": "bullish", "entry_trigger": 100,
            "stop_loss": 97, "target_1": 106, "rr": 2,
        }]})
        _warm(monitor.update_tick, clock, "DRIFT")
        self.assertEqual(len(monitor.get_live_entries()), 1)
        clock.advance(1)
        assessment = monitor.update_tick("DRIFT", _tick(clock, 101.5, 1_500))
        self.assertEqual(assessment["status"], "WAIT")
        self.assertEqual(monitor.get_live_entries(), [])
        cached = monitor._live_by_symbol["DRIFT"]
        self.assertEqual(cached["liveConfirmationMode"], "LIVE_RISK_GATE")
        self.assertEqual(cached["rr"], 1)
        self.assertEqual(cached["risk_reward"], 1)
        self.assertEqual(cached["liveRiskReward"], 1)
        self.assertEqual(cached["riskPerShare"], 4.5)
        self.assertEqual(cached["liveRisks"], assessment["risks"])

    def test_running_quotes_do_not_keep_stalled_candle_scanner_confirmed(self):
        clock = _Clock(datetime.fromisoformat("2026-09-15T10:30:00+05:30").timestamp())
        monitor = self._monitor(clock)
        candle = LiveRiskGateTests()._timed_candle()
        monitor._register_watchlist({"results": [{
            "symbol": "STALEPLAN", "direction": "bullish", "entry_trigger": 100,
            "stop_loss": 95, "target_1": 115, "candle_setup": candle,
        }]})
        _warm(monitor.update_tick, clock, "STALEPLAN")
        self.assertEqual(len(monitor.get_live_entries()), 1)
        for index in range(181):
            clock.advance(10)
            assessment = monitor.update_tick("STALEPLAN", _tick(clock, 100.2, 1_500 + index * 50))
        self.assertTrue(assessment["features"]["isFresh"])
        self.assertEqual(assessment["status"], "REJECT")
        self.assertEqual(assessment["confirmationMode"], "CANDLE_FRESHNESS_GATE")
        self.assertEqual(monitor.get_live_entries(), [])
        self.assertEqual(monitor._live_by_symbol["STALEPLAN"]["candle_setup"], candle)

    def test_malformed_price_tick_immediately_revokes_old_confirmation(self):
        clock = _Clock()
        monitor = self._monitor(clock)
        monitor._register_watchlist({"results": [{
            "symbol": "BADQUOTE", "entry_trigger": 100, "stop_loss": 95, "target_1": 115,
        }]})
        _warm(monitor.update_tick, clock, "BADQUOTE")
        self.assertEqual(len(monitor.get_live_entries()), 1)
        clock.advance(1)
        assessment = monitor.update_tick("BADQUOTE", _tick(clock, float("nan"), 1_500))
        self.assertEqual(assessment["status"], "REJECT")
        self.assertEqual(monitor.get_live_entries(), [])
        self.assertIsNone(monitor._live_by_symbol["BADQUOTE"]["livePrice"])

    def test_explicit_live_pool_is_an_allow_list(self):
        monitor = self._monitor()
        monitor._register_watchlist(
            {
                "results": [
                    {"symbol": "KEEP", "entry_trigger": 100, "stop_loss": 95, "target_1": 115},
                    {"symbol": "DROP", "entry_trigger": 200, "stop_loss": 190, "target_1": 230},
                ],
                "live_candidate_symbols": ["KEEP"],
            }
        )
        self.assertEqual(monitor.get_watched_symbols(), ["KEEP"])

    def test_authoritative_empty_entry_list_cannot_be_resurrected_by_ui_bucket(self):
        monitor = self._monitor()
        monitor._register_watchlist(
            {
                "all_entry_levels": [],
                "live_candidate_symbols": ["BLOCKED"],
                "top_opportunities": [
                    {"symbol": "BLOCKED", "entry_trigger": 100, "stop_loss": 95, "target_1": 115}
                ],
            }
        )

        self.assertEqual(monitor.get_watched_symbols(), [])

    def test_uncapped_legacy_payload_is_bounded_for_fast_tick_path(self):
        monitor = self._monitor()
        monitor._register_watchlist(
            {
                "pre_registered": [
                    {
                        "symbol": f"SYM{index}",
                        "entry_trigger": 100,
                        "stop_loss": 95,
                        "target_1": 115,
                    }
                    for index in range(125)
                ]
            }
        )
        self.assertEqual(monitor.get_watched_count(), 100)
        self.assertEqual(monitor.get_watched_symbols()[0], "SYM0")
        self.assertEqual(monitor.get_watched_symbols()[-1], "SYM99")

    def test_price_only_compatibility_call_keeps_watch_but_does_not_alert(self):
        monitor = self._monitor()
        subscriber = Mock()
        monitor.subscribe(subscriber)
        monitor._register_watchlist(
            {"results": [{"symbol": "LEGACY", "entry_trigger": 100, "stop_loss": 95, "target_1": 115}]}
        )

        assessment = monitor.update_price("LEGACY", 100.0)
        self.assertIsNone(assessment)  # compatibility method keeps its old return contract
        entries = monitor.get_live_entries()
        self.assertEqual(entries, [])
        self.assertEqual(monitor.get_watched_count(), 1)
        self.assertEqual(monitor.get_symbol_assessment("LEGACY")["status"], "WAIT")
        self.assertTrue(monitor._wait_for_notifications())
        subscriber.assert_not_called()

    def test_slow_confirmation_consumer_does_not_block_tick_processing(self):
        clock = _Clock()
        monitor = self._monitor(clock)
        consumer_started = threading.Event()
        release_consumer = threading.Event()

        def slow_consumer(_alert):
            consumer_started.set()
            release_consumer.wait(1.0)

        monitor.subscribe(slow_consumer)
        monitor._register_watchlist(
            {"results": [{"symbol": "ASYNC", "entry_trigger": 100, "stop_loss": 95, "target_1": 115}]}
        )

        started_at = time.monotonic()
        _warm(monitor.update_tick, clock, "ASYNC")
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.2)
        self.assertTrue(consumer_started.wait(0.5))
        release_consumer.set()
        self.assertTrue(monitor._wait_for_notifications())

    def test_full_tick_waits_then_alerts_with_derived_fields(self):
        clock = _Clock()
        monitor = self._monitor(clock)
        monitor._register_watchlist(
            {"results": [{"symbol": "FAST", "entry_trigger": 100, "stop_loss": 95, "target_1": 115}]}
        )

        first = monitor.update_tick(
            "FAST",
            _tick(clock),
        )
        self.assertEqual(first["status"], "WAIT")
        self.assertEqual(monitor.get_live_entries(), [])

        clock.advance(4)
        warming = monitor.update_tick("FAST", _tick(clock, 100.1, 1_100))
        self.assertEqual(warming["status"], "WAIT")
        clock.advance(5)
        second = monitor.update_tick(
            "FAST",
            _tick(clock, 100.2, 1_300),
        )
        self.assertEqual(second["status"], "CONFIRMED")
        entry = monitor.get_live_entries()[0]
        self.assertGreaterEqual(entry["imbalancePersistenceSec"], 8)
        self.assertIsNotNone(entry["bidAskRatio"])
        self.assertIn("liveConfirmation", entry)

    def test_invalid_structure_never_alerts(self):
        clock = _Clock()
        monitor = self._monitor(clock)
        monitor._register_watchlist(
            {
                "results": [
                    {
                        "symbol": "BROKEN",
                        "entry_trigger": 100,
                        "stop_loss": 95,
                        "target_1": 115,
                        "status": "INVALIDATED",
                    }
                ]
            }
        )
        decision = monitor.update_tick("BROKEN", {"ltp": 100.0})
        self.assertEqual(decision["status"], "REJECT")
        self.assertEqual(decision["confirmationMode"], "STRUCTURE_GATE")
        self.assertEqual(monitor.get_live_entries(), [])

    def test_explicit_neutral_row_is_never_inferred_as_a_buy(self):
        monitor = self._monitor()
        monitor._register_watchlist(
            {
                "all_entry_levels": [
                    {
                        "symbol": "NEUTRAL",
                        "direction": "neutral",
                        "entry_trigger": 100,
                        "stop_loss": 95,
                        "target_1": 115,
                    }
                ]
            }
        )

        self.assertEqual(monitor.get_watched_symbols(), [])
        self.assertIsNone(monitor.update_tick("NEUTRAL", {"ltp": 100.0}))

    def test_gael_style_reversal_keeps_strict_three_confirmation_gate(self):
        monitor = self._monitor()
        monitor._register_watchlist(
            {
                "all_entry_levels": [
                    {
                        "symbol": "GAEL",
                        "direction": "bullish",
                        "entry_trigger": 145,
                        "stop_loss": 137,
                        "target_1": 165,
                        "signal_stage": "BULLISH_REVERSAL_CANDIDATE",
                        "requires_live_confirmation": True,
                        "requires_full_tick": True,
                        "live_min_confirmations": 3,
                        "counterRegime": True,
                    }
                ]
            }
        )

        assessment = monitor.update_tick("GAEL", {"ltp": 145.0, "timestamp": monitor._features._clock()})

        self.assertEqual(assessment["status"], "WAIT")
        self.assertEqual(assessment["confirmationMode"], "FULL_TICK_REQUIRED")
        self.assertEqual(assessment["minimumIndependentConfirmations"], 3)
        self.assertTrue(assessment["counterRegime"])
        self.assertEqual(assessment["stop_loss"], 137)
        self.assertEqual(assessment["target_1"], 165)
        self.assertEqual(assessment["signalStage"], "BULLISH_REVERSAL_CANDIDATE")
        self.assertEqual(monitor.get_live_entries(), [])

    def test_confirmed_entry_expires_when_no_fresh_tick_arrives(self):
        clock = _Clock()
        monitor = self._monitor(clock)
        monitor._register_watchlist(
            {"results": [{"symbol": "AGES", "entry_trigger": 100, "stop_loss": 95, "target_1": 115}]}
        )

        with patch("app.services.entry_monitor.time.time", return_value=1_000.0):
            _warm(monitor.update_tick, clock, "AGES")
            self.assertEqual(len(monitor.get_live_entries()), 1)

        with patch("app.services.entry_monitor.time.time", return_value=1_100.0):
            self.assertEqual(monitor.get_live_entries(), [])
            assessment = monitor.get_symbol_assessment("AGES")

        self.assertEqual(assessment["status"], "REJECT")
        self.assertEqual(assessment["confirmationMode"], "STALE_AFTER_CONFIRMATION")
        self.assertTrue(any("No fresh broker tick" in risk for risk in assessment["risks"]))

    def test_wait_assessment_also_expires_during_feed_outage(self):
        monitor = self._monitor()
        monitor._register_watchlist(
            {"results": [{"symbol": "WAITOLD", "entry_trigger": 100, "stop_loss": 95, "target_1": 115}]}
        )
        with patch("app.services.entry_monitor.time.time", return_value=2_000.0):
            first = monitor.update_tick("WAITOLD", {"ltp": 97.0})
        self.assertEqual(first["status"], "WAIT")

        with patch("app.services.entry_monitor.time.time", return_value=2_100.0):
            stale = monitor.get_symbol_assessment("WAITOLD")

        self.assertEqual(stale["status"], "REJECT")
        self.assertEqual(stale["confirmationMode"], "STALE_FEED")
        self.assertEqual(stale["lastTickAgeSec"], 100.0)

    def test_telegram_message_carries_live_evidence_and_identity_warning(self):
        monitor = EntryMonitor(feature_engine=RealtimeFeatureEngine(clock=_Clock()))
        telegram = Mock(enabled=True)
        alert = {
            "symbol": "PROOF",
            "livePrice": 100.2,
            "entryLevel": 100.0,
            "distancePct": 0.2,
            "stopLoss": 95.0,
            "target": 115.0,
            "rr": 3.0,
            "timeHorizon": "swing",
            "direction": "bullish",
            "label": "LIVE ENTRY CONFIRMED",
            "setupType": "breakout",
            "liveTriggerScore": 85,
            "liveConfirmationMode": "FULL_TICK",
            "liveDataQuality": {"quality": "HIGH"},
            "vwapDistancePct": 0.15,
            "volumeVelocityPerMin": 12_500,
            "volumeAcceleration": 1.8,
            "bidAskRatio": 2.1,
            "imbalancePersistenceSec": 12,
            "imbalancePriceResponsePct": 0.2,
            "liveEvidence": ["Observed VWAP holds.", "Depth has price response."],
        }
        with patch(
            "app.services.telegram_market_alerts.get_telegram_market_alerts",
            return_value=telegram,
        ):
            monitor._send_telegram(alert)

        message = telegram.send_message.call_args.args[0]
        self.assertIn("85/100 evidence score", message)
        self.assertIn("VWAP +0.15%", message)
        self.assertIn("12,500/min", message)
        self.assertIn("Participant identity unknown", message)
        self.assertIn("not FII/DII confirmation", message)


if __name__ == "__main__":
    unittest.main()
