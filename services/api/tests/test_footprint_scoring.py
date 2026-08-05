from __future__ import annotations

import sys
import unittest
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from app.services.demand_supply import analyze_demand_supply
from app.services.scoring import ScoringEngine
from app.services.trap_detector import detect_large_money_footprint


class LargeMoneyFootprintTests(unittest.TestCase):
    def test_unverified_delivery_values_are_excluded(self):
        result = detect_large_money_footprint(
            price_change_pct=0.2,
            volume_ratio=1.0,
            delivery_ratio=0.85,
            delivery_spike=3.0,
            cmf=0.0,
            obv_slope=0.0,
            close_location=0.5,
            relative_strength=0.0,
            delivery_available=False,
        )

        self.assertEqual(result["score"], 0.0)
        self.assertFalse(result["data_quality"]["delivery_verified"])
        self.assertTrue(any("delivery data is unavailable" in item.lower() for item in result["warnings"]))

    def test_delivery_and_volume_do_not_claim_participant_identity(self):
        result = detect_large_money_footprint(
            price_change_pct=4.2,
            volume_ratio=4.5,
            delivery_ratio=0.72,
            delivery_spike=2.4,
            cmf=0.18,
            obv_slope=1.0,
            close_location=0.88,
            relative_strength=4.0,
            delivery_available=True,
        )

        self.assertEqual(result["bias"], "strong_buying_pressure")
        self.assertFalse(result["identity_inference_supported"])
        self.assertFalse(result["score_is_calibrated_probability"])
        self.assertTrue(result["data_quality"]["delivery_verified"])
        joined = " ".join(result["evidence"] + result["warnings"] + [result["summary"]]).lower()
        self.assertNotIn("institutional buying confirmed", joined)
        self.assertNotIn("fii buying", joined)
        self.assertIn("participant identity", result["identity_note"].lower())

    def test_high_volume_rejection_is_a_trap_not_a_buying_footprint(self):
        result = analyze_demand_supply(
            {
                "change_pct": 6.0,
                "gap_pct": 3.0,
                "return_5d": 14.0,
                "return_20d": 30.0,
                "relative_volume": 5.0,
                "intraday_volume_ratio": 4.0,
                "rsi": 74.0,
                "close_location": 0.32,
                "upper_wick_pct": 0.55,
                "lower_wick_pct": 0.05,
                "cmf": -0.08,
                "obv_slope": -1.0,
                "distance_to_resistance_pct": 0.3,
                "near_resistance": True,
                "breakout_20": True,
                "above_vwap": False,
                "delivery_available": False,
                "trend_regime": "uptrend",
                "price_above_ema20": True,
                "price_above_ema50": True,
            },
            direction="bullish",
        )

        self.assertTrue(result["blockFreshEntry"])
        self.assertGreaterEqual(result["trapRiskScore"], 70)
        self.assertFalse(result["reversalWatch"])
        self.assertIn(result["largeMoneyFootprint"]["bias"], {"selling_pressure", "mixed_or_neutral"})
        self.assertFalse(result["largeMoneyFootprint"]["identity_inference_supported"])


class ReversalScoringTests(unittest.TestCase):
    def test_gael_style_reversal_overrides_lagging_bearish_tally(self):
        snapshot = {
            "price": 151.64,
            "close": 151.64,
            "prev_close": 139.14,
            "high": 152.89,
            "change_pct": 8.98,
            "relative_volume": 11.05,
            "intraday_volume_ratio": 8.0,
            "rsi": 29.0,
            "macd_hist": -0.8,
            "atr": 5.0,
            "atr_pct": 4.2,
            "atr_expansion": 1.8,
            "relative_strength_20d": -6.0,
            "return_5d": -2.0,
            "return_20d": -12.0,
            "close_location": 0.90,
            "upper_wick_pct": 0.05,
            "lower_wick_pct": 0.10,
            "cmf": 0.16,
            "obv_slope": 1.0,
            "delivery_available": False,
            "above_vwap": True,
            "price_above_ema20": False,
            "price_above_ema50": False,
            "price_above_ema200": False,
            "ema_20": 155.0,
            "ema_50": 160.0,
            "ema_200": 170.0,
            "trend_regime": "downtrend",
            "near_support": True,
            "distance_to_support_pct": 0.5,
            "support_20": 138.0,
            "resistance_20": 165.0,
            "rolling_vwap": 148.0,
            "breakout_20": False,
            "breakdown_20": False,
            "gap_pct": 0.0,
        }

        result = ScoringEngine().evaluate("GAEL", snapshot, calibrate=False)

        self.assertEqual(result["trend_direction"], "bearish")
        self.assertEqual(result["reversal_bias"], "bullish")
        self.assertTrue(result["reversal_watch"])
        self.assertNotEqual(result["direction"], "bearish")
        self.assertEqual(result["signal_stage"], "BULLISH_REVERSAL_CANDIDATE")
        self.assertLessEqual(result["confidence"], 82.0)
        self.assertIsNone(result["probability"])
        self.assertFalse(result["probability_available"])
        self.assertIn("not a probability", result["confidence_note"])
        self.assertFalse(
            any("range expansion confirms the downside" in reason.lower() for reason in result["reasons"])
        )


if __name__ == "__main__":
    unittest.main()
