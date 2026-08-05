from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import unittest
from unittest.mock import patch

from app.services.breakout_radar import build_breakout_radar
from app.services.market_breadth import compute_market_breadth


def _benchmark_mildly_rising() -> dict[str, list[float]]:
    return {"Close": ([100.0] * 54) + [100.0, 100.05, 100.10, 100.15, 100.20, 100.25]}


def _descending_closes(start: float = 120.0) -> dict[str, list[float]]:
    return {"Close": [start - (index * 0.5) for index in range(30)]}


def _ascending_closes(start: float = 80.0) -> dict[str, list[float]]:
    return {"Close": [start + (index * 0.5) for index in range(30)]}


def _load_smart_layers():
    """Import the integration layer in slim images without pandas/numpy."""

    stubs = {}
    if importlib.util.find_spec("pandas") is None:
        fake_pandas = types.ModuleType("pandas")
        fake_pandas.DataFrame = type("DataFrame", (), {})
        fake_pandas.Series = type("Series", (), {})
        stubs["pandas"] = fake_pandas
    if importlib.util.find_spec("numpy") is None:
        fake_numpy = types.ModuleType("numpy")
        fake_numpy.nan = float("nan")
        fake_numpy.number = (int, float)
        stubs["numpy"] = fake_numpy
    with patch.dict(sys.modules, stubs):
        return importlib.import_module("app.services.smart_layers")


class FullUniverseBreadthTests(unittest.TestCase):
    def test_bearish_full_universe_beats_a_bullish_ranked_shortlist(self):
        daily_frames = {
            **{f"BEAR{index}": _descending_closes(120 + index) for index in range(8)},
            **{f"BULL{index}": _ascending_closes(80 + index) for index in range(2)},
        }
        bullish_shortlist = [
            {
                "symbol": f"PICK{index}",
                "change_pct": 4.0,
                "chart_features": {"moving_average_trend": "uptrend"},
            }
            for index in range(3)
        ]

        context = compute_market_breadth(
            _benchmark_mildly_rising(),
            bullish_shortlist,
            daily_frames=daily_frames,
        )

        self.assertEqual(context["breadthSource"], "full_daily_frames")
        self.assertEqual(context["breadthUniverseCount"], 10)
        self.assertEqual(context["breadthSampleCount"], 10)
        self.assertEqual(context["breadthCoveragePct"], 100.0)
        self.assertEqual(context["ema20SampleCount"], 10)
        self.assertEqual(context["advancingCount"], 2)
        self.assertEqual(context["decliningCount"], 8)
        self.assertEqual(context["advanceDeclineRatio"], 0.25)
        self.assertEqual(context["pctStocksAboveEma20"], 20.0)
        self.assertEqual(context["marketMood"], "bearish")
        self.assertTrue(context["freshBuyBlocked"])

    def test_decisively_bearish_benchmark_remains_a_safety_gate(self):
        benchmark = {"Close": [140.0 - (index * 0.6) for index in range(60)]}
        bullish_frames = {
            f"BULL{index}": _ascending_closes(80 + index)
            for index in range(10)
        }

        context = compute_market_breadth(
            benchmark,
            [],
            daily_frames=bullish_frames,
        )

        self.assertEqual(context["pctStocksAboveEma20"], 100.0)
        self.assertEqual(context["marketMood"], "bearish")
        self.assertTrue(context["freshBuyBlocked"])

    def test_coverage_uses_attempted_universe_not_only_downloaded_frames(self):
        frames = {
            f"STOCK{index}": _descending_closes(120 + index)
            for index in range(5)
        }

        context = compute_market_breadth(
            _benchmark_mildly_rising(),
            [],
            daily_frames=frames,
            expected_universe_count=10,
        )

        self.assertEqual(context["breadthUniverseCount"], 10)
        self.assertEqual(context["breadthSampleCount"], 5)
        self.assertEqual(context["breadthCoveragePct"], 50.0)

    def test_smart_layer_passes_the_complete_frame_map_to_breadth(self):
        smart_layers = _load_smart_layers()
        frames = {"A": _ascending_closes(), "B": _descending_closes()}
        context = {
            "marketMood": "neutral",
            "freshBuyBlocked": False,
            "blockedReason": None,
        }

        with (
            patch.object(smart_layers, "compute_market_breadth", return_value=context) as breadth,
            patch.object(smart_layers, "rank_global_sectors", return_value={}),
            patch.object(smart_layers, "build_sector_strength_map", return_value={}),
        ):
            smart_layers.build_smart_scan_payload({}, frames, _benchmark_mildly_rising())

        self.assertIs(breadth.call_args.kwargs["daily_frames"], frames)
        self.assertEqual(breadth.call_args.kwargs["expected_universe_count"], 2)


def _market_hub_shaped_signal(symbol: str = "RBA") -> dict:
    return {
        "symbol": symbol,
        "direction": "bullish",
        "current_price": 100.0,
        "resistance": 102.0,
        "support": 99.0,
        "rolling_vwap": 99.4,
        "change_pct": 0.4,
        "relative_volume": 0.65,
        "intraday_volume_ratio": 1.0,
        "benchmark_relative_strength": 3.2,
        "rsi": 55.0,
        "atr_pct": 1.8,
        "chart_features": {
            "breakout_confirmed": False,
            "tight_consolidation_pct": 3.5,
            "distance_to_resistance_pct": 2.0,
            "higher_lows": True,
            "volume_dryup": True,
            "bb_squeeze": True,
            "bb_width_ratio": 0.65,
            "cmf": 0.14,
            "obv_slope": 2.0,
            "moving_average_trend": "uptrend",
        },
    }


class BreakoutRadarContractTests(unittest.TestCase):
    def test_market_hub_shaped_signal_is_not_silently_dropped(self):
        results = build_breakout_radar(
            [_market_hub_shaped_signal()],
            {
                "nifty_bias": "leaning_bullish",
                "nifty_regime": "trending_up",
            },
            max_results=10,
        )

        self.assertEqual(len(results), 1)
        candidate = results[0]
        self.assertEqual(candidate["symbol"], "RBA")
        self.assertEqual(candidate["resistance_level"], 102.0)
        self.assertEqual(candidate["support_level"], 99.0)
        self.assertGreaterEqual(candidate["breakout_readiness_score"], 60)
        self.assertEqual(candidate["nifty_note"], "Nifty uptrend provides tailwind.")

    def test_leaning_bearish_and_trending_down_use_bearish_semantics(self):
        signals = [_market_hub_shaped_signal(f"STOCK{index}") for index in range(8)]

        results = build_breakout_radar(
            signals,
            {
                "nifty_bias": "leaning_bearish",
                "nifty_regime": "trending_down",
            },
            max_results=10,
        )

        self.assertEqual(len(results), 5)
        self.assertTrue(all(item["nifty_note"].startswith("Caution: Nifty is weak") for item in results))


if __name__ == "__main__":
    unittest.main()
