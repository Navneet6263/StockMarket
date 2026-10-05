"""Synthetic rule tests; passing these does not establish a profitable strategy."""
import unittest

import numpy as np
import pandas as pd

from app.services.candle_decision import analyze_candle_setup, completed_candles


def _history(direction="down", count=16):
    closes = np.linspace(110, 102.4, count)
    if direction == "up":
        closes = 200 - closes
    rows = []
    for close in closes:
        open_ = close + 0.2 if direction == "down" else close - 0.2
        rows.append([open_, close + 0.5, close - 0.5, close, 1000])
    return rows


def _frame(rows, timeframe="15m"):
    frequency = "15min" if timeframe == "15m" else "B"
    start = "2026-09-14 09:15" if timeframe == "15m" else "2026-08-03"
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.date_range(start, periods=len(rows), freq=frequency, tz="Asia/Kolkata"))


def _hammer_rows():
    return _history() + [[102.0, 102.3, 100.1, 102.1, 1400]]


def _confirmed_rows():
    return _hammer_rows() + [[102.2, 103.0, 101.8, 102.8, 1800]]


def _analyze(rows, **kwargs):
    arguments = {"timeframe": "15m", "higher_timeframe_trend": "bullish",
                 "support": 100.0, "resistance": 112.0, "now": "2026-09-14 15:30"}
    arguments.update(kwargs)
    return analyze_candle_setup(_frame(rows), **arguments)


class CompletedCandleTests(unittest.TestCase):
    def test_incomplete_intraday_bar_is_excluded_until_exact_close(self):
        frame = _frame(_confirmed_rows())
        last_open = frame.index[-1]
        incomplete = completed_candles(frame, timeframe="15m", now=last_open + pd.Timedelta(minutes=14, seconds=59))
        complete = completed_candles(frame, timeframe="15m", now=last_open + pd.Timedelta(minutes=15))
        self.assertEqual(len(incomplete), len(frame) - 1)
        self.assertEqual(len(complete), len(frame))

    def test_daily_bar_waits_for_nse_close_not_midnight(self):
        frame = _frame(_hammer_rows(), timeframe="1d")
        last_day = frame.index[-1].normalize()
        before = completed_candles(frame, timeframe="1d", now=last_day + pd.Timedelta(hours=15, minutes=29))
        after = completed_candles(frame, timeframe="1d", now=last_day + pd.Timedelta(hours=15, minutes=30))
        self.assertEqual(len(before), len(frame) - 1)
        self.assertEqual(len(after), len(frame))

    def test_naive_timestamps_are_exchange_local(self):
        frame = _frame(_confirmed_rows())
        frame.index = frame.index.tz_localize(None)
        now = frame.index[-1] + pd.Timedelta(minutes=14)
        self.assertEqual(len(completed_candles(frame, timeframe="15m", now=now)), len(frame) - 1)

    def test_utc_timestamps_preserve_same_close_boundary(self):
        frame = _frame(_confirmed_rows())
        frame.index = frame.index.tz_convert("UTC")
        now = frame.index[-1] + pd.Timedelta(minutes=14)
        self.assertEqual(len(completed_candles(frame, timeframe="15m", now=now)), len(frame) - 1)

    def test_short_last_session_bucket_closes_at_1530(self):
        frame = pd.DataFrame([[100, 101, 99, 100, 1000]], columns=["Open", "High", "Low", "Close", "Volume"],
                             index=pd.DatetimeIndex(["2026-09-14 15:15"], tz="Asia/Kolkata"))
        self.assertEqual(len(completed_candles(frame, timeframe="30m", now="2026-09-14 15:30")), 1)

    def test_duplicate_daily_sessions_fail_closed(self):
        frame = _frame(_history(count=2))
        result = completed_candles(frame, timeframe="1d", now="2026-09-14 15:30")
        self.assertTrue(result.empty)
        self.assertEqual(result.attrs["candle_error"], "duplicate_daily_sessions")

    def test_bad_or_ambiguous_history_fails_closed(self):
        frame = _frame(_confirmed_rows())
        variants = []
        variants.append(frame.reset_index(drop=True))
        variants.append(frame.iloc[::-1])
        variants.append(pd.concat([frame, frame.tail(1)]))
        invalid = frame.copy()
        invalid.iloc[-1, invalid.columns.get_loc("Low")] = 999
        variants.append(invalid)
        for value in variants:
            with self.subTest(index_type=type(value.index)):
                result = analyze_candle_setup(value, timeframe="15m", now="2026-09-14 15:30")
                self.assertEqual(result["status"], "INVALID")
                self.assertFalse(result["entry_ready"])

    def test_nonfinite_completed_ohlcv_fails_closed(self):
        for column, value in [("High", float("inf")), ("Volume", float("nan")), ("Volume", -1), ("Close", 0)]:
            frame = _frame(_confirmed_rows())
            frame.loc[frame.index[-1], column] = value
            result = analyze_candle_setup(frame, timeframe="15m", now="2026-09-14 15:30")
            self.assertEqual(result["status"], "INVALID")

    def test_future_bad_candle_does_not_repaint_closed_setup(self):
        frame = _frame(_confirmed_rows() + [[1, float("nan"), 0, 0, 0]])
        result = analyze_candle_setup(frame, timeframe="15m", higher_timeframe_trend="bullish",
                                      support=100, resistance=112, now=frame.index[-1])
        self.assertEqual(result["status"], "READY")

    def test_unsupported_timeframe_and_invalid_now_fail_closed(self):
        frame = _frame(_confirmed_rows())
        self.assertTrue(completed_candles(frame, timeframe="1wk").empty)
        self.assertEqual(completed_candles(frame, timeframe="15m", now="not-a-time").attrs["candle_error"],
                         "invalid_timestamps_or_ohlcv")

    def test_helper_is_bounded_and_does_not_mutate_input(self):
        frame = _frame(_history(count=100), timeframe="1d")
        original = frame.copy(deep=True)
        result = completed_candles(frame, timeframe="1d", now="2027-01-01")
        self.assertEqual(len(result), 80)
        pd.testing.assert_frame_equal(frame, original)


class CandleDecisionTests(unittest.TestCase):
    def test_hammer_is_pullback_watch_not_shape_only_buy(self):
        result = _analyze(_hammer_rows())
        self.assertEqual(result["pattern"], "hammer")
        self.assertEqual(result["status"], "WAIT")
        self.assertEqual(result["phase"], "bullish_pullback")
        self.assertEqual(result["higher_timeframe_trend"], "bullish")
        self.assertEqual(result["local_trend"], "bearish")
        self.assertIn("awaiting_closed_candle_confirmation", result["reasons"])
        self.assertGreater(result["trigger"], 102.3)
        self.assertLess(result["invalidation"], 100.1)

    def test_completed_later_confirmation_and_structural_rr_can_be_ready(self):
        result = _analyze(_confirmed_rows())
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["entry_ready"])
        self.assertGreater(result["confirmation_time"], result["signal_time"])
        self.assertEqual(result["target"], 112)
        self.assertEqual(result["entry_reference"], 102.8)
        self.assertGreaterEqual(result["risk_reward"], 1.5)
        self.assertEqual(result["basis"], "research_heuristic")

    def test_incomplete_confirmation_cannot_produce_ready(self):
        frame = _frame(_confirmed_rows())
        result = analyze_candle_setup(frame, timeframe="15m", support=100, resistance=112,
                                     now=frame.index[-1] + pd.Timedelta(minutes=10))
        self.assertEqual(result["status"], "WAIT")
        self.assertIsNone(result["confirmation_time"])

    def test_wick_break_without_directional_close_is_not_confirmation(self):
        result = _analyze(_hammer_rows() + [[102.2, 104, 101.8, 102.1, 1800]])
        self.assertFalse(result["entry_ready"])
        self.assertIn("awaiting_closed_candle_confirmation", result["reasons"])

    def test_stop_first_then_recovery_is_invalid_not_success(self):
        result = _analyze(_hammer_rows() + [[102.2, 103.0, 99.8, 102.8, 1800]])
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("structure_invalidated_after_signal", result["reasons"])

    def test_low_volume_confirmation_is_not_ready(self):
        rows = _confirmed_rows()
        rows[-1][-1] = 1
        result = _analyze(rows)
        self.assertFalse(result["entry_ready"])
        self.assertIsNone(result["confirmation_time"])

    def test_zero_volume_signal_is_not_ready(self):
        rows = _confirmed_rows()
        rows[-2][-1] = 0
        result = _analyze(rows)
        self.assertFalse(result["entry_ready"])
        self.assertIn("signal_volume_insufficient", result["reasons"])

    def test_missing_known_target_never_manufactures_rr(self):
        result = _analyze(_confirmed_rows(), resistance=[])
        self.assertEqual(result["status"], "WAIT")
        self.assertIsNone(result["target"])
        self.assertIsNone(result["risk_reward"])
        self.assertIn("no_known_opposing_target", result["reasons"])

    def test_first_opposing_level_is_used_even_when_rr_is_bad(self):
        result = _analyze(_confirmed_rows(), resistance=[103.2, 112, 120])
        self.assertEqual(result["target"], 103.2)
        self.assertLess(result["risk_reward"], 1.5)
        self.assertEqual(result["status"], "WAIT")
        self.assertIn("insufficient_room_at_current_price", result["reasons"])

    def test_rr_is_recalculated_at_latest_close_not_original_trigger(self):
        result = _analyze(_confirmed_rows(), resistance=106.5)
        initial_rr = (106.5 - result["trigger"]) / (result["trigger"] - result["invalidation"])
        self.assertGreater(initial_rr, 1.5)
        self.assertLess(result["risk_reward"], 1.5)
        self.assertFalse(result["entry_ready"])

    def test_large_late_candle_never_authorizes_chase(self):
        result = _analyze(_hammer_rows() + [[102.2, 105.2, 101.8, 105, 1800]], resistance=120)
        self.assertFalse(result["entry_ready"])
        self.assertIn("late_entry_wait_for_retest", result["reasons"])

    def test_higher_timeframe_conflict_remains_visible_but_not_ready(self):
        result = _analyze(_confirmed_rows(), higher_timeframe_trend="bearish")
        self.assertEqual(result["status"], "WAIT")
        self.assertEqual(result["phase"], "countertrend_reversal")
        self.assertIn("higher_timeframe_opposes_setup", result["reasons"])

    def test_same_lower_wick_after_rise_is_hanging_man_not_bullish_hammer(self):
        rows = _history("up") + [[98, 98.3, 96.1, 98.1, 1400]]
        result = _analyze(rows, support=90, resistance=98.5, higher_timeframe_trend="neutral")
        self.assertEqual(result["pattern"], "hanging_man")
        self.assertEqual(result["direction"], "bearish")
        self.assertFalse(result["entry_ready"])

    def test_hammer_away_from_support_is_watch_only(self):
        result = _analyze(_hammer_rows(), support=90)
        self.assertEqual(result["status"], "WATCH")
        self.assertIn("pattern_context_missing", result["reasons"])

    def test_failed_breakdown_requires_later_reclaim_confirmation(self):
        rows = _history() + [[100.2, 101.2, 99.2, 101, 1500]]
        waiting = _analyze(rows)
        ready = _analyze(rows + [[101.1, 101.8, 100.8, 101.6, 1800]])
        self.assertEqual(waiting["pattern"], "failed_breakdown_reclaim")
        self.assertEqual(waiting["status"], "WAIT")
        self.assertEqual(ready["status"], "READY")

    def test_bearish_mirror_has_downward_trigger_stop_above_and_lower_target(self):
        rows = [[200 - o, 200 - low, 200 - h, 200 - c, v] for o, h, low, c, v in _confirmed_rows()]
        result = _analyze(rows, higher_timeframe_trend="bearish", support=88, resistance=100)
        self.assertEqual(result["pattern"], "shooting_star")
        self.assertEqual(result["direction"], "bearish")
        self.assertEqual(result["phase"], "bearish_pullback")
        self.assertEqual(result["status"], "READY")
        self.assertLess(result["target"], result["trigger"])
        self.assertGreater(result["invalidation"], result["trigger"])

    def test_bearish_failed_breakout_is_symmetric(self):
        rows = _history() + [[100.2, 101.2, 99.2, 101, 1500], [101.1, 101.8, 100.8, 101.6, 1800]]
        mirror = [[200 - o, 200 - low, 200 - h, 200 - c, v] for o, h, low, c, v in rows]
        result = _analyze(mirror, higher_timeframe_trend="bearish", support=88, resistance=100)
        self.assertEqual(result["pattern"], "failed_breakout_rejection")
        self.assertEqual(result["status"], "READY")

    def test_bullish_engulfing_uses_both_bars_for_invalidation(self):
        rows = _history() + [[102.5, 102.7, 101.8, 102, 1000], [101.9, 102.9, 101.85, 102.8, 1400]]
        result = _analyze(rows, support=101.8)
        self.assertEqual(result["pattern"], "bullish_engulfing")
        self.assertLess(result["invalidation"], 101.8)
        self.assertEqual(result["status"], "WAIT")

    def test_bearish_engulfing_uses_both_bars_for_invalidation(self):
        rows = _history() + [[102.5, 102.7, 101.8, 102, 1000], [101.9, 102.9, 101.85, 102.8, 1400]]
        mirror = [[200 - o, 200 - low, 200 - h, 200 - c, v] for o, h, low, c, v in rows]
        result = _analyze(mirror, higher_timeframe_trend="bearish", support=88, resistance=98.2)
        self.assertEqual(result["pattern"], "bearish_engulfing")
        self.assertGreater(result["invalidation"], 98.2)
        self.assertEqual(result["status"], "WAIT")

    def test_old_signal_expires_instead_of_remaining_ready_forever(self):
        rows = _confirmed_rows() + [[102.8, 103.1, 102.5, 102.9, 1000]] * 3
        result = _analyze(rows)
        self.assertFalse(result["entry_ready"])
        self.assertIsNone(result["signal_time"])

    def test_empty_and_insufficient_history_are_unavailable(self):
        self.assertEqual(analyze_candle_setup(None, timeframe="15m")["status"], "UNAVAILABLE")
        self.assertEqual(_analyze(_history(count=4))["status"], "UNAVAILABLE")
        frame = _frame(_confirmed_rows()).drop(columns="Volume")
        self.assertEqual(analyze_candle_setup(frame, timeframe="15m")["status"], "UNAVAILABLE")

    def test_previous_session_confirmation_cannot_be_ready_at_next_open(self):
        result = _analyze(_confirmed_rows(), now="2026-09-15 09:20")
        self.assertEqual(result["status"], "WAIT")
        self.assertIn("awaiting_current_session_closed_candle", result["reasons"])

    def test_stale_intraday_bars_cannot_confirm_during_market_hours(self):
        result = _analyze(_confirmed_rows(), now="2026-09-14 15:00")
        self.assertEqual(result["status"], "WAIT")
        self.assertIn("stale_completed_candles", result["reasons"])

    def test_yesterday_confirmation_is_not_revived_by_today_nonconfirming_bar(self):
        frame = _frame(_confirmed_rows() + [[102.9, 103.1, 102.5, 102.8, 1000]])
        frame.index = frame.index[:-1].append(pd.DatetimeIndex(["2026-09-15 09:15"], tz="Asia/Kolkata"))
        result = analyze_candle_setup(frame, timeframe="15m", higher_timeframe_trend="bullish",
                                      support=100, resistance=112, now="2026-09-15 09:30")
        self.assertEqual(result["status"], "WAIT")
        self.assertIsNone(result["confirmation_time"])

    def test_default_levels_use_prefix_history_only_and_do_not_make_targets(self):
        rows = _confirmed_rows()
        result = _analyze(rows, support=None, resistance=None)
        observed_highs = [row[1] for row in rows[:-2]]
        self.assertIn(result["target"], observed_highs)


if __name__ == "__main__":
    unittest.main()
