from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from app.services import angelone_live
from app.services.angelone_live import AutoOrderService


class AutoOrderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.service = AutoOrderService()

    @patch.object(angelone_live, "AUTO_ORDER_CAPITAL", 100_000.0)
    @patch.object(angelone_live, "AUTO_ORDER_MAX_CAPITAL_PCT", 25.0)
    @patch.object(angelone_live, "AUTO_ORDER_MAX_ACCOUNT_RISK_PCT", 1.0)
    @patch.object(angelone_live, "ENABLE_AUTO_ORDER", True)
    def test_valid_plan_is_fail_closed_until_broker_native_exits_exist(self):
        with patch.object(
            angelone_live,
            "_get_angel_session",
            side_effect=AssertionError("a naked entry must never reach the broker"),
        ):
            result = self.service.place_gtt_order(
                "GAEL", "bullish", 150.0, 146.0, 165.0, quantity=10
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "native_protective_exits_not_implemented")

    @patch.object(angelone_live, "AUTO_ORDER_CAPITAL", 100_000.0)
    @patch.object(angelone_live, "AUTO_ORDER_MAX_CAPITAL_PCT", 100.0)
    @patch.object(angelone_live, "AUTO_ORDER_MAX_ACCOUNT_RISK_PCT", 1.0)
    def test_rejects_non_finite_and_inverted_plans(self):
        cases = (
            (("GAEL", "bullish", float("nan"), 140.0, 170.0, 1), "invalid_order_parameters"),
            (("GAEL", "bullish", 150.0, 151.0, 170.0, 1), "invalid_bullish_risk_plan"),
            (("GAEL", "bearish", 150.0, 145.0, 130.0, 1), "invalid_bearish_risk_plan"),
            (("GAEL;DROP", "bullish", 150.0, 145.0, 170.0, 1), "invalid_symbol"),
            (("GAEL", "bullish", 150.0, 145.0, 170.0, True), "invalid_order_parameters"),
        )
        for args, expected in cases:
            with self.subTest(reason=expected, args=args):
                result = self.service.place_gtt_order(*args)
                self.assertEqual(result["reason"], expected)

    @patch.object(angelone_live, "AUTO_ORDER_CAPITAL", 100_000.0)
    @patch.object(angelone_live, "AUTO_ORDER_MAX_CAPITAL_PCT", 100.0)
    @patch.object(angelone_live, "AUTO_ORDER_MAX_ACCOUNT_RISK_PCT", 1.0)
    def test_rejects_position_whose_stop_risk_exceeds_account_budget(self):
        result = self.service.place_gtt_order(
            "GRAPHITE", "bullish", 700.0, 650.0, 800.0, quantity=25
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "auto_order_account_risk_limit_exceeded")
        self.assertEqual(result["requestedRisk"], 1_250.0)
        self.assertEqual(result["maxAllowedRisk"], 1_000.0)


class ExpirySelectionTests(unittest.TestCase):
    @staticmethod
    def _load_options_module():
        module_path = Path(__file__).parents[1] / "app" / "options_analyzer.py"
        spec = importlib.util.spec_from_file_location("options_analyzer_expiry_test", module_path)
        module = importlib.util.module_from_spec(spec)
        fake_numpy = types.ModuleType("numpy")
        fake_pandas = types.ModuleType("pandas")
        with patch.dict(sys.modules, {"numpy": fake_numpy, "pandas": fake_pandas}):
            spec.loader.exec_module(module)
        return module

    def test_selects_nearest_real_expiry_with_swing_room(self):
        module = self._load_options_module()

        selected = module._select_swing_expiry(
            ["06-Aug-2026", "27-Aug-2026", "20-Aug-2026", "bad-value"],
            as_of=date(2026, 8, 5),
        )

        self.assertEqual(selected, "27-Aug-2026")

    def test_returns_none_instead_of_guessing_an_expiry(self):
        module = self._load_options_module()

        selected = module._select_swing_expiry(
            ["06-Aug-2026", "13-Aug-2026"],
            as_of=date(2026, 8, 5),
        )

        self.assertIsNone(selected)

    def test_weak_one_sided_indicator_does_not_become_100_percent_alignment(self):
        module = self._load_options_module()
        analyzer = module.OptionsAnalyzer()
        analyzer._technical_scorecard = lambda *_: (["one weak bullish rule"], 10, 0)

        result = analyzer._swing_prediction(
            "NIFTY",
            25_000.0,
            1.0,
            25_000,
            {"support": 24_800, "resistance": 25_200},
            {"bias": "neutral"},
            None,
        )

        self.assertEqual(result["direction"], "bullish")
        self.assertLess(result["confidence"], module.CONFIDENCE_THRESHOLD)
        self.assertFalse(result["technical_gate_passed"])
        self.assertEqual(result["option_action"], "NO_TRADE")

    def test_symbol_allowlist_never_substitutes_unknown_index(self):
        module = self._load_options_module()

        self.assertEqual(module._normalize_index_symbol(" midcpnifty "), "MIDCPNIFTY")
        self.assertEqual(module.INDEX_META["MIDCPNIFTY"]["yf"], "NIFTY_MID_SELECT.NS")
        self.assertIsNone(module._normalize_index_symbol("SENSEX"))

        analyzer = module.OptionsAnalyzer()
        analyzer._fetch_raw_chain = lambda *_: self.fail("unsupported symbol must not be fetched")
        result = analyzer.get_full_analysis("sensex")
        self.assertEqual(result["recommendation"]["action"], "NO_TRADE")
        self.assertEqual(result["data_source"], "invalid_symbol")

    def test_chain_price_and_vix_freshness_are_fail_closed(self):
        module = self._load_options_module()
        now = datetime(2026, 8, 6, 10, 5, tzinfo=module.IST)

        self.assertTrue(
            module._assess_chain_freshness("06-Aug-2026 10:00:00", as_of=now)["fresh"]
        )
        self.assertFalse(
            module._assess_chain_freshness("06-Aug-2026 09:54:59", as_of=now)["fresh"]
        )
        self.assertFalse(module._assess_chain_freshness("bad", as_of=now)["fresh"])
        self.assertTrue(module._assess_price_bar_freshness("05-Aug-2026", as_of=now)["fresh"])
        self.assertFalse(module._assess_price_bar_freshness("04-Aug-2026", as_of=now)["fresh"])
        self.assertTrue(
            module._assess_vix_bar_freshness("06-Aug-2026 10:01:00", as_of=now)["fresh"]
        )
        self.assertFalse(
            module._assess_vix_bar_freshness("06-Aug-2026 09:54:00", as_of=now)["fresh"]
        )

    def test_after_hours_chain_requires_latest_completed_close(self):
        module = self._load_options_module()
        saturday = datetime(2026, 8, 8, 11, 0, tzinfo=module.IST)

        self.assertTrue(
            module._assess_chain_freshness("07-Aug-2026 15:30:00", as_of=saturday)["fresh"]
        )
        self.assertFalse(
            module._assess_chain_freshness("06-Aug-2026 15:30:00", as_of=saturday)["fresh"]
        )

    def test_liquid_contract_requires_exact_row_and_leg_expiry(self):
        module = self._load_options_module()
        expiry = "27-Aug-2026"
        rows = [
            {
                "strikePrice": 25_000,
                "CE": {
                    "expiryDate": expiry,
                    "lastPrice": 120,
                    "totalTradedVolume": 50_000,
                    "openInterest": 100_000,
                },
            },
            {
                "expiryDate": expiry,
                "strikePrice": 25_000,
                "CE": {
                    "expiryDate": "03-Sep-2026",
                    "lastPrice": 110,
                    "totalTradedVolume": 50_000,
                    "openInterest": 100_000,
                },
            },
            {
                "expiryDate": expiry,
                "strikePrice": 25_000,
                "CE": {
                    "expiryDate": expiry,
                    "lastPrice": 100,
                    "totalTradedVolume": 1_000,
                    "openInterest": 5_000,
                    "bidprice": 99,
                    "askPrice": 101,
                    "bidQty": 50,
                    "askQty": 40,
                },
            },
        ]

        contract = module._select_liquid_contract(
            rows,
            expiry_date=expiry,
            option_action="BUY_CE",
            spot=25_010,
        )

        self.assertIsNotNone(contract)
        self.assertEqual(contract["strike"], 25_000)
        self.assertEqual(contract["expiry"], expiry)
        self.assertEqual(contract["spread_pct"], 2.0)

    def test_liquid_contract_rejects_zero_liquidity_and_wide_spread(self):
        module = self._load_options_module()
        expiry = "27-Aug-2026"
        bad_legs = (
            {"lastPrice": 100, "totalTradedVolume": 0, "openInterest": 5_000},
            {"lastPrice": 100, "totalTradedVolume": 99, "openInterest": 5_000, "bidprice": 99, "askPrice": 101},
            {"lastPrice": 100, "totalTradedVolume": 100, "openInterest": 499, "bidprice": 99, "askPrice": 101},
            {"lastPrice": 100, "totalTradedVolume": 100, "openInterest": 5_000},
            {"lastPrice": 100, "totalTradedVolume": 100, "openInterest": 5_000, "bidprice": 99, "askPrice": 101, "bidQty": 0, "askQty": 10},
            {"lastPrice": 100, "totalTradedVolume": 100, "openInterest": 5_000, "bidprice": 80, "askPrice": 120},
            {"lastPrice": 0, "totalTradedVolume": 100, "openInterest": 5_000},
        )
        for leg in bad_legs:
            leg = {"expiryDate": expiry, **leg}
            rows = [{"expiryDate": expiry, "strikePrice": 25_000, "CE": leg}]
            with self.subTest(leg=leg):
                self.assertIsNone(
                    module._select_liquid_contract(
                        rows,
                        expiry_date=expiry,
                        option_action="BUY_CE",
                        spot=25_000,
                    )
                )

    def test_oi_references_are_never_fabricated(self):
        module = self._load_options_module()
        analyzer = module.OptionsAnalyzer()

        empty = analyzer._support_resistance([], 25_000)
        zero_oi = analyzer._support_resistance(
            [{"strikePrice": 24_900, "PE": {"openInterest": 0}}],
            25_000,
        )

        self.assertIsNone(empty["support"])
        self.assertIsNone(empty["resistance"])
        self.assertIsNone(zero_oi["support"])
        self.assertIsNone(analyzer._pcr([], 25_000))

    def test_structural_pivots_come_from_observed_prices(self):
        module = self._load_options_module()

        self.assertEqual(
            module._latest_confirmed_pivot(
                [100, 96, 99, 98, 101], direction="bullish", spot=105
            ),
            98.0,
        )
        self.assertEqual(
            module._latest_confirmed_pivot(
                [100, 105, 101, 102, 99], direction="bearish", spot=95
            ),
            102.0,
        )

    def _valid_recommendation_inputs(self, module):
        expiry = "27-Aug-2026"
        return {
            "symbol": "NIFTY",
            "direction": "bullish",
            "confidence": 83,
            "spot": 100.0,
            "atm": 100,
            "step": 50,
            "swing": {
                "direction": "bullish",
                "confidence": 83,
                "option_action": "BUY_CE",
                "technical_gate_passed": True,
                "signals": ["Price trend aligned"],
                "vix": 15.0,
            },
            "sr": {"support": 90, "resistance": 108},
            "max_pain": 100,
            "data_source": "live",
            "expiry_date": expiry,
            "contract": {
                "strike": 100,
                "option_type": "CE",
                "expiry": expiry,
                "last_price": 5.0,
                "volume": 1_000,
                "open_interest": 5_000,
                "bid": 4.9,
                "ask": 5.1,
                "spread_pct": 4.0,
                "quote_quality": "two_sided_quote",
            },
            "price_context": {
                "bullish_invalidation": 96.0,
                "bearish_invalidation": 104.0,
                "bullish_targets": [108.0],
                "bearish_targets": [92.0],
                "invalidation_method": "latest_confirmed_three_bar_price_pivot",
                "target_method": "confirmed_completed_bar_price_pivot",
            },
            "data_quality": {
                "chain": {"fresh": True},
                "price_history": {"fresh": True},
                "vix": {"fresh": True},
            },
        }

    def test_recommendation_uses_structural_invalidation_and_rr(self):
        module = self._load_options_module()
        result = module.OptionsAnalyzer()._build_recommendation(
            **self._valid_recommendation_inputs(module)
        )

        self.assertEqual(result["action"], "BUY_CE")
        self.assertEqual(result["underlying_invalidation"], 96.0)
        self.assertEqual(result["underlying_structure_target"], 108.0)
        self.assertEqual(result["underlying_risk_reward"], 2.0)
        self.assertNotIn("stop_loss_underlying", result)
        self.assertNotIn("target_underlying", result)

    def test_recommendation_blocks_stale_vix_missing_structure_and_poor_rr(self):
        module = self._load_options_module()
        analyzer = module.OptionsAnalyzer()

        stale = self._valid_recommendation_inputs(module)
        stale["data_quality"]["vix"] = {"fresh": False}
        self.assertEqual(analyzer._build_recommendation(**stale)["action"], "NO_TRADE")

        missing_structure = self._valid_recommendation_inputs(module)
        missing_structure["price_context"]["bullish_invalidation"] = None
        self.assertEqual(
            analyzer._build_recommendation(**missing_structure)["action"],
            "NO_TRADE",
        )

        poor_rr = self._valid_recommendation_inputs(module)
        poor_rr["price_context"]["bullish_targets"] = [105.0]
        poor_result = analyzer._build_recommendation(**poor_rr)
        self.assertEqual(poor_result["action"], "NO_TRADE")
        self.assertEqual(
            poor_result["structural_plan"]["blockCode"],
            "INSUFFICIENT_STRUCTURE_REWARD",
        )

    def test_analyze_rejects_rows_without_exact_expiry_before_network_inputs(self):
        module = self._load_options_module()
        analyzer = module.OptionsAnalyzer()
        analyzer._technical_context = lambda *_args, **_kwargs: self.fail("must block before price fetch")
        analyzer._fetch_vix_snapshot = lambda **_kwargs: self.fail("must block before VIX fetch")
        now = datetime(2026, 8, 6, 10, 5, tzinfo=module.IST)
        raw = {
            "records": {
                "timestamp": "06-Aug-2026 10:02:00",
                "underlyingValue": 25_000,
                "expiryDates": ["27-Aug-2026"],
                "data": [{"strikePrice": 25_000, "CE": {"expiryDate": "27-Aug-2026"}}],
            }
        }

        result = analyzer._analyze("NIFTY", raw, "live", as_of=now)

        self.assertEqual(result["recommendation"]["action"], "NO_TRADE")
        self.assertEqual(result["data_source"], "live_no_valid_expiry")

    def test_analyze_emits_buy_only_when_every_safety_gate_passes(self):
        module = self._load_options_module()
        analyzer = module.OptionsAnalyzer()
        expiry = "27-Aug-2026"
        now = datetime(2026, 8, 6, 10, 5, tzinfo=module.IST)
        analyzer._technical_context = lambda *_args, **_kwargs: {
            "signals": ["Four aligned completed-session rules"],
            "bull_score": 29,
            "bear_score": 0,
            "freshness": {
                "fresh": True,
                "source_timestamp": "2026-08-05",
            },
            "bullish_invalidation": 96.0,
            "bearish_invalidation": 104.0,
            "bullish_targets": [110.0],
            "bearish_targets": [90.0],
            "invalidation_method": "latest_confirmed_three_bar_price_pivot",
            "target_method": "confirmed_completed_bar_price_pivot",
        }
        analyzer._fetch_vix_snapshot = lambda **_kwargs: {
            "value": 15.0,
            "freshness": {
                "fresh": True,
                "source_timestamp": "2026-08-06",
            },
        }

        def leg(side, strike, oi):
            return {
                "expiryDate": expiry,
                "underlying": "NIFTY",
                "strikePrice": strike,
                "optionType": side,
                "lastPrice": 5.0,
                "totalTradedVolume": 1_000,
                "openInterest": oi,
                "bidprice": 4.9,
                "askPrice": 5.1,
            }

        raw = {
            "records": {
                "timestamp": "06-Aug-2026 10:02:00",
                "underlyingValue": 100,
                "expiryDates": [expiry],
                "data": [
                    {"expiryDate": expiry, "strikePrice": 90, "PE": leg("PE", 90, 2_000)},
                    {
                        "expiryDate": expiry,
                        "strikePrice": 100,
                        "CE": leg("CE", 100, 5_000),
                        "PE": leg("PE", 100, 4_000),
                    },
                    {"expiryDate": expiry, "strikePrice": 110, "CE": leg("CE", 110, 3_000)},
                ],
            }
        }

        result = analyzer._analyze("NIFTY", raw, "live", as_of=now)

        self.assertEqual(result["recommendation"]["action"], "BUY_CE")
        self.assertEqual(result["recommendation"]["strike"], 100)
        self.assertEqual(result["recommendation"]["underlying_invalidation"], 96.0)
        self.assertEqual(result["recommendation"]["underlying_structure_target"], 110.0)
        self.assertEqual(result["data_quality"]["status"], "verified")

    def test_strong_separated_technical_case_can_pass_the_options_gate(self):
        module = self._load_options_module()
        analyzer = module.OptionsAnalyzer()
        analyzer._technical_scorecard = lambda *_: (["four aligned rules"], 29, 0)

        result = analyzer._swing_prediction(
            "NIFTY",
            25_000.0,
            1.0,
            25_000,
            {"support": 24_800, "resistance": 25_200},
            {"bias": "neutral"},
            None,
        )

        self.assertGreaterEqual(result["confidence"], module.CONFIDENCE_THRESHOLD)
        self.assertTrue(result["technical_gate_passed"])
        self.assertEqual(result["option_action"], "BUY_CE")

    def test_conflicted_high_scores_remain_no_trade(self):
        module = self._load_options_module()
        analyzer = module.OptionsAnalyzer()
        analyzer._technical_scorecard = lambda *_: (["conflicted rules"], 29, 24)

        result = analyzer._swing_prediction(
            "NIFTY",
            25_000.0,
            1.0,
            25_000,
            {"support": 24_800, "resistance": 25_200},
            {"bias": "neutral"},
            None,
        )

        self.assertFalse(result["technical_gate_passed"])
        self.assertEqual(result["option_action"], "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
