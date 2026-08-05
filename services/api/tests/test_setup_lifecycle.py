from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from app.services.lifecycle import build_lifecycle_advice
from app.services.setup_store import SetupStore
from app.services.setup_tracker import SetupTrackerService


class MutableHistory:
    def __init__(self, frames: dict[str, pd.DataFrame] | None = None):
        self.frames = frames or {}

    def fetch_batch_history(self, symbols, period="1mo"):
        return {symbol: self.frames.get(symbol, pd.DataFrame()) for symbol in symbols}


class ClockedTracker(SetupTrackerService):
    def __init__(self, settings, data, now: datetime):
        self.test_now = now
        super().__init__(settings, data)

    def _now(self) -> datetime:
        return self.test_now


def candle_frame(rows):
    return pd.DataFrame(
        rows,
        columns=["Date", "Open", "High", "Low", "Close", "Volume"],
    ).set_index(pd.to_datetime([row[0] for row in rows])).drop(columns=["Date"])


def signal(symbol: str, entry: float, target_1: float, stop: float, target_2: float) -> dict:
    return {
        "symbol": symbol,
        "direction": "bullish",
        "setup_label": "Accumulation breakout",
        "timeframe_label": "Swing",
        "timeframe_days": 10,
        "current_price": entry,
        "target_price": target_1,
        "target_1": target_1,
        "target_2": target_2,
        "extended_target_price": target_2,
        "stop_loss": stop,
        "trailing_stop": stop,
        "invalidation": stop,
        "confidence": 82,
        "model_confidence": 80,
        "evidence_confidence": 78,
        "historical_evidence_status": "confirmed",
        "expected_move_pct": 12,
        "risk_level": "medium",
        "risk_reward": 2.2,
        "move_quality": 76,
        "relative_volume": 1.8,
        "intraday_volume_ratio": 1.6,
        "change_pct": 1.2,
        "rsi": 61,
        "volume": 2_000_000,
        "signal_summary": "Demand is holding above the invalidation.",
        "reasons": ["Volume expansion"],
        "risk_factors": [],
        "tags": ["accumulation"],
        "allow_buy_call": True,
        "attention_only": False,
    }


class SetupLifecycleRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.db_path = str(Path(self.temp_dir.name) / "tracker.db")
        self.settings = SimpleNamespace(
            tracked_setup_db_path=self.db_path,
            setup_memory_days=60,
            min_price=10,
            min_volume=50_000,
            tracked_promotion_confidence_min=68,
            tracked_promotion_move_quality_min=58,
            tracked_review_limit=10,
            scan_cache_ttl_sec=300,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cupid_t1_books_partial_then_keeps_winner_in_trailing_hold(self):
        history = MutableHistory(
            {
                "CUPID": candle_frame(
                    [
                        ("2026-04-17", 106.0, 109.0, 105.0, 108.0, 39_900_000),
                        ("2026-04-20", 108.0, 111.0, 107.0, 110.0, 39_300_000),
                        ("2026-04-22", 111.0, 114.6, 110.0, 113.8, 22_900_000),
                        ("2026-04-24", 113.0, 116.0, 111.0, 115.0, 12_000_000),
                        ("2026-04-30", 116.0, 120.0, 114.5, 119.5, 18_000_000),
                    ]
                )
            }
        )
        tracker = ClockedTracker(self.settings, history, datetime(2026, 4, 30, 12, tzinfo=timezone.utc))
        record = tracker._base_record(signal("CUPID", 107.0, 114.0, 103.0, 140.0), "top_opportunities", "Cupid", None, "scanner_suggested")
        record["detected_at"] = "2026-04-17T09:15:00+00:00"
        record["suggested_at"] = record["detected_at"]
        tracker.store.insert_setup(record)

        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", False):
            tracker.evaluate_open_setups()
            first = tracker.store.get_setup(record["id"])
            self.assertEqual(first["status"], "active")
            self.assertEqual(first["hold_or_exit"], "PARTIAL_BOOK")
            self.assertEqual(first["scanner_call_status"], "TARGET_1_HIT")
            self.assertEqual(first["lifecycle_state"], "PARTIAL_BOOK")
            self.assertIsNotNone(first["target_hit_at"])
            self.assertIsNone(first["closed_at"])

            tracker.evaluate_open_setups()
            runner = tracker.store.get_setup(record["id"])

        self.assertEqual(runner["status"], "active")
        self.assertEqual(runner["hold_or_exit"], "HOLD")
        self.assertEqual(runner["scanner_call_status"], "TRAILING_HOLD")
        self.assertEqual(runner["lifecycle_state"], "TRAILING_HOLD")
        self.assertGreaterEqual(runner["target_progress_pct"], 100)
        self.assertIsNone(runner["closed_at"])

    def test_rba_target_retest_and_reclaim_are_retained_and_rearmed(self):
        history = MutableHistory()
        tracker = ClockedTracker(self.settings, history, datetime(2026, 7, 27, 12, tzinfo=timezone.utc))
        rba_signal = signal("RBA", 67.0, 71.0, 65.0, 84.0)
        record = tracker._base_record(rba_signal, "breakout_candidates", "Restaurant Brands Asia", None, "scanner_suggested")
        # The entry was confirmed in the prior session; the July 27 daily
        # candle is therefore eligible without using a partial detection-day
        # candle as hindsight.
        record["detected_at"] = "2026-07-24T09:15:00+00:00"
        record["suggested_at"] = record["detected_at"]
        tracker.store.insert_setup(record)

        history.frames["RBA"] = candle_frame(
            [("2026-07-27", 67.0, 71.4, 66.5, 70.5, 5_000_000)]
        )
        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", False):
            tracker.evaluate_open_setups()
            self.assertEqual(tracker.store.get_setup(record["id"])["lifecycle_state"], "PARTIAL_BOOK")

            tracker.test_now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
            history.frames["RBA"] = candle_frame(
                [
                    ("2026-07-27", 67.0, 71.4, 66.5, 70.5, 5_000_000),
                    ("2026-07-28", 69.0, 69.5, 66.2, 66.7, 1_500_000),
                    ("2026-07-31", 66.8, 68.0, 66.1, 66.6, 1_200_000),
                ]
            )
            tracker.evaluate_open_setups()
            retest = tracker.store.get_setup(record["id"])
            self.assertEqual(retest["status"], "active")
            self.assertEqual(retest["lifecycle_state"], "RETEST")
            self.assertEqual(retest["scanner_call_status"], "RETEST")

            tracker.test_now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
            history.frames["RBA"] = candle_frame(
                [
                    ("2026-07-27", 67.0, 71.4, 66.5, 70.5, 5_000_000),
                    ("2026-07-28", 69.0, 69.5, 66.2, 66.7, 1_500_000),
                    ("2026-07-31", 66.8, 68.0, 66.1, 66.6, 1_200_000),
                    ("2026-08-03", 67.0, 72.0, 67.0, 71.2, 5_400_000),
                ]
            )
            tracker.evaluate_open_setups()
            reclaimed = tracker.store.get_setup(record["id"])

        self.assertEqual(reclaimed["status"], "active")
        self.assertEqual(reclaimed["lifecycle_state"], "REARMED")
        self.assertEqual(reclaimed["scanner_call_status"], "REARMED")
        self.assertEqual(reclaimed["continuation_pivot"], 71.0)
        self.assertIsNotNone(reclaimed["last_retest_at"])
        self.assertIsNotNone(reclaimed["continuation_rearmed_at"])
        self.assertGreaterEqual(reclaimed["post_target_high"], 72.0)

    def test_resolved_setup_gets_one_fresh_generation_on_repeated_scan(self):
        history = MutableHistory()
        tracker = ClockedTracker(self.settings, history, datetime(2026, 8, 5, 6, tzinfo=timezone.utc))
        old = tracker._base_record(signal("GAEL", 165.0, 178.0, 155.0, 190.0), "top_opportunities", "GAEL", None, "scanner_suggested")
        old.update(
            {
                "detected_at": "2026-07-03T09:15:00+00:00",
                "suggested_at": "2026-07-03T09:15:00+00:00",
                "status": "failed",
                "lifecycle_state": "INVALIDATED",
                "scanner_call_status": "STOP_LOSS_HIT",
                "closed_at": "2026-07-10T10:00:00+00:00",
                "exit_reason": "stop_loss_hit",
            }
        )
        tracker.store.insert_setup(old)

        fresh = signal("GAEL", 151.0, 165.0, 137.0, 184.0)
        fresh.update(
            {
                "reversal_watch": True,
                "reversal_bias": "bullish",
                "bullish_reversal_score": 72,
                "signal_stage": "BULLISH_REVERSAL_CANDIDATE",
                "change_pct": 8.9,
            }
        )
        payload = {
            "top_opportunities": [fresh],
            "breakout_candidates": [],
            "unusual_volume": [],
            "bearish_risks": [],
        }
        first_sync = tracker.sync_scan_payload(payload)
        second_sync = tracker.sync_scan_payload(payload)

        rows = tracker.store.list_setups(
            "SELECT * FROM tracked_setups WHERE symbol = ? AND direction = ? ORDER BY detected_at",
            ("GAEL", "bullish"),
        )
        active = [row for row in rows if row["status"] in {"active", "watch_only"}]
        parent = tracker.store.get_setup(old["id"])
        child = active[0]

        self.assertEqual(first_sync["promoted"], 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(active), 1)
        self.assertEqual(child["parent_setup_id"], old["id"])
        self.assertEqual(child["setup_generation"], 2)
        self.assertEqual(child["lifecycle_state"], "REARMED")
        self.assertEqual(child["source_mode"], "scanner_suggested")
        self.assertEqual(parent["rearmed_setup_id"], child["id"])
        self.assertIsNotNone(parent["rearmed_at"])
        self.assertEqual(second_sync["promoted"], 0)

    def test_graphite_stale_late_signal_waits_for_cooldown_and_confirmed_recovery(self):
        tracker = ClockedTracker(
            self.settings,
            MutableHistory(),
            datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
        )
        old = tracker._base_record(
            signal("GRAPHITE", 670.0, 705.0, 637.0, 735.0),
            "top_opportunities",
            "Graphite India",
            None,
            "scanner_suggested",
        )
        old.update(
            {
                "detected_at": "2026-06-08T09:15:00+00:00",
                "suggested_at": "2026-06-08T09:15:00+00:00",
                "last_evaluated_at": "2026-06-10T10:00:00+00:00",
                "closed_at": "2026-06-10T10:00:00+00:00",
                "status": "failed",
                "lifecycle_state": "INVALIDATED",
                "scanner_call_status": "STOP_LOSS_HIT",
                "exit_reason": "stop_loss_hit",
            }
        )
        tracker.store.insert_setup(old)

        stale = signal("GRAPHITE", 645.0, 680.0, 620.0, 710.0)
        stale.update(
            {
                "setup_stage": "AVOID_LATE_ENTRY",
                "signal_stage": "WATCH",
                "tags": ["chase_risk"],
            }
        )
        stale_payload = {
            "top_opportunities": [stale],
            "breakout_candidates": [],
            "unusual_volume": [],
            "bearish_risks": [],
        }
        tracker.sync_scan_payload(stale_payload)
        self.assertIsNone(tracker.store.get_open_setup("GRAPHITE", "bullish"))
        blocked = tracker.store.get_setup(old["id"])
        self.assertIn("late/chasing", blocked["rearm_block_reason"])

        # A plausible recovery pattern inside the cooldown remains a watch,
        # not a fresh trade generation.
        tracker.test_now = datetime(2026, 6, 12, 12, tzinfo=timezone.utc)
        early_recovery = signal("GRAPHITE", 612.0, 665.0, 570.0, 700.0)
        early_recovery.update(
            {
                "reversal_watch": True,
                "reversal_bias": "bullish",
                "bullish_reversal_score": 72,
                "signal_stage": "BULLISH_REVERSAL_CANDIDATE",
                "change_pct": 5.4,
                "relative_volume": 4.0,
                "intraday_volume_ratio": 3.0,
                "tags": ["bullish_reversal", "support_respect"],
            }
        )
        recovery_payload = {
            "top_opportunities": [early_recovery],
            "breakout_candidates": [],
            "unusual_volume": [],
            "bearish_risks": [],
        }
        tracker.sync_scan_payload(recovery_payload)
        self.assertIsNone(tracker.store.get_open_setup("GRAPHITE", "bullish"))
        blocked = tracker.store.get_setup(old["id"])
        self.assertIn("cooldown active", blocked["rearm_block_reason"])

        # After a real drop/base interval, the same high-volume directional
        # recovery is eligible as generation 2.
        tracker.test_now = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
        tracker.sync_scan_payload(recovery_payload)
        child = tracker.store.get_open_setup("GRAPHITE", "bullish")
        self.assertIsNotNone(child)
        self.assertEqual(child["parent_setup_id"], old["id"])
        self.assertEqual(child["setup_generation"], 2)
        self.assertEqual(child["lifecycle_state"], "REARMED")
        self.assertIn("confirmed_directional_reversal", child["rearm_evidence"]["evidence"])

        tracker.sync_scan_payload(recovery_payload)
        rows = tracker.store.list_setups(
            "SELECT * FROM tracked_setups WHERE symbol = ? AND direction = ?",
            ("GRAPHITE", "bullish"),
        )
        self.assertEqual(len(rows), 2)

    def test_intrabar_trailing_break_closes_runner_even_if_candle_recovers(self):
        history = MutableHistory(
            {
                "CUPID": candle_frame(
                    [
                        ("2026-04-17", 107.0, 111.0, 105.0, 110.0, 10_000_000),
                        ("2026-04-18", 111.0, 115.0, 110.0, 114.0, 12_000_000),
                        # Low breaches the persisted trail, then the candle
                        # recovers. A real stop would already have executed.
                        ("2026-04-20", 114.0, 118.0, 108.0, 116.0, 14_000_000),
                    ]
                )
            }
        )
        tracker = ClockedTracker(self.settings, history, datetime(2026, 4, 20, 12, tzinfo=timezone.utc))
        record = tracker._base_record(
            signal("CUPID", 107.0, 114.0, 103.0, 140.0),
            "top_opportunities",
            "Cupid",
            None,
            "scanner_suggested",
        )
        record.update(
            {
                "detected_at": "2026-04-17T09:15:00+00:00",
                "suggested_at": "2026-04-17T09:15:00+00:00",
                "target_hit_at": "2026-04-18T12:00:00+00:00",
                "partial_book_at": "2026-04-18T12:00:00+00:00",
                "trailing_stop": 109.0,
                "lifecycle_state": "TRAILING_HOLD",
                "scanner_call_status": "TRAILING_HOLD",
            }
        )
        tracker.store.insert_setup(record)

        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", False):
            tracker.evaluate_open_setups()
        closed = tracker.store.get_setup(record["id"])
        self.assertEqual(closed["status"], "passed")
        self.assertEqual(closed["lifecycle_state"], "CLOSED")
        self.assertEqual(closed["scanner_call_status"], "EXIT_SUGGESTED")
        self.assertEqual(closed["hold_or_exit"], "EXIT")
        self.assertEqual(closed["result_pct"], round(((109.0 / 107.0) - 1) * 100, 2))

    def test_future_raised_trail_is_not_applied_to_an_older_pullback(self):
        frame = candle_frame(
            [
                ("2026-01-01", 90.0, 94.0, 88.0, 92.0, 1_000_000),
                ("2026-01-02", 92.0, 101.0, 91.0, 100.0, 1_200_000),
                ("2026-01-03", 100.0, 106.0, 98.0, 104.0, 1_500_000),
                # This pullback is safe against the then-live 90 trail, but
                # below trail values that only become known on later bars.
                ("2026-01-05", 104.0, 108.0, 95.0, 106.0, 1_100_000),
                ("2026-01-06", 106.0, 122.0, 112.0, 120.0, 2_000_000),
            ]
        )
        history = MutableHistory({"RUNNER": frame})
        tracker = ClockedTracker(self.settings, history, datetime(2026, 1, 6, 12, tzinfo=timezone.utc))
        record = tracker._base_record(
            signal("RUNNER", 92.0, 100.0, 80.0, 140.0),
            "top_opportunities",
            "Runner",
            None,
            "scanner_suggested",
        )
        record.update(
            {
                "detected_at": "2026-01-01T09:15:00+00:00",
                "suggested_at": "2026-01-01T09:15:00+00:00",
                "target_hit_at": "2026-01-03T12:00:00+00:00",
                "partial_book_at": "2026-01-03T12:00:00+00:00",
                "last_evaluated_at": "2026-01-03T12:00:00+00:00",
                "trailing_stop": 90.0,
                "lifecycle_state": "TRAILING_HOLD",
                "scanner_call_status": "TRAILING_HOLD",
            }
        )
        tracker.store.insert_setup(record)
        scanner_refresh = signal("RUNNER", 120.0, 130.0, 108.0, 145.0)
        scanner_refresh["trailing_stop"] = 115.0
        tracker.sync_scan_payload(
            {
                "top_opportunities": [scanner_refresh],
                "breakout_candidates": [],
                "unusual_volume": [],
                "bearish_risks": [],
            }
        )
        # The current scanner candle may propose 115, but that level did not
        # exist during the older 95 pullback and must not be persisted first.
        self.assertEqual(tracker.store.get_setup(record["id"])["trailing_stop"], 90.0)
        sequential_trails = pd.Series(
            [float("nan"), float("nan"), float("nan"), 110.0, 115.0],
            index=frame.index,
        )

        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", True), patch(
            "app.services.setup_tracker.chandelier_exit_series",
            return_value=sequential_trails,
        ):
            tracker.evaluate_open_setups()
        runner = tracker.store.get_setup(record["id"])

        self.assertEqual(runner["status"], "active")
        self.assertEqual(runner["scanner_call_status"], "TRAILING_HOLD")
        self.assertEqual(runner["trailing_stop"], 115.0)

    def test_repeated_same_session_check_catches_a_new_intraday_trail_breach(self):
        history = MutableHistory(
            {
                "CUPID": candle_frame(
                    [
                        ("2026-04-17", 107.0, 112.0, 105.0, 111.0, 10_000_000),
                        ("2026-04-18", 111.0, 115.0, 110.0, 114.0, 12_000_000),
                        ("2026-04-20", 114.0, 116.0, 110.0, 114.5, 6_000_000),
                    ]
                )
            }
        )
        # 10:00 IST: today's low is still above the already-effective trail.
        tracker = ClockedTracker(self.settings, history, datetime(2026, 4, 20, 4, 30, tzinfo=timezone.utc))
        record = tracker._base_record(
            signal("CUPID", 107.0, 114.0, 103.0, 140.0),
            "top_opportunities",
            "Cupid",
            None,
            "scanner_suggested",
        )
        record.update(
            {
                "detected_at": "2026-04-17T09:15:00+00:00",
                "suggested_at": "2026-04-17T09:15:00+00:00",
                "target_hit_at": "2026-04-18T12:00:00+00:00",
                "partial_book_at": "2026-04-18T12:00:00+00:00",
                "last_evaluated_at": "2026-04-18T12:00:00+00:00",
                "trailing_stop": 109.0,
                "lifecycle_state": "TRAILING_HOLD",
                "scanner_call_status": "TRAILING_HOLD",
            }
        )
        tracker.store.insert_setup(record)
        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", False):
            tracker.evaluate_open_setups()
        morning = tracker.store.get_setup(record["id"])
        self.assertEqual(morning["status"], "active")
        self.assertEqual(morning["last_bar_low"], 110.0)

        # 14:00 IST: the same daily bar now contains a new 108 low, below the
        # 109 trail that was already active at the morning check. The close/LTP
        # has recovered, but the stop execution cannot be undone.
        tracker.test_now = datetime(2026, 4, 20, 8, 30, tzinfo=timezone.utc)
        history.frames["CUPID"] = candle_frame(
            [
                ("2026-04-17", 107.0, 112.0, 105.0, 111.0, 10_000_000),
                ("2026-04-18", 111.0, 115.0, 110.0, 114.0, 12_000_000),
                ("2026-04-20", 114.0, 118.0, 108.0, 116.0, 15_000_000),
            ]
        )
        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", False):
            tracker.evaluate_open_setups()
        closed = tracker.store.get_setup(record["id"])
        self.assertEqual(closed["status"], "passed")
        self.assertEqual(closed["scanner_call_status"], "EXIT_SUGGESTED")
        self.assertEqual(closed["result_pct"], round(((109.0 / 107.0) - 1) * 100, 2))

    def test_lifecycle_unit_t1_is_non_terminal_and_trail_closes_runner(self):
        partial = build_lifecycle_advice(
            direction="bullish",
            entry_price=107,
            current_price=114,
            target_1=114,
            target_2=140,
            stop_loss=103,
            trailing_stop=109,
            target_1_hit_now=True,
        )
        self.assertEqual(partial["hold_or_exit"], "PARTIAL_BOOK")
        self.assertFalse(partial["exit_signal"])

        hold = build_lifecycle_advice(
            direction="bullish",
            entry_price=107,
            current_price=120,
            target_1=114,
            target_2=140,
            stop_loss=103,
            trailing_stop=109,
            lifecycle_state="PARTIAL_BOOK",
            target_1_hit_before=True,
        )
        self.assertEqual(hold["scanner_call_status"], "TRAILING_HOLD")
        self.assertEqual(hold["hold_or_exit"], "HOLD")

        closed = build_lifecycle_advice(
            direction="bullish",
            entry_price=107,
            current_price=108,
            target_1=114,
            target_2=140,
            stop_loss=103,
            trailing_stop=109,
            lifecycle_state="TRAILING_HOLD",
            target_1_hit_before=True,
        )
        self.assertTrue(closed["exit_signal"])
        self.assertEqual(closed["scanner_call_status"], "EXIT_SUGGESTED")

    def test_watch_only_never_becomes_a_filled_trade_or_rearm_parent(self):
        watch_signal = signal("WATCH", 100.0, 110.0, 94.0, 120.0)
        watch_signal["relative_volume"] = 0.8
        watch_signal["intraday_volume_ratio"] = 0.7
        history = MutableHistory(
            {
                "WATCH": candle_frame(
                    [
                        ("2026-08-05", 100.0, 101.0, 99.0, 100.0, 500_000),
                        # Both trade boundaries are crossed after discovery.
                        # A watch has no fill, so neither is an outcome.
                        ("2026-08-06", 100.0, 115.0, 90.0, 112.0, 2_000_000),
                    ]
                )
            }
        )
        tracker = ClockedTracker(self.settings, history, datetime(2026, 8, 6, 12, tzinfo=timezone.utc))
        watch = tracker._base_record(
            watch_signal,
            "top_opportunities",
            "Watch Limited",
            None,
            "scanner",
        )
        watch["detected_at"] = "2026-08-05T04:00:00+00:00"
        tracker.store.insert_setup(watch)

        result = tracker.evaluate_open_setups()
        observed = tracker.store.get_setup(watch["id"])

        self.assertEqual(result, {"evaluated": 0, "observed": 1})
        self.assertEqual(observed["status"], "watch_only")
        self.assertEqual(observed["lifecycle_state"], "WATCH")
        self.assertIsNone(observed["entry_price"])
        self.assertIsNone(observed["suggested_at"])
        self.assertIsNone(observed["target_hit_at"])
        self.assertIsNone(observed["stop_loss_hit_at"])
        self.assertIsNone(observed["closed_at"])
        self.assertEqual(observed["current_pnl_pct"], 0.0)
        self.assertEqual(observed["result_pct"], 0.0)

        # Even a malformed legacy resolved watch is not used as the parent of
        # a later confirmed trade.
        tracker.store.update_setup(watch["id"], {"status": "expired", "lifecycle_state": "EXPIRED"})
        confirmed = signal("WATCH", 105.0, 116.0, 98.0, 125.0)
        tracker.sync_scan_payload(
            {
                "top_opportunities": [confirmed],
                "breakout_candidates": [],
                "unusual_volume": [],
                "bearish_risks": [],
            }
        )
        fresh = tracker.store.get_open_setup("WATCH", "bullish")
        parent = tracker.store.get_setup(watch["id"])
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["setup_generation"], 1)
        self.assertIsNone(fresh["parent_setup_id"])
        self.assertIsNone(parent["rearmed_setup_id"])

    def test_daily_detection_candle_is_excluded_but_next_session_is_evaluated(self):
        history = MutableHistory(
            {
                "NOLOOK": candle_frame(
                    [
                        # This is the detection-day candle. Its low happened
                        # before the 09:30 entry and must not invalidate it.
                        ("2026-08-05", 100.0, 115.0, 85.0, 108.0, 3_000_000),
                    ]
                )
            }
        )
        tracker = ClockedTracker(self.settings, history, datetime(2026, 8, 5, 12, tzinfo=timezone.utc))
        record = tracker._base_record(
            signal("NOLOOK", 100.0, 110.0, 90.0, 120.0),
            "top_opportunities",
            "No Lookahead",
            None,
            "scanner_suggested",
        )
        record["detected_at"] = "2026-08-05T04:00:00+00:00"
        record["suggested_at"] = record["detected_at"]
        tracker.store.insert_setup(record)

        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", False):
            tracker.evaluate_open_setups()
        same_day = tracker.store.get_setup(record["id"])
        self.assertEqual(same_day["status"], "active")
        self.assertIsNone(same_day["target_hit_at"])
        self.assertIsNone(same_day["stop_loss_hit_at"])
        self.assertEqual(same_day["current_pnl_pct"], 0.0)
        self.assertIsNone(same_day["last_bar_date"])

        tracker.test_now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
        history.frames["NOLOOK"] = candle_frame(
            [
                ("2026-08-05", 100.0, 115.0, 85.0, 108.0, 3_000_000),
                ("2026-08-06", 104.0, 111.0, 99.0, 110.5, 2_000_000),
            ]
        )
        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", False):
            tracker.evaluate_open_setups()
        next_session = tracker.store.get_setup(record["id"])
        self.assertEqual(next_session["status"], "active")
        self.assertEqual(next_session["lifecycle_state"], "PARTIAL_BOOK")
        self.assertIsNotNone(next_session["target_hit_at"])
        self.assertIsNone(next_session["stop_loss_hit_at"])
        self.assertEqual(next_session["last_bar_date"], "2026-08-06")

    def test_timestamp_resolved_bars_use_strict_confirmation_time(self):
        history = MutableHistory(
            {
                "INTRA": candle_frame(
                    [
                        ("2026-08-05 04:15:00", 100.0, 102.0, 85.0, 101.0, 100_000),
                        ("2026-08-05 04:45:00", 101.0, 111.0, 99.0, 110.0, 250_000),
                        ("2026-08-05 05:00:00", 110.0, 112.0, 108.0, 111.0, 200_000),
                    ]
                )
            }
        )
        tracker = ClockedTracker(self.settings, history, datetime(2026, 8, 5, 6, tzinfo=timezone.utc))
        record = tracker._base_record(
            signal("INTRA", 100.0, 110.0, 90.0, 125.0),
            "top_opportunities",
            "Intraday",
            None,
            "scanner_suggested",
        )
        record["detected_at"] = "2026-08-05T04:30:00+00:00"
        record["suggested_at"] = record["detected_at"]
        tracker.store.insert_setup(record)

        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", False):
            tracker.evaluate_open_setups()
        evaluated = tracker.store.get_setup(record["id"])
        self.assertEqual(evaluated["status"], "active")
        self.assertEqual(evaluated["lifecycle_state"], "PARTIAL_BOOK")
        self.assertIsNotNone(evaluated["target_hit_at"])
        self.assertIsNone(evaluated["stop_loss_hit_at"])

    def test_auto_calls_require_confirmed_setup_and_do_not_activate_watch(self):
        tracker = ClockedTracker(self.settings, MutableHistory(), datetime(2026, 8, 5, 6, tzinfo=timezone.utc))
        watch = signal("EARLY", 100.0, 110.0, 94.0, 120.0)
        watch["relative_volume"] = 0.8
        watch["intraday_volume_ratio"] = 0.7
        watch_payload = {
            "top_opportunities": [watch],
            "breakout_candidates": [],
            "unusual_volume": [],
            "bearish_risks": [],
        }
        watch_sync = tracker.sync_scan_payload(watch_payload)
        early = tracker.store.get_open_setup("EARLY", "bullish")

        self.assertEqual(watch_sync["auto_created"], 0)
        self.assertEqual(early["status"], "watch_only")
        self.assertEqual(early["source_mode"], "scanner")
        self.assertIsNone(early["entry_price"])
        self.assertIsNone(early["suggested_at"])

        tracker.test_now = datetime(2026, 8, 6, 6, tzinfo=timezone.utc)
        confirmed_early = signal("EARLY", 102.0, 112.0, 95.0, 122.0)
        promotion_sync = tracker.sync_scan_payload(
            {
                "top_opportunities": [confirmed_early],
                "breakout_candidates": [],
                "unusual_volume": [],
                "bearish_risks": [],
            }
        )
        promoted_early = tracker.store.get_open_setup("EARLY", "bullish")
        self.assertEqual(promotion_sync["auto_created"], 1)
        self.assertEqual(promoted_early["status"], "active")
        self.assertEqual(promoted_early["entry_price"], 102.0)
        self.assertTrue(promoted_early["suggested_at"].startswith("2026-08-06"))

        confirmed_payload = {
            "top_opportunities": [signal("READY", 200.0, 220.0, 188.0, 240.0)],
            "breakout_candidates": [],
            "unusual_volume": [],
            "bearish_risks": [],
        }
        confirmed_sync = tracker.sync_scan_payload(confirmed_payload)
        ready = tracker.store.get_open_setup("READY", "bullish")
        self.assertEqual(confirmed_sync["auto_created"], 1)
        self.assertEqual(ready["status"], "active")
        self.assertEqual(ready["source_mode"], "scanner_suggested")
        self.assertIsNotNone(ready["entry_price"])
        self.assertIsNotNone(ready["suggested_at"])

    def test_legacy_active_confirmed_row_backfills_armed_at_before_evaluation(self):
        history = MutableHistory(
            {"LEGACY": candle_frame([("2026-08-06", 100.0, 111.0, 99.0, 110.0, 1_000_000)])}
        )
        tracker = ClockedTracker(self.settings, history, datetime(2026, 8, 6, 12, tzinfo=timezone.utc))
        record = tracker._base_record(
            signal("LEGACY", 100.0, 110.0, 90.0, 120.0),
            "top_opportunities",
            "Legacy Confirmed",
            None,
            "scanner",
        )
        record["detected_at"] = "2026-08-05T04:00:00+00:00"
        record["suggested_at"] = None
        tracker.store.insert_setup(record)

        with patch("app.services.setup_tracker.ENABLE_DYNAMIC_TRAIL", False):
            tracker.evaluate_open_setups()
        migrated = tracker.store.get_setup(record["id"])
        self.assertEqual(migrated["suggested_at"], record["detected_at"])
        self.assertIsNotNone(migrated["target_hit_at"])

    def test_legacy_database_is_migrated_without_losing_outcome(self):
        legacy_path = str(Path(self.temp_dir.name) / "legacy.db")
        with closing(sqlite3.connect(legacy_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE tracked_setups (
                        id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        source_mode TEXT NOT NULL,
                        detected_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        target_price REAL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO tracked_setups VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("legacy-rba", "RBA", "bullish", "scanner_suggested", "2026-08-01T09:15:00+00:00", "2026-08-01T09:15:00+00:00", "passed", 71.0),
                )

        store = SetupStore(legacy_path)
        migrated = store.get_setup("legacy-rba")
        self.assertEqual(migrated["status"], "passed")
        self.assertEqual(migrated["lifecycle_state"], "TARGET_1_HIT")
        self.assertEqual(migrated["memory_sessions"], 60)
        self.assertIsNotNone(migrated["memory_expires_at"])


if __name__ == "__main__":
    unittest.main()
