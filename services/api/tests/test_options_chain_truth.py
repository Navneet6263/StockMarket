from __future__ import annotations

import threading
import unittest

from app.services.options_chain import LiveOptionsChainService


class LiveOptionsChainTruthTests(unittest.TestCase):
    def _service(self):
        service = object.__new__(LiveOptionsChainService)
        service._chain_data = {}
        service._chain_meta = {}
        service._option_symbol_lookup = {
            "NIFTY20AUG2625000CE": (
                "NIFTY",
                {"strike": 25_000.0, "side": "CE", "symbol": "NIFTY20AUG2625000CE"},
            )
        }
        service._data_lock = threading.RLock()
        return service

    def test_option_tick_uses_direct_lookup_and_never_claims_participant_identity(self):
        service = self._service()
        service._on_tick(
            "NIFTY20AUG2625000CE",
            {
                "symbol": "NIFTY20AUG2625000CE",
                "ltp": 125.0,
                "volume": 1_000,
                "open_interest": 10_000,
            },
        )
        service._on_tick(
            "NIFTY20AUG2625000CE",
            {
                "symbol": "NIFTY20AUG2625000CE",
                "ltp": 130.0,
                "volume": 1_500,
                "open_interest": 13_000,
            },
        )

        leg = service._chain_data["NIFTY"]["25000.0"]["CE"]
        self.assertTrue(leg["large_oi_change_flag"])
        self.assertEqual(leg["participant_identity"], "UNKNOWN_FROM_MARKET_DATA")
        self.assertNotIn("institutional_flag", leg)

    def test_missing_oi_is_not_fabricated_and_iv_context_is_not_a_percentile(self):
        service = self._service()
        service._on_tick(
            "NIFTY20AUG2625000CE",
            {"symbol": "NIFTY20AUG2625000CE", "ltp": 125.0, "volume": 1_000},
        )

        leg = service._chain_data["NIFTY"]["25000.0"]["CE"]
        self.assertEqual(leg["oi"], 0)
        self.assertIsNone(service._calculate_iv_cross_section_median(service._chain_data["NIFTY"]))


if __name__ == "__main__":
    unittest.main()
