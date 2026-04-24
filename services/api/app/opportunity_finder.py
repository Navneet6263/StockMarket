from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

from app.market_intelligence import MarketIntelligenceService
from app.market_universe import MarketUniverseService


class OpportunityFinder:
    def __init__(self):
        self.market_intelligence = MarketIntelligenceService()
        self.market_universe = MarketUniverseService()
        self.scan_cache: Dict[str, tuple[datetime, Dict]] = {}
        self.profile_cache: Dict[str, tuple[datetime, Dict]] = {}
        self.scan_ttl = timedelta(minutes=20)
        self.profile_ttl = timedelta(hours=12)

    def _cache_get(self, cache: Dict[str, tuple[datetime, Dict]], key: str, ttl: timedelta) -> Optional[Dict]:
        cached = cache.get(key)
        if cached and datetime.now(timezone.utc) - cached[0] < ttl:
            return cached[1]
        return None

    def _cache_set(self, cache: Dict[str, tuple[datetime, Dict]], key: str, value: Dict):
        cache[key] = (datetime.now(timezone.utc), value)

    def _resolve_symbol(self, symbol: str) -> str:
        clean = (symbol or "").upper().replace(" ", "")
        if clean.startswith("^") or "." in clean:
            return clean
        return f"{clean}.NS"

    def _chunk(self, values: List[str], size: int) -> List[List[str]]:
        return [values[index : index + size] for index in range(0, len(values), size)]

    def _download_universe(self, symbols: List[str], period: str = "1y") -> Dict[str, pd.DataFrame]:
        results: Dict[str, pd.DataFrame] = {}
        clean_symbols = list(dict.fromkeys([symbol.upper() for symbol in symbols]))

        for chunk in self._chunk(clean_symbols, 25):
            ticker_map = {symbol: self._resolve_symbol(symbol) for symbol in chunk}
            joined = " ".join(ticker_map.values())
            try:
                data = yf.download(
                    tickers=joined,
                    period=period,
                    group_by="ticker",
                    auto_adjust=False,
                    progress=False,
                    threads=True,
                )
            except Exception:
                continue

            for symbol, yf_symbol in ticker_map.items():
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        frame = data[yf_symbol].copy()
                    else:
                        frame = data.copy()
                except Exception:
                    continue

                if frame.empty:
                    continue

                required = {"Open", "High", "Low", "Close", "Volume"}
                if not required.issubset(set(frame.columns)):
                    continue

                frame = frame.dropna(subset=["Close"]).copy()
                if len(frame) < 80:
                    continue
                results[symbol] = frame

        return results

    def _calculate_rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss + 0.0001)
        return 100 - (100 / (1 + rs))

    def _technical_snapshot(self, symbol: str, frame: pd.DataFrame) -> Optional[Dict]:
        close = frame["Close"].astype(float)
        high = frame["High"].astype(float)
        low = frame["Low"].astype(float)
        volume = frame["Volume"].astype(float)

        if len(close) < 80:
            return None

        price = float(close.iloc[-1])
        prev_price = float(close.iloc[-2]) if len(close) > 1 else price
        sma20 = float(close.rolling(20).mean().iloc[-1])
        sma50 = float(close.rolling(50).mean().iloc[-1])
        vol_avg_20 = float(volume.rolling(20).mean().iloc[-1])
        volume_ratio = float(volume.iloc[-1] / vol_avg_20) if vol_avg_20 else 1.0
        rsi = float(self._calculate_rsi(close, 14).iloc[-1])

        prior_high_20 = float(high.rolling(20).max().shift(1).iloc[-1])
        prior_low_20 = float(low.rolling(20).min().shift(1).iloc[-1])
        high_60 = float(high.rolling(60).max().iloc[-1])

        return_20d = ((price / float(close.iloc[-21])) - 1) * 100
        return_60d = ((price / float(close.iloc[-61])) - 1) * 100
        drawdown_from_high = ((price / high_60) - 1) * 100 if high_60 else 0.0
        day_change = ((price / prev_price) - 1) * 100 if prev_price else 0.0
        breakout = bool(price > prior_high_20) if prior_high_20 else False
        breakdown = bool(price < prior_low_20) if prior_low_20 else False

        direction = "neutral"
        if price > sma20 > sma50 and return_20d > 0:
            direction = "up"
        elif price < sma20 < sma50 and return_20d < 0:
            direction = "down"

        composite_score = (
            max(return_20d, 0) * 0.5
            + max(return_60d, 0) * 0.3
            + max(volume_ratio - 1, 0) * 22
            + (8 if breakout else 0)
            - max(-drawdown_from_high, 0) * 0.15
        )

        return {
            "symbol": symbol,
            "price": round(price, 2),
            "day_change_pct": round(day_change, 2),
            "return_20d_pct": round(return_20d, 2),
            "return_60d_pct": round(return_60d, 2),
            "volume_ratio": round(volume_ratio, 2),
            "rsi": round(rsi, 1),
            "sma20": round(sma20, 2),
            "sma50": round(sma50, 2),
            "above_sma20": price > sma20,
            "above_sma50": price > sma50,
            "breakout_20d": breakout,
            "breakdown_20d": breakdown,
            "drawdown_from_high_pct": round(drawdown_from_high, 2),
            "direction": direction,
            "composite_score": round(composite_score, 2),
        }

    def _numeric(self, payload: Dict, key: str) -> Optional[float]:
        value = payload.get(key)
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if np.isnan(value):
                return None
            return float(value)
        return None

    def _brief_summary(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", (text or "").strip())
        if not clean:
            return "Business summary unavailable."
        parts = re.split(r"(?<=[.!?])\s+", clean)
        return " ".join(parts[:2])[:340]

    def _format_market_cap(self, market_cap: Optional[float]) -> str:
        if not market_cap:
            return "n/a"
        crore = market_cap / 10000000
        if crore >= 100000:
            return f"{crore / 100000:.2f} lakh crore"
        return f"{crore:,.0f} crore"

    def _score_business_model(self, info: Dict) -> Dict:
        revenue_growth = self._numeric(info, "revenueGrowth")
        earnings_growth = self._numeric(info, "earningsGrowth")
        profit_margin = self._numeric(info, "profitMargins")
        operating_margin = self._numeric(info, "operatingMargins")
        debt_to_equity = self._numeric(info, "debtToEquity")
        roe = self._numeric(info, "returnOnEquity")
        market_cap = self._numeric(info, "marketCap")
        free_cashflow = self._numeric(info, "freeCashflow")

        score = 50
        strengths: List[str] = []
        risks: List[str] = []

        if revenue_growth is not None:
            if revenue_growth >= 0.15:
                score += 12
                strengths.append("Revenue growth is strong.")
            elif revenue_growth >= 0.05:
                score += 6
            elif revenue_growth < 0:
                score -= 12
                risks.append("Revenue is shrinking.")

        if earnings_growth is not None:
            if earnings_growth >= 0.15:
                score += 12
                strengths.append("Earnings growth is scaling.")
            elif earnings_growth >= 0.05:
                score += 6
            elif earnings_growth < 0:
                score -= 10
                risks.append("Earnings trend is weak.")

        if profit_margin is not None:
            if profit_margin >= 0.12:
                score += 8
                strengths.append("Profit margins are healthy.")
            elif profit_margin <= 0:
                score -= 12
                risks.append("Profit margins are weak or negative.")

        if operating_margin is not None:
            if operating_margin >= 0.15:
                score += 6
            elif operating_margin <= 0:
                score -= 6

        if debt_to_equity is not None:
            if debt_to_equity <= 60:
                score += 8
                strengths.append("Debt looks manageable.")
            elif debt_to_equity >= 150:
                score -= 12
                risks.append("Leverage is high.")

        if roe is not None:
            if roe >= 0.15:
                score += 8
                strengths.append("Return on equity is healthy.")
            elif roe < 0.08:
                score -= 6
                risks.append("Return on equity is weak.")

        if free_cashflow is not None:
            if free_cashflow > 0:
                score += 5
            elif free_cashflow < 0:
                score -= 5
                risks.append("Free cash flow is negative.")

        if market_cap is not None and market_cap < 15000000000:
            risks.append("Small market cap can be volatile.")

        score = max(5, min(int(round(score)), 95))
        if score >= 78:
            rating = "strong"
            note = "Business model looks scalable and financially healthy."
        elif score >= 64:
            rating = "good"
            note = "Business model looks workable, but monitor execution closely."
        elif score >= 48:
            rating = "average"
            note = "Business model is mixed. Price can move, but conviction is not high."
        else:
            rating = "weak"
            note = "Business model looks fragile or speculative right now."

        growth_stock = bool(
            score >= 64
            and (revenue_growth or 0) > 0.12
            and (earnings_growth or 0) > 0.10
        )

        return {
            "score": score,
            "rating": rating,
            "note": note,
            "strengths": strengths[:4],
            "risks": risks[:4],
            "is_growth_stock": growth_stock,
            "revenue_growth": round((revenue_growth or 0) * 100, 2) if revenue_growth is not None else None,
            "earnings_growth": round((earnings_growth or 0) * 100, 2) if earnings_growth is not None else None,
            "profit_margin": round((profit_margin or 0) * 100, 2) if profit_margin is not None else None,
            "debt_to_equity": round(debt_to_equity, 2) if debt_to_equity is not None else None,
            "roe": round((roe or 0) * 100, 2) if roe is not None else None,
        }

    def _build_long_term_view(self, info: Dict, score_card: Dict, sector: str, industry: str, market_cap: Optional[float]) -> Dict:
        sector_view = self.market_intelligence.get_sector_scenario_view(sector, industry)
        revenue_growth = score_card.get("revenue_growth")
        earnings_growth = score_card.get("earnings_growth")
        score = score_card.get("score", 50)
        is_growth_stock = score_card.get("is_growth_stock", False)
        small_cap = bool(market_cap and market_cap < 50000000000)
        micro_cap = bool(market_cap and market_cap < 15000000000)

        label = "cyclical_watch"
        multibagger_potential = "medium"
        summary = "This looks more cyclical than structural. Position sizing matters more than dreaming about outsized returns."
        must_go_right = [
            "Sales and earnings need to keep compounding, not just the stock price.",
            "Debt, dilution, and governance need to stay under control.",
        ]
        failure_points = [
            "If growth stalls, re-rating can reverse quickly.",
            "If the balance sheet weakens, downside can be deep.",
        ]

        if is_growth_stock and score >= 72:
            label = "emerging_compounder"
            multibagger_potential = "high" if small_cap else "medium"
            summary = (
                "This has the profile of an emerging compounder. If execution holds for years, it can create big wealth, "
                "but only if growth stays real and cash generation follows."
            )
            must_go_right = [
                "Revenue and earnings growth must remain durable for several years.",
                "Management needs to reinvest without damaging margins or balance sheet quality.",
                "Industry structure should stay favorable enough for market-share gains.",
            ]
            failure_points = [
                "A sudden debt build-up or margin collapse can break the compounding case.",
                "If the company loses execution edge, high expectations can unwind fast.",
            ]
        elif small_cap and score >= 58:
            label = "asymmetric_small_cap"
            multibagger_potential = "high"
            summary = (
                "This is a smaller-cap asymmetry case. The upside can be large if the business keeps scaling, "
                "but the path will usually be volatile and full of shakeouts."
            )
            must_go_right = [
                "The company must keep winning orders or expanding its addressable market.",
                "Cash flow conversion and promoter behavior need to improve or remain clean.",
                "Volume should keep improving when the stock starts trending.",
            ]
            failure_points = [
                "Small caps get punished hard if growth narrative breaks.",
                "Liquidity can disappear fast during market stress.",
            ]
        elif micro_cap or score < 48:
            label = "speculative_turnaround"
            multibagger_potential = "lottery_ticket"
            summary = (
                "This is speculative. It can explode on narrative, liquidity, or a turnaround rumor, "
                "but it does not yet deserve a strong 10-year compounder label."
            )
            must_go_right = [
                "Business execution must improve materially, not just trading volume.",
                "Debt, dilution, and promoter risk must stay contained.",
            ]
            failure_points = [
                "Weak business quality can turn a fast rally into a permanent capital loss.",
                "Penny-stock volatility can trap traders after a single bad quarter.",
            ]

        return {
            "label": label,
            "multibagger_potential": multibagger_potential,
            "summary": summary,
            "must_go_right": must_go_right[:3],
            "failure_points": failure_points[:3],
            "sector_scenario": sector_view,
        }

    def get_company_snapshot(self, symbol: str, technical: Optional[Dict] = None) -> Dict:
        cache_key = symbol.upper()
        cached = self._cache_get(self.profile_cache, cache_key, self.profile_ttl)
        if cached is not None:
            return cached

        resolved = self._resolve_symbol(symbol)
        info: Dict = {}
        try:
            info = yf.Ticker(resolved).info or {}
        except Exception:
            info = {}

        score_card = self._score_business_model(info)
        market_cap = self._numeric(info, "marketCap")
        company_name = info.get("longName") or info.get("shortName") or symbol.upper()
        sector = info.get("sector") or "Unknown"
        industry = info.get("industry") or "Unknown"
        summary = self._brief_summary(info.get("longBusinessSummary", ""))
        sector_scenario = self.market_intelligence.get_sector_scenario_view(sector, industry)
        long_term_view = self._build_long_term_view(info, score_card, sector, industry, market_cap)
        current_price = self._numeric(info, "currentPrice") or self._numeric(info, "regularMarketPrice")
        if current_price is None and technical is not None:
            current_price = technical.get("price")

        payload = {
            "symbol": symbol.upper(),
            "company_name": company_name,
            "sector": sector,
            "industry": industry,
            "price": round(float(current_price), 2) if current_price is not None else None,
            "market_cap": market_cap,
            "market_cap_label": self._format_market_cap(market_cap),
            "business_summary": summary,
            "business_model": score_card,
            "sector_scenario": sector_scenario,
            "long_term_view": long_term_view,
        }
        self._cache_set(self.profile_cache, cache_key, payload)
        return payload

    def _build_opportunity_item(self, category: str, technical: Dict, company: Dict, discovery_meta: Optional[Dict] = None) -> Dict:
        business_model = company.get("business_model", {})
        sector_scenario = company.get("sector_scenario", {})
        long_term_view = company.get("long_term_view", {})
        company_name = company.get("company_name", technical["symbol"])
        summary = company.get("business_summary", "Business summary unavailable.")
        rating = business_model.get("rating", "average")
        score = business_model.get("score", 50)
        note = business_model.get("note", "")
        discovery_tags = (discovery_meta or {}).get("tags", [])

        if category == "growth_leaders":
            brief = (
                f"{company_name} is showing growth-stock behavior. Price is up {technical['return_60d_pct']}% in 60 days "
                f"with {technical['volume_ratio']}x volume, and the business model is rated {rating}."
            )
            caution = "If volume dries up or price slips below the recent breakout, momentum can cool quickly."
        elif category == "quiet_compounders":
            brief = (
                f"{company_name} looks like a quieter compounder. Business quality is {rating}, "
                f"and the chart is constructive without looking overheated."
            )
            caution = "These usually move slowly. Do not chase if it becomes too extended from moving averages."
        elif category == "penny_movers":
            brief = (
                f"{company_name} is a low-priced mover with unusual participation. Price is {technical['price']} and volume is {technical['volume_ratio']}x average. "
                f"Business quality is {rating}."
            )
            caution = "Cheap stocks can reverse violently. If business quality is weak, treat it as speculative only."
        elif category == "microcap_sprinters":
            brief = (
                f"{company_name} is in the deep low-price bucket. This is where 1 to 50 rupee names can suddenly wake up, "
                f"but only a few have enough business support to sustain the move."
            )
            caution = "Assume high manipulation risk until business quality and volume both keep improving."
        elif category == "recovery_watchlist":
            brief = (
                f"{company_name} sits in a sector that can recover early if geopolitical stress cools. "
                f"Current price structure is {technical['direction']} with {technical['volume_ratio']}x volume."
            )
            caution = "Recovery trades fail fast if crude, yields, or VIX turn back up."
        elif category == "falling_alerts":
            brief = (
                f"{company_name} is under pressure. Price is down {abs(technical['drawdown_from_high_pct'])}% from its recent high "
                f"and the technical tone is weakening."
            )
            caution = "Avoid bottom fishing until it reclaims trend support or fresh business evidence improves."
        else:
            brief = (
                f"{company_name} is on the long-term asymmetric watchlist. Business model is {rating} and current growth signals are being monitored."
            )
            caution = "This is a watchlist, not a guarantee. Even strong small caps can underperform for long periods."

        if discovery_tags:
            discovery_line = ", ".join(tag.replace("_", " ") for tag in discovery_tags[:3])
            brief = f"{brief} Market picked it through: {discovery_line}."

        return {
            "symbol": technical["symbol"],
            "company_name": company_name,
            "sector": company.get("sector"),
            "industry": company.get("industry"),
            "current_price": technical["price"],
            "market_cap_label": company.get("market_cap_label"),
            "business_summary": summary,
            "business_model_score": score,
            "business_model_rating": rating,
            "business_model_note": note,
            "is_growth_stock": business_model.get("is_growth_stock", False),
            "volume_ratio": technical["volume_ratio"],
            "day_change_pct": technical["day_change_pct"],
            "return_20d_pct": technical["return_20d_pct"],
            "return_60d_pct": technical["return_60d_pct"],
            "drawdown_from_high_pct": technical["drawdown_from_high_pct"],
            "rsi": technical["rsi"],
            "breakout_20d": technical["breakout_20d"],
            "breakdown_20d": technical["breakdown_20d"],
            "brief": brief,
            "caution": caution,
            "strengths": business_model.get("strengths", []),
            "risks": business_model.get("risks", []),
            "war_cools_impact": sector_scenario.get("war_cools_impact"),
            "war_escalates_impact": sector_scenario.get("war_escalates_impact"),
            "recovery_rank": sector_scenario.get("recovery_rank"),
            "long_term_label": long_term_view.get("label"),
            "long_term_summary": long_term_view.get("summary"),
            "multibagger_potential": long_term_view.get("multibagger_potential"),
            "discovered_by": discovery_tags,
        }

    def scan_opportunities(self, force_refresh: bool = False) -> Dict:
        cache_key = "default"
        if not force_refresh:
            cached = self._cache_get(self.scan_cache, cache_key, self.scan_ttl)
            if cached is not None:
                return cached

        discovery = self.market_universe.discover_market(force_refresh=force_refresh)
        discovery_meta = discovery.get("symbol_meta", {})
        universe = discovery.get("opportunity_symbols", discovery.get("symbols", []))
        downloads = self._download_universe(universe, period="1y")
        technicals = []
        for symbol, frame in downloads.items():
            snapshot = self._technical_snapshot(symbol, frame)
            if snapshot:
                technicals.append(snapshot)

        growth_symbols = {
            symbol for symbol, meta in discovery_meta.items() if "fundamental_growth" in meta.get("tags", [])
        }
        quality_symbols = {
            symbol for symbol, meta in discovery_meta.items() if "quality_compounders" in meta.get("tags", [])
        }
        value_symbols = {
            symbol for symbol, meta in discovery_meta.items() if "value_growth" in meta.get("tags", [])
        }
        breakout_symbols = {
            symbol for symbol, meta in discovery_meta.items() if "breakout_candidates" in meta.get("tags", [])
        }
        low_price_symbols = {
            symbol for symbol, meta in discovery_meta.items() if "low_price_active" in meta.get("tags", [])
        }
        weak_symbols = {
            symbol for symbol, meta in discovery_meta.items() if "selling_pressure" in meta.get("tags", [])
        }
        turnaround_symbols = {
            symbol for symbol, meta in discovery_meta.items() if "turnaround_watch" in meta.get("tags", [])
        }

        momentum_raw = sorted(
            [
                item
                for item in technicals
                if item["direction"] == "up"
                and (
                    item["breakout_20d"]
                    or item["return_20d_pct"] >= 2
                    or item["return_60d_pct"] >= 6
                    or item["symbol"] in breakout_symbols
                )
            ],
            key=lambda item: item["composite_score"],
            reverse=True,
        )[:16]

        quiet_raw = sorted(
            [
                item
                for item in technicals
                if item["direction"] == "up"
                and item["above_sma50"]
                and 2 <= item["return_60d_pct"] <= 30
                and item["drawdown_from_high_pct"] >= -15
                and (
                    item["symbol"] in quality_symbols
                    or item["symbol"] in value_symbols
                    or item["volume_ratio"] >= 0.8
                )
            ],
            key=lambda item: (item["return_60d_pct"], item["volume_ratio"]),
            reverse=True,
        )[:16]

        penny_raw = sorted(
            [
                item
                for item in technicals
                if item["price"] <= 120
                and (item["symbol"] in low_price_symbols or item["price"] <= 80)
                and item["return_60d_pct"] >= -20
                and item["volume_ratio"] >= 0.7
            ],
            key=lambda item: (item["return_20d_pct"], item["volume_ratio"]),
            reverse=True,
        )[:16]

        microcap_raw = sorted(
            [
                item
                for item in technicals
                if item["price"] <= 60
                and (item["symbol"] in low_price_symbols or item["price"] <= 30)
                and item["volume_ratio"] >= 0.7
                and (item["breakout_20d"] or item["return_20d_pct"] >= -2 or item["return_60d_pct"] >= -10)
            ],
            key=lambda item: (item["volume_ratio"], item["return_20d_pct"], item["return_60d_pct"]),
            reverse=True,
        )[:16]
        if not microcap_raw:
            microcap_raw = sorted(
                [item for item in technicals if item["price"] <= 50],
                key=lambda item: (item["volume_ratio"], item["return_20d_pct"], item["return_60d_pct"]),
                reverse=True,
            )[:16]

        falling_raw = sorted(
            [
                item
                for item in technicals
                if (
                    item["symbol"] in weak_symbols
                    or (
                        item["direction"] == "down"
                        and (item["breakdown_20d"] or item["drawdown_from_high_pct"] <= -15)
                    )
                )
                and item["volume_ratio"] >= 1.0
            ],
            key=lambda item: (item["drawdown_from_high_pct"], -item["volume_ratio"]),
        )[:16]

        long_term_raw = sorted(
            [
                item
                for item in technicals
                if item["price"] >= 20
                and item["volume_ratio"] >= 0.6
                and (
                    item["above_sma50"]
                    or item["return_60d_pct"] >= 0
                    or item["symbol"] in growth_symbols
                    or item["symbol"] in quality_symbols
                    or item["symbol"] in value_symbols
                )
                and item["symbol"] not in weak_symbols
            ],
            key=lambda item: (item["composite_score"], item["return_60d_pct"], item["volume_ratio"]),
            reverse=True,
        )[:24]

        recovery_raw = sorted(
            [
                item
                for item in technicals
                if (
                    item["symbol"] in turnaround_symbols
                    or item["symbol"] in quality_symbols
                    or item["symbol"] in value_symbols
                )
                and item["direction"] != "down"
                and item["above_sma20"]
                and item["volume_ratio"] >= 0.8
                and item["drawdown_from_high_pct"] >= -25
            ],
            key=lambda item: (item["volume_ratio"], item["return_20d_pct"], item["return_60d_pct"]),
            reverse=True,
        )[:20]

        shortlist = list(
            dict.fromkeys(
                [
                    item["symbol"]
                    for item in momentum_raw + quiet_raw + penny_raw + microcap_raw + falling_raw + long_term_raw + recovery_raw
                ]
            )
        )

        technical_map = {item["symbol"]: item for item in technicals}
        profiles = {
            symbol: self.get_company_snapshot(symbol, technical_map.get(symbol))
            for symbol in shortlist
        }

        def build_group(source: List[Dict], category: str, limit: int, filter_fn=None, allow_filter_fallback: bool = True) -> List[Dict]:
            items: List[Dict] = []
            for item in source:
                profile = profiles.get(item["symbol"])
                if not profile:
                    continue
                card = self._build_opportunity_item(category, item, profile, discovery_meta.get(item["symbol"]))
                if filter_fn and not filter_fn(card):
                    continue
                items.append(card)
                if len(items) >= limit:
                    break
            if not items and filter_fn and allow_filter_fallback:
                for item in source:
                    profile = profiles.get(item["symbol"])
                    if not profile:
                        continue
                    items.append(self._build_opportunity_item(category, item, profile, discovery_meta.get(item["symbol"])))
                    if len(items) >= limit:
                        break
            return items

        growth_leaders = build_group(
            momentum_raw,
            "growth_leaders",
            6,
            lambda card: card["business_model_score"] >= 50,
        )
        quiet_compounders = build_group(
            quiet_raw,
            "quiet_compounders",
            6,
            lambda card: card["business_model_score"] >= 68,
        )
        penny_movers = build_group(penny_raw, "penny_movers", 6)
        microcap_sprinters = build_group(
            microcap_raw,
            "microcap_sprinters",
            6,
            lambda card: card["business_model_score"] >= 35 or card["volume_ratio"] >= 1.4,
        )
        falling_alerts = build_group(falling_raw, "falling_alerts", 6)
        long_term_watchlist = build_group(
            long_term_raw,
            "long_term_watchlist",
            6,
            lambda card: (
                card["business_model_score"] >= 70
                and card["multibagger_potential"] in ("high", "medium")
                and card["long_term_label"] != "speculative_turnaround"
                and card["current_price"] >= 40
            ),
            allow_filter_fallback=False,
        )
        recovery_watchlist = build_group(
            recovery_raw,
            "recovery_watchlist",
            6,
            lambda card: card.get("war_cools_impact") == "beneficiary" and card["business_model_score"] >= 60,
            allow_filter_fallback=False,
        )

        try:
            macro_context = self.market_intelligence.get_macro_snapshot()
        except Exception as exc:
            macro_context = {
                "risk_mode": "mixed",
                "summary": f"Macro snapshot unavailable: {exc}",
                "checklist": [],
            }

        try:
            global_context = self.market_intelligence.get_global_scenario()
        except Exception as exc:
            global_context = {
                "state": "unknown",
                "summary": f"Global scenario unavailable: {exc}",
                "timing_note": "Use live macro and price action until scenario data refreshes.",
                "current_setup": [],
                "what_to_watch": [],
                "if_conflict_cools": [],
                "if_conflict_worsens": [],
                "recovery_order": [],
                "gauges": [],
                "headline_watch": [],
            }

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "monitored_stocks": len(technicals),
            "low_price_monitored": len([item for item in technicals if item["price"] <= 50]),
            "market_discovery": {
                "source_mode": discovery.get("source_mode"),
                "note": discovery.get("note"),
                "bucket_counts": discovery.get("bucket_counts", {}),
                "generated_at": discovery.get("generated_at"),
            },
            "macro_context": macro_context,
            "global_context": global_context,
            "disclaimer": (
                "This page now starts from live market screeners, not a fixed stock list. "
                "No system can guarantee 30000% returns. Use this to narrow research, then verify charts, business quality, and risk before acting."
            ),
            "categories": {
                "growth_leaders": growth_leaders,
                "quiet_compounders": quiet_compounders,
                "penny_movers": penny_movers,
                "microcap_sprinters": microcap_sprinters,
                "falling_alerts": falling_alerts,
                "long_term_watchlist": long_term_watchlist,
                "recovery_watchlist": recovery_watchlist,
            },
        }
        self._cache_set(self.scan_cache, cache_key, payload)
        return payload
