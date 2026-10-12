"""Synthetic market data for the fresh-zone scanner tests."""

from __future__ import annotations

import pandas as pd


def week_days(week_ohlc: list[tuple[float, float, float, float]], start: str = "2020-01-06") -> pd.DataFrame:
    """Daily bars whose Monday–Friday aggregate equals each weekly (O, H, L, C).

    Mon: O → low, Tue: low → high, Wed: high → C, Thu/Fri: flat at C.
    The order of low and high inside the week is fixed and known.
    """

    rows, index = [], []
    monday = pd.Timestamp(start)
    for o, h, l, c in week_ohlc:
        days = [
            (o, max(o, l), l, l),
            (l, h, l, h),
            (h, h, min(h, c), c),
            (c, c, c, c),
            (c, c, c, c),
        ]
        for offset, (do, dh, dl, dc) in enumerate(days):
            index.append(monday + pd.Timedelta(days=offset))
            rows.append({"Open": do, "High": max(do, dh, dc), "Low": min(do, dl, dc), "Close": dc, "Volume": 2_000_000})
        monday += pd.Timedelta(days=7)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(index))


def chop(n: int, mid: float = 100.0, half_range: float = 5.0) -> list[tuple[float, float, float, float]]:
    """Neutral weekly candles of range 2×half_range (ATR ≈ 2×half_range)."""
    out = []
    for i in range(n):
        o, c = (mid - 1, mid + 1) if i % 2 == 0 else (mid + 1, mid - 1)
        out.append((o, mid + half_range, mid - half_range, c))
    return out


# A clean weekly DBR: leg-in down, one-candle base, strong rally.
DBR_WEEKS = [
    *chop(20, mid=100),
    (100.0, 101.0, 84.0, 85.0),   # leg-in: body 15
    (85.0, 88.0, 83.0, 86.0),     # base: body 1 / range 5
    (86.0, 106.0, 86.0, 105.0),   # departure: body 19, close above base high
    (105.0, 112.0, 103.0, 110.0),
    (110.0, 116.0, 106.0, 112.0),
]
