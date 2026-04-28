from __future__ import annotations

from datetime import datetime, timezone
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
from app.services.scoring import ScoringEngine
from app.services.setup_tracker import SetupTrackerService


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
        self.scan_cache: TTLCache[Dict] = TTLCache(settings.scan_cache_ttl_sec)
        self.detail_cache: TTLCache[Dict] = TTLCache(settings.detail_cache_ttl_sec)

    def _scan_symbols(self, discovery: Dict) -> list[str]:
        custom = list(self.settings.custom_universe)
        ranked = discovery.get("scan_symbols") or discovery.get("symbols") or []
        if custom:
            merged = list(dict.fromkeys(custom + ranked))
            return merged[: self.settings.scan_symbol_limit]
        return ranked[: self.settings.scan_symbol_limit]

    def _top_symbols(self, results: list[Dict], limit: int) -> list[str]:
        ranked = sorted(
            [item for item in results if item["direction"] != "neutral"],
            key=lambda item: (item["move_quality"], item["confidence"], item["relative_volume"]),
            reverse=True,
        )
        return [item["symbol"] for item in ranked[:limit]]

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

        live_frame = self.data.overlay_quote(frame, quote)
        feature_frame = self.indicators.build_feature_frame(live_frame, benchmark_frame)
        snapshot = self.indicators.build_snapshot(symbol, live_frame, feature_frame, intraday_frame)
        backtest = self.backtest.evaluate(symbol, frame, benchmark_frame) if with_backtest else {}
        signal = self.scoring.evaluate(symbol, snapshot, backtest if with_backtest else None)
        if quote:
            signal["current_price"] = round(safe_float(quote.get("price"), signal["current_price"]), 2)
            signal["change_pct"] = round(safe_float(quote.get("change_percent"), signal["change_pct"]), 2)
            signal["volume"] = safe_int(quote.get("volume"), signal["volume"])
        return signal | {"backtest": backtest}

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

    def _shape_scan_payload(self, discovery: Dict, results: list[Dict], benchmark_frame: pd.DataFrame) -> Dict:
        symbol_meta = discovery.get("symbol_meta", {})
        for item in results:
            meta = symbol_meta.get(item.get("symbol"), {})
            if meta:
                item.setdefault("company_name", meta.get("short_name") or item.get("symbol"))

        top_ranked = self._unique_signals(sorted(
            [
                item for item in results
                if item["direction"] != "neutral" and item["alert_level"] in {"high_priority", "watchlist", "low_priority"}
            ],
            key=lambda item: (
                item["alert_level"] == "high_priority",
                item["move_quality"],
                item["confidence"],
                item["relative_volume"],
            ),
            reverse=True,
        ))
        top_opportunities = top_ranked[:8]
        surfaced = {item["symbol"] for item in top_opportunities}

        volume_ranked = self._unique_signals(sorted(
            [
                item for item in results
                if item["relative_volume"] >= 1.6 or item["intraday_volume_ratio"] >= 1.4
            ],
            key=lambda item: (item["relative_volume"], item["intraday_volume_ratio"], item["move_quality"]),
            reverse=True,
        ))
        unusual_volume = self._fill_signal_bucket(
            [item for item in volume_ranked if item["symbol"] not in surfaced],
            volume_ranked,
            8,
        )

        breakouts = self._unique_signals([
            item for item in results
            if "breakout" in item["tags"] or "breakdown" in item["tags"]
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
            8,
        )

        bearish_ranked = self._unique_signals(sorted(
            [item for item in results if item["direction"] == "bearish"],
            key=lambda item: (item["move_quality"], item["confidence"]),
            reverse=True,
        ))
        bearish_risks = bearish_ranked[:8]
        surfaced.update(item["symbol"] for item in breakout_candidates)
        surfaced.update(item["symbol"] for item in bearish_risks)

        mover_ranked = [
            {
                "symbol": meta["symbol"],
                "company_name": meta.get("short_name") or meta["symbol"],
                "price": meta.get("price"),
                "change_pct": meta.get("change_pct"),
                "volume": meta.get("volume"),
                "tags": meta.get("tags", []),
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
        breadth = self._build_market_breadth(results, benchmark_frame)
        market_mood = self._market_mood(breadth, top_opportunities, bearish_risks)

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "universe_size": len(results),
            "market_breadth": breadth,
            "market_discovery": {
                "source_mode": discovery.get("source_mode"),
                "note": discovery.get("note"),
                "bucket_counts": discovery.get("bucket_counts", {}),
            },
            "macro_context": self.market_intelligence.get_macro_snapshot(),
            "global_context": self.market_intelligence.get_global_scenario(),
            "top_opportunities": top_opportunities,
            "unusual_volume": unusual_volume,
            "breakout_candidates": breakout_candidates,
            "bearish_risks": bearish_risks,
            "top_movers": top_movers,
            "summary": {
                "high_priority": len([item for item in results if item["alert_level"] == "high_priority"]),
                "watchlist": len([item for item in results if item["alert_level"] == "watchlist"]),
                "avoid": len([item for item in results if item["alert_level"] == "avoid"]),
                "opportunities_count": len(top_opportunities),
                "breakout_count": len(breakout_candidates),
                "bearish_risk_count": len(bearish_risks),
                "unusual_volume_count": len(unusual_volume),
                "market_mood": market_mood,
                "scanner_leader": top_opportunities[0]["symbol"] if top_opportunities else None,
            },
        }

    def scan_market(self, force_refresh: bool = False) -> Dict:
        cache_key = "market_overview"
        if not force_refresh:
            cached = self.scan_cache.get(cache_key)
            if cached is not None:
                return cached

        discovery = self.universe.discover_market(force_refresh=force_refresh)
        symbols = self._scan_symbols(discovery)
        benchmark_symbol = self.settings.benchmark_symbol
        benchmark_frame = self.data.fetch_history(benchmark_symbol, period=self.settings.scan_history_period)
        daily_frames = self.data.fetch_batch_history(symbols, period=self.settings.scan_history_period)

        preliminary: list[Dict] = []
        for symbol in symbols:
            frame = daily_frames.get(symbol)
            if frame is None or frame.empty or len(frame) < self.settings.min_history_bars:
                continue
            signal = self._evaluate_symbol(
                symbol,
                frame,
                benchmark_frame,
                discovery.get("symbol_meta", {}).get(symbol),
                None,
                with_backtest=False,
            )
            signal["discovered_by"] = discovery.get("symbol_meta", {}).get(symbol, {}).get("tags", [])
            preliminary.append(signal)

        if not preliminary:
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
                "market_discovery": {"source_mode": discovery.get("source_mode"), "note": "No valid symbols were scanned."},
                "macro_context": self.market_intelligence.get_macro_snapshot(),
                "global_context": self.market_intelligence.get_global_scenario(),
                "top_opportunities": [],
                "unusual_volume": [],
                "breakout_candidates": [],
                "bearish_risks": [],
                "top_movers": [],
                "summary": {
                    "high_priority": 0,
                    "watchlist": 0,
                    "avoid": 0,
                    "opportunities_count": 0,
                    "breakout_count": 0,
                    "bearish_risk_count": 0,
                    "unusual_volume_count": 0,
                    "market_mood": "waiting_for_scan",
                    "scanner_leader": None,
                },
            }

        top_symbols = self._top_symbols(preliminary, self.settings.intraday_symbol_limit)
        intraday_frames = self.data.fetch_batch_history(top_symbols, period="5d", interval="15m", chunk_size=10)
        enhanced = {item["symbol"]: item for item in preliminary}

        for symbol in top_symbols:
            frame = daily_frames.get(symbol)
            if frame is None or frame.empty:
                continue
            enhanced[symbol] = self._evaluate_symbol(
                symbol,
                frame,
                benchmark_frame,
                discovery.get("symbol_meta", {}).get(symbol),
                intraday_frames.get(symbol),
                with_backtest=True,
            ) | {"discovered_by": discovery.get("symbol_meta", {}).get(symbol, {}).get("tags", [])}

        payload = self._shape_scan_payload(discovery, list(enhanced.values()), benchmark_frame)
        self.tracker.sync_scan_payload(payload, discovery.get("symbol_meta", {}))
        self.scan_cache.set(cache_key, payload)
        return payload

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
        return {
            "symbol": self.data.clean_symbol(symbol),
            "period": period,
            "data": self.data.serialize_candles(history, limit=None),
            "count": len(history),
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
