from __future__ import annotations

import sys
import unittest
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from app.services.zone_detector import ZoneDetector


def candle(open_price: float, high: float, low: float, close: float) -> dict[str, float]:
    return {"open": open_price, "high": high, "low": low, "close": close}


def demand_formation() -> list[dict[str, float]]:
    return [
        candle(105.0, 106.0, 98.0, 99.0),       # exciting red leg-in
        candle(99.5, 101.0, 98.5, 100.0),       # base
        candle(100.0, 108.0, 99.0, 107.0),      # leg-out starts
        candle(107.0, 115.0, 106.0, 114.0),     # actual leg-out ends
        candle(114.0, 116.0, 112.0, 114.5),     # post-formation pause
    ]


def supply_formation() -> list[dict[str, float]]:
    return [
        candle(95.0, 102.0, 94.0, 101.0),       # exciting green leg-in
        candle(100.0, 101.5, 99.0, 100.5),      # base
        candle(100.0, 101.0, 92.0, 93.0),       # leg-out starts
        candle(93.0, 94.0, 85.0, 86.0),         # actual leg-out ends
        candle(86.0, 88.0, 84.0, 85.5),         # post-formation pause
    ]


def original_zone(zones: list[dict], zone_type: str) -> dict:
    return next(zone for zone in zones if zone["type"] == zone_type and zone["base_start_idx"] == 1)


class ZoneLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = ZoneDetector()

    def test_untouched_zones_are_fresh(self):
        for zone_type, history in {
            "demand": demand_formation(),
            "supply": supply_formation(),
        }.items():
            with self.subTest(zone_type=zone_type):
                zone = original_zone(self.detector.detect_zones(history), zone_type)

                self.assertEqual(zone["status"], "fresh")
                self.assertTrue(zone["is_fresh"])
                self.assertFalse(zone["is_tested"])
                self.assertTrue(zone["is_active"])

    def test_demand_first_touch_on_current_candle_remains_visible(self):
        history = demand_formation() + [candle(105.0, 106.0, 99.5, 103.0)]

        zones = self.detector.detect_zones(history)
        zone = original_zone(zones, "demand")

        self.assertEqual(zone["status"], "currently_testing")
        self.assertTrue(zone["currently_testing"])
        self.assertTrue(zone["is_active"])
        self.assertEqual(zone["prior_touch_count"], 0)
        self.assertEqual(zone["first_touch_idx"], len(history) - 1)
        self.assertEqual(zone["base_end_idx"], 1)
        self.assertEqual(zone["leg_out_start_idx"], 2)
        self.assertEqual(zone["leg_out_end_idx"], 3)
        self.assertEqual(zone["end_idx"], 3)

    def test_supply_first_touch_on_current_candle_remains_visible(self):
        history = supply_formation() + [candle(97.0, 100.5, 96.0, 99.0)]

        zones = self.detector.detect_zones(history)
        zone = original_zone(zones, "supply")

        self.assertEqual(zone["status"], "currently_testing")
        self.assertTrue(zone["currently_testing"])
        self.assertTrue(zone["is_active"])
        self.assertEqual(zone["prior_touch_count"], 0)
        self.assertEqual(zone["first_touch_idx"], len(history) - 1)
        self.assertEqual(zone["leg_out_end_idx"], 3)
        self.assertEqual(zone["leg_out_candle_count"], 2)

    def test_prior_touch_marks_zone_consumed_and_hides_it_from_active_results(self):
        cases = {
            "demand": demand_formation()
            + [
                candle(104.0, 105.0, 99.5, 100.5),
                candle(106.0, 110.0, 105.0, 109.0),
            ],
            "supply": supply_formation()
            + [
                candle(96.0, 100.5, 95.0, 99.5),
                candle(94.0, 95.0, 88.0, 89.0),
            ],
        }

        for zone_type, history in cases.items():
            with self.subTest(zone_type=zone_type):
                all_zones = self.detector.detect_zones(history, include_inactive=True)
                zone = original_zone(all_zones, zone_type)
                active_zones = self.detector.detect_zones(history)

                self.assertEqual(zone["status"], "consumed")
                self.assertTrue(zone["is_consumed"])
                self.assertFalse(zone["is_active"])
                self.assertEqual(zone["prior_touch_count"], 1)
                self.assertFalse(
                    any(
                        item["type"] == zone_type and item["base_start_idx"] == 1
                        for item in active_zones
                    )
                )

    def test_distal_breach_marks_zone_broken_and_hides_it(self):
        cases = {
            "demand": demand_formation() + [candle(101.0, 102.0, 97.5, 98.0)],
            "supply": supply_formation() + [candle(99.0, 102.5, 98.0, 102.0)],
        }

        for zone_type, history in cases.items():
            with self.subTest(zone_type=zone_type):
                all_zones = self.detector.detect_zones(history, include_inactive=True)
                zone = original_zone(all_zones, zone_type)
                active_zones = self.detector.detect_zones(history)

                self.assertEqual(zone["status"], "broken")
                self.assertTrue(zone["is_broken"])
                self.assertFalse(zone["is_active"])
                self.assertEqual(zone["broken_idx"], len(history) - 1)
                self.assertFalse(
                    any(
                        item["type"] == zone_type and item["base_start_idx"] == 1
                        for item in active_zones
                    )
                )


if __name__ == "__main__":
    unittest.main()
