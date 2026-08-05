from __future__ import annotations

import importlib.util
import importlib
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import pandas as pd
except ModuleNotFoundError:  # The slim CI image can still run non-frame truth tests.
    pd = None

from app.core.settings import Settings
from app.services import nifty_context_analyzer
from app.services import strict_options
from app.services.angelone_live import (
    _broker_open_interest_fields,
    _exchange_tick_timestamp,
    _visible_depth_quantities,
)

if pd is not None:
    from app.services.nse_delivery import NSEDeliveryArchive, parse_delivery_csv
else:
    NSEDeliveryArchive = None
    parse_delivery_csv = None


@unittest.skipIf(pd is None, "pandas is required for delivery-frame tests")
class NSEDeliveryTruthTests(unittest.TestCase):
    def test_official_like_delivery_csv_is_parsed_without_substituting_total_volume(self):
        raw = """ SYMBOL, SERIES, DATE1, TTL_TRD_QNTY, DELIV_QTY, DELIV_PER
RBA, EQ, 05-Aug-2026, 150000, 97500, 65.00
IGNORE, XX, 05-Aug-2026, 1000, 900, 90.00
"""

        parsed = parse_delivery_csv(raw)

        self.assertEqual(parsed["SYMBOL"].tolist(), ["RBA"])
        self.assertEqual(float(parsed.iloc[0]["TTL_TRD_QNTY"]), 150_000)
        self.assertEqual(float(parsed.iloc[0]["DELIV_QTY"]), 97_500)
        self.assertEqual(float(parsed.iloc[0]["DELIV_PER"]), 65.0)
        self.assertEqual(parsed.iloc[0]["TRADE_DATE"], pd.Timestamp("2026-08-05"))

    def test_candle_only_or_non_delivery_csv_is_rejected(self):
        bodies = (
            "SYMBOL,DATE1,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,TTL_TRD_QNTY\nRBA,05-Aug-2026,70,72,69,71,150000\n",
            "SYMBOL,DATE1,TTL_TRD_QNTY,VOLUME_PERCENT\nRBA,05-Aug-2026,150000,65\n",
        )

        for raw in bodies:
            with self.subTest(header=raw.splitlines()[0]):
                self.assertTrue(parse_delivery_csv(raw).empty)

    def test_disabled_archive_does_not_touch_cache_and_enrichment_is_identity_noop(self):
        with (
            patch.object(Path, "mkdir") as mkdir,
            patch.object(NSEDeliveryArchive, "_load_cached_files") as load_cached,
        ):
            archive = NSEDeliveryArchive(Settings(enable_nse_delivery_archive=False))

        frames = {"RBA": pd.DataFrame({"Close": [71.0]})}
        returned = archive.enrich_frames(frames)

        mkdir.assert_not_called()
        load_cached.assert_not_called()
        self.assertIs(returned, frames)
        self.assertNotIn("DELIV_PER", returned["RBA"].columns)
        self.assertNotIn("Deliverable Volume", returned["RBA"].columns)

    def test_daily_timezone_preserves_nse_session_date_for_delivery_join(self):
        fake_yfinance = types.ModuleType("yfinance")
        fake_yfinance.Ticker = type("Ticker", (), {})
        fake_yfinance.download = lambda *args, **kwargs: pd.DataFrame()
        with patch.dict(sys.modules, {"yfinance": fake_yfinance}):
            module = importlib.import_module("app.services.data_provider")
        service = object.__new__(module.MarketDataService)
        raw_frame = pd.DataFrame(
            {
                "Open": [70.0],
                "High": [72.0],
                "Low": [69.0],
                "Close": [71.0],
                "Volume": [150_000],
            },
            index=pd.DatetimeIndex(["2026-08-05 00:00:00+05:30"]),
        )

        daily = service._normalize_frame(raw_frame, interval="1d")
        intraday = service._normalize_frame(raw_frame, interval="5m")
        archive = object.__new__(NSEDeliveryArchive)
        archive.enabled = True
        archive._lock = threading.RLock()
        archive._rows = parse_delivery_csv(
            "SYMBOL,SERIES,DATE1,TTL_TRD_QNTY,DELIV_QTY,DELIV_PER\n"
            "RBA,EQ,05-Aug-2026,150000,97500,65\n"
        )
        enriched = archive.enrich_frames({"RBA": daily})["RBA"]

        self.assertEqual(daily.index[0], pd.Timestamp("2026-08-05"))
        self.assertEqual(intraday.index[0], pd.Timestamp("2026-08-04 18:30:00"))
        self.assertEqual(float(enriched.iloc[0]["DELIV_PER"]), 65.0)
        self.assertEqual(float(enriched.iloc[0]["Deliverable Volume"]), 97_500)


class AggregateFlowTruthTests(unittest.TestCase):
    def test_current_nse_category_schema_parses_commas_and_combines_market_wide_flows(self):
        official_like = [
            {
                "category": "FII/FPI",
                "date": "05-Aug-2026",
                "buyValue": "12,000.50",
                "sellValue": "12,500.75",
                "netValue": "-500.25",
            },
            {
                "category": "DII",
                "date": "05-Aug-2026",
                "buyValue": "9,000.50",
                "sellValue": "7,750.00",
                "netValue": "1,250.50",
            },
        ]

        with (
            patch.object(nifty_context_analyzer, "ENABLE_FII_DII", True),
            patch.object(nifty_context_analyzer, "_get", return_value=official_like),
        ):
            result = nifty_context_analyzer.fetch_fii_dii()

        self.assertTrue(result["available"])
        self.assertEqual(result["fiiNet"], -500.25)
        self.assertEqual(result["diiNet"], 1_250.50)
        self.assertEqual(result["combinedNet"], 750.25)
        self.assertEqual(result["date"], "05-Aug-2026")
        self.assertEqual(result["scope"], "aggregate_cash_market")
        self.assertFalse(result["symbolLevelIdentityAvailable"])

    def test_unexpected_fii_dii_schema_is_unavailable_instead_of_fabricating_zero(self):
        with (
            patch.object(nifty_context_analyzer, "ENABLE_FII_DII", True),
            patch.object(
                nifty_context_analyzer,
                "_get",
                return_value=[{"category": "FII/FPI", "buyValue": "1,000"}],
            ),
        ):
            result = nifty_context_analyzer.fetch_fii_dii()

        self.assertFalse(result["available"])
        self.assertIsNone(result["fiiNet"])
        self.assertIsNone(result["diiNet"])
        self.assertIsNone(result["combinedNet"])
        self.assertEqual(result["reason"], "unexpected_nse_schema")
        self.assertEqual(result["scope"], "aggregate_cash_market")
        self.assertFalse(result["symbolLevelIdentityAvailable"])


class OptionsPositioningTruthTests(unittest.TestCase):
    def test_max_pain_uses_aggregate_payout_not_minimum_same_strike_oi(self):
        chain = {
            "records": {
                "expiryDates": ["27-Aug-2026"],
                "data": [
                    {"expiryDate": "27-Aug-2026", "strikePrice": 90, "CE": {"openInterest": 1}, "PE": {"openInterest": 1}},
                    {"expiryDate": "27-Aug-2026", "strikePrice": 100, "CE": {"openInterest": 1}, "PE": {"openInterest": 1}},
                    {"expiryDate": "27-Aug-2026", "strikePrice": 110, "CE": {"openInterest": 1}, "PE": {"openInterest": 5}},
                ],
            }
        }
        with (
            patch.object(nifty_context_analyzer, "ENABLE_PCR", True),
            patch.object(nifty_context_analyzer, "_get", return_value=chain),
        ):
            result = nifty_context_analyzer.fetch_pcr_max_pain("NIFTY")

        self.assertTrue(result["available"])
        self.assertEqual(result["maxPain"], 110.0)
        self.assertEqual(result["maxPainMethod"], "minimum_aggregate_writer_payout_across_all_strikes")
        self.assertEqual(result["pcrSignal"], "put_oi_heavy")
        self.assertFalse(result["directionalInferenceAvailable"])
        self.assertIn("does not identify", result["pcrMeaning"])

    def test_oi_only_pcr_does_not_vote_market_direction(self):
        with (
            patch.object(
                nifty_context_analyzer,
                "fetch_fii_dii",
                return_value={"available": False, "mood": "unknown", "fiiNet": None, "diiNet": None},
            ),
            patch.object(
                nifty_context_analyzer,
                "fetch_pcr_max_pain",
                return_value={
                    "available": True,
                    "pcr": 2.0,
                    "pcrSignal": "put_oi_heavy",
                    "directionalInferenceAvailable": False,
                },
            ),
        ):
            context = nifty_context_analyzer.get_nifty_context()

        self.assertEqual(context["combinedMood"], "neutral")

    def test_stock_option_oi_change_is_not_mislabeled_as_buying_or_writing(self):
        from app.services.trap_detector import analyze_option_chain_traps

        result = analyze_option_chain_traps(
            spot_price=100,
            pcr=2.0,
            max_pain=90,
            call_oi_at_resistance=500_000,
            put_oi_at_support=100_000,
            call_oi_change=200_000,
            put_oi_change=50_000,
            iv_percentile=85,
        )
        wording = " ".join(
            [
                result["options_summary"],
                *result["bullish_reasons"],
                *result["trap_warnings"],
            ]
        ).lower()

        self.assertEqual(result["options_bias"], "neutral")
        self.assertEqual(result["options_score"], 0)
        self.assertFalse(result["directional_inference_available"])
        self.assertNotIn("call writing", wording)
        self.assertNotIn("put writing", wording)
        self.assertIn("cannot be identified", wording)


class AngelOnePacketTruthTests(unittest.TestCase):
    def test_snapquote_open_interest_is_preserved_only_when_broker_provides_it(self):
        self.assertEqual(
            _broker_open_interest_fields(
                {"open_interest": 12345, "open_interest_change_percentage": -275}
            ),
            {
                "open_interest": 12345,
                "open_interest_change_percentage_raw": -275.0,
            },
        )
        self.assertEqual(_broker_open_interest_fields({}), {})

    def test_best_five_side_flags_win_when_sdk_container_names_are_swapped(self):
        packet = {
            # The SDK can expose these containers under reversed names. Flags
            # remain authoritative: 0=buy and 1=sell.
            "best_5_buy_data": [
                {"flag": 1, "quantity": 40},
                {"flag": 1, "quantity": "10"},
            ],
            "best_5_sell_data": [
                {"flag": 0, "quantity": 120},
                {"flag": 0, "quantity": "30"},
            ],
            "total_buy_quantity": 9_999,
            "total_sell_quantity": 1,
        }

        buy_qty, sell_qty, source = _visible_depth_quantities(packet)

        self.assertEqual(buy_qty, 150.0)
        self.assertEqual(sell_qty, 50.0)
        self.assertEqual(source, "best_5_flag_classified")

    def test_exchange_timestamp_raw_value_is_preserved_while_iso_is_normalized(self):
        raw_milliseconds = 1_700_000_000_123

        raw, iso_value = _exchange_tick_timestamp({"exchange_timestamp": raw_milliseconds})

        self.assertEqual(raw, raw_milliseconds)
        self.assertEqual(iso_value, "2023-11-14T22:13:20.123000+00:00")


class StrictOptionsTruthTests(unittest.TestCase):
    @staticmethod
    def _underlying_signal():
        return {
            "symbol": "GAEL",
            "direction": "bullish",
            "score": 82,
            "risk": "medium",
            "chart": {"volumeSpike": True},
            "raw": {
                "current_price": 150.0,
                "entry_trigger": 149.0,
                "stop_loss": 145.0,
                "target_1": 160.0,
                "risk_reward": 2.0,
                "relative_volume": 1.5,
            },
        }

    @patch.object(strict_options, "is_fno_symbol", return_value=True)
    def test_underlying_confirmation_is_only_contract_watch_not_executable_option(self, _fno):
        idea = strict_options.build_strict_option_idea(
            self._underlying_signal(),
            {"side": "bullish", "status": "BULLISH_CONFIRMED"},
            "test",
        )

        self.assertEqual(idea["status"], "WATCH_CONTRACT")
        self.assertFalse(idea["tradeGatePassed"])
        self.assertTrue(idea["contractVerificationRequired"])
        self.assertIn("No expiry is assumed", idea["expiryRule"])
        self.assertIn("no broker-native option stop", idea["invalidationRule"])

    @patch.object(strict_options, "is_fno_symbol", return_value=True)
    def test_missing_structural_target_blocks_option_candidate(self, _fno):
        signal = self._underlying_signal()
        signal["raw"].pop("target_1")
        idea = strict_options.build_strict_option_idea(
            signal,
            {"side": "bullish", "status": "BULLISH_CONFIRMED"},
            "test",
        )

        self.assertEqual(idea["status"], "NO_TRADE")
        self.assertTrue(any("target" in reason.lower() for reason in idea["blockers"]))

def _market_hub_service_class():
    """Import the ranking class without requiring yfinance in the test image."""

    missing_pandas = importlib.util.find_spec("pandas") is None
    missing_numpy = importlib.util.find_spec("numpy") is None
    missing_yfinance = importlib.util.find_spec("yfinance") is None
    if not (missing_pandas or missing_numpy or missing_yfinance):
        from app.services.market_hub import MarketHubService

        return MarketHubService

    stubs = {}
    if missing_pandas:
        fake_pandas = types.ModuleType("pandas")
        fake_pandas.DataFrame = type("DataFrame", (), {})
        fake_pandas.Series = type("Series", (), {})
        stubs["pandas"] = fake_pandas
    if missing_numpy:
        fake_numpy = types.ModuleType("numpy")
        fake_numpy.nan = float("nan")
        fake_numpy.number = (int, float)
        stubs["numpy"] = fake_numpy
    if missing_yfinance:
        fake_yfinance = types.ModuleType("yfinance")
        fake_yfinance.EquityQuery = type("EquityQuery", (), {})
        stubs["yfinance"] = fake_yfinance
    with patch.dict(sys.modules, stubs):
        market_hub_module = importlib.import_module("app.services.market_hub")

    return market_hub_module.MarketHubService


def _smart_layers_module():
    """Load the integration layer in slim test images without running frame math."""

    missing_pandas = importlib.util.find_spec("pandas") is None
    missing_numpy = importlib.util.find_spec("numpy") is None
    stubs = {}
    if missing_pandas:
        fake_pandas = types.ModuleType("pandas")
        fake_pandas.DataFrame = type("DataFrame", (), {})
        fake_pandas.Series = type("Series", (), {})
        stubs["pandas"] = fake_pandas
    if missing_numpy:
        fake_numpy = types.ModuleType("numpy")
        fake_numpy.nan = float("nan")
        fake_numpy.number = (int, float)
        stubs["numpy"] = fake_numpy
    with patch.dict(sys.modules, stubs):
        return importlib.import_module("app.services.smart_layers")


class LivePoolDiversificationTests(unittest.TestCase):
    def test_negative_footprint_is_directional_and_pool_remains_diversified_and_capped(self):
        market_hub_class = _market_hub_service_class()
        hub = object.__new__(market_hub_class)
        hub._persistent_watch_map = lambda: {}
        results = [
            {
                "symbol": "NEGFOOT",
                "direction": "bearish",
                "large_money_footprint": {"score": -90},
                "move_quality": 99,
                "confidence": 95,
            },
            {
                "symbol": "BASE",
                "direction": "bullish",
                "is_pre_breakout": True,
                "base_quality_score": 90,
                "move_quality": 88,
            },
            {
                "symbol": "MOMENTUM",
                "direction": "bullish",
                "change_pct": 8.0,
                "relative_volume": 3.0,
                "move_quality": 85,
            },
            {"symbol": "QUALITY", "direction": "bullish", "move_quality": 80},
            {"symbol": "EXTRA1", "direction": "bullish", "move_quality": 70},
            {"symbol": "EXTRA2", "direction": "bearish", "move_quality": 60},
        ]

        selected = hub._top_symbols(results, limit=4)

        self.assertEqual(len(selected), 4)
        self.assertEqual(len(set(selected)), 4)
        self.assertIn("NEGFOOT", selected)
        self.assertIn("BASE", selected)
        self.assertIn("MOMENTUM", selected)
        negative = next(item for item in results if item["symbol"] == "NEGFOOT")
        self.assertEqual(
            negative["live_pool_reason"],
            "REVERSAL_OR_DIRECTIONAL_LARGE_MONEY_FOOTPRINT",
        )


class StructuralPlanTruthTests(unittest.TestCase):
    def test_inside_zone_wins_over_stronger_nearby_zone_without_state_leak(self):
        market_hub_class = _market_hub_service_class()
        select_zone = market_hub_class._evaluate_symbol.__globals__["_select_gtf_zone"]
        demand_zones = [
            {"type": "demand", "distal": 97, "proximal": 100, "strength": 2},
            {"type": "demand", "distal": 90, "proximal": 96, "strength": 9},
        ]
        supply_zones = [
            {"type": "supply", "proximal": 98, "distal": 101, "strength": 2},
            {"type": "supply", "proximal": 102, "distal": 110, "strength": 9},
        ]

        demand_choice = select_zone(demand_zones, 99, "demand")
        supply_choice = select_zone(supply_zones, 99, "supply")

        self.assertEqual(demand_choice["state"], "inside")
        self.assertEqual(demand_choice["zone"]["proximal"], 100)
        self.assertEqual(supply_choice["state"], "inside")
        self.assertEqual(supply_choice["zone"]["proximal"], 98)

    def test_nearest_real_structure_is_used_and_target_is_not_manufactured(self):
        from app.services.trade_plan import assess_structural_plan

        allowed = assess_structural_plan("bullish", 100, 96, [130, 108])
        poor_reward = assess_structural_plan("bullish", 100, 96, [105])

        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["target1"], 108)
        self.assertEqual(allowed["riskReward"], 2.0)
        self.assertEqual(allowed["maxPositionPctAt1PctAccountRisk"], 25.0)
        self.assertFalse(poor_reward["allowed"])
        self.assertEqual(poor_reward["target1"], 105)
        self.assertEqual(poor_reward["blockCode"], "INSUFFICIENT_STRUCTURE_REWARD")

    def test_wide_structural_stop_stays_watch_only_and_never_enters_live_pool(self):
        from app.services.entry_monitor import EntryMonitor
        from app.services.trade_plan import assess_structural_plan, watch_only_plan_fields

        plan = assess_structural_plan("bullish", 100, 91, [130])
        row = {
            "symbol": "WIDE",
            "direction": "bullish",
            **watch_only_plan_fields(plan),
        }
        monitor = EntryMonitor()
        monitor._register_watchlist({"all_entry_levels": [row]})

        self.assertFalse(plan["allowed"])
        self.assertEqual(plan["blockCode"], "STRUCTURAL_STOP_TOO_WIDE")
        self.assertEqual(plan["riskPct"], 9.0)
        self.assertEqual(monitor.get_watched_symbols(), [])


class PostSmartGateIntegrationTests(unittest.TestCase):
    def test_bearish_market_blocks_bullish_arm_and_monitor_cannot_resurrect_display_row(self):
        smart_layers = _smart_layers_module()
        from app.services.entry_monitor import EntryMonitor

        market_context = {
            "marketMood": "bearish",
            "freshBuyBlocked": True,
            "blockedReason": "Market breadth weak; fresh buy calls blocked.",
        }
        bullish = {
            "symbol": "BULL",
            "direction": "bullish",
            "action": "BUY",
            "safe_entry_price": 100,
            "stop_loss": 95,
            "target_1": 115,
        }
        bearish = {
            "symbol": "BEAR",
            "direction": "bearish",
            "action": "SELL",
            "safe_entry_price": 100,
            "stop_loss": 105,
            "target_1": 85,
        }
        payload = {
            # BULL intentionally remains in a display bucket. The live monitor
            # must consume only the authoritative post-gate arm list.
            "top_opportunities": [bullish],
            "breakout_radar": [
                {
                    **bullish,
                    "symbol": "RADAR",
                    "current_price": 100,
                    "accumulation_score": 90,
                }
            ],
            "all_entry_levels": [bullish, bearish],
        }

        with (
            patch.object(smart_layers, "compute_market_breadth", return_value=market_context),
            patch.object(smart_layers, "rank_global_sectors", return_value={}),
            patch.object(smart_layers, "should_block_buy", side_effect=lambda context: bool(context.get("freshBuyBlocked"))),
        ):
            output = smart_layers.build_smart_scan_payload(
                payload,
                daily_frames={},
                benchmark_frame=object(),
            )

        self.assertEqual([row["symbol"] for row in output["all_entry_levels"]], ["BEAR"])
        self.assertEqual(output["liveEntryGate"]["armedCount"], 1)
        self.assertEqual(output["liveEntryGate"]["blockedCount"], 1)
        self.assertEqual(output["liveEntryGate"]["blocked"][0]["symbol"], "BULL")
        self.assertTrue(output["top_opportunities"][0]["marketGateBlocked"])
        self.assertFalse(output["top_opportunities"][0]["allow_buy_call"])
        self.assertTrue(output["breakout_radar"][0]["marketGateBlocked"])

        monitor = EntryMonitor()
        monitor._register_watchlist(output)
        self.assertEqual(monitor.get_watched_symbols(), ["BEAR"])

        from app.services.telegram_market_alerts import TelegramMarketAlertService
        telegram = object.__new__(TelegramMarketAlertService)
        self.assertEqual(telegram.collect_alerts(output), [])

        if pd is not None:
            from app.services.setup_tracker import SetupTrackerService
            tracker = object.__new__(SetupTrackerService)
            tracker.settings = SimpleNamespace(
                min_price=10,
                min_volume=50_000,
                tracked_promotion_confidence_min=68,
                tracked_promotion_move_quality_min=58,
            )
            blocked = {
                **output["top_opportunities"][0],
                "current_price": 100,
                "volume": 1_000_000,
                "risk_reward": 2,
                "risk_level": "medium",
                "confidence": 90,
                "move_quality": 90,
                "rsi": 55,
                "relative_volume": 2,
                "intraday_volume_ratio": 2,
            }
            self.assertEqual(tracker._tracking_label(blocked), "Avoid / High Risk")
            self.assertIsNone(tracker._status_for_label(tracker._tracking_label(blocked)))

    def test_bullish_market_blocks_weak_short_but_allows_only_strong_strict_breakdown(self):
        smart_layers = _smart_layers_module()
        market_context = {
            "marketMood": "bullish",
            "freshBuyBlocked": False,
            "blockedReason": None,
        }
        weak_short = {
            "symbol": "WEAKSHORT",
            "direction": "bearish",
            "action": "SELL",
            "entry_trigger": 100,
            "stop_loss": 105,
            "target_1": 85,
            "large_money_footprint": {"score": -10},
        }
        strong_breakdown = {
            **weak_short,
            "symbol": "BREAKDOWN",
            "signal_stage": "BREAKDOWN_WATCH",
            "action": "WAIT_FOR_BREAKDOWN_CONFIRMATION",
            "requires_live_confirmation": True,
            "large_money_footprint": {"score": -70},
        }
        payload = {
            "bearish_risks": [weak_short, strong_breakdown],
            "all_entry_levels": [weak_short, strong_breakdown],
        }

        with (
            patch.object(smart_layers, "compute_market_breadth", return_value=market_context),
            patch.object(smart_layers, "rank_global_sectors", return_value={}),
        ):
            output = smart_layers.build_smart_scan_payload(payload, {}, object())

        self.assertEqual([item["symbol"] for item in output["all_entry_levels"]], ["BREAKDOWN"])
        armed = output["all_entry_levels"][0]
        self.assertTrue(armed["counterRegime"])
        self.assertTrue(armed["requires_full_tick"])
        self.assertEqual(armed["live_min_confirmations"], 3)
        self.assertTrue(output["bearish_risks"][0]["marketGateBlocked"])

    def test_strong_gael_style_reversal_is_strict_watch_not_bear_market_buy(self):
        smart_layers = _smart_layers_module()
        market_context = {
            "marketMood": "bearish",
            "freshBuyBlocked": True,
            "blockedReason": "Market breadth weak; fresh buy calls blocked.",
        }
        reversal = {
            "symbol": "GAEL",
            "direction": "bullish",
            "action": "WAIT_FOR_LIVE_CONFIRMATION",
            "signal_stage": "BULLISH_REVERSAL_CANDIDATE",
            "reversal_bias": "bullish",
            "bullish_reversal_score": 72,
            "entry_trigger": 145,
            "stop_loss": 137,
            "target_1": 165,
            "requires_live_confirmation": True,
            "attention_only": True,
            "allow_buy_call": False,
        }
        payload = {"retest_entry": [reversal], "all_entry_levels": [reversal]}

        with (
            patch.object(smart_layers, "compute_market_breadth", return_value=market_context),
            patch.object(smart_layers, "rank_global_sectors", return_value={}),
            patch.object(smart_layers, "should_block_buy", return_value=True),
        ):
            output = smart_layers.build_smart_scan_payload(payload, {}, object())

        self.assertEqual([item["symbol"] for item in output["all_entry_levels"]], ["GAEL"])
        armed = output["all_entry_levels"][0]
        self.assertFalse(armed.get("marketGateBlocked", False))
        self.assertTrue(armed["counterRegime"])
        self.assertEqual(armed["marketGateDecision"], "COUNTER_REGIME_STRICT_CONFIRMATION")
        self.assertEqual(armed["live_min_confirmations"], 3)
        self.assertFalse(armed["allow_buy_call"])


if __name__ == "__main__":
    unittest.main()
