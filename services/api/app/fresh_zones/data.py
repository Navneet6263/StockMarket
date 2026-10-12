"""Market-data access and universe filtering for the fresh-zone scanner.

The scanner never labels data as live unless the owner has confirmed a live
feed and its permitted usage (``FRESH_ZONE_LIVE_CONFIRMED=true`` together with
a broker quote source).  Without that, quotes are DELAYED and the scanner can
also run purely END_OF_DAY.

Provider status (to be confirmed by the owner before production use):
  * Yahoo Finance (``yfinance``): free, unofficial, daily history back many
    years; OHLC is split/bonus adjusted with ``auto_adjust=False`` but not
    dividend adjusted; quotes are delayed and their exchange timestamp is not
    guaranteed; rate limits are undocumented.
  * AngelOne SmartAPI (already integrated in this repository): exchange LTP
    and intraday candles; licence terms and rate limits must be confirmed.
"""

from __future__ import annotations

import csv
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import pandas as pd

from app.fresh_zones.models import DataMode, DataQuality
from app.fresh_zones.params import ScannerParams
from app.fresh_zones.timeframes import IST, to_ist


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    at: datetime                 # timestamp the price refers to (aware)
    quality: DataQuality
    session_open: float | None = None
    timestamp_is_retrieval_time: bool = False


class MarketDataProvider(Protocol):
    name: str

    def daily_history(self, symbol: str) -> pd.DataFrame: ...

    def quote(self, symbol: str) -> Quote | None: ...

    def intraday_candles(self, symbol: str, minutes: int, since: datetime | None) -> pd.DataFrame:
        """Completed intraday candles (aware start-time index, OHLC columns)."""
        ...

    def quality(self) -> DataQuality: ...


def _only_completed(frame: pd.DataFrame, minutes: int, now: datetime, since: datetime | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    data = frame.copy()
    data.columns = [str(c).lower() for c in data.columns]
    index = pd.DatetimeIndex(data.index)
    index = index.tz_localize("UTC") if index.tz is None else index
    data.index = index.tz_convert(IST)
    ends = data.index + pd.Timedelta(minutes=minutes)
    data = data[ends <= pd.Timestamp(now)]
    if since is not None:
        data = data[data.index + pd.Timedelta(minutes=minutes) > pd.Timestamp(since)]
    return data[["open", "high", "low", "close"]]


class YahooFinanceProvider:
    """Daily history, delayed quotes and 15-minute candles from Yahoo."""

    name = "yfinance"

    def __init__(self, history_period: str = "max", timeout: int = 20):
        import yfinance  # noqa: F401  (fail early if unavailable)

        self.history_period = history_period
        self.timeout = timeout

    @staticmethod
    def _ticker(symbol: str) -> str:
        return symbol if "." in symbol or symbol.startswith("^") else f"{symbol}.NS"

    def quality(self) -> DataQuality:
        return DataQuality(
            source="yfinance",
            mode=DataMode.DELAYED,
            delay_seconds=None,
            limitations=(
                "unofficial source; delay not guaranteed",
                "daily OHLC split/bonus adjusted, not dividend adjusted",
            ),
        )

    def daily_history(self, symbol: str) -> pd.DataFrame:
        import yfinance as yf

        try:
            frame = yf.Ticker(self._ticker(symbol)).history(
                period=self.history_period, interval="1d", auto_adjust=False, timeout=self.timeout
            )
        except Exception as exc:  # network / provider errors are data gaps, not crashes
            logger.warning("fresh-zones daily history failed symbol=%s error=%s", symbol, exc)
            return pd.DataFrame()
        return frame

    def quote(self, symbol: str) -> Quote | None:
        import yfinance as yf

        try:
            ticker = yf.Ticker(self._ticker(symbol))
            info = dict(getattr(ticker, "fast_info", {}) or {})
        except Exception as exc:
            logger.warning("fresh-zones quote failed symbol=%s error=%s", symbol, exc)
            return None
        price = info.get("lastPrice") or info.get("last_price")
        if not price or not math.isfinite(float(price)):
            return None
        session_open = info.get("open")
        return Quote(
            symbol=symbol,
            price=float(price),
            at=datetime.now(timezone.utc),
            quality=self.quality(),
            session_open=float(session_open) if session_open else None,
            timestamp_is_retrieval_time=True,
        )

    def intraday_candles(self, symbol: str, minutes: int, since: datetime | None) -> pd.DataFrame:
        import yfinance as yf

        try:
            frame = yf.Ticker(self._ticker(symbol)).history(period="5d", interval=f"{minutes}m", auto_adjust=False)
        except Exception as exc:
            logger.warning("fresh-zones intraday failed symbol=%s error=%s", symbol, exc)
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        return _only_completed(frame, minutes, datetime.now(timezone.utc), since)


class BrokerQuoteProvider(YahooFinanceProvider):
    """Yahoo daily history with broker (AngelOne) quotes.

    Quotes are labelled LIVE only when the owner has confirmed the feed and
    its permitted usage via ``FRESH_ZONE_LIVE_CONFIRMED=true``.
    """

    name = "angelone+yfinance"

    def __init__(self, broker: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self.broker = broker
        self.live_confirmed = os.getenv("FRESH_ZONE_LIVE_CONFIRMED", "false").strip().lower() in {"1", "true", "yes"}

    def quality(self) -> DataQuality:
        mode = DataMode.LIVE if self.live_confirmed else DataMode.DELAYED
        limitations = ["daily history from yfinance (split/bonus adjusted, not dividend adjusted)"]
        if not self.live_confirmed:
            limitations.append("live feed usage not confirmed by owner; treated as delayed")
        return DataQuality(source="angelone", mode=mode, delay_seconds=0 if self.live_confirmed else None,
                           limitations=tuple(limitations))

    def quote(self, symbol: str) -> Quote | None:
        if not self.broker.is_available():
            return super().quote(symbol)
        try:
            data = self.broker.fetch_live_quote(symbol) or {}
        except Exception as exc:
            logger.warning("fresh-zones broker quote failed symbol=%s error=%s", symbol, exc)
            return None
        price = data.get("price")
        if not price:
            return None
        raw_ts = data.get("timestamp")
        at, retrieval = datetime.now(timezone.utc), True
        if raw_ts:
            try:
                parsed = pd.Timestamp(raw_ts)
                at = (parsed.tz_localize(IST) if parsed.tz is None else parsed).to_pydatetime()
                retrieval = False
            except (ValueError, TypeError):
                pass
        return Quote(symbol, float(price), at, self.quality(),
                     float(data["open"]) if data.get("open") else None, retrieval)


class FrameProvider:
    """In-memory provider for tests, replays and CSV-driven runs."""

    name = "frames"

    def __init__(self, daily: dict[str, pd.DataFrame], quotes: dict[str, Quote] | None = None,
                 intraday: dict[str, pd.DataFrame] | None = None, mode: DataMode = DataMode.END_OF_DAY):
        self.daily = daily
        self.quotes = quotes or {}
        self.intraday = intraday or {}
        self.mode = mode

    def quality(self) -> DataQuality:
        return DataQuality(source=self.name, mode=self.mode)

    def daily_history(self, symbol: str) -> pd.DataFrame:
        return self.daily.get(symbol, pd.DataFrame())

    def quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    def intraday_candles(self, symbol: str, minutes: int, since: datetime | None) -> pd.DataFrame:
        frame = self.intraday.get(symbol)
        if frame is None:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        now = max(q.at for q in self.quotes.values()) if self.quotes else datetime.now(timezone.utc)
        return _only_completed(frame, minutes, now, since)


def get_provider() -> MarketDataProvider:
    choice = os.getenv("FRESH_ZONE_PROVIDER", "yfinance").strip().lower()
    if choice in {"angelone", "broker"}:
        from app.services.broker_adapter import get_broker_adapter

        return BrokerQuoteProvider(get_broker_adapter())
    return YahooFinanceProvider()


# ── Universe ────────────────────────────────────────────────────────────────

def load_symbol_list(path: str, column: str = "Symbol") -> list[str]:
    """Read symbols from an NSE index-constituent CSV (e.g. ind_nifty500list.csv)."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [row[column].strip().upper() for row in reader if row.get(column, "").strip()]


def normalise_symbols(symbols: list[str]) -> list[str]:
    """NSE main-board symbols, de-duplicated across exchange suffixes."""
    seen: dict[str, None] = {}
    for raw in symbols:
        symbol = raw.strip().upper()
        for suffix in (".NS", ".BO", "-EQ"):
            if symbol.endswith(suffix):
                symbol = symbol[: -len(suffix)]
        if symbol and not symbol.startswith("^"):
            seen.setdefault(symbol, None)
    return list(seen)


def load_exclusions() -> set[str]:
    """Owner-maintained ASM/GSM/restricted list (one symbol per line or CSV)."""
    path = os.getenv("FRESH_ZONE_EXCLUSIONS_FILE", "").strip()
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8-sig") as handle:
        return {line.split(",")[0].strip().upper() for line in handle if line.strip() and not line.startswith("#")}


def eligibility(symbol: str, daily: pd.DataFrame, params: ScannerParams, exclusions: set[str],
                asof: pd.Timestamp | None = None) -> tuple[bool, list[str], dict[str, Any]]:
    """Universe filters on a clean daily frame (lower-case columns).

    Returns (eligible, reasons-if-not, measured statistics).
    """

    reasons: list[str] = []
    stats: dict[str, Any] = {"sessions": int(len(daily))}
    if params.exclude_asm_gsm and symbol in exclusions:
        reasons.append("excluded by owner list (ASM/GSM/restricted)")
    if len(daily) < params.min_history_sessions:
        reasons.append(f"history {len(daily)} sessions < {params.min_history_sessions}")
    if daily.empty:
        return False, reasons or ["no data"], stats
    recent = daily.tail(20)
    traded_value = (recent["close"] * recent["volume"]).mean()
    stats["avg_traded_value_cr"] = None if pd.isna(traded_value) else round(float(traded_value) / 1e7, 2)
    if stats["avg_traded_value_cr"] is None:
        reasons.append("traded value unavailable")
    elif stats["avg_traded_value_cr"] < params.min_avg_traded_value_cr:
        reasons.append(f"avg traded value ₹{stats['avg_traded_value_cr']} cr < ₹{params.min_avg_traded_value_cr} cr")
    zero_volume = int((recent["volume"].fillna(0) <= 0).sum())
    if zero_volume >= 5:
        reasons.append(f"{zero_volume} zero-volume sessions in the last 20 (possible suspension)")
    last = daily.iloc[-1]
    # A session that printed a single price is a strong hint of a circuit lock.
    stats["last_session_single_price"] = bool(last["high"] == last["low"])
    if asof is not None and (pd.Timestamp(asof) - daily.index[-1]).days > 7:
        reasons.append(f"no data since {daily.index[-1].date()} (suspended or delisted?)")
    return not reasons, reasons, stats


def now_ist() -> datetime:
    return to_ist(datetime.now(timezone.utc))
