"""Session clock, daily-data validation, higher-timeframe candles and ATR.

All higher-timeframe candles are built from corporate-action-adjusted daily
candles so that zone prices remain comparable with current prices.  A
higher-timeframe candle is *completed* only when no further regular session
can fall inside its period; unfinished candles are kept but flagged and are
never used for structure, ATR or invalidation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.fresh_zones.params import ScannerParams


IST = ZoneInfo("Asia/Kolkata")
_PERIOD_FREQ = {"W": "W-SUN", "M": "M"}


def to_ist(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetimes are ambiguous; pass a timezone-aware timestamp")
    return moment.astimezone(IST)


def _clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


@dataclass(frozen=True)
class SessionCalendar:
    """Regular NSE equity session with explicit holidays and special sessions.

    ``holidays`` are weekday dates with no regular session.  ``special_sessions``
    maps a date (e.g. a weekend budget day or Muhurat session) to its
    (open, close) wall-clock times in IST.
    """

    open_time: time = time(9, 15)
    close_time: time = time(15, 30)
    holidays: frozenset[date] = frozenset()
    special_sessions: tuple[tuple[date, time, time], ...] = ()

    @classmethod
    def from_params(cls, params: ScannerParams, holidays=(), special_sessions=()) -> "SessionCalendar":
        return cls(_clock(params.session_open), _clock(params.session_close), frozenset(holidays), tuple(special_sessions))

    def session_hours(self, day: date) -> tuple[time, time] | None:
        for special_day, open_at, close_at in self.special_sessions:
            if special_day == day:
                return open_at, close_at
        if day.weekday() >= 5 or day in self.holidays:
            return None
        return self.open_time, self.close_time

    def is_trading_day(self, day: date) -> bool:
        return self.session_hours(day) is not None

    def is_open(self, moment: datetime) -> bool:
        local = to_ist(moment)
        hours = self.session_hours(local.date())
        return hours is not None and hours[0] <= local.time() < hours[1]

    def session_closed(self, day: date, moment: datetime) -> bool:
        """True once ``day``'s session (if any) can no longer print trades."""
        local = to_ist(moment)
        if day < local.date():
            return True
        if day > local.date():
            return False
        hours = self.session_hours(day)
        return hours is None or local.time() >= hours[1]

    def last_completed_session(self, moment: datetime) -> date:
        local = to_ist(moment)
        day = local.date()
        if self.is_trading_day(day) and self.session_closed(day, moment):
            return day
        probe = pd.Timestamp(day)
        for _ in range(30):
            probe -= pd.Timedelta(days=1)
            if self.is_trading_day(probe.date()):
                return probe.date()
        return probe.date()


# ── Daily data validation ───────────────────────────────────────────────────

def validate_daily(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Return a clean daily frame (lower-case OHLCV, sorted dates) and issues."""

    issues: list[str] = []
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"]), ["no daily data"]
    data = frame.copy()
    data.columns = [str(column).lower() for column in data.columns]
    missing = {"open", "high", "low", "close"} - set(data.columns)
    if missing:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"]), [f"missing columns: {sorted(missing)}"]
    if "volume" not in data.columns:
        data["volume"] = np.nan
        issues.append("volume unavailable")
    data = data[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    index = pd.DatetimeIndex(pd.to_datetime(data.index))
    if index.tz is not None:
        index = index.tz_convert(IST).tz_localize(None)
    data.index = index.normalize()
    if data.index.has_duplicates:
        issues.append(f"{int(data.index.duplicated().sum())} duplicate session(s) dropped (kept last)")
        data = data[~data.index.duplicated(keep="last")]
    data = data.sort_index()
    bad = data[["open", "high", "low", "close"]].isna().any(axis=1)
    bad |= (data[["open", "high", "low", "close"]] <= 0).any(axis=1)
    bad |= data["high"] < data[["open", "close", "low"]].max(axis=1)
    bad |= data["low"] > data[["open", "close", "high"]].min(axis=1)
    if bad.any():
        issues.append(f"{int(bad.sum())} malformed candle(s) dropped")
        data = data[~bad]
    return data, issues


# ── Higher-timeframe candles ────────────────────────────────────────────────

def _period_index(index: pd.DatetimeIndex, timeframe: str, year_definition: str) -> pd.PeriodIndex:
    if timeframe == "Y":
        return index.to_period("Y-MAR" if year_definition == "financial" else "Y-DEC")
    if timeframe == "D":
        return index.to_period("D")
    return index.to_period(_PERIOD_FREQ[timeframe])


def build_candles(
    daily: pd.DataFrame,
    timeframe: str,
    *,
    params: ScannerParams,
    now: datetime,
    calendar: SessionCalendar | None = None,
) -> pd.DataFrame:
    """Aggregate clean daily candles into ``timeframe`` candles.

    Columns: open/high/low/close/volume, ``start``/``end`` (first/last session
    in the candle), ``period_end`` (last calendar day of the period),
    ``complete`` and ``label``.  Rows are positional (RangeIndex).
    """

    calendar = calendar or SessionCalendar.from_params(params)
    columns = ["label", "start", "end", "period_end", "open", "high", "low", "close", "volume", "complete"]
    if daily.empty:
        return pd.DataFrame(columns=columns)
    periods = _period_index(pd.DatetimeIndex(daily.index), timeframe, params.year_definition)
    grouped = daily.groupby(periods, sort=True)
    candles = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(min_count=1),
            "start": grouped.apply(lambda g: g.index[0].date(), include_groups=False),
            "end": grouped.apply(lambda g: g.index[-1].date(), include_groups=False),
        }
    )
    candles["period_end"] = [period.end_time.date() for period in candles.index]
    candles["label"] = [_label(period, timeframe, params.year_definition) for period in candles.index]
    candles["complete"] = [calendar.session_closed(end, now) for end in candles["period_end"]]
    # History that begins part-way through the first period yields a candle
    # whose high/low/open are unknown; it must never be used as structure.
    first_period = candles.index[0]
    if (candles["start"].iloc[0] - first_period.start_time.date()).days > _PARTIAL_TOLERANCE_DAYS.get(timeframe, 0):
        candles = candles.iloc[1:]
    return candles.reset_index(drop=True)[columns]


_PARTIAL_TOLERANCE_DAYS = {"W": 4, "M": 7, "Y": 7, "D": 0}


def _label(period: pd.Period, timeframe: str, year_definition: str) -> str:
    if timeframe == "Y" and year_definition == "financial":
        start = period.start_time.year
        return f"FY{start}-{(start + 1) % 100:02d}"
    return str(period)


def completed(candles: pd.DataFrame) -> pd.DataFrame:
    if candles.empty:
        return candles
    return candles[candles["complete"].astype(bool)].reset_index(drop=True)


# ── ATR ─────────────────────────────────────────────────────────────────────

def atr_series(candles: pd.DataFrame, period: int, method: str = "wilder") -> pd.Series:
    """ATR(period) aligned to ``candles``; NaN until enough candles exist."""

    if candles.empty:
        return pd.Series(dtype=float)
    high = candles["high"].astype(float).to_numpy()
    low = candles["low"].astype(float).to_numpy()
    close = candles["close"].astype(float).to_numpy()
    prev_close = np.roll(close, 1)
    true_range = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    true_range[0] = high[0] - low[0]
    result = np.full(len(true_range), np.nan)
    if len(true_range) < period:
        return pd.Series(result, index=candles.index)
    if method == "sma":
        result = pd.Series(true_range).rolling(period).mean().to_numpy()
    else:
        result[period - 1] = true_range[:period].mean()
        for idx in range(period, len(true_range)):
            result[idx] = (result[idx - 1] * (period - 1) + true_range[idx]) / period
    return pd.Series(result, index=candles.index)
