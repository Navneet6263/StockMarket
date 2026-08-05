from __future__ import annotations

import unittest

from app.services.live_chart import VisibleFootprintDetector


def _candle(index: int, *, open_: float = 100, close: float = 100.2, high: float = 101, low: float = 99, volume: int = 100):
    return {
        "time": index,
        "open": open_,
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
    }


class LiveChartPatternTruthTests(unittest.TestCase):
    def test_intraday_volume_is_compared_with_intraday_baseline_and_label_is_truthful(self):
        candles = [_candle(index) for index in range(6)]
        candles.append(_candle(6, open_=100, close=100.1, volume=220))

        patterns = VisibleFootprintDetector().detect("RBA", candles)

        absorption = next(item for item in patterns if item["type"] == "ABSORPTION_PROXY")
        self.assertIn("proxy", absorption["message"].lower())
        self.assertNotIn("institution", " ".join(item["message"] for item in patterns).lower())

    def test_completed_bar_structure_detects_failed_break_without_network_context(self):
        candles = [_candle(index, high=101, low=99) for index in range(5)]
        candles.append(_candle(5, close=102, high=103, low=100, volume=100))
        candles.append(_candle(6, open_=102, close=100, high=102, low=99.5, volume=100))

        patterns = VisibleFootprintDetector().detect("RBA", candles)

        self.assertIn("BULL_TRAP", [item["type"] for item in patterns])


if __name__ == "__main__":
    unittest.main()
