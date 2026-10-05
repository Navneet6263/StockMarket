"""Orchestration regressions; synthetic tests do not prove a trading edge."""
import unittest
from unittest.mock import patch

import pandas as pd

from app.services.book_strategy import apply_candle_gate, build_candle_context
from app.services.hot_picks import _dashboard_profile, _guidance, map_pick
from app.services.trade_plan import assess_structural_plan
from app.services import smart_layers
from app.services.entry_monitor import EntryMonitor
from app.services.realtime_features import RealtimeFeatureEngine


def signal(status="READY", **changes):
    item = {"symbol": "TEST", "direction": "bullish", "action": "BUY", "confidence": 85,
            "allow_buy_call": True, "current_price": 100.2,
            "candle_setup": {"status": status, "entry_ready": status == "READY",
                             "direction": "bullish", "trigger": 100.0, "invalidation": 98.0,
                             "target": 105.0, "reasons": ["awaiting_closed_candle_confirmation"],
                             "phase": "bullish_pullback", "rules_version": "candle_context_v1"}}
    item.update(changes)
    return item


class BookStrategyTests(unittest.TestCase):
    def test_ready_only_arms_live_never_static_buy(self):
        result = apply_candle_gate(signal())
        self.assertTrue(result["candleGateReady"])
        self.assertTrue(result["requires_live_confirmation"])
        self.assertFalse(result["allow_buy_call"])
        self.assertEqual(result["effectiveAction"], "WATCH")
        self.assertEqual(result["stop_loss"], 98.0)
        self.assertEqual(result["target_1"], 105.0)
        self.assertEqual(apply_candle_gate(result), result)

    def test_wait_cannot_be_promoted_by_buy_or_fast_move(self):
        result = apply_candle_gate(signal("WAIT", is_valid_entry_after_move=True, is_momentum_continuation=True))
        result.update(action="BUY", effectiveAction="BUY", allow_buy_call=True)
        result = apply_candle_gate(result)
        self.assertFalse(result["candleGateReady"])
        self.assertTrue(result["requires_live_confirmation"])
        self.assertEqual(_dashboard_profile(result, score=90)["dashboardBucket"], "MOMENTUM_WATCH")
        self.assertTrue(_guidance(result)["tradeDecision"].startswith("No fresh entry"))

    def test_no_target_clears_old_fabricated_levels(self):
        item = signal("WAIT", new_target=999, target_1=500, stop_loss=97, support=96, resistance=110)
        item["candle_setup"]["target"] = None
        result = apply_candle_gate(item)
        self.assertIsNone(result["new_target"])
        self.assertIsNone(result["target_1"])
        self.assertIsNone(result["safe_entry_price"])
        pick = map_pick(result, "2026-09-15")
        self.assertEqual(pick["stoploss"], "-")
        self.assertEqual(pick["entryTrigger"], "-")
        self.assertEqual(pick["candle_setup"]["invalidation"], 98.0)

    def test_wide_stop_is_not_moved_to_force_rr(self):
        item = signal()
        item["candle_setup"].update(invalidation=90, target=140)
        result = apply_candle_gate(item)
        self.assertFalse(result["candleGateReady"])
        self.assertIsNone(result["stop_loss"])
        self.assertEqual(result["structural_invalidation"], 90)

    def test_market_and_structure_veto_survive_ready(self):
        for key in ("marketGateBlocked", "entry_plan_blocked", "tradePlanGateBlocked", "chase_risk"):
            with self.subTest(key=key):
                result = apply_candle_gate(signal(**{key: True}))
                self.assertFalse(result["candleGateReady"])
                self.assertFalse(result["requires_live_confirmation"])
                self.assertTrue(result[key])

    def test_trap_veto_cannot_be_rearmed_from_watch(self):
        for fields in ({"trap_risk": "high"}, {"demand_supply": {"blockFreshEntry": True}},
                       {"demand_supply": {"trapRisk": "high"}}, {"trapRisk": "very_high"},
                       {"bull_trap": {"bull_trap_detected": True}}):
            with self.subTest(fields=fields):
                result = apply_candle_gate(signal(action="WATCH", allow_buy_call=False, **fields))
                self.assertTrue(result["entry_plan_blocked"])
                self.assertFalse(result["requires_live_confirmation"])
                self.assertFalse(apply_candle_gate(result)["candleGateReady"])

    def test_candle_only_bucket_survives_smart_gate_and_arms_actual_monitor(self):
        item = signal(relative_volume=1.3)
        item["candle_setup"].update(timeframe="15m", latest_closed_time="2026-09-15T09:45:00+05:30",
                                    evaluated_at="2026-09-15T09:50:00+05:30",
                                    confirmation_time="2026-09-15T09:30:00+05:30")
        item = apply_candle_gate(item)
        payload = {"candle_watch_setups": [item], "all_entry_levels": [item], "live_candidate_symbols": ["TEST"]}
        with patch.object(smart_layers, "compute_market_breadth", return_value={"marketMood": "neutral", "freshBuyBlocked": False}), patch.object(smart_layers, "rank_global_sectors", return_value={}):
            output = smart_layers.build_smart_scan_payload(payload, {}, pd.DataFrame())
        from app.services.hot_picks import _all_signals
        self.assertEqual([x["symbol"] for x in _all_signals(output)], ["TEST"])
        self.assertEqual(len(output["all_entry_levels"]), 1)
        epoch = [pd.Timestamp("2026-09-15T10:00:00+05:30").timestamp()]
        monitor = EntryMonitor(feature_engine=RealtimeFeatureEngine(clock=lambda: epoch[0]))
        monitor._register_watchlist(output)
        with patch.object(monitor, "_check_entry"):
            for seconds, price, volume in ((0, 100, 1000), (4, 100.1, 1100), (5, 100.2, 1300)):
                epoch[0] += seconds
                assessment = monitor.update_tick("TEST", {"ltp": price, "volume": volume,
                    "timestamp": epoch[0], "total_buy_qty": 3000, "total_sell_qty": 1000})
        self.assertEqual(assessment["status"], "CONFIRMED")
        self.assertGreater(assessment["liveRiskReward"], 1.5)
        self.assertEqual(assessment["candle_setup"]["status"], "READY")

    def test_direction_conflict_does_not_flip_a_trade(self):
        result = apply_candle_gate(signal(direction="bearish"))
        self.assertEqual(result["direction"], "bearish")
        self.assertFalse(result["candle_setup"]["entry_ready"])
        self.assertFalse(result["candleGateReady"])

    def test_invalid_candle_revokes_live_arming(self):
        result = apply_candle_gate(signal("INVALID"))
        self.assertTrue(result["entry_plan_blocked"])
        self.assertFalse(result["requires_live_confirmation"])

    def test_legacy_record_is_unchanged(self):
        item = {"symbol": "OLD", "action": "WATCH"}
        self.assertEqual(apply_candle_gate(item), item)

    def test_preliminary_scan_never_runs_dataframe_engine(self):
        with patch("app.services.book_strategy.completed_candles") as daily, patch("app.services.book_strategy.analyze_candle_setup") as engine:
            result = build_candle_context(None, None, enhanced=False)
        daily.assert_not_called()
        engine.assert_not_called()
        self.assertFalse(result["entry_ready"])

    def test_provider_utc_naive_index_is_restored_without_mutation(self):
        raw = pd.DataFrame({"Close": [100]}, index=pd.DatetimeIndex(["2026-09-15 03:45"]))
        with patch("app.services.book_strategy.analyze_candle_setup", return_value={"status": "WATCH", "entry_ready": False}) as engine:
            build_candle_context(None, raw, enhanced=True, now="2026-09-15 12:00")
        passed = engine.call_args.args[0]
        self.assertEqual(passed.index[0].tz_convert("Asia/Kolkata").hour, 9)
        self.assertIsNone(raw.index.tz)

    def test_missing_daily_context_blocks_otherwise_ready(self):
        with patch("app.services.book_strategy.analyze_candle_setup", return_value={"status": "READY", "entry_ready": True, "reasons": []}):
            result = build_candle_context(None, None, enhanced=True)
        self.assertEqual(result["status"], "WAIT")
        self.assertFalse(result["entry_ready"])

    def test_unclosed_daily_crash_does_not_change_higher_trend(self):
        close = list(range(100, 130)) + [20]
        daily = pd.DataFrame({"Open": close, "High": [x + 1 for x in close],
                              "Low": [x - 1 for x in close], "Close": close, "Volume": 1000},
                             index=pd.bdate_range(end="2026-09-15", periods=31))
        with patch("app.services.book_strategy.analyze_candle_setup", return_value={"status": "WATCH", "entry_ready": False}) as engine:
            build_candle_context(daily, None, enhanced=True, now="2026-09-15 12:00")
        self.assertEqual(engine.call_args.kwargs["higher_timeframe_trend"], "bullish")

    def test_nonfinite_and_boolean_risk_levels_fail_closed(self):
        for value in (float("inf"), float("nan"), True):
            with self.subTest(value=value):
                self.assertFalse(assess_structural_plan("bullish", 100, value, [105])["allowed"])
                self.assertFalse(assess_structural_plan("bullish", 100, 98, [value])["allowed"])


if __name__ == "__main__":
    unittest.main()
