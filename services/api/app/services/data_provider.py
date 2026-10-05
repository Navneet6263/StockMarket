from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import Dict, Iterable

import pandas as pd
import yfinance as yf

from app.core.cache import TTLCache
from app.core.settings import Settings
from app.services.broker_adapter import get_broker_adapter


logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.broker = get_broker_adapter()
        self.provider_name = self.broker.__class__.__name__.replace("Adapter", "").lower()
        self.use_broker_history = os.getenv("USE_BROKER_HISTORY", "false").strip().lower() in {"1", "true", "yes", "on"}
        self.history_cache: TTLCache[pd.DataFrame] = TTLCache(settings.history_cache_ttl_sec)
        self.quote_cache: TTLCache[Dict] = TTLCache(max(5, settings.quote_cache_ttl_sec))
        self.symbol_map = {
            "NIFTY": "^NSEI",
            "NIFTY50": "^NSEI",
            "BANKNIFTY": "^NSEBANK",
            "SENSEX": "^BSESN",
        }
        self.cache_dir = Path(__file__).resolve().parents[3] / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_parquet_path(self, resolved_symbol: str, period: str, interval: str) -> Path:
        safe_sym = resolved_symbol.replace("^", "").replace(".", "_")
        return self.cache_dir / f"{safe_sym}_{period}_{interval}.parquet"

    def _load_from_parquet(
        self, path: Path, interval: str = "1d", *, now: datetime | None = None,
    ) -> pd.DataFrame | None:
        """Reuse daily history as before, but never recycle stale intraday bars.

        Intraday parquet indexes use this provider's UTC-naive convention. During
        a regular NSE session both the file age and the most recent bar matter:
        re-reading an old file must not renew its RAM cache lifetime indefinitely.
        Fully completed sessions may be reused off-hours without extra downloads.
        No exchange holiday calendar is inferred here; live entry gates remain
        responsible for current-session/closed-candle validation.
        """
        if not path.exists():
            return None
        try:
            intraday_minutes = {"1m": 1, "2m": 2, "3m": 3, "5m": 5, "10m": 10,
                                "15m": 15, "30m": 30, "60m": 60, "1h": 60, "90m": 90}.get(str(interval).lower())
            if intraday_minutes is not None:
                clock = pd.Timestamp(now if now is not None else datetime.now(timezone.utc))
                clock = clock.tz_localize("UTC") if clock.tz is None else clock.tz_convert("UTC")
                file_time = pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC")
                age_seconds = (clock - file_time).total_seconds()
                if age_seconds < -5:
                    return None
                local_clock = clock.tz_convert("Asia/Kolkata")
                minute_of_day = local_clock.hour * 60 + local_clock.minute
                session_active = local_clock.dayofweek < 5 and 555 <= minute_of_day < 930
                ttl = max(0, self._history_ttl(interval))
                if session_active and age_seconds >= ttl:
                    return None
            df = pd.read_parquet(path)
            if df.empty:
                return None
            if intraday_minutes is not None:
                if not isinstance(df.index, pd.DatetimeIndex) or df.index.hasnans:
                    return None
                last_bar = pd.Timestamp(df.index[-1])
                last_bar = last_bar.tz_localize("UTC") if last_bar.tz is None else last_bar.tz_convert("UTC")
                if last_bar > clock + pd.Timedelta(seconds=5):
                    return None
                local_bar = last_bar.tz_convert("Asia/Kolkata")
                bar_minute = local_bar.hour * 60 + local_bar.minute
                if local_bar.dayofweek >= 5 or not 555 <= bar_minute < 930:
                    return None
                bar_session_close = local_bar.normalize() + pd.Timedelta(hours=15, minutes=30)
                bar_close = min(local_bar + pd.Timedelta(minutes=intraday_minutes), bar_session_close)
                if session_active:
                    if local_bar.date() != local_clock.date():
                        return None
                    if (local_clock - bar_close).total_seconds() > max(intraday_minutes * 180, ttl):
                        return None
                else:
                    expected_day = local_clock.normalize()
                    if local_clock.dayofweek >= 5 or minute_of_day < 555:
                        expected_day -= pd.offsets.BDay(1)
                    expected_close = expected_day + pd.Timedelta(hours=15, minutes=30)
                    completed_session = bar_close == expected_close and file_time >= expected_close
                    if not completed_session and age_seconds >= ttl:
                        return None
                return df
            import numpy as np
            last_date = df.index[-1].date()
            today = now.date() if now is not None else datetime.now().date()
            bus_days_diff = np.busday_count(last_date, today)
            if bus_days_diff > 1:
                return None
            return df
        except Exception as e:
            logger.warning("Corrupted parquet cache %s: %s", path, e)
            return None

    def _save_to_parquet(self, df: pd.DataFrame, path: Path):
        try:
            df.to_parquet(path)
        except Exception as e:
            logger.warning("Failed to save parquet cache %s: %s", path, e)

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

    def _normalize_frame(self, frame: pd.DataFrame, interval: str = "1d") -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()
        normalized = frame.copy()
        normalized.columns = [str(column).title() for column in normalized.columns]
        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(set(normalized.columns)):
            return pd.DataFrame()
        normalized = normalized.dropna(subset=["Close"]).copy()
        if getattr(normalized.index, "tz", None) is not None:
            if str(interval).lower() in {"1d", "1wk", "1mo", "3mo"}:
                # Daily bars represent an exchange session date. Converting an
                # IST midnight to UTC first shifts it to the previous calendar
                # day and breaks the official NSE delivery-data join.
                normalized.index = normalized.index.tz_convert("Asia/Kolkata").tz_localize(None)
            else:
                # Intraday timestamps remain a UTC-normalised timeline.
                normalized.index = normalized.index.tz_convert("UTC").tz_localize(None)
        return normalized

    def fetch_history(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        clean = self.clean_symbol(symbol)
        if clean in set(self.settings.invalid_symbols):
            logger.info("skipped invalid symbol=%s", clean)
            return pd.DataFrame()
        resolved = self.resolve_symbol(symbol)
        cache_key = f"{resolved}:{period}:{interval}"
        cached = self.history_cache.get(cache_key)
        if cached is not None:
            return cached.copy()
            
        pq_path = self._get_parquet_path(resolved, period, interval)
        pq_df = self._load_from_parquet(pq_path, interval=interval)
        if pq_df is not None:
            self.history_cache.set(cache_key, pq_df, ttl_seconds=self._history_ttl(interval))
            return pq_df.copy()

        # Try broker adapter first (AngelOne when configured)
        if self.use_broker_history and self.broker.is_available() and self.provider_name != "yfinance":
            frame = self.broker.fetch_history(clean, period, interval)
            if not frame.empty:
                normalized = self._normalize_frame(frame, interval=interval)
                if not normalized.empty:
                    self.history_cache.set(cache_key, normalized, ttl_seconds=self._history_ttl(interval))
                    return normalized.copy()

        # Fallback: yfinance
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
            logger.warning("history fetch failed symbol=%s error=%s", clean, exc)
            return pd.DataFrame()
        frame = self._normalize_frame(history, interval=interval)
        if not frame.empty:
            self.history_cache.set(cache_key, frame, ttl_seconds=self._history_ttl(interval))
            self._save_to_parquet(frame, pq_path)
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
                pq_path = self._get_parquet_path(resolved, period, interval)
                pq_df = self._load_from_parquet(pq_path, interval=interval)
                if pq_df is not None:
                    self.history_cache.set(cache_key, pq_df, ttl_seconds=self._history_ttl(interval))
                    results[clean] = pq_df.copy()
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

                normalized = self._normalize_frame(frame, interval=interval)
                if normalized.empty:
                    logger.info("skipped no-data symbol=%s period=%s interval=%s", clean, period, interval)
                    continue
                self.history_cache.set(f"{resolved}:{period}:{interval}", normalized, ttl_seconds=self._history_ttl(interval))
                self._save_to_parquet(normalized, self._get_parquet_path(resolved, period, interval))
                results[clean] = normalized.copy()

        return results

    def fetch_live_snapshot(self, symbol: str) -> Dict:
        clean = self.clean_symbol(symbol)
        resolved = self.resolve_symbol(symbol)
        cache_key = f"quote:{resolved}"
        cached = self.quote_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        broker_quote: Dict = {}
        if self.broker.is_available() and self.provider_name != "yfinance":
            try:
                broker_quote = self.broker.fetch_live_quote(clean) or {}
            except Exception:
                logger.warning("broker live quote failed symbol=%s", clean, exc_info=True)

        history = self.fetch_history(clean, period="5d", interval="1d")
        if history.empty and not broker_quote.get("price"):
            raise ValueError(f"No live data available for {clean}")

        broker_price = float(broker_quote.get("price") or 0)
        latest_close = float(history["Close"].iloc[-1]) if not history.empty else broker_price
        previous_close = (
            float(history["Close"].iloc[-2])
            if not history.empty and len(history) > 1
            else float(broker_quote.get("previous_close") or broker_quote.get("close") or latest_close)
        )
        volume = int(history["Volume"].iloc[-1]) if not history.empty else int(broker_quote.get("volume") or 0)

        if broker_quote.get("price"):
            price = float(broker_quote.get("price") or latest_close)
            volume = int(broker_quote.get("volume") or volume)
            data_provider = str(broker_quote.get("data_provider") or self.provider_name)
            timestamp = str(broker_quote.get("timestamp") or datetime.now(timezone.utc).isoformat())
        else:
            ticker = yf.Ticker(resolved)
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
            data_provider = "yfinance"
            timestamp = datetime.now(timezone.utc).isoformat()

        change = price - previous_close
        change_pct = (change / previous_close) * 100 if previous_close else 0.0

        snapshot = {
            "symbol": clean,
            "resolved_symbol": resolved,
            "data_provider": data_provider,
            "price": round(price, 4),
            "previous_close": round(previous_close, 4),
            "change": round(change, 4),
            "change_percent": round(change_pct, 4),
            "volume": volume,
            "timestamp": timestamp,
        }
        self.quote_cache.set(cache_key, snapshot)
        return dict(snapshot)

    def _history_ttl(self, interval: str) -> int:
        return self.settings.intraday_cache_ttl_sec if interval and interval != "1d" else self.settings.history_cache_ttl_sec

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
