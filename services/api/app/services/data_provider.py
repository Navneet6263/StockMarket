from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Dict, Iterable

import pandas as pd
import yfinance as yf

from app.core.cache import TTLCache
from app.core.settings import Settings


logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.history_cache: TTLCache[pd.DataFrame] = TTLCache(settings.history_cache_ttl_sec)
        self.quote_cache: TTLCache[Dict] = TTLCache(max(20, settings.detail_cache_ttl_sec // 2))
        self.symbol_map = {
            "NIFTY": "^NSEI",
            "NIFTY50": "^NSEI",
            "BANKNIFTY": "^NSEBANK",
            "SENSEX": "^BSESN",
        }

    def resolve_symbol(self, symbol: str) -> str:
        clean = (symbol or "").upper().replace(" ", "")
        if not clean:
            return "^NSEI"
        if clean in self.symbol_map:
            return self.symbol_map[clean]
        if clean.startswith("^") or "." in clean:
            return clean
        return f"{clean}.NS"

    def clean_symbol(self, symbol: str) -> str:
        clean = (symbol or "").upper().replace(" ", "")
        if clean.endswith(".NS"):
            return clean[:-3]
        return clean

    def _normalize_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()
        normalized = frame.copy()
        normalized.columns = [str(column).title() for column in normalized.columns]
        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(set(normalized.columns)):
            return pd.DataFrame()
        normalized = normalized.dropna(subset=["Close"]).copy()
        if getattr(normalized.index, "tz", None) is not None:
            normalized.index = normalized.index.tz_convert(None)
        return normalized

    def fetch_history(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        clean = self.clean_symbol(symbol)
        if clean in set(self.settings.invalid_symbols):
            logger.info("skipped invalid symbol=%s period=%s interval=%s", clean, period, interval)
            return pd.DataFrame()
        resolved = self.resolve_symbol(symbol)
        cache_key = f"{resolved}:{period}:{interval}"
        cached = self.history_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        try:
            history = yf.Ticker(resolved).history(
                period=period,
                interval=interval,
                auto_adjust=False,
                timeout=self.settings.yahoo_timeout_sec,
            )
        except TypeError:
            history = yf.Ticker(resolved).history(period=period, interval=interval, auto_adjust=False)
        except Exception as exc:
            logger.warning("history fetch failed symbol=%s period=%s interval=%s error=%s", clean, period, interval, exc)
            return pd.DataFrame()
        frame = self._normalize_frame(history)
        if not frame.empty:
            self.history_cache.set(cache_key, frame)
        else:
            logger.info("skipped no-data symbol=%s period=%s interval=%s", clean, period, interval)
        return frame.copy()

    def fetch_batch_history(
        self,
        symbols: Iterable[str],
        period: str = "1y",
        interval: str = "1d",
        chunk_size: int = 20,
    ) -> dict[str, pd.DataFrame]:
        invalid_symbols = set(self.settings.invalid_symbols)
        resolved_map = {
            self.clean_symbol(symbol): self.resolve_symbol(symbol)
            for symbol in dict.fromkeys(symbols)
            if symbol and self.clean_symbol(symbol) not in invalid_symbols
        }
        results: dict[str, pd.DataFrame] = {}
        pending: list[tuple[str, str]] = []

        for clean, resolved in resolved_map.items():
            cache_key = f"{resolved}:{period}:{interval}"
            cached = self.history_cache.get(cache_key)
            if cached is not None:
                results[clean] = cached.copy()
            else:
                pending.append((clean, resolved))

        effective_chunk_size = max(1, min(chunk_size, self.settings.yahoo_batch_chunk_size))
        for offset in range(0, len(pending), effective_chunk_size):
            batch = pending[offset : offset + effective_chunk_size]
            if not batch:
                continue
            joined = " ".join(resolved for _, resolved in batch)
            try:
                try:
                    downloaded = yf.download(
                        tickers=joined,
                        period=period,
                        interval=interval,
                        group_by="ticker",
                        auto_adjust=False,
                        progress=False,
                        threads=True,
                        timeout=self.settings.yahoo_timeout_sec,
                    )
                except TypeError:
                    downloaded = yf.download(
                        tickers=joined,
                        period=period,
                        interval=interval,
                        group_by="ticker",
                        auto_adjust=False,
                        progress=False,
                        threads=True,
                    )
            except Exception:
                logger.warning(
                    "batch history fetch failed symbols=%s period=%s interval=%s",
                    ",".join(clean for clean, _ in batch),
                    period,
                    interval,
                    exc_info=True,
                )
                downloaded = pd.DataFrame()

            for clean, resolved in batch:
                try:
                    if isinstance(downloaded.columns, pd.MultiIndex):
                        frame = downloaded[resolved].copy()
                    else:
                        frame = downloaded.copy()
                except Exception:
                    frame = pd.DataFrame()

                normalized = self._normalize_frame(frame)
                if normalized.empty:
                    logger.info("skipped no-data symbol=%s period=%s interval=%s", clean, period, interval)
                    continue
                self.history_cache.set(f"{resolved}:{period}:{interval}", normalized)
                results[clean] = normalized.copy()

        return results

    def fetch_live_snapshot(self, symbol: str) -> Dict:
        clean = self.clean_symbol(symbol)
        resolved = self.resolve_symbol(symbol)
        cache_key = f"quote:{resolved}"
        cached = self.quote_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        ticker = yf.Ticker(resolved)
        history = self.fetch_history(clean, period="5d", interval="1d")
        if history.empty:
            raise ValueError(f"No live data available for {clean}")

        latest_close = float(history["Close"].iloc[-1])
        previous_close = float(history["Close"].iloc[-2]) if len(history) > 1 else latest_close
        volume = int(history["Volume"].iloc[-1])

        fast_info = {}
        try:
            fast_info = dict(getattr(ticker, "fast_info", {}) or {})
        except Exception:
            fast_info = {}

        price = float(
            fast_info.get("lastPrice")
            or fast_info.get("last_price")
            or fast_info.get("regularMarketPrice")
            or latest_close
        )
        volume = int(fast_info.get("lastVolume") or fast_info.get("last_volume") or volume)
        change = price - previous_close
        change_pct = (change / previous_close) * 100 if previous_close else 0.0

        snapshot = {
            "symbol": clean,
            "resolved_symbol": resolved,
            "price": round(price, 4),
            "previous_close": round(previous_close, 4),
            "change": round(change, 4),
            "change_percent": round(change_pct, 4),
            "volume": volume,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.quote_cache.set(cache_key, snapshot)
        return dict(snapshot)

    def overlay_quote(self, frame: pd.DataFrame, quote: Dict | None) -> pd.DataFrame:
        if frame.empty or not quote:
            return frame.copy()
        live_frame = frame.copy()
        last_index = live_frame.index[-1]

        raw_price = quote.get("price")
        if raw_price is None:
            raw_price = live_frame.at[last_index, "Close"]
        if raw_price is None:
            return live_frame

        raw_volume = quote.get("volume")
        if raw_volume is None:
            raw_volume = live_frame.at[last_index, "Volume"]

        try:
            price = float(raw_price)
            volume = int(raw_volume or 0)
        except Exception:
            return live_frame

        live_frame.at[last_index, "Close"] = price
        live_frame.at[last_index, "High"] = max(float(live_frame.at[last_index, "High"]), price)
        live_frame.at[last_index, "Low"] = min(float(live_frame.at[last_index, "Low"]), price)
        live_frame.at[last_index, "Volume"] = max(int(live_frame.at[last_index, "Volume"]), volume)
        return live_frame

    def serialize_candles(self, frame: pd.DataFrame, limit: int | None = None) -> list[Dict]:
        if frame.empty:
            return []
        limited = frame.tail(limit or self.settings.chart_limit)
        payload = []
        for idx, row in limited.iterrows():
            timestamp = idx.to_pydatetime().replace(tzinfo=timezone.utc) if hasattr(idx, "to_pydatetime") else idx
            payload.append(
                {
                    "time": int(timestamp.timestamp()),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]),
                }
            )
        return payload
