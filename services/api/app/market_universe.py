from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import yfinance as yf
from yfinance import EquityQuery

from app.core.settings import Settings, get_settings
from app.market_universe_config import UNIVERSE_CONFIG, get_full_nse_universe

# ── Exchange constants ─────────────────────────────────────────────────────────
_YF_NSE = "NSI"   # Yahoo Finance exchange code for NSE
_YF_BSE = "BSE"   # Yahoo Finance exchange code for BSE

# Suffix used for raw_symbol when building configured-universe fallbacks
_NSE_SUFFIX = ".NS"
_BSE_SUFFIX = ".BO"

# AngelOne exchange segment identifiers
_AO_EXCHANGE_NSE = "NSE"
_AO_EXCHANGE_BSE = "BSE"

# Yahoo Finance quoteType for common equity
_EQUITY_QUOTE_TYPE = "EQUITY"

# Tokens in company short-names or symbols that indicate non-stock instruments
_BLOCKED_NAME_TOKENS = (
    "ETF", "BEES", "INVIT", "LIQUID", "GOLD", "SILV",
    "INDEX", "MUTUAL", "FUND",
)
_BLOCKED_SYMBOL_TOKENS = ("BEES", "INVIT")


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
            "top_gainers": 3,
            "volume_shockers": 3,
            "breakout_candidates": 3,
            "selling_pressure": 2,
            "low_price_active": 2,
            "fundamental_growth": 4,
            "quality_compounders": 4,
            "value_growth": 3,
            "turnaround_watch": 2,
            # BSE-specific buckets get same weights
            "bse_most_active": 2,
            "bse_top_gainers": 3,
            "bse_volume_shockers": 3,
            "bse_breakout_candidates": 3,
        }

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _cache_get(self, key: str) -> Optional[Dict]:
        cached = self.cache.get(key)
        if cached and datetime.now(timezone.utc) - cached[0] < self.cache_ttl:
            return cached[1]
        return None

    def _cache_set(self, key: str, payload: Dict):
        self.cache[key] = (datetime.now(timezone.utc), payload)

    # ── Symbol normalization ──────────────────────────────────────────────────

    def _normalize_symbol(self, symbol: str) -> str:
        """Strip exchange suffix (.NS / .BO) and apply known aliases."""
        clean = (symbol or "").upper().strip()
        if "." in clean:
            clean = clean.split(".")[0]
        aliases = {
            "GARDENREACH": "GRSE",
            "GARDENREACHSHIPBUILDERS": "GRSE",
            "GARDENREACHSHIP": "GRSE",
            "GREENPLYINDUSTRIES": "GREENPLY",
            "GREENPLYIND": "GREENPLY",
        }
        return aliases.get(clean, clean)

    def _configured_symbols(self) -> list[str]:
        group_map = {
            "NIFTY50": UNIVERSE_CONFIG.nifty_50,
            "NIFTY_50": UNIVERSE_CONFIG.nifty_50,
            "NIFTY100": UNIVERSE_CONFIG.nifty_100,
            "NIFTY_100": UNIVERSE_CONFIG.nifty_100,
            "NIFTY200": UNIVERSE_CONFIG.nifty_200,
            "NIFTY_200": UNIVERSE_CONFIG.nifty_200,
            "NIFTY500": UNIVERSE_CONFIG.nifty_200,
            "NIFTY_500": UNIVERSE_CONFIG.nifty_200,
            "ALL_NSE": get_full_nse_universe(),
            "FNO": UNIVERSE_CONFIG.fno_stocks,
            "F&O": UNIVERSE_CONFIG.fno_stocks,
        }
        configured: list[str] = []
        invalid_symbols = set(self.settings.invalid_symbols)
        for group in self.settings.universe_groups:
            configured.extend(group_map.get(group.upper(), ()))
        # If AngelOne is active and no specific group set, use full NSE universe
        import os
        if os.getenv("BROKER_PROVIDER", "yfinance").lower() == "angelone" and not configured:
            configured = list(get_full_nse_universe())
        configured.extend(self.settings.custom_universe)
        return list(
            dict.fromkeys(
                self._normalize_symbol(symbol)
                for symbol in configured
                if symbol and self._normalize_symbol(symbol) not in invalid_symbols
            )
        )

    # ── Stock classification ──────────────────────────────────────────────────

    @staticmethod
    def _as_float(value) -> float:
        if isinstance(value, bool) or value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        return 0.0

    def _is_common_stock(self, quote: Dict) -> bool:
        """Return True only for real NSE or BSE equity listings (no ETFs/funds)."""
        raw_symbol = (quote.get("symbol") or "").upper()
        short_name = ((quote.get("shortName") or quote.get("longName") or "")).upper()
        exchange = (quote.get("exchange") or "").upper()

        # Accept .NS (NSE) and .BO (BSE) symbols only
        if not (raw_symbol.endswith(_NSE_SUFFIX) or raw_symbol.endswith(_BSE_SUFFIX)):
            return False
        if quote.get("quoteType") != _EQUITY_QUOTE_TYPE:
            return False
        if any(token in short_name for token in _BLOCKED_NAME_TOKENS):
            return False
        if any(token in raw_symbol for token in _BLOCKED_SYMBOL_TOKENS):
            return False
        return True

    def _passes_liquidity_filters(self, quote: Dict) -> bool:
        price = self._as_float(quote.get("regularMarketPrice") or quote.get("intradayprice"))
        volume = self._as_float(quote.get("regularMarketVolume") or quote.get("dayvolume"))
        market_cap = self._as_float(quote.get("marketCap") or quote.get("intradaymarketcap"))
        short_name = ((quote.get("shortName") or quote.get("longName") or "")).upper()
        exchange = (quote.get("exchange") or "").upper()

        if price < self.settings.min_price:
            return False

        # BSE stocks can have lower liquidity — use a separate (lower) threshold
        effective_min_volume = (
            self.settings.bse_min_volume
            if exchange == _YF_BSE
            else self.settings.min_volume
        )
        if volume < effective_min_volume:
            return False
        if self.settings.min_market_cap and market_cap and market_cap < self.settings.min_market_cap:
            return False
        if not self.settings.include_sme and "SME" in short_name:
            return False
        return True

    def _snapshot_quote(self, quote: Dict) -> Dict:
        raw_symbol = quote.get("symbol", "")
        exchange = (quote.get("exchange") or "").upper()
        return {
            "symbol": self._normalize_symbol(raw_symbol),
            "raw_symbol": raw_symbol,
            "exchange": exchange,
            # Canonical exchange tag used by broker adapter
            "exchange_segment": _AO_EXCHANGE_BSE if exchange == _YF_BSE else _AO_EXCHANGE_NSE,
            "price": quote.get("regularMarketPrice") or quote.get("intradayprice"),
            "change_pct": quote.get("regularMarketChangePercent") or quote.get("percentchange"),
            "volume": quote.get("regularMarketVolume") or quote.get("dayvolume"),
            "market_cap": quote.get("marketCap") or quote.get("intradaymarketcap"),
            "short_name": quote.get("shortName") or quote.get("longName"),
        }

    # ── Query builders ────────────────────────────────────────────────────────

    def _exchange_filter(self, *filters: EquityQuery) -> EquityQuery:
        """Combined NSE + BSE filter (OR) so a single query covers both exchanges."""
        nse = EquityQuery("eq", ["exchange", _YF_NSE])
        bse = EquityQuery("eq", ["exchange", _YF_BSE])
        return EquityQuery("and", [EquityQuery("or", [nse, bse]), *filters])

    def _nse_query(self, *filters: EquityQuery) -> EquityQuery:
        """NSE-only query (kept for backward compatibility)."""
        return EquityQuery("and", [EquityQuery("eq", ["exchange", _YF_NSE]), *filters])

    def _bse_query(self, *filters: EquityQuery) -> EquityQuery:
        """BSE-only query."""
        return EquityQuery("and", [EquityQuery("eq", ["exchange", _YF_BSE]), *filters])

    # ── Screen query engine ───────────────────────────────────────────────────

    def _screen_query(
        self,
        label: str,
        query: EquityQuery,
        *,
        sort_field: str,
        sort_asc: bool = False,
        max_pages: int = 3,
        # Symbols already confirmed as NSE listings; BSE duplicate should be dropped
        nse_preferred: set[str] | None = None,
    ) -> Dict:
        quotes: List[Dict] = []
        seen = set()  # normalised symbols already added in this bucket

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
                norm_symbol = snapshot["symbol"]
                if not norm_symbol or norm_symbol in seen:
                    continue
                # If this is a BSE result and we already have the NSE listing, skip it
                if snapshot["exchange"] == _YF_BSE and nse_preferred and norm_symbol in nse_preferred:
                    continue
                seen.add(norm_symbol)
                quotes.append(snapshot)
                page_added += 1

            if len(raw_quotes) < self.page_size:
                break
            if page_added == 0 and page >= 1:
                break

        return {"label": label, "quotes": quotes, "symbols": [item["symbol"] for item in quotes]}

    # ── Screen definitions ────────────────────────────────────────────────────

    def _screen_definitions(self) -> List[Dict]:
        p = self.settings.min_price
        v = self.settings.min_volume
        bv = self.settings.bse_min_volume
        return [
            # ── NSE screens ──────────────────────────────────────────────────
            {
                "label": "most_active",
                "query": self._nse_query(
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                    EquityQuery("gt", ["dayvolume", max(50000, v // 2)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 4,
                "exchange": _YF_NSE,
            },
            {
                "label": "top_gainers",
                "query": self._nse_query(
                    EquityQuery("gt", ["percentchange", 3.0]),
                    EquityQuery("gt", ["dayvolume", max(50000, v // 2)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "percentchange",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_NSE,
            },
            {
                "label": "volume_shockers",
                "query": self._nse_query(
                    EquityQuery("gt", ["dayvolume", max(150000, v)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 4,
                "exchange": _YF_NSE,
            },
            {
                "label": "breakout_candidates",
                "query": self._nse_query(
                    EquityQuery("gt", ["percentchange", 1.5]),
                    EquityQuery("gt", ["dayvolume", max(50000, v // 2)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "percentchange",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_NSE,
            },
            {
                "label": "selling_pressure",
                "query": self._nse_query(
                    EquityQuery("lt", ["percentchange", -1.8]),
                    EquityQuery("gt", ["dayvolume", max(50000, v // 2)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "percentchange",
                "sort_asc": True,
                "max_pages": 3,
                "exchange": _YF_NSE,
            },
            {
                "label": "low_price_active",
                "query": self._nse_query(
                    EquityQuery("btwn", ["intradayprice", max(1, p), 120]),
                    EquityQuery("gt", ["dayvolume", max(80000, v)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_NSE,
            },
            {
                "label": "fundamental_growth",
                "query": self._nse_query(
                    EquityQuery("gte", ["quarterlyrevenuegrowth.quarterly", 5]),
                    EquityQuery("gte", ["epsgrowth.lasttwelvemonths", 5]),
                    EquityQuery("gt", ["dayvolume", max(30000, v // 3)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_NSE,
            },
            {
                "label": "quality_compounders",
                "query": self._nse_query(
                    EquityQuery("gte", ["returnonequity.lasttwelvemonths", 12]),
                    EquityQuery("gte", ["netincomemargin.lasttwelvemonths", 8]),
                    EquityQuery("lt", ["totaldebtequity.lasttwelvemonths", 120]),
                    EquityQuery("gt", ["dayvolume", max(30000, v // 3)]),
                    EquityQuery("gte", ["intradayprice", max(20, p)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_NSE,
            },
            {
                "label": "value_growth",
                "query": self._nse_query(
                    EquityQuery("btwn", ["peratio.lasttwelvemonths", 0, 35]),
                    EquityQuery("gte", ["epsgrowth.lasttwelvemonths", 5]),
                    EquityQuery("gt", ["dayvolume", max(30000, v // 3)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_NSE,
            },
            {
                "label": "turnaround_watch",
                "query": self._nse_query(
                    EquityQuery("btwn", ["percentchange", -8, 3]),
                    EquityQuery("gte", ["quarterlyrevenuegrowth.quarterly", 0]),
                    EquityQuery("gt", ["dayvolume", max(50000, v // 2)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_NSE,
            },
            # ── BSE-exclusive screens (lower volume thresholds) ───────────────
            {
                "label": "bse_most_active",
                "query": self._bse_query(
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                    EquityQuery("gt", ["dayvolume", max(10000, bv // 2)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 4,
                "exchange": _YF_BSE,
            },
            {
                "label": "bse_top_gainers",
                "query": self._bse_query(
                    EquityQuery("gt", ["percentchange", 3.0]),
                    EquityQuery("gt", ["dayvolume", max(5000, bv // 4)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "percentchange",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_BSE,
            },
            {
                "label": "bse_volume_shockers",
                "query": self._bse_query(
                    EquityQuery("gt", ["dayvolume", max(20000, bv)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "dayvolume",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_BSE,
            },
            {
                "label": "bse_breakout_candidates",
                "query": self._bse_query(
                    EquityQuery("gt", ["percentchange", 1.5]),
                    EquityQuery("gt", ["dayvolume", max(5000, bv // 4)]),
                    EquityQuery("gte", ["intradayprice", max(1, p)]),
                ),
                "sort_field": "percentchange",
                "sort_asc": False,
                "max_pages": 3,
                "exchange": _YF_BSE,
            },
        ]

    # ── Market discovery ──────────────────────────────────────────────────────

    def discover_market(self, force_refresh: bool = False) -> Dict:
        cache_key = "yahoo_dynamic_nse_bse_market"
        if not force_refresh:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached

        buckets: List[Dict] = []
        symbol_meta: Dict[str, Dict] = {}

        # Pass 1: run all NSE screens first and build a set of confirmed NSE symbols
        # so that subsequent BSE screens can skip duplicates.
        nse_symbols_found: set[str] = set()

        for config in self._screen_definitions():
            is_bse_screen = config.get("exchange") == _YF_BSE
            bucket = self._screen_query(
                config["label"],
                config["query"],
                sort_field=config["sort_field"],
                sort_asc=config["sort_asc"],
                max_pages=config["max_pages"],
                # For BSE screens pass already-seen NSE symbols to deduplicate
                nse_preferred=nse_symbols_found if is_bse_screen else None,
            )
            buckets.append(bucket)

            for quote in bucket.get("quotes", []):
                symbol = quote["symbol"]
                exchange = quote.get("exchange", _YF_NSE)

                # Track NSE symbols so BSE screens can skip them
                if exchange == _YF_NSE:
                    nse_symbols_found.add(symbol)

                meta = symbol_meta.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "raw_symbol": quote.get("raw_symbol"),
                        "exchange": exchange,
                        "exchange_segment": quote.get("exchange_segment", _AO_EXCHANGE_NSE),
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
                for key in ("raw_symbol", "exchange", "exchange_segment", "price", "change_pct", "volume", "market_cap", "short_name"):
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
                    "raw_symbol": f"{symbol}{_NSE_SUFFIX}",
                    "exchange": _YF_NSE,
                    "exchange_segment": _AO_EXCHANGE_NSE,
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
            "source_mode": "yahoo_dynamic_nse_bse" if ranked_symbols else "yahoo_unavailable",
            "note": (
                "Universe is built from live Yahoo screeners for both NSE and BSE with liquidity filters. "
                "When a stock is listed on both exchanges, the NSE listing is preferred to avoid duplicates."
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
                "bse_min_volume": self.settings.bse_min_volume,
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
