from __future__ import annotations

import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable

import pandas as pd

from app.core.cache import TTLCache
from app.core.settings import Settings
from app.market_intelligence import MarketIntelligenceService
from app.market_universe import MarketUniverseService
from app.opportunity_finder import OpportunityFinder
from app.services.backtest import BacktestService
from app.services.data_provider import MarketDataService
from app.services.indicators import IndicatorEngine
from app.services.narrative import NarrativeService
from app.services.nse_delivery import NSEDeliveryArchive
from app.services.scoring import ScoringEngine
from app.services.smart_layers import build_smart_scan_payload
from app.services.setup_tracker import SetupTrackerService
from app.services.nifty_context_analyzer import analyze_nifty_context, stock_nifty_alignment_score
from app.services.breakout_radar import build_breakout_radar
from app.services.chart_patterns import detect_chart_pattern_setup
from app.services.telegram_market_alerts import get_telegram_market_alerts
from app.services.trade_plan import (
    actionable_plan_fields,
    assess_structural_plan,
    watch_only_plan_fields,
)
from app.services.zone_detector import ZoneDetector


logger = logging.getLogger(__name__)


def _select_gtf_zone(zones: list[Dict], current_price: float, zone_type: str) -> Dict | None:
    """Select one zone and keep its own touch state attached.

    A currently tested zone wins over a stronger nearby zone.  This prevents a
    global ``in_zone`` flag from accidentally arming an untouched level.
    """

    candidates: list[Dict] = []
    for zone in zones:
        try:
            prox_val = zone.get("proximal")
            dist_val = zone.get("distal")
            if prox_val is None or dist_val is None:
                continue
            proximal = float(prox_val)
            distal = float(dist_val)
        except (TypeError, ValueError):
            continue
        if zone_type == "demand":
            inside = distal <= current_price <= proximal
            nearby = proximal < current_price <= proximal * 1.05
            distance = 0.0 if inside else abs(current_price - proximal) / current_price
        else:
            inside = proximal <= current_price <= distal
            nearby = proximal * 0.95 <= current_price < proximal
            distance = 0.0 if inside else abs(proximal - current_price) / current_price
        if not (inside or nearby):
            continue
        candidates.append(
            {
                "zone": zone,
                "state": "inside" if inside else "near",
                "distance": distance,
                "strength": float(zone.get("strength") or 0),
            }
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: (
            candidate["state"] == "inside",
            candidate["strength"],
            -candidate["distance"],
        ),
    )


class MarketHubService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.data = MarketDataService(settings)
        self.universe = MarketUniverseService()
        self.market_intelligence = MarketIntelligenceService()
        self.company_research = OpportunityFinder()
        self.indicators = IndicatorEngine()
        self.scoring = ScoringEngine()
        self.backtest = BacktestService(settings, self.indicators, self.scoring)
        self.narrative = NarrativeService()
        self.tracker = SetupTrackerService(settings, self.data)
        self.delivery_archive = NSEDeliveryArchive(settings)
        self.zone_detector = ZoneDetector()
        self.scan_cache: TTLCache[Dict] = TTLCache(settings.scan_cache_ttl_sec)
        self.detail_cache: TTLCache[Dict] = TTLCache(settings.detail_cache_ttl_sec)
        self.scan_refresh_lock = threading.Lock()
        self.background_scan_started = False
        self.background_scan_stop = threading.Event()
        self.last_successful_scan: Dict | None = None
        self.last_successful_scan_at = 0.0
        self._nifty_context_cache: Dict | None = None
        self._nifty_context_at: float = 0.0
        self._nifty_context_ttl: float = 300.0  # 5 minutes

    def start_background_scanner(self) -> bool:
        if self.background_scan_started:
            return False
        self.background_scan_started = True

        def loop():
            logger.info("market pattern background scanner started interval_sec=%s", self.settings.scan_interval_sec)
            while not self.background_scan_stop.is_set():
                try:
                    if not self._last_success_is_usable():
                        self._start_background_refresh(force_refresh=True)
                except Exception:
                    logger.exception("market pattern background scanner tick failed")
                self.background_scan_stop.wait(max(30, self.settings.scan_interval_sec))

        thread = threading.Thread(target=loop, name="market-pattern-scanner", daemon=True)
        thread.start()
        return True

    def _scan_symbols(self, discovery: Dict) -> list[str]:
        custom = list(self.settings.custom_universe)
        persistent = list(self._persistent_watch_map())
        ranked = discovery.get("scan_symbols") or discovery.get("symbols") or []
        invalid_symbols = set(self.settings.invalid_symbols)
        ranked = [symbol for symbol in ranked if symbol not in invalid_symbols]
        if custom:
            merged = list(
                dict.fromkeys(symbol for symbol in custom + persistent + ranked if symbol not in invalid_symbols)
            )
            return merged[: self.settings.scan_symbol_limit]
        # Previously surfaced calls stay in the research universe for the full
        # setup-memory window.  This is what lets a stopped/expired setup form a
        # fresh base and re-arm instead of disappearing forever.
        return list(dict.fromkeys(persistent + ranked))[: self.settings.scan_symbol_limit]

    def _copy_with_warning(self, payload: Dict, warning: str, *, cache_status: str) -> Dict:
        response = copy.deepcopy(payload)
        response["warning"] = warning
        response["cache_status"] = cache_status
        return response

    def _empty_scan_payload(self, warning: str) -> Dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "universe_size": 0,
            "market_breadth": {
                "advancing": 0,
                "declining": 0,
                "advance_decline_ratio": 0.0,
                "bullish_setups": 0,
                "bearish_setups": 0,
                "bullish_ratio": 0.0,
                "benchmark_change_pct": 0.0,
            },
            "market_discovery": {"source_mode": "unavailable", "note": warning},
            "macro_context": self.market_intelligence.get_macro_snapshot(),
            "global_context": self.market_intelligence.get_global_scenario(),
            "top_opportunities": [],
            "top_validated_calls": [],
            "unusual_volume": [],
            "breakout_candidates": [],
            "bearish_risks": [],
            "top_movers": [],
            "fast_movers_missed_moves": [],
            "pre_breakout_setups": [],
            "pattern_forming_setups": [],
            "alert_above_setups": [],
            "retest_entry": [],
            "momentum_continuation": [],
            "re_entry_setups": [],
            "missed_moves_analysis": [],
            "avoid_late_entry": [],
            "candidates": [],
            "avoid_risky": [],
            "live_candidate_symbols": [],
            "live_candidate_pool": [],
            "all_entry_levels": [],
            "data_health": {
                "institution_identity_available": False,
                "large_money_label": "visible behavioural footprint",
            },
            "breakout_radar": [],
            "nifty_context": {"nifty_bias": "neutral", "nifty_regime": "unknown", "market_score": 50, "data_available": False},
            "summary": {
                "high_priority": 0,
                "watchlist": 0,
                "avoid": 0,
                "opportunities_count": 0,
                "top_validated_count": 0,
                "breakout_count": 0,
                "bearish_risk_count": 0,
                "unusual_volume_count": 0,
                "market_mood": "waiting_for_scan",
                "scanner_leader": None,
                "total_scanned_universe": 0,
                "fast_movers_count": 0,
                "pre_breakout_count": 0,
                "pattern_forming_count": 0,
                "alert_above_count": 0,
                "retest_entry_count": 0,
                "momentum_continuation_count": 0,
                "re_entry_count": 0,
                "missed_moves_count": 0,
                "avoid_late_entry_count": 0,
                "candidates_count": 0,
                "avoid_count": 0,
            },
            "warning": warning,
            "cache_status": "empty_fallback",
        }

    def _signal_from_tracked_setup(self, row: Dict) -> Dict:
        current_price = row.get("current_price") or row.get("entry_price") or 0
        target_price = row.get("target_price") or row.get("target_1") or current_price
        return {
            "symbol": row.get("symbol"),
            "company_name": row.get("company_name") or row.get("symbol"),
            "direction": row.get("direction") or "neutral",
            "setup_label": row.get("setup_label") or row.get("tracking_label") or "Tracked scanner call",
            "alert_level": "watchlist",
            "confidence": row.get("confidence") or 0,
            "model_confidence": row.get("model_confidence") or row.get("confidence") or 0,
            "evidence_confidence": row.get("evidence_confidence"),
            "historical_evidence_status": row.get("evidence_status") or "not_loaded",
            "confidence_note": "Loaded from persisted scanner calls while the live scan refreshes.",
            # Confidence is an evidence score, not an empirically calibrated
            # probability.  Keep the distinction explicit in persisted calls.
            "probability": None,
            "probability_available": False,
            "model_alignment_score": row.get("confidence") or 0,
            "move_quality": row.get("move_quality") or 0,
            "expected_move_pct": row.get("expected_move_pct") or 0,
            "risk_level": row.get("risk_level") or "medium",
            "current_price": current_price,
            "target_price": target_price,
            "target_1": row.get("target_1") or target_price,
            "target_2": row.get("target_2") or row.get("extended_target_price"),
            "extended_target_price": row.get("extended_target_price"),
            "extended_target": row.get("target_2") or row.get("extended_target_price"),
            "stop_loss": row.get("stop_loss"),
            "trailing_stop": row.get("trailing_stop"),
            "exit_signal": row.get("hold_or_exit") == "EXIT",
            "hold_or_exit": row.get("hold_or_exit") or "WAIT",
            "reason_for_exit_decision": row.get("reason_for_exit_decision") or row.get("last_update_note") or "",
            "risk_reward": row.get("risk_reward") or 0,
            "timeframe_label": row.get("timeframe_label") or "tracked",
            "timeframe_days": row.get("timeframe_days") or 1,
            "change_pct": row.get("change_pct") or 0,
            "volume": 0,
            "relative_volume": row.get("relative_volume") or 1,
            "gap_pct": 0,
            "intraday_volume_ratio": row.get("intraday_volume_ratio") or 1,
            "benchmark_relative_strength": 0,
            "rsi": 50,
            "atr_pct": 0,
            "support": None,
            "resistance": None,
            "rolling_vwap": None,
            "invalidation": row.get("invalidation"),
            "reasons": row.get("reasons") or [],
            "weaknesses": [],
            "risk_factors": row.get("risk_factors") or [],
            "tags": row.get("tags") or [],
            "signal_summary": row.get("reason_summary") or row.get("last_update_note") or "Persisted scanner call.",
            "score_breakdown": {},
            "discovered_by": [row.get("scanner_bucket") or "scanner_suggested"],
        }

    def _tracked_fallback_payload(self, warning: str) -> Dict:
        try:
            rows = self.tracker.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0
                  AND ignored = 0
                  AND source_mode = 'scanner_suggested'
                  AND scanner_call_status IN (
                    'ACTIVE', 'TARGET_1_HIT', 'TARGET_2_HIT', 'PARTIAL_BOOK',
                    'TRAILING_HOLD', 'RETEST', 'REARMED', 'CONTINUATION', 'EXIT_SUGGESTED'
                  )
                  AND status != 'expired'
                ORDER BY suggested_at DESC, confidence DESC
                LIMIT 20
                """
            )
        except Exception:
            logger.exception("failed to build tracked fallback payload")
            rows = []

        if not rows:
            return self._empty_scan_payload(warning)

        signals = [self._signal_from_tracked_setup(row) for row in rows]
        bullish = [item for item in signals if item["direction"] == "bullish"]
        bearish = [item for item in signals if item["direction"] == "bearish"]
        unusual = [item for item in signals if item.get("relative_volume", 1) >= 1.6]
        payload = self._empty_scan_payload(warning)
        payload.update(
            {
                "universe_size": len(signals),
                "valid_signal_count": len(signals),
                "market_breadth": {
                    **payload["market_breadth"],
                    "bullish_setups": len(bullish),
                    "bearish_setups": len(bearish),
                    "bullish_ratio": round(len(bullish) / max(len(signals), 1), 3),
                },
                "top_opportunities": bullish[:8],
                "top_validated_calls": bullish[:8],
                "bearish_risks": bearish[:8],
                "breakout_candidates": signals[:8],
                "unusual_volume": unusual[:8],
                "pre_breakout_setups": [],
                "pattern_forming_setups": [],
                "alert_above_setups": [],
                "retest_entry": [],
                "momentum_continuation": [],
                "re_entry_setups": [],
                "missed_moves_analysis": [],
                "avoid_late_entry": [],
                "summary": {
                    **payload["summary"],
                    "watchlist": len(signals),
                    "opportunities_count": len(bullish[:8]),
                    "top_validated_count": len(bullish[:8]),
                    "bearish_risk_count": len(bearish[:8]),
                    "breakout_count": len(signals[:8]),
                    "unusual_volume_count": len(unusual[:8]),
                    "scanner_leader": signals[0]["symbol"] if signals else None,
                    "total_scanned_universe": len(signals),
                    "valid_signal_count": len(signals),
                    "pre_breakout_count": 0,
                    "pattern_forming_count": 0,
                    "alert_above_count": 0,
                    "retest_entry_count": 0,
                    "momentum_continuation_count": 0,
                    "re_entry_count": 0,
                    "missed_moves_count": 0,
                    "avoid_late_entry_count": 0,
                },
                "cache_status": "tracked_fallback",
            }
        )
        return payload

    def _last_success_is_usable(self) -> bool:
        return (
            self.last_successful_scan is not None
            and time.time() - self.last_successful_scan_at <= self.settings.stale_scan_cache_ttl_sec
        )

    def _save_successful_scan(self, payload: Dict) -> Dict:
        self.last_successful_scan = copy.deepcopy(payload)
        self.last_successful_scan_at = time.time()
        self.scan_cache.set("market_overview", payload)
        # Feed fresh entry levels into the real-time entry monitor
        try:
            from app.services.entry_monitor import get_entry_monitor
            monitor = get_entry_monitor()
            monitor.start(payload)
            # Ensure the WebSocket is subscribed to ALL watched symbols
            watched_symbols = monitor.get_watched_symbols()
            if watched_symbols:
                from app.services.angelone_live import get_angelone_live
                get_angelone_live().start_feed(watched_symbols)
                logger.info("Live feed updated with %d watched symbols", len(watched_symbols))
        except Exception:
            logger.debug("entry_monitor / live feed update skipped", exc_info=True)
        return payload

    def _start_background_refresh(self, force_refresh: bool = True) -> bool:
        if not self.scan_refresh_lock.acquire(blocking=False):
            return False

        def refresh():
            try:
                self._refresh_scan_market(force_refresh=force_refresh)
            except Exception:
                logger.exception("market overview background refresh failed")
            finally:
                self.scan_refresh_lock.release()

        thread = threading.Thread(target=refresh, name="market-overview-refresh", daemon=True)
        thread.start()
        return True

    def _get_nifty_context(self) -> Dict:
        """Get Nifty context with caching (5 min TTL)."""
        now = time.time()
        if self._nifty_context_cache and (now - self._nifty_context_at) < self._nifty_context_ttl:
            return self._nifty_context_cache
        try:
            ctx = analyze_nifty_context()
            self._nifty_context_cache = ctx
            self._nifty_context_at = now
            return ctx
        except Exception:
            logger.exception("nifty context analysis failed")
            return {"nifty_bias": "neutral", "nifty_regime": "unknown", "market_score": 50, "data_available": False}

    def _persistent_watch_map(self) -> dict[str, Dict]:
        """Return recent scanner calls that still deserve re-entry surveillance.

        A target, stop, or expiry closes one trade decision; it does not erase
        the symbol's setup history.  Keeping these calls in the research pool
        for 30-60 sessions prevents RBA/GAEL-style target-hit amnesia.
        """

        now_iso = datetime.now(timezone.utc).isoformat()
        # Legacy rows have no memory_expires_at. Approximate trading sessions
        # as calendar days only for those rows; new lifecycle rows carry the
        # exact persisted memory horizon.
        legacy_days = max(30, round(self.settings.setup_memory_days * 7 / 5))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=legacy_days)).isoformat()
        limit = max(200, int(self.settings.intraday_symbol_limit) * 4)
        try:
            rows = self.tracker.store.list_setups(
                """
                SELECT symbol, direction, status, scanner_call_status,
                       detected_at, suggested_at, confidence, move_quality
                FROM tracked_setups
                WHERE archived = 0
                  AND ignored = 0
                  AND source_mode = 'scanner_suggested'
                  AND (
                    memory_expires_at >= ?
                    OR (memory_expires_at IS NULL AND COALESCE(suggested_at, detected_at) >= ?)
                  )
                ORDER BY
                  CASE scanner_call_status
                    WHEN 'TARGET_1_HIT' THEN 0
                    WHEN 'PARTIAL_BOOK' THEN 1
                    WHEN 'ACTIVE' THEN 2
                    WHEN 'STOP_LOSS_HIT' THEN 3
                    WHEN 'EXPIRED' THEN 4
                    ELSE 5
                  END,
                  COALESCE(suggested_at, detected_at) DESC,
                  confidence DESC
                LIMIT ?
                """,
                (now_iso, cutoff, limit),
            )
        except Exception:
            logger.debug("persistent setup pool unavailable", exc_info=True)
            return {}

        persistent: dict[str, Dict] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol and symbol not in persistent:
                persistent[symbol] = row
        return persistent

    @staticmethod
    def _live_pool_reason(signal: Dict, persistent: dict[str, Dict]) -> str:
        symbol = str(signal.get("symbol") or "").upper()
        tracked = persistent.get(symbol)
        if tracked:
            status = tracked.get("scanner_call_status") or tracked.get("status") or "TRACKED"
            return f"SETUP_MEMORY_{str(status).upper()}"

        footprint = signal.get("large_money_footprint") or {}
        footprint_score = float(footprint.get("score") or signal.get("large_money_footprint_score") or 0)
        tags = {str(tag).lower() for tag in signal.get("tags", [])}
        rsi = float(signal.get("rsi") or 50)
        if (
            signal.get("reversal_watch")
            or float(signal.get("reversal_score") or 0) >= 55
            or signal.get("selling_exhaustion")
            or (rsi <= 38 and (signal.get("support") or "support_respect" in tags))
            or abs(footprint_score) >= 35
        ):
            return "REVERSAL_OR_DIRECTIONAL_LARGE_MONEY_FOOTPRINT"
        if signal.get("is_pre_breakout") or tags.intersection(
            {"base_building", "pre_breakout", "accumulation_watch", "demand_absorption"}
        ):
            return "BASE_OR_PRE_BREAKOUT"
        if abs(float(signal.get("change_pct") or 0)) >= 3 or float(signal.get("relative_volume") or 1) >= 1.6:
            return "FAST_MOVE_OR_UNUSUAL_VOLUME"
        return "QUALITY_RANK"

    def _top_symbols(self, results: list[Dict], limit: int) -> list[str]:
        """Build a diversified, bounded pool for expensive intraday analysis.

        Pure momentum ranking repeatedly evicted quiet bases and stopped calls.
        Quotas preserve lifecycle, reversal/absorption, base and momentum names,
        then quality ranking fills any unused slots.  The returned list remains
        hard-capped, so the live path does not become slower.
        """

        if limit <= 0:
            return []
        persistent = self._persistent_watch_map()

        def number(value, fallback: float = 0.0) -> float:
            try:
                return float(value if value is not None else fallback)
            except (TypeError, ValueError):
                return fallback

        def quality(item: Dict) -> tuple[float, float, float, float]:
            footprint = item.get("large_money_footprint") or {}
            return (
                number(item.get("move_quality")),
                number(item.get("confidence")),
                abs(number(footprint.get("score") or item.get("large_money_footprint_score"))),
                number(item.get("relative_volume"), 1),
            )

        def is_reversal(item: Dict) -> bool:
            tags = {str(tag).lower() for tag in item.get("tags", [])}
            footprint = item.get("large_money_footprint") or {}
            return bool(
                item.get("reversal_watch")
                or item.get("selling_exhaustion")
                or number(item.get("reversal_score")) >= 55
                or abs(number(footprint.get("score") or item.get("large_money_footprint_score"))) >= 35
                or (
                    number(item.get("rsi"), 50) <= 38
                    and (item.get("support") or tags.intersection({"support_respect", "demand_absorption"}))
                )
            )

        def is_base(item: Dict) -> bool:
            tags = {str(tag).lower() for tag in item.get("tags", [])}
            return bool(
                item.get("is_pre_breakout")
                or number(item.get("baseQualityScore") or item.get("base_quality_score")) >= 55
                or tags.intersection({"base_building", "pre_breakout", "accumulation_watch", "demand_absorption"})
            )

        persistent_bucket = sorted(
            [item for item in results if str(item.get("symbol") or "").upper() in persistent],
            key=quality,
            reverse=True,
        )
        reversal_bucket = sorted([item for item in results if is_reversal(item)], key=quality, reverse=True)
        base_bucket = sorted([item for item in results if is_base(item)], key=quality, reverse=True)
        momentum_bucket = sorted(
            [
                item for item in results
                if abs(number(item.get("change_pct"))) >= 3 or number(item.get("relative_volume"), 1) >= 1.6
            ],
            key=lambda item: (
                abs(number(item.get("change_pct"))),
                number(item.get("relative_volume"), 1),
                *quality(item),
            ),
            reverse=True,
        )
        general_bucket = sorted(
            [item for item in results if item.get("direction") != "neutral"],
            key=quality,
            reverse=True,
        ) + sorted([item for item in results if item.get("direction") == "neutral"], key=quality, reverse=True)

        quotas = (
            (persistent_bucket, max(1, round(limit * 0.25))),
            (reversal_bucket, max(1, round(limit * 0.25))),
            (base_bucket, max(1, round(limit * 0.25))),
            (momentum_bucket, max(1, round(limit * 0.15))),
        )
        selected: list[Dict] = []
        seen: set[str] = set()

        def add(pool: list[Dict], cap: int) -> None:
            added = 0
            for item in pool:
                symbol = str(item.get("symbol") or "").strip().upper()
                if not symbol or symbol in seen:
                    continue
                selected.append(item)
                seen.add(symbol)
                added += 1
                if len(selected) >= limit or added >= cap:
                    return

        for bucket, quota in quotas:
            add(bucket, quota)
            if len(selected) >= limit:
                break
        if len(selected) < limit:
            add(general_bucket, limit - len(selected))

        for item in selected:
            item["live_pool_reason"] = self._live_pool_reason(item, persistent)
        return [str(item.get("symbol") or "").upper() for item in selected]

    def _evaluate_symbol(
        self,
        symbol: str,
        frame: pd.DataFrame,
        benchmark_frame: pd.DataFrame,
        quote: Dict | None,
        intraday_frame: pd.DataFrame | None,
        *,
        with_backtest: bool,
    ) -> Dict:
        def safe_float(value, fallback: float) -> float:
            if value is None or pd.isna(value):
                return fallback
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        def safe_int(value, fallback: int) -> int:
            if value is None or pd.isna(value):
                return fallback
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        def apply_structural_trade_plan(
            direction: str,
            entry: float,
            stop: float,
            opposing_levels: Iterable,
            metadata: Dict,
        ) -> Dict:
            """Publish only plans whose real structure supports stop and target."""

            plan = assess_structural_plan(direction, entry, stop, opposing_levels)
            signal.update(metadata)
            if plan["allowed"]:
                signal.update(actionable_plan_fields(plan))
            else:
                signal.update(watch_only_plan_fields(plan))
                risk_factors = list(signal.get("risk_factors") or [])
                if plan.get("blockReason") and plan["blockReason"] not in risk_factors:
                    risk_factors.append(plan["blockReason"])
                signal["risk_factors"] = risk_factors
            return plan

        live_frame = self.data.overlay_quote(frame, quote)
        feature_frame = self.indicators.build_feature_frame(live_frame, benchmark_frame)
        snapshot = self.indicators.build_snapshot(symbol, live_frame, feature_frame, intraday_frame)
        snapshot["advanced_chart_pattern"] = detect_chart_pattern_setup(live_frame, feature_frame, intraday_frame)
        backtest = self.backtest.evaluate(symbol, frame, benchmark_frame) if with_backtest else {}
        signal = self.scoring.evaluate(symbol, snapshot, backtest if with_backtest else None)
        
        # --- GTF Strategy Integration ---
        try:
            gtf_zones = []
            if with_backtest:
                # The all-universe preliminary pass already has the lighter
                # demand/supply scorer. Run detailed 200-bar zone lifecycle
                # only for the capped enhanced/live pool.
                zone_df = live_frame.copy()
                zone_df.columns = [c.lower() for c in zone_df.columns]
                gtf_zones = self.zone_detector.detect_zones(zone_df, max_lookback=200)
            demand_zones = [z for z in gtf_zones if z["type"] == "demand"]
            supply_zones = [z for z in gtf_zones if z["type"] == "supply"]
            current_price = float(quote.get("price") if quote else live_frame["Close"].iloc[-1])
            
            demand_choice = _select_gtf_zone(demand_zones, current_price, "demand")
            supply_choice = _select_gtf_zone(supply_zones, current_price, "supply")
            best_zone = demand_choice["zone"] if demand_choice else None
            best_supply_zone = supply_choice["zone"] if supply_choice else None
            in_demand = bool(demand_choice and demand_choice["state"] == "inside")
            forming_demand = bool(demand_choice and demand_choice["state"] == "near")
            in_supply = bool(supply_choice and supply_choice["state"] == "inside")
            approaching_supply = bool(supply_choice and supply_choice["state"] == "near")
            strongest_demand = demand_choice["strength"] if demand_choice else 0
            strongest_supply = supply_choice["strength"] if supply_choice else 0

            if best_zone and best_supply_zone:
                demand_distance = float(demand_choice["distance"])
                supply_distance = float(supply_choice["distance"])
                if supply_distance < demand_distance:
                    best_zone = None
                    demand_choice = None
                    in_demand = False
                    forming_demand = False
                else:
                    best_supply_zone = None
                    supply_choice = None
                    in_supply = False
                    approaching_supply = False

            if best_zone:
                existing_demand = signal.get("demand_supply") or {}
                zone_status = "IN_DEMAND_ZONE" if in_demand else "NEAR_DEMAND_ZONE"
                zone_score = min(100.0, float(strongest_demand) * 10)
                signal["demand_supply"] = {
                    **existing_demand,
                    "gtfZoneStatus": zone_status,
                    "demandScore": max(float(existing_demand.get("demandScore") or 0), zone_score),
                    "proximal": best_zone["proximal"],
                    "distal": best_zone["distal"],
                    "visibleDemandZone": True,
                    "smartMoneyRead": (
                        "Price is testing a visible demand zone. Buyer identity and pending institutional "
                        "orders cannot be inferred; require live price/volume confirmation."
                    ),
                    "zone_pattern": best_zone.get("pattern"),
                    "identity_inference_supported": False,
                }
                signal["visible_demand_zone"] = True
                signal["demand_zone_score"] = round(zone_score, 1)
                signal["demand_zone_approach_only"] = bool(forming_demand and not in_demand)

                tags = set(signal.get("tags", []))
                if in_demand:
                    tags.add("support_respect")
                if forming_demand:
                    tags.add("base_building")
                    tags.add("accumulation_watch")
                    signal["is_pre_breakout"] = True

                if in_demand and signal.get("direction") == "bullish":
                    # Zone is context; the real-time engine still has to confirm
                    # price response, VWAP, volume velocity and visible depth.
                    entry = round(float(best_zone["proximal"]) * 1.002, 2)
                    stop = round(float(best_zone["distal"]) * 0.995, 2)
                    apply_structural_trade_plan(
                        "bullish",
                        entry,
                        stop,
                        [signal.get("resistance")],
                        {
                            "setup_stage": "RETEST_ENTRY",
                            "signal_stage": "RETEST_ENTRY",
                            "action": "WAIT_FOR_LIVE_CONFIRMATION",
                            "recommended_action": "WAIT_FOR_LIVE_CONFIRMATION",
                            "attention_only": True,
                            "allow_buy_call": False,
                            "requires_live_confirmation": True,
                        },
                    )
                elif in_demand and signal.get("direction") == "bearish" and (
                    str(signal.get("reversal_bias") or "").lower() == "bullish"
                    and safe_float(
                        signal.get("bullish_reversal_score") or signal.get("reversal_risk_score"),
                        0,
                    ) >= 65
                ):
                    # Counter-trend reversal plan: do not turn the bearish model
                    # into an instant BUY. Arm only a reclaim above the demand
                    # zone and require full live footprint confirmation.
                    reversal_score = safe_float(
                        signal.get("bullish_reversal_score") or signal.get("reversal_risk_score"),
                        65,
                    )
                    entry = round(float(best_zone["proximal"]) * 1.002, 2)
                    stop = round(float(best_zone["distal"]) * 0.995, 2)
                    apply_structural_trade_plan(
                        "bullish",
                        entry,
                        stop,
                        [signal.get("resistance")],
                        {
                            "direction": "bullish",
                            "setup_label": "Bullish reversal reclaim watch",
                            "confidence": min(75.0, max(55.0, reversal_score)),
                            "alignment_score": min(75.0, max(55.0, reversal_score)),
                            "setup_stage": "RETEST_ENTRY",
                            "signal_stage": "BULLISH_REVERSAL_CANDIDATE",
                            "action": "WAIT_FOR_LIVE_CONFIRMATION",
                            "recommended_action": "WAIT_FOR_LIVE_CONFIRMATION",
                            "attention_only": True,
                            "allow_buy_call": False,
                            "allow_trade_call": False,
                            "requires_live_confirmation": True,
                            "requires_full_tick": True,
                            "live_min_confirmations": 3,
                            "countertrend_reversal_plan": True,
                            "bearish_support_conflict": True,
                            "alert_level": "watchlist",
                        },
                    )
                    tags.add("bullish_reversal_watch")
                    tags.add("demand_reclaim_required")
                elif in_demand and signal.get("direction") == "bearish":
                    # A lagging trend model must not print 95% bearish while
                    # price is absorbing supply at established support.
                    confidence_cap = 68.0 if in_demand else 75.0
                    signal["confidence"] = min(float(signal.get("confidence") or 50), confidence_cap)
                    signal["bearish_support_conflict"] = True
                    signal["bearish_requires_breakdown"] = True
                    signal["reversal_watch"] = True
                    entry = round(float(best_zone["distal"]) * 0.998, 2)
                    stop = round(float(best_zone["proximal"]) * 1.005, 2)
                    apply_structural_trade_plan(
                        "bearish",
                        entry,
                        stop,
                        [signal.get("support")],
                        {
                            "setup_stage": "BREAKDOWN_WATCH",
                            "signal_stage": "BREAKDOWN_WATCH",
                            "action": "WAIT_FOR_BREAKDOWN_CONFIRMATION",
                            "recommended_action": "WAIT_FOR_BREAKDOWN_CONFIRMATION",
                            "attention_only": True,
                            "allow_buy_call": False,
                            "allow_trade_call": False,
                            "requires_live_confirmation": True,
                            "requires_full_tick": True,
                            "live_min_confirmations": 3,
                            "alert_level": "watchlist",
                        },
                    )
                    weaknesses = list(signal.get("weaknesses") or [])
                    weaknesses.append(
                        "Bearish trend evidence conflicts with a visible demand zone; wait for a decisive close below it."
                    )
                    signal["weaknesses"] = weaknesses
                    tags.add("bearish_support_conflict")
                    tags.add("reversal_watch")
                signal["tags"] = list(tags)

            if best_supply_zone and not best_zone:
                existing_supply = signal.get("demand_supply") or {}
                supply_status = "IN_SUPPLY_ZONE" if in_supply else "NEAR_SUPPLY_ZONE"
                supply_score = min(100.0, float(strongest_supply) * 10)
                signal["demand_supply"] = {
                    **existing_supply,
                    "gtfZoneStatus": supply_status,
                    "supplyScore": max(float(existing_supply.get("supplyScore") or 0), supply_score),
                    "supplyProximal": best_supply_zone["proximal"],
                    "supplyDistal": best_supply_zone["distal"],
                    "visibleSupplyZone": True,
                    "smartMoneyRead": (
                        "Price is testing a visible supply zone. Seller identity and pending orders "
                        "cannot be inferred; require live price/volume confirmation."
                    ),
                    "zone_pattern": best_supply_zone.get("pattern"),
                    "identity_inference_supported": False,
                }
                signal["visible_supply_zone"] = True
                signal["supply_zone_score"] = round(supply_score, 1)
                signal["supply_zone_approach_only"] = bool(approaching_supply and not in_supply)
                tags = set(signal.get("tags", []))
                tags.add("supply_reaction_watch")

                if in_supply and signal.get("direction") == "bearish":
                    entry = round(float(best_supply_zone["proximal"]) * 0.998, 2)
                    stop = round(float(best_supply_zone["distal"]) * 1.005, 2)
                    apply_structural_trade_plan(
                        "bearish",
                        entry,
                        stop,
                        [signal.get("support")],
                        {
                            "setup_stage": "SUPPLY_REJECTION_WATCH",
                            "signal_stage": "SUPPLY_REJECTION_WATCH",
                            "action": "WAIT_FOR_LIVE_CONFIRMATION",
                            "recommended_action": "WAIT_FOR_LIVE_CONFIRMATION",
                            "attention_only": True,
                            "allow_buy_call": False,
                            "allow_trade_call": False,
                            "requires_live_confirmation": True,
                            "requires_full_tick": True,
                            "live_min_confirmations": 3,
                        },
                    )
                elif in_supply and signal.get("direction") == "bullish":
                    # Do not buy directly into supply; only arm a clean break
                    # above its distal edge with supply as invalidation.
                    signal["bullish_supply_conflict"] = True
                    signal["confidence"] = min(float(signal.get("confidence") or 50), 75.0)
                    entry = round(float(best_supply_zone["distal"]) * 1.002, 2)
                    stop = round(float(best_supply_zone["proximal"]) * 0.995, 2)
                    apply_structural_trade_plan(
                        "bullish",
                        entry,
                        stop,
                        [signal.get("resistance")],
                        {
                            "setup_stage": "SUPPLY_BREAKOUT_WATCH",
                            "signal_stage": "SUPPLY_BREAKOUT_WATCH",
                            "action": "WAIT_FOR_BREAKOUT_CONFIRMATION",
                            "recommended_action": "WAIT_FOR_BREAKOUT_CONFIRMATION",
                            "attention_only": True,
                            "allow_buy_call": False,
                            "allow_trade_call": False,
                            "requires_live_confirmation": True,
                            "requires_full_tick": True,
                            "live_min_confirmations": 3,
                        },
                    )
                    tags.add("supply_breakout_required")
                signal["tags"] = list(tags)
        except Exception as e:
            logger.warning("GTF zone detection failed in scanner for %s: %s", symbol, e)
        # --- End GTF Integration ---

        # A high-quality bullish exhaustion/reversal outside a surviving GTF
        # zone still needs a route to live confirmation. Build a conservative
        # reclaim plan from visible support; never emit an immediate BUY.
        if (
            signal.get("signal_stage") == "BULLISH_REVERSAL_CANDIDATE"
            and str(signal.get("reversal_bias") or "").lower() == "bullish"
            and safe_float(
                signal.get("bullish_reversal_score") or signal.get("reversal_risk_score"),
                0,
            ) >= 65
            and not signal.get("requires_live_confirmation")
        ):
            current = safe_float(signal.get("current_price"), 0)
            support = safe_float(signal.get("support") or signal.get("invalidation"), 0)
            reversal_score = safe_float(
                signal.get("bullish_reversal_score") or signal.get("reversal_risk_score"),
                65,
            )
            near_support = bool(current > 0 and support > 0 and support < current <= support * 1.08)
            if near_support:
                observed_vwap = safe_float(signal.get("rolling_vwap"), 0)
                reclaim_reference = (
                    observed_vwap
                    if current <= observed_vwap <= current * 1.025
                    else current * 1.005
                )
                entry = round(max(current * 1.003, reclaim_reference * 1.001), 2)
                stop = round(support * 0.99, 2)
                resistance = safe_float(signal.get("resistance"), 0)
                original_direction = str(signal.get("direction") or "bearish")
                apply_structural_trade_plan(
                    "bullish",
                    entry,
                    stop,
                    [resistance],
                    {
                        "original_direction": original_direction,
                        "direction": "bullish",
                        "setup_label": "Bullish reversal reclaim watch",
                        "confidence": min(75.0, max(55.0, reversal_score)),
                        "alignment_score": min(75.0, max(55.0, reversal_score)),
                        "setup_stage": "RETEST_ENTRY",
                        "action": "WAIT_FOR_LIVE_CONFIRMATION",
                        "recommended_action": "WAIT_FOR_LIVE_CONFIRMATION",
                        "attention_only": True,
                        "allow_buy_call": False,
                        "allow_trade_call": False,
                        "requires_live_confirmation": True,
                        "requires_full_tick": True,
                        "live_min_confirmations": 3,
                        "countertrend_reversal_plan": True,
                        "alert_level": "watchlist",
                    },
                )
                reversal_tags = set(signal.get("tags", []))
                reversal_tags.update({"bullish_reversal_watch", "support_reclaim_required"})
                signal["tags"] = list(reversal_tags)

        if with_backtest and self.settings.news_api_key:
            signal["catalyst_summary"] = self._catalyst_summary(symbol)
        if quote:
            signal["current_price"] = round(safe_float(quote.get("price"), signal["current_price"]), 2)
            signal["change_pct"] = round(safe_float(quote.get("change_percent"), signal["change_pct"]), 2)
            signal["volume"] = safe_int(quote.get("volume"), signal["volume"])
        from app.services.book_strategy import apply_candle_gate, build_candle_context
        signal["candle_setup"] = build_candle_context(frame, intraday_frame, enhanced=with_backtest)
        return apply_candle_gate(signal) | {"backtest": backtest}

    def _catalyst_summary(self, symbol: str) -> Dict:
        try:
            news = self.market_intelligence.get_news_feed(symbol, limit=4)
        except Exception:
            logger.exception("catalyst news fetch failed symbol=%s", symbol)
            return {"sentiment": "neutral", "summary": "Catalyst scan unavailable.", "catalysts": []}

        positive_words = ("order", "win", "growth", "profit", "upgrade", "approval", "expansion", "deal", "launch")
        negative_words = ("probe", "loss", "downgrade", "penalty", "fraud", "weak", "fall", "regulation", "ban")
        catalyst_words = ("earnings", "order", "management", "sector", "regulation", "bulk", "deal", "stake", "result")
        score = 0
        catalysts: list[str] = []
        titles: list[str] = []
        for item in news.get("items", [])[:4]:
            title = str(item.get("title") or "")
            lower = title.lower()
            titles.append(title)
            score += sum(1 for word in positive_words if word in lower)
            score -= sum(1 for word in negative_words if word in lower)
            catalysts.extend(word for word in catalyst_words if word in lower)

        sentiment = "positive" if score > 0 else "negative" if score < 0 else "neutral"
        return {
            "sentiment": sentiment,
            "summary": titles[0] if titles else "No fresh catalyst headline found.",
            "catalysts": list(dict.fromkeys(catalysts))[:5],
            "headlines": titles[:3],
        }

    def _build_market_breadth(self, results: list[Dict], benchmark_frame: pd.DataFrame) -> Dict:
        advancing = len([item for item in results if item["change_pct"] > 0])
        declining = len([item for item in results if item["change_pct"] < 0])
        total = len(results) or 1
        bullish = len([item for item in results if item["direction"] == "bullish"])
        bearish = len([item for item in results if item["direction"] == "bearish"])
        benchmark_change = 0.0
        if not benchmark_frame.empty and len(benchmark_frame) > 1:
            benchmark_change = ((benchmark_frame["Close"].iloc[-1] / benchmark_frame["Close"].iloc[-2]) - 1) * 100
        return {
            "advancing": advancing,
            "declining": declining,
            "advance_decline_ratio": round(advancing / max(declining, 1), 2),
            "bullish_setups": bullish,
            "bearish_setups": bearish,
            "bullish_ratio": round(bullish / total, 3),
            "benchmark_change_pct": round(float(benchmark_change), 2),
        }

    def _unique_signals(self, items: Iterable[Dict]) -> list[Dict]:
        unique: list[Dict] = []
        seen: set[str] = set()
        for item in items:
            symbol = item.get("symbol")
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            unique.append(item)
        return unique

    def _fill_signal_bucket(self, primary: list[Dict], fallback: list[Dict], limit: int) -> list[Dict]:
        items: list[Dict] = []
        seen: set[str] = set()
        for pool in (primary, fallback):
            for item in pool:
                symbol = item.get("symbol")
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                items.append(item)
                if len(items) >= limit:
                    return items
        return items

    def _fill_mover_bucket(self, primary: list[Dict], fallback: list[Dict], limit: int) -> list[Dict]:
        items: list[Dict] = []
        seen: set[str] = set()
        for pool in (primary, fallback):
            for item in pool:
                symbol = item.get("symbol")
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                items.append(item)
                if len(items) >= limit:
                    return items
        return items

    def _market_mood(self, breadth: Dict, opportunities: list[Dict], bearish_risks: list[Dict]) -> str:
        if not breadth:
            return "waiting_for_scan"
        bullish_ratio = breadth.get("bullish_ratio", 0)
        benchmark = breadth.get("benchmark_change_pct", 0)
        if bullish_ratio >= 0.58 and benchmark >= 0 and len(opportunities) >= max(3, len(bearish_risks)):
            return "risk_on"
        if bullish_ratio <= 0.42 or len(bearish_risks) > len(opportunities):
            return "defensive"
        return "mixed"

    def _missed_movers(self, discovery: Dict, results: list[Dict], surfaced: set[str]) -> list[Dict]:
        result_symbols = {item.get("symbol") for item in results}
        result_by_symbol = {item.get("symbol"): item for item in results}
        prior_pre_breakout = {
            item.get("symbol")
            for item in (self.last_successful_scan or {}).get("pre_breakout_setups", [])
            if item.get("symbol")
        }
        setup_memory = self._persistent_watch_map()
        movers: list[Dict] = []
        for meta in discovery.get("symbol_meta", {}).values():
            symbol = meta.get("symbol")
            change_pct = meta.get("change_pct") or 0
            volume = meta.get("volume")
            try:
                change = float(change_pct or 0)
            except (TypeError, ValueError):
                change = 0.0
            if abs(change) < 5:
                continue
            if symbol in surfaced:
                continue
            tags = meta.get("tags", [])
            result = result_by_symbol.get(symbol, {})
            was_pre_breakout = symbol in prior_pre_breakout
            if symbol in setup_memory:
                remembered_status = setup_memory[symbol].get("scanner_call_status") or setup_memory[symbol].get("status")
                missed_reason = f"remembered_setup_reaccelerated_after_{remembered_status or 'prior_call'}"
            elif was_pre_breakout:
                missed_reason = "was_in_prior_pre_breakout_scan"
            elif symbol not in result_symbols:
                missed_reason = "not_in_scanned_history_or_failed_data"
            elif result.get("pre_breakout_score", 0) and result.get("pre_breakout_score", 0) < 5:
                missed_reason = "setup_score_too_low_before_move"
            elif "volume_shockers" not in tags and "most_active" not in tags:
                missed_reason = "volume_came_later_or_setup_was_weak_earlier"
            else:
                missed_reason = "late_momentum_without_full_entry_confirmation"

            abs_change = abs(change)
            is_profit_booking_risk = abs_change >= 8.0
            is_chase_risk = abs_change >= 5.0

            if is_profit_booking_risk:
                entry_label = "PROFIT_BOOKING_RISK"
                display_action = "WAIT_FOR_PULLBACK"
                chase_warnings = [
                    f"Stock already moved {abs_change:.1f}% today. "
                    "High chance of profit booking tomorrow.",
                    "Do not suggest fresh entry unless new confirmation forms.",
                ]
                next_day_risk = [
                    "High profit booking risk next session.",
                    "Suggest trailing stop if already holding.",
                ]
            elif is_chase_risk:
                entry_label = "CHASE_RISK"
                display_action = "WAIT_FOR_PULLBACK"
                chase_warnings = [
                    f"Stock moved {abs_change:.1f}% intraday. "
                    "Avoid late entry unless it holds VWAP/support."
                ]
                next_day_risk = []
            else:
                entry_label = "EARLY_ENTRY"
                display_action = "WATCH"
                chase_warnings = []
                next_day_risk = []

            ui_guidance = [
                "⚠️ Attention only — do not chase this move.",
                "Wait for a pullback to VWAP or support before entry.",
            ]

            movers.append(
                {
                    "symbol": symbol,
                    "company_name": meta.get("short_name") or symbol,
                    "price": meta.get("price"),
                    "change_pct": round(change, 2),
                    "volume": volume,
                    "move_bucket": "10%+" if abs_change >= 10 else "8%+" if abs_change >= 8 else "5%+",
                    "missed_reason": missed_reason,
                    "was_in_prior_pre_breakout_scan": was_pre_breakout,
                    "was_pre_breakout_setup_yesterday": was_pre_breakout,
                    "pre_breakout_score_before_move": result.get("pre_breakout_score"),
                    "missed_move_analysis": (
                        "This was already visible in the previous pre-breakout scan."
                        if was_pre_breakout
                        else "Missed because the setup was not strong enough, data was unavailable, or volume arrived suddenly."
                    ),
                    "tags": tags,
                    # Chase-risk fields
                    "entry_label": entry_label,
                    "display_action": display_action,
                    "is_chase_risk": is_chase_risk,
                    "is_profit_booking_risk": is_profit_booking_risk,
                    "chase_warnings": chase_warnings,
                    "next_day_risk": next_day_risk,
                    "ui_guidance": ui_guidance,
                }
            )
        return sorted(movers, key=lambda item: abs(item.get("change_pct") or 0), reverse=True)[:15]

    def _shape_scan_payload(self, discovery: Dict, results: list[Dict], benchmark_frame: pd.DataFrame) -> Dict:
        symbol_meta = discovery.get("symbol_meta", {})
        for item in results:
            meta = symbol_meta.get(item.get("symbol"), {})
            if meta:
                item.setdefault("company_name", meta.get("short_name") or item.get("symbol"))

        def is_confirmed_top_call(item: Dict) -> bool:
            price = float(item.get("current_price") or 0)
            stop = float(item.get("trailing_stop") or item.get("stop_loss") or item.get("invalidation") or 0)
            stop_nearby = bool(price and stop and abs((price - stop) / price) <= 0.085)
            confirmed = (
                "breakout" in item.get("tags", [])
                or "breakdown" in item.get("tags", [])
                or item.get("setup_stage") == "VALID_BREAKOUT"
                or item.get("relative_volume", 0) >= 1.6
                or item.get("intraday_volume_ratio", 0) >= 1.4
                or (item.get("confidence", 0) >= 70 and item.get("move_quality", 0) >= 55)
            )
            return confirmed and stop_nearby

        def is_historically_validated(item: Dict) -> bool:
            evidence = item.get("historical_evidence") or {}
            sample_count = int(evidence.get("sample_count") or item.get("historical_context", {}).get("signal_count", 0) or 0)
            win_rate = float(evidence.get("win_rate") or item.get("historical_context", {}).get("win_rate", 0) or 0)
            return sample_count >= 30 and win_rate >= 0.60

        def is_live_pattern_ready(item: Dict) -> bool:
            return bool(
                item.get("live_pattern_ready")
                and item.get("direction") == "bullish"
                and not item.get("pattern_late_entry_risk")
                and (item.get("risk_reward") or 0) >= 1.3
                and item.get("allow_buy_call", True)
            )

        top_ranked = self._unique_signals(sorted(
            [
                item for item in results
                if item["direction"] != "neutral" 
                and item.get("allow_buy_call", True)
                and not item.get("attention_only", False)
                and not item.get("overextended_fresh_entry", False)
                and item.get("setup_stage") not in {"CHASE_RISK", "AVOID_LATE_ENTRY", "PROFIT_BOOKING_RISK"}
                and (item.get("risk_reward") or 0) >= 1.0
            ],
            key=lambda item: (
                is_live_pattern_ready(item),
                item["alert_level"] == "high_priority",
                item.get("chart_pattern_score", 0),
                item["move_quality"],
                item["confidence"],
                item["relative_volume"],
            ),
            reverse=True,
        ))
        top_opportunities = top_ranked[:500]
        surfaced = {item["symbol"] for item in top_opportunities}

        retest_entry = self._unique_signals(sorted(
            [
                item for item in results
                if item.get("signal_stage") == "RETEST_ENTRY"
                or item.get("signal_stage") in {
                    "BULLISH_REVERSAL_CANDIDATE",
                    "SUPPLY_REJECTION_WATCH",
                    "SUPPLY_BREAKOUT_WATCH",
                }
                or item.get("continuation_type") == "pullback"
                or "support_bounce" in item.get("pattern_labels", [])
            ],
            key=lambda item: (item.get("confidence", 0), item.get("risk_reward") or 0, item.get("benchmark_relative_strength", 0)),
            reverse=True,
        ))[:500]

        pre_breakout_setups = self._unique_signals(sorted(
            [
                item for item in results
                if item.get("is_pre_breakout")
                and not item.get("chase_risk", False)
            ],
            key=lambda item: (
                item.get("pre_breakout_confidence", item.get("confidence", 0)),
                item.get("pre_breakout_score", 0),
                item.get("benchmark_relative_strength", 0),
            ),
            reverse=True,
        ))[:500]

        alert_above_setups = [
            item for item in pre_breakout_setups
            if item.get("pre_breakout_action") == "ALERT_ABOVE_LEVEL"
            or item.get("recommended_action") == "ALERT_ABOVE_LEVEL"
        ]

        momentum_continuation = self._unique_signals(sorted(
            [
                item for item in results
                if item.get("is_momentum_continuation")
                and item.get("attention_only", False)
                and item.get("allow_buy_call", False) is False
            ],
            key=lambda item: (
                item.get("continuation_action") == "REENTRY_BUY",
                item.get("continuation_risk_reward", 0),
                item.get("confidence", 0),
                item.get("benchmark_relative_strength", 0),
            ),
            reverse=True,
        ))[:500]

        # Candidates bucket — medium conviction, setup forming
        candidates_bucket = self._unique_signals(sorted(
            [
                item for item in results
                if item["direction"] != "neutral"
                and item["alert_level"] in {"watchlist", "low_priority"}
                and item["symbol"] not in surfaced
                and item["confidence"] >= 55
                and item.get("allow_buy_call", True)
                and not item.get("attention_only", False)
                and not item.get("overextended_fresh_entry", False)
                and not is_historically_validated(item)
            ],
            key=lambda item: (item["move_quality"], item["confidence"]),
            reverse=True,
        ))[:500]

        # Avoid / Risky bucket — weak setups, high risk
        avoid_risky_bucket = self._unique_signals(sorted(
            [
                item for item in results
                if item["alert_level"] == "avoid" or item["risk_level"] == "high"
            ],
            key=lambda item: item["confidence"],
            reverse=True,
        ))[:500]

        avoid_late_entry = self._unique_signals(sorted(
            [
                item for item in results
                if (
                    item.get("overextended_fresh_entry")
                    or item.get("setup_stage") in {"CHASE_RISK", "AVOID_LATE_ENTRY", "PROFIT_BOOKING_RISK"}
                    or "avoid_late_entry" in item.get("tags", [])
                )
                and not item.get("is_momentum_continuation")
            ],
            key=lambda item: (
                abs(float(item.get("return_20d") or 0)),
                abs(float(item.get("return_5d") or 0)),
                abs(float(item.get("change_pct") or 0)),
            ),
            reverse=True,
        ))[:500]

        volume_ranked = self._unique_signals(sorted(
            [
                item for item in results
                if (item["relative_volume"] >= 1.6 or item["intraday_volume_ratio"] >= 1.4)
                # Allow high-volume pre-breakout stocks (3x+) — they deserve to be seen
                and (not item.get("is_pre_breakout") or item["relative_volume"] >= 3.0)
            ],
            key=lambda item: (item["relative_volume"], item["intraday_volume_ratio"], item["move_quality"]),
            reverse=True,
        ))

        # NEW: Catch early volume buildups (stealth accumulation, vol building)
        vol_buildup_stocks = self._unique_signals(sorted(
            [
                item for item in results
                if item["relative_volume"] >= 1.2
                and (not item.get("is_pre_breakout") or item["relative_volume"] >= 2.0)
                and item["symbol"] not in {v["symbol"] for v in volume_ranked}
                and (
                    item.get("score_breakdown", {}).get("vol_building")
                    or item.get("score_breakdown", {}).get("stealth_accumulation")
                    or item.get("score_breakdown", {}).get("volume_streak", 0) >= 3
                    or item.get("score_breakdown", {}).get("vol_price_divergence")
                )
            ],
            key=lambda item: (
                item.get("score_breakdown", {}).get("volume_streak", 0),
                item["relative_volume"],
                item["move_quality"],
            ),
            reverse=True,
        ))
        # Merge: big volume first, then early buildups
        all_volume = volume_ranked + vol_buildup_stocks

        unusual_volume = self._fill_signal_bucket(
            [item for item in all_volume if item["symbol"] not in surfaced],
            all_volume,
            500,
        )

        breakouts = self._unique_signals([
            item for item in results
            if ("breakout" in item["tags"] or "breakdown" in item["tags"]
            or item.get("setup_stage") == "VALID_BREAKOUT"
            or item.get("live_pattern_ready"))
            and not item.get("is_pre_breakout")
        ])
        breakout_candidates = self._fill_signal_bucket(
            [
                item for item in sorted(
                    breakouts,
                    key=lambda item: (item["move_quality"], item["confidence"], item["relative_volume"]),
                    reverse=True,
                )
                if item["symbol"] not in surfaced
            ],
            sorted(
                breakouts,
                key=lambda item: (item["move_quality"], item["confidence"], item["relative_volume"]),
                reverse=True,
            ),
            500,
        )

        bearish_ranked = self._unique_signals(sorted(
            [item for item in results if item["direction"] == "bearish" and not item.get("is_pre_breakout")],
            key=lambda item: (item["move_quality"], item["confidence"]),
            reverse=True,
        ))
        bearish_risks = bearish_ranked[:500]
        surfaced.update(item["symbol"] for item in breakout_candidates)
        surfaced.update(item["symbol"] for item in bearish_risks)
        surfaced.update(item["symbol"] for item in candidates_bucket)
        surfaced.update(item["symbol"] for item in pre_breakout_setups)
        surfaced.update(item["symbol"] for item in momentum_continuation)

        trap_signals = self._unique_signals(sorted(
            [
                item for item in results
                if item.get("trap_risk") in {"high", "medium"} or item.get("institutional_selling")
            ],
            key=lambda item: item.get("confidence", 0),
            reverse=True,
        ))[:500]
        surfaced.update(item["symbol"] for item in trap_signals)


        mover_ranked = [
            {
                "symbol": meta["symbol"],
                "company_name": meta.get("short_name") or meta["symbol"],
                "price": meta.get("price"),
                "change_pct": meta.get("change_pct"),
                "volume": meta.get("volume"),
                "tags": meta.get("tags", []),
                "attention_only": True,
                "recommended_action": "WAIT_FOR_PULLBACK",
                "trade_labels": ["CHASE_RISK", "WAIT_FOR_PULLBACK"],
                "chase_risk_reason": "Fast mover discovery only; do not chase without fresh setup confirmation.",
            }
            for meta in sorted(
                discovery.get("symbol_meta", {}).values(),
                key=lambda item: abs(float(item.get("change_pct") or 0)),
                reverse=True,
            )
        ]
        top_movers = self._fill_mover_bucket(
            [item for item in mover_ranked if item["symbol"] not in surfaced],
            mover_ranked,
            8,
        )
        fast_movers_missed_moves = self._missed_movers(discovery, results, surfaced)
        missed_moves_analysis = [item for item in fast_movers_missed_moves if abs(float(item.get("change_pct") or 0)) >= 8]
        for item in fast_movers_missed_moves:
            change = abs(float(item.get("change_pct") or 0))
            item["attention_only"] = True
            item["recommended_action"] = "WAIT_FOR_PULLBACK"
            item["display_action"] = "WAIT_FOR_PULLBACK"
            item["entry_label"] = "PROFIT_BOOKING_RISK" if change >= 8 else "CHASE_RISK"
            item["trade_labels"] = list(
                dict.fromkeys(
                    [
                        "CHASE_RISK",
                        "PROFIT_BOOKING_RISK" if change >= 8 else "WAIT_FOR_PULLBACK",
                        "AVOID_LATE_ENTRY",
                    ]
                )
            )
            item["chase_risk_reason"] = (
                "High chance of profit booking tomorrow. Suggest trailing stop if already holding."
                if change >= 8
                else "Stock already moved strongly; avoid late entry unless it holds VWAP/support."
            )
            item["ui_guidance"] = [
                "Attention only - do not chase this move.",
                "Wait for a pullback to VWAP or support before entry.",
            ]
        breadth = self._build_market_breadth(results, benchmark_frame)
        market_mood = self._market_mood(breadth, top_opportunities, bearish_risks)

        # ── Nifty Context (Financial Expert Level Index Analysis) ─────────────
        nifty_context = self._get_nifty_context()

        # ── Extract Zero-Latency Live Triggers for the Entire Universe ────────
        live_candidate_symbols = [
            str(symbol).upper()
            for symbol in discovery.get("live_candidate_symbols", [])
            if symbol
        ][: self.settings.intraday_symbol_limit]
        live_candidate_set = set(live_candidate_symbols)
        persistent = self._persistent_watch_map()
        result_by_symbol = {
            str(item.get("symbol") or "").upper(): item
            for item in results
            if item.get("symbol")
        }
        live_candidate_pool = []
        for rank, symbol in enumerate(live_candidate_symbols, start=1):
            item = result_by_symbol.get(symbol, {})
            reason = item.get("live_pool_reason") or self._live_pool_reason(item | {"symbol": symbol}, persistent)
            item["live_pool_reason"] = reason
            item["live_pool_rank"] = rank
            footprint = item.get("large_money_footprint") or {}
            live_candidate_pool.append(
                {
                    "symbol": symbol,
                    "rank": rank,
                    "reason": reason,
                    "candle_setup": item.get("candle_setup"),
                    "direction": item.get("direction", "neutral"),
                    "confidence": item.get("confidence"),
                    "large_money_footprint_score": (
                        footprint.get("score")
                        if isinstance(footprint, dict)
                        else item.get("large_money_footprint_score")
                    ),
                }
            )

        all_entry_levels = []
        for item in results:
            symbol = str(item.get("symbol") or "").upper()
            if symbol not in live_candidate_set:
                continue
            direction = str(item.get("direction") or "neutral").lower()
            if direction not in {"bullish", "bearish"}:
                continue
            action = str(
                item.get("effectiveAction")
                or item.get("action")
                or item.get("recommended_action")
                or "WATCH"
            ).upper()
            stage = str(item.get("signal_stage") or item.get("setup_stage") or "").upper()
            armable_confirmation = bool(
                item.get("requires_live_confirmation")
                or item.get("is_pre_breakout")
                or item.get("is_momentum_continuation")
                or stage in {
                    "RETEST_ENTRY", "BREAKDOWN_WATCH", "PATTERN_FORMING",
                    "ALERT_ABOVE_LEVEL", "LIVE_PATTERN_READY", "RE_ENTRY_SETUP",
                }
            )
            blocked_actions = {
                "AVOID", "EXIT", "NO_TRADE", "WAIT_FOR_PULLBACK",
                "AVOID_LATE_ENTRY", "AVOID_CHASE", "PROFIT_BOOKING_RISK",
                "WAIT_FOR_BETTER_ENTRY",
            }
            if (
                action in blocked_actions
                or item.get("entry_plan_blocked")
                or item.get("blockedBuyReason")
                or item.get("overextended_fresh_entry")
                or item.get("chase_risk")
                or (item.get("attention_only") and not armable_confirmation)
                or (direction == "bullish" and item.get("allow_buy_call") is False and not armable_confirmation)
            ):
                continue
            entry = float(item.get("safe_entry_price") or item.get("entry_trigger") or item.get("breakoutTrigger") or 0)
            if entry > 0:
                all_entry_levels.append({
                    "symbol": symbol,
                    "candle_setup": item.get("candle_setup"),
                    "entry_trigger": entry,
                    "safe_entry_price": item.get("safe_entry_price"),
                    "stop_loss": item.get("invalidation_level") or item.get("stop_loss"),
                    "target_1": item.get("target_1") or item.get("new_target"),
                    "direction": direction,
                    "setup_type": item.get("setup_type") or item.get("setup_label", ""),
                    "confidence": item.get("confidence") or 0,
                    "risk_reward": item.get("risk_reward") or 0,
                    "riskPct": item.get("riskPct"),
                    "riskPerShare": item.get("riskPerShare"),
                    "maxPositionPctAt1PctAccountRisk": item.get("maxPositionPctAt1PctAccountRisk"),
                    "entry_plan_status": item.get("entry_plan_status"),
                    "entry_plan_blocked": bool(item.get("entry_plan_blocked")),
                    "entry_plan_blocked_reason": item.get("entry_plan_blocked_reason"),
                    "enforce_structural_risk_cap": bool(item.get("enforce_structural_risk_cap")),
                    "max_structural_risk_pct": item.get("max_structural_risk_pct"),
                    "targetMethod": item.get("targetMethod"),
                    "current_price": item.get("current_price"),
                    "pre_breakout_timeframe": item.get("pre_breakout_timeframe") or item.get("time_horizon"),
                    "signal_stage": stage,
                    "action": action,
                    "attention_only": bool(item.get("attention_only")),
                    "allow_buy_call": item.get("allow_buy_call"),
                    "requires_live_confirmation": armable_confirmation,
                    "requires_full_tick": bool(item.get("requires_full_tick")),
                    "live_min_confirmations": item.get("live_min_confirmations"),
                    "countertrend_reversal_plan": bool(item.get("countertrend_reversal_plan")),
                    "counterRegime": bool(item.get("counterRegime")),
                    "reversal_bias": item.get("reversal_bias"),
                    "bullish_reversal_score": item.get("bullish_reversal_score"),
                    "bearish_reversal_score": item.get("bearish_reversal_score"),
                    "reversal_risk_score": item.get("reversal_risk_score"),
                    "marketMood": item.get("marketMood") or item.get("market_mood"),
                    "large_money_footprint": item.get("large_money_footprint"),
                    "large_money_footprint_score": (
                        (item.get("large_money_footprint") or {}).get("score")
                        if isinstance(item.get("large_money_footprint"), dict)
                        else None
                    ) or item.get("large_money_footprint_score"),
                    "blockedBuyReason": item.get("blockedBuyReason"),
                    "overextended_fresh_entry": bool(item.get("overextended_fresh_entry")),
                    "chase_risk": bool(item.get("chase_risk")),
                })


        # Enrich each signal with Nifty alignment score
        for item in results:
            try:
                alignment = float(stock_nifty_alignment_score(item, nifty_context))
                item["nifty_alignment_score"] = round(alignment, 2)
                if alignment >= 0.75:
                    item["nifty_alignment"] = "aligned"
                    item["nifty_alignment_warnings"] = []
                elif alignment <= 0.30:
                    item["nifty_alignment"] = "counter_regime"
                    item["nifty_alignment_warnings"] = [
                        "Stock direction conflicts with aggregate Nifty context; require stricter live confirmation."
                    ]
                else:
                    item["nifty_alignment"] = "mixed_or_unknown"
                    item["nifty_alignment_warnings"] = [
                        "Nifty context is mixed or unavailable; it is not a directional confirmation."
                    ]
            except Exception:
                pass

        # ── Breakout Radar ("1-2 din mein fatne wale") ───────────────────────
        # Use all scanned signals (not just top_opportunities) to find pre-breakout setups
        breakout_radar_picks = build_breakout_radar(
            all_signals=results,
            nifty_context=nifty_context,
            max_results=10,
        )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "universe_size": int(discovery.get("scan_attempted_count") or len(results)),
            "valid_signal_count": len(results),
            "market_breadth": breadth,
            "market_discovery": {
                "source_mode": discovery.get("source_mode"),
                "note": discovery.get("note"),
                "bucket_counts": discovery.get("bucket_counts", {}),
                "total_discovered": len(discovery.get("symbols", [])),
                "total_scanned": int(discovery.get("scan_attempted_count") or len(results)),
                "valid_signal_count": len(results),
            },
            "macro_context": self.market_intelligence.get_macro_snapshot(),
            "global_context": self.market_intelligence.get_global_scenario(),
            "nifty_context": nifty_context,
            "breakout_radar": breakout_radar_picks,
            "top_opportunities": top_opportunities,
            "top_validated_calls": top_opportunities,
            "unusual_volume": unusual_volume,
            "breakout_candidates": breakout_candidates,
            "bearish_risks": bearish_risks,
            "top_movers": top_movers,
            "fast_movers_missed_moves": fast_movers_missed_moves,
            "pre_breakout_setups": pre_breakout_setups,
            "pattern_forming_setups": pre_breakout_setups,
            "alert_above_setups": alert_above_setups,
            "retest_entry": retest_entry,
            "momentum_continuation": momentum_continuation,
            "re_entry_setups": momentum_continuation,
            "missed_moves_analysis": missed_moves_analysis,
            "avoid_late_entry": avoid_late_entry,
            "candidates": candidates_bucket,
            "avoid_risky": avoid_risky_bucket,
            "trap_signals": trap_signals,
            "summary": {
                "high_priority": len([item for item in results if item["alert_level"] == "high_priority"]),
                "watchlist": len([item for item in results if item["alert_level"] == "watchlist"]),
                "avoid": len([item for item in results if item["alert_level"] == "avoid"]),
                "opportunities_count": len(top_opportunities),
                "top_validated_count": len(top_opportunities),
                "breakout_count": len(breakout_candidates),
                "bearish_risk_count": len(bearish_risks),
                "unusual_volume_count": len(unusual_volume),
                "market_mood": market_mood,
                "scanner_leader": top_opportunities[0]["symbol"] if top_opportunities else None,
                "total_scanned_universe": int(discovery.get("scan_attempted_count") or len(results)),
                "valid_signal_count": len(results),
                "fast_movers_count": len(fast_movers_missed_moves),
                "pre_breakout_count": len(pre_breakout_setups),
                "pattern_forming_count": len(pre_breakout_setups),
                "alert_above_count": len(alert_above_setups),
                "retest_entry_count": len(retest_entry),
                "reversal_reclaim_watch_count": len([
                    item for item in retest_entry
                    if item.get("signal_stage") == "BULLISH_REVERSAL_CANDIDATE"
                ]),
                "momentum_continuation_count": len(momentum_continuation),
                "re_entry_count": len(momentum_continuation),
                "missed_moves_count": len(missed_moves_analysis),
                "avoid_late_entry_count": len(avoid_late_entry),
                "candidates_count": len(candidates_bucket),
                "avoid_count": len(avoid_risky_bucket),
                "trap_signals_count": len(trap_signals),
                "breakout_radar_count": len(breakout_radar_picks),
                "nifty_bias": nifty_context.get("nifty_bias", "neutral"),
                "nifty_regime": nifty_context.get("nifty_regime", "unknown"),
                "market_score": nifty_context.get("market_score", 50),
                "live_candidate_count": len(live_candidate_symbols),
            },
            "live_candidate_symbols": live_candidate_symbols,
            "live_candidate_pool": live_candidate_pool,
            "all_entry_levels": all_entry_levels,
            # Keep contextual candle watches visible even when static BUY
            # buckets are empty. The existing live shortlist bounds this list.
            "candle_watch_setups": [
                item for item in results
                if str(item.get("symbol") or "").upper() in live_candidate_set
                and isinstance(item.get("candle_setup"), dict)
                and item["candle_setup"].get("available")
                and item["candle_setup"].get("pattern")
            ],
        }

    def _refresh_scan_market(self, force_refresh: bool = False) -> Dict:
        refresh_started = time.perf_counter()
        timing_stats = {}
        logger.info("market overview refresh started force_refresh=%s", force_refresh)

        # Refresh official EOD delivery data in parallel.  Cached rows can be
        # consumed immediately; exchange latency never blocks the live scan.
        delivery_refresh_started = self.delivery_archive.refresh_async()

        t0 = time.perf_counter()
        discovery = self.universe.discover_market(force_refresh=force_refresh)
        symbols = self._scan_symbols(discovery)
        discovery["scan_attempted_count"] = len(symbols)
        timing_stats["discovery_ms"] = int((time.perf_counter() - t0) * 1000)
        
        benchmark_symbol = self.settings.benchmark_symbol
        t1 = time.perf_counter()
        try:
            benchmark_frame = self.data.fetch_history(benchmark_symbol, period=self.settings.scan_history_period)
        except Exception:
            logger.exception("benchmark fetch failed symbol=%s", benchmark_symbol)
            benchmark_frame = pd.DataFrame()
        try:
            daily_frames = self.data.fetch_batch_history(
                symbols,
                period=self.settings.scan_history_period,
                chunk_size=self.settings.yahoo_batch_chunk_size,
            )
        except Exception:
            logger.exception("daily batch fetch failed")
            daily_frames = {}
        daily_frames = self.delivery_archive.enrich_frames(daily_frames)
        timing_stats["data_fetch_ms"] = int((time.perf_counter() - t1) * 1000)
        timing_stats["delivery_background_refresh_started"] = delivery_refresh_started

        preliminary: list[Dict] = []
        pre_filter_passed = 0
        pre_filter_dropped = 0
        filter_lock = threading.Lock()
        persistent_symbols = set(self._persistent_watch_map())
        
        t2 = time.perf_counter()

        def evaluate_preliminary(symbol: str) -> Dict | None:
            nonlocal pre_filter_passed, pre_filter_dropped
            frame = daily_frames.get(symbol)
            if frame is None or frame.empty or len(frame) < self.settings.min_history_bars:
                return None
                
            quote = discovery.get("symbol_meta", {}).get(symbol)
            
            # FAST PRE-FILTER
            if self.settings.enable_prefilter:
                try:
                    last_close = float(quote.get("price") if quote and quote.get("price") else frame['Close'].iloc[-1])
                    # Keep this pre-filter structural only.  Intraday partial
                    # volume, quiet five-day bases and >6% moves used to be
                    # dropped here, exactly where reversal/re-entry candidates
                    # disappeared.  Directional quality belongs in scoring.
                    if not (self.settings.min_price <= last_close <= 100000):
                        with filter_lock:
                            pre_filter_dropped += 1
                        return None

                    # Use completed-session liquidity, never today's partial
                    # cumulative volume. Remembered setups bypass this gate so
                    # a temporarily quiet retest remains observable.
                    completed_volume = pd.to_numeric(frame["Volume"], errors="coerce").iloc[:-1].tail(20)
                    typical_volume = float(completed_volume.median()) if not completed_volume.empty else 0.0
                    min_liquidity = (
                        self.settings.bse_min_volume
                        if str(symbol).upper().endswith(".BO")
                        else self.settings.min_volume
                    )
                    if typical_volume < min_liquidity and symbol.upper() not in persistent_symbols:
                        with filter_lock:
                            pre_filter_dropped += 1
                        return None
                except Exception:
                    pass # Fall through if parse fails

            with filter_lock:
                pre_filter_passed += 1

            try:
                signal = self._evaluate_symbol(
                    symbol,
                    frame,
                    benchmark_frame,
                    quote,
                    None,
                    with_backtest=False,
                )
                signal["discovered_by"] = discovery.get("symbol_meta", {}).get(symbol, {}).get("tags", [])
                return signal
            except Exception:
                logger.exception("skipped symbol=%s reason=evaluation_failed", symbol)
                return None

        with ThreadPoolExecutor(max_workers=max(1, self.settings.scanner_max_workers)) as executor:
            futures = [executor.submit(evaluate_preliminary, symbol) for symbol in symbols]
            for future in as_completed(futures):
                signal = future.result()
                if signal is not None:
                    preliminary.append(signal)
                    
        timing_stats["pre_filter_passed"] = pre_filter_passed
        timing_stats["pre_filter_dropped"] = pre_filter_dropped
        timing_stats["preliminary_ml_ms"] = int((time.perf_counter() - t2) * 1000)

        if not preliminary:
            logger.warning(
                "market overview refresh completed with no valid symbols duration_ms=%s",
                round((time.perf_counter() - refresh_started) * 1000),
            )
            raise RuntimeError("No valid symbols were scanned.")

        top_symbols = self._top_symbols(preliminary, self.settings.intraday_symbol_limit)
        discovery["live_candidate_symbols"] = top_symbols
        live_quotes: dict[str, Dict] = {}
        if top_symbols:
            def fetch_live_quote(symbol: str) -> tuple[str, Dict] | None:
                try:
                    return symbol, self.data.fetch_live_snapshot(symbol)
                except Exception:
                    logger.warning("live quote fetch failed during scan symbol=%s", symbol, exc_info=True)
                    return None

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = []
                for symbol in top_symbols:
                    futures.append(executor.submit(fetch_live_quote, symbol))
                    time.sleep(0.05)
                for future in as_completed(futures):
                    result = future.result()
                    if result is not None:
                        symbol, quote = result
                        live_quotes[symbol] = quote
        try:
            intraday_frames = self.data.fetch_batch_history(top_symbols, period="5d", interval="15m", chunk_size=10)
        except Exception:
            logger.exception("intraday batch fetch failed")
            intraday_frames = {}
        enhanced = {item["symbol"]: item for item in preliminary}

        def evaluate_enhanced(symbol: str) -> tuple[str, Dict] | None:
            frame = daily_frames.get(symbol)
            if frame is None or frame.empty:
                return None
            try:
                signal = self._evaluate_symbol(
                    symbol,
                    frame,
                    benchmark_frame,
                    live_quotes.get(symbol) or discovery.get("symbol_meta", {}).get(symbol),
                    intraday_frames.get(symbol),
                    with_backtest=True,
                ) | {"discovered_by": discovery.get("symbol_meta", {}).get(symbol, {}).get("tags", [])}
                return symbol, signal
            except Exception:
                logger.exception("skipped symbol=%s reason=enhanced_evaluation_failed", symbol)
                return None

        with ThreadPoolExecutor(max_workers=max(1, min(self.settings.scanner_max_workers, len(top_symbols) or 1))) as executor:
            futures = [executor.submit(evaluate_enhanced, symbol) for symbol in top_symbols]
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    symbol, signal = result
                    enhanced[symbol] = signal

        payload = self._shape_scan_payload(discovery, list(enhanced.values()), benchmark_frame)
        payload["data_health"] = {
            **payload.get("data_health", {}),
            "delivery_archive": self.delivery_archive.status(),
            "institution_identity_available": False,
            "large_money_label": "visible behavioural footprint",
        }
        try:
            payload = build_smart_scan_payload(
                payload,
                daily_frames,
                benchmark_frame,
                ml_models=None,  # model cache wired in future iteration
            )
        except Exception:
            logger.exception("smart_layers enrichment failed — returning base payload")
        try:
            self.tracker.sync_scan_payload(payload, discovery.get("symbol_meta", {}))
        except Exception:
            logger.exception("tracker sync failed during market overview refresh")
        try:
            payload["telegram_alerts"] = get_telegram_market_alerts().send_payload_alerts(payload)
        except Exception:
            logger.exception("telegram market alerts failed during market overview refresh")
            payload["telegram_alerts"] = {"sent": 0, "error": "telegram_alerts_failed"}
            
        timing_stats["total_scan_ms"] = int((time.perf_counter() - refresh_started) * 1000)
        payload["timing_stats"] = timing_stats
        
        saved = self._save_successful_scan(payload)
        logger.info(
            "market overview refresh completed symbols=%s valid=%s duration_ms=%s stats=%s",
            len(symbols),
            len(enhanced),
            timing_stats["total_scan_ms"],
            timing_stats
        )
        return saved

    def scan_market(self, force_refresh: bool = False) -> Dict:
        started = time.perf_counter()
        cache_key = "market_overview"
        if not force_refresh:
            cached = self.scan_cache.get(cache_key)
            if cached is not None:
                logger.info("market overview cache hit duration_ms=%s", round((time.perf_counter() - started) * 1000))
                return cached

            if self._last_success_is_usable():
                launched = self._start_background_refresh(force_refresh=True)
                logger.info(
                    "market overview cache stale returning_last_success background_refresh_started=%s duration_ms=%s",
                    launched,
                    round((time.perf_counter() - started) * 1000),
                )
                return self._copy_with_warning(
                    self.last_successful_scan or {},
                    "Returning last successful market scan while a fresh scan is running.",
                    cache_status="stale",
                )

            logger.info("market overview cache miss")
            launched = self._start_background_refresh(force_refresh=True)
            logger.info(
                "market overview cold start returning tracked fallback background_refresh_started=%s duration_ms=%s",
                launched,
                round((time.perf_counter() - started) * 1000),
            )
            return self._tracked_fallback_payload("Live scan is warming up; showing persisted scanner calls.")

        if not self.scan_refresh_lock.acquire(blocking=False):
            if self._last_success_is_usable():
                logger.info(
                    "market overview refresh already running returning_last_success duration_ms=%s",
                    round((time.perf_counter() - started) * 1000),
                )
                return self._copy_with_warning(
                    self.last_successful_scan or {},
                    "Refresh already running; returning last successful market scan.",
                    cache_status="refresh_in_progress",
                )
            logger.warning("market overview refresh already running and no cache available")
            return self._tracked_fallback_payload("Market scan is warming up. Showing persisted scanner calls.")

        try:
            payload = self._refresh_scan_market(force_refresh=force_refresh)
            logger.info("market overview response completed duration_ms=%s", round((time.perf_counter() - started) * 1000))
            return payload
        except Exception:
            logger.exception("market overview refresh failed")
            if self._last_success_is_usable():
                return self._copy_with_warning(
                    self.last_successful_scan or {},
                    "Refresh failed; returning last successful market scan.",
                    cache_status="refresh_failed_stale",
                )
            return self._empty_scan_payload("Market scan failed and no previous cache is available.")
        finally:
            self.scan_refresh_lock.release()

    def get_market_context(self, symbol: str, limit: int = 6) -> Dict:
        return {
            "symbol": symbol.upper(),
            "macro": self.market_intelligence.get_macro_snapshot(),
            "news": self.market_intelligence.get_news_feed(symbol, limit=max(1, min(limit, 10))),
            "global": self.market_intelligence.get_global_scenario(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_stock_detail(self, symbol: str, force_refresh: bool = False) -> Dict:
        clean = self.data.clean_symbol(symbol)
        cache_key = f"detail:{clean}"
        if force_refresh:
            self.data.quote_cache.clear()
        if not force_refresh:
            cached = self.detail_cache.get(cache_key)
            if cached is not None:
                return cached

        quote = self.data.fetch_live_snapshot(clean)
        history = self.data.fetch_history(clean, period=self.settings.detail_history_period)
        if history.empty or len(history) < self.settings.min_history_bars:
            raise ValueError(f"Insufficient history for {clean}")

        benchmark = self.data.fetch_history(self.settings.benchmark_symbol, period=self.settings.detail_history_period)
        intraday = self.data.fetch_history(clean, period="5d", interval="15m")
        signal = self._evaluate_symbol(clean, history, benchmark, quote, intraday, with_backtest=True)
        macro_context = self.market_intelligence.get_macro_snapshot()
        news_context = self.market_intelligence.get_news_feed(clean, limit=6)
        global_context = self.market_intelligence.get_global_scenario()
        company_context = self.company_research.get_company_snapshot(clean, technical={"price": signal["current_price"]})
        explanation = self.narrative.build(signal, macro_context, news_context, global_context, company_context)

        payload = {
            "symbol": clean,
            "quote": quote,
            "prediction": signal,
            "signals": {
                "reasons": signal["reasons"],
                "weaknesses": signal["weaknesses"],
                "risk_factors": signal["risk_factors"],
                "tags": signal["tags"],
                "score_breakdown": signal["score_breakdown"],
            },
            "backtest": signal.get("backtest", {}),
            "macro_context": macro_context,
            "news_context": news_context,
            "global_context": global_context,
            "company_context": company_context,
            "explanation": explanation,
            "chart": self.data.serialize_candles(history, limit=self.settings.chart_limit),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.detail_cache.set(cache_key, payload)
        return payload

    def get_stock_prediction(self, symbol: str, force_refresh: bool = False) -> Dict:
        return self.get_stock_detail(symbol, force_refresh=force_refresh)["prediction"]

    def get_stock_signals(self, symbol: str, force_refresh: bool = False) -> Dict:
        detail = self.get_stock_detail(symbol, force_refresh=force_refresh)
        return {
            "symbol": detail["symbol"],
            "signals": detail["signals"],
            "explanation": detail["explanation"],
            "generated_at": detail["generated_at"],
        }

    def get_historical_chart(self, symbol: str, period: str = "6mo") -> Dict:
        history = self.data.fetch_history(symbol, period=period)
        if history.empty:
            raise ValueError(f"No history found for {symbol.upper()}")
            
        zone_df = history.copy()
        zone_df.columns = [c.lower() for c in zone_df.columns]
        active_zones = self.zone_detector.detect_zones(zone_df, max_lookback=200)
            
        return {
            "symbol": self.data.clean_symbol(symbol),
            "period": period,
            "data": self.data.serialize_candles(history, limit=None),
            "count": len(history),
            "active_zones": active_zones,
        }

    def get_live_payload(self, symbol: str) -> Dict:
        detail = self.get_stock_detail(symbol)
        quote = detail["quote"]
        return {
            "symbol": quote["symbol"],
            "price": quote["price"],
            "change": quote["change"],
            "change_percent": quote["change_percent"],
            "volume": quote["volume"],
            "prediction": detail["prediction"],
            "timestamp": quote["timestamp"],
            "data_source": "near_live",
        }

    def get_backtest(self, symbol: str) -> Dict:
        history = self.data.fetch_history(symbol, period=self.settings.scan_history_period)
        benchmark = self.data.fetch_history(self.settings.benchmark_symbol, period=self.settings.scan_history_period)
        if history.empty:
            raise ValueError(f"No history found for {symbol.upper()}")
        return {
            "symbol": self.data.clean_symbol(symbol),
            "backtest": self.backtest.evaluate(symbol, history, benchmark),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_tracker_dashboard(self) -> Dict:
        return self.tracker.get_dashboard()

    def get_tracker_symbol(self, symbol: str) -> Dict:
        return self.tracker.get_symbol_history(symbol)

    def evaluate_tracked_setups(self) -> Dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **self.tracker.evaluate_open_setups(),
        }

    def create_manual_watch(
        self,
        symbol: str,
        *,
        notes: str | None = None,
        pinned: bool = False,
        timeframe_label: str | None = None,
    ) -> Dict:
        detail = self.get_stock_detail(symbol)
        return self.tracker.create_manual_watch(detail, notes=notes, pinned=pinned, timeframe_label=timeframe_label)

    def update_tracked_setup(self, setup_id: str, values: Dict) -> Dict | None:
        return self.tracker.update_setup(setup_id, values)

    def archive_tracked_setup(self, setup_id: str) -> Dict | None:
        return self.tracker.archive_setup(setup_id)

    def ignore_tracked_setup(self, setup_id: str) -> Dict | None:
        return self.tracker.ignore_setup(setup_id)
