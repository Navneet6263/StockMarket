"""Real hub shaping regressions; no broker, network or service startup required."""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from app.services.book_strategy import apply_candle_gate


def hub_class():
    # Historical-provider downloads are not involved in payload shaping.
    if "yfinance" not in sys.modules and importlib.util.find_spec("yfinance") is None:
        fake = types.ModuleType("yfinance")
        fake.EquityQuery = type("EquityQuery", (), {})
        with patch.dict(sys.modules, {"yfinance": fake}):
            return importlib.import_module("app.services.market_hub").MarketHubService
    return importlib.import_module("app.services.market_hub").MarketHubService


def candle_signal(symbol: str, status: str = "READY", **overrides):
    ready = status == "READY"
    candle = {
        "status": status, "available": ready, "entry_ready": ready,
        "pattern": "hammer" if ready else None, "direction": "bullish" if ready else None,
        "trigger": 100.0 if ready else None, "invalidation": 98.0 if ready else None,
        "target": 105.0 if ready else None, "timeframe": "15m",
        "phase": "bullish_pullback", "higher_timeframe_trend": "bullish",
        "reasons": ["contextual_pattern_confirmed_on_closed_bar"] if ready else ["missing_history"],
        "rules_version": "candle_context_v1",
    }
    item = {
        "symbol": symbol, "direction": "bullish", "current_price": 100.2,
        "action": "WATCH", "allow_buy_call": True, "confidence": 80,
        "move_quality": 75, "alert_level": "watchlist", "risk_level": "medium",
        "relative_volume": 1.3, "intraday_volume_ratio": 1.0,
        "change_pct": 0.2, "benchmark_relative_strength": 1.0,
        "tags": [], "pattern_labels": [], "setup_type": "trend",
        "signal_stage": "WATCH", "score_breakdown": {}, "candle_setup": candle,
    }
    item.update(overrides)
    return apply_candle_gate(item)


class CandleHubShapeTests(unittest.TestCase):
    def setUp(self):
        cls = hub_class()
        self.hub = object.__new__(cls)
        self.hub.settings = SimpleNamespace(intraday_symbol_limit=100)
        self.hub.last_successful_scan = None
        self.hub._persistent_watch_map = lambda: {}
        self.hub._get_nifty_context = lambda: {"nifty_bias": "neutral", "nifty_regime": "unknown", "market_score": 50}
        self.hub._build_market_breadth = lambda *args: {"bullish_ratio": 0.5, "benchmark_change_pct": 0, "advance_decline_ratio": 1}
        self.hub._market_mood = lambda *args: "neutral"
        self.hub.market_intelligence = SimpleNamespace(get_macro_snapshot=lambda: {}, get_global_scenario=lambda: {})

    def shape(self, rows, live_symbols=None):
        names = live_symbols if live_symbols is not None else [row["symbol"] for row in rows]
        discovery = {"symbols": names, "live_candidate_symbols": names, "symbol_meta": {}, "scan_attempted_count": len(rows)}
        return self.hub._shape_scan_payload(discovery, rows, pd.DataFrame())

    def test_quiet_ready_candle_is_discoverable_and_armable_without_old_buy_bucket(self):
        row = candle_signal("QUIET")
        output = self.shape([row])
        self.assertEqual(output["top_opportunities"], [])
        self.assertEqual(output["unusual_volume"], [])
        self.assertEqual([item["symbol"] for item in output["candle_watch_setups"]], ["QUIET"])
        self.assertEqual([item["symbol"] for item in output["all_entry_levels"]], ["QUIET"])
        self.assertEqual(output["all_entry_levels"][0]["candle_setup"], row["candle_setup"])
        self.assertEqual(output["live_candidate_pool"][0]["candle_setup"], row["candle_setup"])

    def test_unavailable_none_levels_do_not_crash_or_create_entry(self):
        pending = candle_signal("PENDING", "UNAVAILABLE")
        self.assertIsNone(pending["risk_reward"])
        self.assertIsNone(pending["target_1"])
        output = self.shape([candle_signal("READY"), pending])
        self.assertEqual([item["symbol"] for item in output["all_entry_levels"]], ["READY"])
        self.assertEqual(len(output["live_candidate_symbols"]), 2)

    def test_equal_confidence_retests_accept_pending_none_risk_reward(self):
        rows = [candle_signal("READY", signal_stage="RETEST_ENTRY"),
                candle_signal("PENDING", "UNAVAILABLE", signal_stage="RETEST_ENTRY")]
        output = self.shape(rows)
        self.assertEqual({item["symbol"] for item in output["retest_entry"]}, {"READY", "PENDING"})
        self.assertEqual([item["symbol"] for item in output["all_entry_levels"]], ["READY"])

    def test_candle_watch_and_live_metadata_respect_explicit_shortlist_cap(self):
        self.hub.settings.intraday_symbol_limit = 1
        output = self.shape([candle_signal("FIRST"), candle_signal("SECOND")])
        self.assertEqual(output["live_candidate_symbols"], ["FIRST"])
        self.assertEqual([item["symbol"] for item in output["candle_watch_setups"]], ["FIRST"])
        self.assertEqual([item["symbol"] for item in output["all_entry_levels"]], ["FIRST"])


if __name__ == "__main__":
    unittest.main()
