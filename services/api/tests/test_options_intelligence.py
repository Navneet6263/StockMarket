"""
Unit tests for options_intelligence.py
Validates the 4 proprietary market-reading signals:
1. OI Change Velocity (1-min)
2. IV Skew Asymmetry
3. Gamma Exposure & Squeeze Detection
4. Max Pain Gravity & Expiry Pinning
5. Combined Verdict Synthesis
"""
import unittest
import time
from app.services.options_intelligence import (
    OIVelocityTracker,
    calculate_iv_skew,
    calculate_gamma_exposure,
    analyze_max_pain_gravity,
    synthesize_verdict,
    run_options_intelligence,
)


class TestOptionsIntelligence(unittest.TestCase):

    def setUp(self):
        # Sample realistic chain snapshot for NIFTY around 24500
        self.mock_snapshot_t0 = {
            "symbol": "NIFTY",
            "spot_price": 24520.0,
            "max_pain": 24500.0,
            "days_to_expiry": 2,
            "strikes": [
                {
                    "strike": 24400.0,
                    "ce_oi": 50000,
                    "pe_oi": 150000,
                    "ce_iv": 0.13,
                    "pe_iv": 0.17,
                    "ce_greeks": {"gamma": 0.03, "iv": 0.13},
                    "pe_greeks": {"gamma": 0.04, "iv": 0.17},
                },
                {
                    "strike": 24450.0,
                    "ce_oi": 40000,
                    "pe_oi": 120000,
                    "ce_iv": 0.135,
                    "pe_iv": 0.175,
                    "ce_greeks": {"gamma": 0.05, "iv": 0.135},
                    "pe_greeks": {"gamma": 0.06, "iv": 0.175},
                },
                {
                    "strike": 24500.0,
                    "ce_oi": 120000,
                    "pe_oi": 110000,
                    "ce_iv": 0.14,
                    "pe_iv": 0.18,
                    "ce_greeks": {"gamma": 0.09, "iv": 0.14},
                    "pe_greeks": {"gamma": 0.09, "iv": 0.18},
                },
                {
                    "strike": 24550.0,
                    "ce_oi": 140000,
                    "pe_oi": 45000,
                    "ce_iv": 0.145,
                    "pe_iv": 0.185,
                    "ce_greeks": {"gamma": 0.05, "iv": 0.145},
                    "pe_greeks": {"gamma": 0.04, "iv": 0.185},
                },
                {
                    "strike": 24600.0,
                    "ce_oi": 180000,
                    "pe_oi": 30000,
                    "ce_iv": 0.15,
                    "pe_iv": 0.19,
                    "ce_greeks": {"gamma": 0.03, "iv": 0.15},
                    "pe_greeks": {"gamma": 0.02, "iv": 0.19},
                },
            ],
        }

    def test_iv_skew_asymmetry(self):
        # PE IV is around 0.18, CE IV is around 0.14 -> skew_ratio ~ 0.14/0.18 = 0.77 -> PUT_SKEW
        result = calculate_iv_skew(self.mock_snapshot_t0)
        self.assertTrue(result["available"])
        self.assertEqual(result["signal"], "PUT_SKEW")
        self.assertEqual(result["direction"], "bearish")
        self.assertLess(result["skew_ratio"], 0.87)

    def test_gamma_exposure(self):
        result = calculate_gamma_exposure(self.mock_snapshot_t0)
        self.assertTrue(result["available"])
        self.assertIsNotNone(result["nearest_gamma_wall"])
        # Strike 24500 has gamma 0.09 > 0.08 threshold
        high_strikes = [s["strike"] for s in result["high_gamma_strikes"]]
        self.assertIn(24500.0, high_strikes)

    def test_max_pain_gravity(self):
        # Max pain is 24500, spot is 24520, days to expiry = 2
        result = analyze_max_pain_gravity(self.mock_snapshot_t0)
        self.assertTrue(result["available"])
        self.assertEqual(result["max_pain"], 24500.0)
        self.assertTrue(result["spot_in_band"])
        self.assertIn(result["pin_probability"], ["high", "very_high"])

    def test_oi_velocity_tracker(self):
        tracker = OIVelocityTracker("TEST_NIFTY")
        # Snapshot 1
        tracker.push_snapshot(self.mock_snapshot_t0)

        # Insufficient snapshots check
        v1 = tracker.calculate_velocity()
        self.assertFalse(v1["available"])
        self.assertEqual(v1["reason"], "insufficient_snapshots")

        # Snapshot 2 (simulating 1 minute later with massive PE additions at 24500)
        snap_t1 = {
            "symbol": "NIFTY",
            "spot_price": 24510.0,
            "strikes": [
                dict(s, pe_oi=s["pe_oi"] + (30000 if s["strike"] == 24500 else 5000))
                for s in self.mock_snapshot_t0["strikes"]
            ],
        }
        time.sleep(0.05)
        tracker.push_snapshot(snap_t1)

        v2 = tracker.calculate_velocity()
        self.assertTrue(v2["available"])
        self.assertIn(v2["signal"], ["BEARISH_BUILD", "MIXED"])
        self.assertGreater(v2["total_pe_oi_added"], 0)

    def test_synthesize_verdict(self):
        oi_v = {"available": True, "direction": "bearish", "confidence": 75, "label": "PE building fast"}
        skew = {"available": True, "direction": "bearish", "confidence": 80, "label": "Put skew heavy"}
        gex = {"available": True, "label": "Gamma wall near spot", "nearest_gamma_wall": {"strike": 24500}}
        pain = {"available": True, "gravity_direction": "bearish", "pin_label": "Pulling to 24500"}

        verdict = synthesize_verdict(oi_v, skew, gex, pain)
        self.assertEqual(verdict["direction"], "bearish")
        self.assertIn(verdict["action"], ["PE_WATCH", "PE_BIAS"])
        self.assertGreaterEqual(verdict["confidence"], 60)

    def test_run_options_intelligence_master(self):
        result = run_options_intelligence("NIFTY_MASTER", self.mock_snapshot_t0, push_to_buffer=True)
        self.assertEqual(result["symbol"], "NIFTY_MASTER")
        self.assertIn("verdict", result)
        self.assertIn("iv_skew", result)
        self.assertIn("gamma_exposure", result)
        self.assertIn("max_pain_gravity", result)


if __name__ == "__main__":
    unittest.main()
