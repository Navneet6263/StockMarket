from __future__ import annotations

import threading
import time
import unittest
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


class RealtimeFeatureEngineTests(unittest.TestCase):
    def test_observed_vwap_uses_only_cumulative_volume_deltas(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)

        first = engine.update("TEST", {"ltp": 100, "volume": 1_000})
        self.assertIsNone(first["sessionVwapApprox"])

        clock.advance(5)
        second = engine.update("TEST", {"ltp": 102, "volume": 1_100})
        self.assertEqual(second["volumeDelta"], 100)
        self.assertEqual(second["sessionVwapApprox"], 102.0)

        clock.advance(5)
        third = engine.update("TEST", {"ltp": 104, "volume": 1_300})
        self.assertEqual(third["volumeDelta"], 200)
        self.assertAlmostEqual(third["sessionVwapApprox"], 103.3333, places=4)
        self.assertGreater(third["volumeAcceleration"], 1.0)

    def test_persistent_depth_needs_supporting_price_response(self):
        clock = _Clock()
        engine = RealtimeFeatureEngine(clock=clock)
        first = engine.update(
            "FLOW",
            {"ltp": 100.0, "total_buy_qty": 3_000, "total_sell_qty": 1_000},
        )
        self.assertEqual(classify_live_trigger(first, _context())["status"], "WAIT")

        clock.advance(9)
        second = engine.update(
            "FLOW",
            {"ltp": 100.2, "total_buy_qty": 3_200, "total_sell_qty": 1_000},
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
            {"ltp": 100.0, "total_buy_qty": 5_000, "total_sell_qty": 1_000},
        )
        clock.advance(9)
        features = engine.update(
            "TRAP",
            {"ltp": 99.7, "total_buy_qty": 5_500, "total_sell_qty": 1_000},
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

    def test_price_only_feed_degrades_with_explicit_low_quality_label(self):
        engine = RealtimeFeatureEngine(clock=_Clock())
        features = engine.update("LTPONLY", {"ltp": 100.0})
        decision = classify_live_trigger(features, _context())

        self.assertEqual(decision["status"], "CONFIRMED")
        self.assertEqual(decision["confirmationMode"], "PRICE_ONLY_FALLBACK")
        self.assertEqual(decision["dataQuality"]["quality"], "LOW")
        self.assertIn("not available", decision["identityDisclaimer"])

    def test_reversal_or_counter_regime_cannot_confirm_on_price_only_tick(self):
        engine = RealtimeFeatureEngine(clock=_Clock())
        features = engine.update("STRICT", {"ltp": 100.0})
        decision = classify_live_trigger(
            features,
            _context(requires_full_tick=True, live_min_confirmations=3, counter_regime=True),
        )

        self.assertEqual(decision["status"], "WAIT")
        self.assertEqual(decision["confirmationMode"], "FULL_TICK_REQUIRED")
        self.assertEqual(decision["minimumIndependentConfirmations"], 3)
        self.assertTrue(decision["counterRegime"])


class EntryMonitorTests(unittest.TestCase):
    def _monitor(self, clock: _Clock | None = None) -> EntryMonitor:
        monitor = EntryMonitor(feature_engine=RealtimeFeatureEngine(clock=clock or _Clock()))
        monitor._send_telegram = Mock()
        return monitor

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

    def test_price_only_call_preserves_legacy_alert_with_quality_warning(self):
        monitor = self._monitor()
        subscriber = Mock()
        monitor.subscribe(subscriber)
        monitor._register_watchlist(
            {"results": [{"symbol": "LEGACY", "entry_trigger": 100, "stop_loss": 95, "target_1": 115}]}
        )

        assessment = monitor.update_price("LEGACY", 100.0)
        self.assertIsNone(assessment)  # compatibility method keeps its old return contract
        entries = monitor.get_live_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["liveTriggerStatus"], "CONFIRMED")
        self.assertEqual(entries[0]["liveConfirmationMode"], "PRICE_ONLY_FALLBACK")
        self.assertEqual(entries[0]["participantIdentity"], "UNKNOWN_FROM_MARKET_TICK")
        self.assertIn("identityDisclaimer", entries[0]["liveDataQuality"])
        self.assertTrue(monitor._wait_for_notifications())
        subscriber.assert_called_once()

    def test_slow_confirmation_consumer_does_not_block_tick_processing(self):
        monitor = self._monitor()
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
        monitor.update_price("ASYNC", 100.0)
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
            {"ltp": 100.0, "total_buy_qty": 3_000, "total_sell_qty": 1_000},
        )
        self.assertEqual(first["status"], "WAIT")
        self.assertEqual(monitor.get_live_entries(), [])

        clock.advance(9)
        second = monitor.update_tick(
            "FAST",
            {"ltp": 100.2, "total_buy_qty": 3_200, "total_sell_qty": 1_000},
        )
        self.assertEqual(second["status"], "CONFIRMED")
        entry = monitor.get_live_entries()[0]
        self.assertGreaterEqual(entry["imbalancePersistenceSec"], 8)
        self.assertIsNotNone(entry["bidAskRatio"])
        self.assertIn("liveConfirmation", entry)

    def test_invalid_structure_never_alerts(self):
        monitor = self._monitor()
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

        assessment = monitor.update_tick("GAEL", {"ltp": 145.0})

        self.assertEqual(assessment["status"], "WAIT")
        self.assertEqual(assessment["confirmationMode"], "FULL_TICK_REQUIRED")
        self.assertEqual(assessment["minimumIndependentConfirmations"], 3)
        self.assertTrue(assessment["counterRegime"])
        self.assertEqual(assessment["stop_loss"], 137)
        self.assertEqual(assessment["target_1"], 165)
        self.assertEqual(assessment["signalStage"], "BULLISH_REVERSAL_CANDIDATE")
        self.assertEqual(monitor.get_live_entries(), [])

    def test_confirmed_entry_expires_when_no_fresh_tick_arrives(self):
        monitor = self._monitor()
        monitor._register_watchlist(
            {"results": [{"symbol": "AGES", "entry_trigger": 100, "stop_loss": 95, "target_1": 115}]}
        )

        with patch("app.services.entry_monitor.time.time", return_value=1_000.0):
            monitor.update_tick("AGES", {"ltp": 100.0})
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
