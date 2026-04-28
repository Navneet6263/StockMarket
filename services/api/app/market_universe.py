from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import yfinance as yf
from yfinance import EquityQuery

from app.core.settings import Settings, get_settings
from app.market_universe_config import UNIVERSE_CONFIG


class MarketUniverseService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.cache: Dict[str, tuple[datetime, Dict]] = {}
        self.cache_ttl = timedelta(minutes=15)
        self.page_size = 100
        self.max_symbols = max(120, self.settings.universe_size)
        self.max_scan_symbols = min(max(80, self.settings.scan_symbol_limit), self.max_symbols)
        self.max_opportunity_symbols = min(max(self.max_scan_symbols, self.settings.scan_symbol_limit + 80), self.max_symbols)
        self.tag_weights = {
            "most_active": 2,
            "breakout_candidates": 3,
            "selling_pressure": 2,
            "low_price_active": 2,
            "fundamental_growth": 4,
            "quality_compounders": 4,
            "value_growth": 3,
            "turnaround_watch": 2,
        }

    def _cache_get(self, key: str) -> Optional[Dict]:
        cached = self.cache.get(key)
        if cached and datetime.now(timezone.utc) - cached[0] < self.cache_ttl:
            return cached[1]
        return None

    def _cache_set(self, key: str, payload: Dict):
        self.cache[key] = (datetime.now(timezone.utc), payload)

    def _normalize_symbol(self, symbol: str) -> str:
        clean = (symbol or "").upper().strip()
        if "." in clean:
            clean = clean.split(".")[0]
        return clean

    def _configured_symbols(self) -> list[str]:
        group_map = {
            "NIFTY50": UNIVERSE_CONFIG.nifty_50,
            "NIFTY_50": UNIVERSE_CONFIG.nifty_50,
            "NIFTY100": UNIVERSE_CONFIG.nifty_100,
            "NIFTY_100": UNIVERSE_CONFIG.nifty_100,
            "NIFTY200": UNIVERSE_CONFIG.nifty_200,
            "NIFTY_200": UNIVERSE_CONFIG.nifty_200,
            "NIFTY500": UNIVERSE_CONFIG.nifty_500_seed,
            "NIFTY_500": UNIVERSE_CONFIG.nifty_500_seed,
            "FNO": UNIVERSE_CONFIG.fno_stocks,
            "F&O": UNIVERSE_CONFIG.fno_stocks,
        }
        configured: list[str] = []
        invalid_symbols = set(self.settings.invalid_symbols)
        for group in self.settings.universe_groups:
            configured.extend(group_map.get(group.upper(), ()))
        configured.extend(self.settings.custom_universe)
        return list(
            dict.fromkeys(
                self._normalize_symbol(symbol)
                for symbol in configured
                if symbol and self._normalize_symbol(symbol) not in invalid_symbols
            )
        )

    @staticmethod
    def _as_float(value) -> float:
        if isinstance(value, bool) or value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        return 0.0

    def _is_common_stock(self, quote: Dict) -> bool:
        raw_symbol = (quote.get("symbol") or "").upper()
        short_name = ((quote.get("shortName") or quote.get("longName") or "")).upper()

        if not raw_symbol.endswith(".NS"):
            return False
        if quote.get("quoteType") != "EQUITY":
            return False
        blocked_tokens = (
            "ETF",
            "BEES",
            "INVIT",
            "LIQUID",
            "GOLD",
            "SILV",
            "INDEX",
            "MUTUAL",
            "FUND",
        )
        if any(token in short_name for token in blocked_tokens):
            return False
        if any(token in raw_symbol for token in ("BEES", "INVIT")):
            return False
        return True

    def _passes_liquidity_filters(self, quote: Dict) -> bool:
        price = self._as_float(quote.get("regularMarketPrice") or quote.get("intradayprice"))
        volume = self._as_float(quote.get("regularMarketVolume") or quote.get("dayvolume"))
        market_cap = self._as_float(quote.get("marketCap") or quote.get("intradaymarketcap"))
        short_name = ((quote.get("shortName") or quote.get("longName") or "")).upper()

        if price < self.settings.min_price:
            return False
        if volume < self.settings.min_volume:
            return False
        if self.settings.min_market_cap and market_cap and market_cap < self.settings.min_market_cap:
            return False
        if not self.settings.include_sme and "SME" in short_name:
            return False
        return True

    def _snapshot_quote(self, item: Dict) -> Dict:
        return {
            "symbol": self._normalize_symbol(item.get("symbol", "")),
            "raw_symbol": item.get("symbol"),
            "exchange": item.get("exchange"),
            "price": item.get("regularMarketPrice") or item.get("intradayprice"),
            "change_pct": item.get("regularMarketChangePercent") or item.get("percentchange"),
            "volume": item.get("regularMarketVolume") or item.get("dayvolume"),
            "market_cap": item.get("marketCap") or item.get("intradaymarketcap"),
            "short_name": item.get("shortName") or item.get("longName"),
        }

    def _nse_query(self, *filters: EquityQuery) -> EquityQuery:
        return EquityQuery("and", [EquityQuery("eq", ["exchange", "NSI"]), *filters])

    def _screen_query(
        self,
        label: str,
        query: EquityQuery,
        *,
        sort_field: str,
        sort_asc: bool = False,
        max_pages: int = 3,
    ) -> Dict:
        quotes: List[Dict] = []
        seen = set()

        for page in range(max_pages):
            offset = page * self.page_size
            try:
                payload = yf.screen(query, offset=offset, size=self.page_size, sortField=sort_field, sortAsc=sort_asc)
            except Exception as exc:
                return {"label": label, "quotes": quotes, "symbols": [item["symbol"] for item in quotes], "error": str(exc)}

            raw_quotes = payload.get("quotes", []) if isinstance(payload, dict) else []
            if not raw_quotes:
                break

            page_added = 0
            for item in raw_quotes:
                if not self._is_common_stock(item):
                    continue
                if not self._passes_liquidity_filters(item):
                    continue
                snapshot = self._snapshot_quote(item)
                if not snapshot["symbol"] or snapshot["symbol"] in seen:
                    continue
                seen.add(snapshot["symbol"])
                quotes.append(snapshot)
                page_added += 1

            if len(raw_quotes) < self.page_size:
                break
            if page_added == 0 and page >= 1:
                break

        return {"label": label, "quotes": quotes, "symbols": [item["symbol"] for item in quotes]}

    def _screen_definitions(self) -> List[Dict]:
        return [
            {
                "label": "most_active",
                "query": self._nse_query(
                    EquityQuery("gte", ["intradayprice", max(1, self.settings.min_price)]),
                    EquityQuery("gt", ["dayvolume", max(50000, self.settings.min_volume // 2)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 4,
            },
            {
                "label": "breakout_candidates",
                "query": self._nse_query(
                    EquityQuery("gt", ["percentchange", 1.5]),
                    EquityQuery("gt", ["dayvolume", max(50000, self.settings.min_volume // 2)]),
                    EquityQuery("gte", ["intradayprice", max(1, self.settings.min_price)]),
                ),
                "sort_field": "percentchange",
                "sort_asc": False,
                "max_pages": 3,
            },
            {
                "label": "selling_pressure",
                "query": self._nse_query(
                    EquityQuery("lt", ["percentchange", -1.8]),
                    EquityQuery("gt", ["dayvolume", max(50000, self.settings.min_volume // 2)]),
                    EquityQuery("gte", ["intradayprice", max(1, self.settings.min_price)]),
                ),
                "sort_field": "percentchange",
                "sort_asc": True,
                "max_pages": 3,
            },
            {
                "label": "low_price_active",
                "query": self._nse_query(
                    EquityQuery("btwn", ["intradayprice", max(1, self.settings.min_price), 120]),
                    EquityQuery("gt", ["dayvolume", max(80000, self.settings.min_volume)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
            },
            {
                "label": "fundamental_growth",
                "query": self._nse_query(
                    EquityQuery("gte", ["quarterlyrevenuegrowth.quarterly", 5]),
                    EquityQuery("gte", ["epsgrowth.lasttwelvemonths", 5]),
                    EquityQuery("gt", ["dayvolume", max(30000, self.settings.min_volume // 3)]),
                    EquityQuery("gte", ["intradayprice", max(1, self.settings.min_price)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
            },
            {
                "label": "quality_compounders",
                "query": self._nse_query(
                    EquityQuery("gte", ["returnonequity.lasttwelvemonths", 12]),
                    EquityQuery("gte", ["netincomemargin.lasttwelvemonths", 8]),
                    EquityQuery("lt", ["totaldebtequity.lasttwelvemonths", 120]),
                    EquityQuery("gt", ["dayvolume", max(30000, self.settings.min_volume // 3)]),
                    EquityQuery("gte", ["intradayprice", max(20, self.settings.min_price)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
            },
            {
                "label": "value_growth",
                "query": self._nse_query(
                    EquityQuery("btwn", ["peratio.lasttwelvemonths", 0, 35]),
                    EquityQuery("gte", ["epsgrowth.lasttwelvemonths", 5]),
                    EquityQuery("gt", ["dayvolume", max(30000, self.settings.min_volume // 3)]),
                    EquityQuery("gte", ["intradayprice", max(1, self.settings.min_price)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
            },
            {
                "label": "turnaround_watch",
                "query": self._nse_query(
                    EquityQuery("btwn", ["percentchange", -8, 3]),
                    EquityQuery("gte", ["quarterlyrevenuegrowth.quarterly", 0]),
                    EquityQuery("gt", ["dayvolume", max(50000, self.settings.min_volume // 2)]),
                    EquityQuery("gte", ["intradayprice", max(1, self.settings.min_price)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
            },
        ]

    def discover_market(self, force_refresh: bool = False) -> Dict:
        cache_key = "yahoo_dynamic_nse_market"
        if not force_refresh:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached

        buckets: List[Dict] = []
        symbol_meta: Dict[str, Dict] = {}

        for config in self._screen_definitions():
            bucket = self._screen_query(
                config["label"],
                config["query"],
                sort_field=config["sort_field"],
                sort_asc=config["sort_asc"],
                max_pages=config["max_pages"],
            )
            buckets.append(bucket)

            for quote in bucket.get("quotes", []):
                symbol = quote["symbol"]
                meta = symbol_meta.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "raw_symbol": quote.get("raw_symbol"),
                        "exchange": quote.get("exchange"),
                        "tags": [],
                        "price": quote.get("price"),
                        "change_pct": quote.get("change_pct"),
                        "volume": quote.get("volume"),
                        "market_cap": quote.get("market_cap"),
                        "short_name": quote.get("short_name"),
                        "discovery_score": 0,
                    },
                )
                if config["label"] not in meta["tags"]:
                    meta["tags"].append(config["label"])
                    meta["discovery_score"] += self.tag_weights.get(config["label"], 1)
                for key in ("raw_symbol", "exchange", "price", "change_pct", "volume", "market_cap", "short_name"):
                    if meta.get(key) in (None, "", 0):
                        meta[key] = quote.get(key)

        ranked_symbols = sorted(
            symbol_meta,
            key=lambda symbol: (
                -self._as_float(symbol_meta[symbol].get("discovery_score")),
                -self._as_float(symbol_meta[symbol].get("volume")),
                -abs(self._as_float(symbol_meta[symbol].get("change_pct"))),
                -self._as_float(symbol_meta[symbol].get("market_cap")),
                symbol,
            ),
        )[: self.max_symbols]

        invalid_symbols = set(self.settings.invalid_symbols)
        ranked_symbols = [symbol for symbol in ranked_symbols if symbol not in invalid_symbols]
        configured_symbols = self._configured_symbols()
        ranked_symbols = list(dict.fromkeys(configured_symbols + ranked_symbols))[: self.max_symbols]

        ranked_meta = {symbol: symbol_meta[symbol] for symbol in ranked_symbols if symbol in symbol_meta}
        for symbol in configured_symbols:
            ranked_meta.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "raw_symbol": f"{symbol}.NS",
                    "exchange": "NSI",
                    "tags": ["configured_universe"],
                    "price": None,
                    "change_pct": None,
                    "volume": None,
                    "market_cap": None,
                    "short_name": symbol,
                    "discovery_score": 1,
                },
            )
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_mode": "yahoo_dynamic" if ranked_symbols else "yahoo_unavailable",
            "note": (
                "Universe is built only from live Yahoo screeners with liquidity filters. "
                "No hardcoded stock fallback is used."
            ),
            "symbols": ranked_symbols,
            "scan_symbols": ranked_symbols[: self.max_scan_symbols],
            "opportunity_symbols": ranked_symbols[: self.max_opportunity_symbols],
            "symbol_meta": ranked_meta,
            "filters": {
                "universe_size": self.max_symbols,
                "universe_groups": list(self.settings.universe_groups),
                "min_price": self.settings.min_price,
                "min_volume": self.settings.min_volume,
                "min_market_cap": self.settings.min_market_cap,
                "include_sme": self.settings.include_sme,
                "include_indices": self.settings.include_indices,
                "scan_interval_sec": self.settings.scan_interval_sec,
            },
            "indices": [self.settings.benchmark_symbol] if self.settings.include_indices else [],
            "bucket_counts": {
                bucket["label"]: len(bucket.get("symbols", []))
                for bucket in buckets
            },
            "buckets": [
                {
                    "label": bucket["label"],
                    "count": len(bucket.get("symbols", [])),
                    "symbols": bucket.get("symbols", [])[:20],
                    "error": bucket.get("error"),
                }
                for bucket in buckets
            ],
        }
        self._cache_set(cache_key, payload)
        return payload
