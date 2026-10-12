"""Freshness, test counting and invalidation from historical candles.

Freshness is a pass/fail gate.  Every daily bar after the qualifying
departure is checked (the departure candles themselves are exempt).  Daily
high/low covers every trade of the session, so a daily bar that does not
overlap the zone proves no trade entered it that day.  The current, still
forming daily bar is included because it is observed traded data.

Test counting uses only bars that did *not* overlap the zone to measure how
far price moved away between entries: inside one overlapping daily bar the
order of the high and the low is unknown, so such a bar cannot prove a
qualifying excursion.
"""

from __future__ import annotations

import math
from datetime import date
import numpy as np
import pandas as pd

from app.fresh_zones.models import Freshness, TimingConfidence, Zone, ZoneStatus, ZoneType
from app.fresh_zones.params import ScannerParams


TOUCH = "TOUCH"
GAP_INTO = "GAP_THROUGH_INTO_ZONE"
GAP_BEYOND = "GAP_THROUGH_BEYOND_ZONE"


def classify_entry(zone: Zone, open_price: float) -> str:
    """How a bar that overlaps the zone got there, judged from its open."""
    if zone.contains(open_price):
        return GAP_INTO
    if zone.zone_type == ZoneType.DEMAND and open_price < zone.lower:
        return GAP_BEYOND
    if zone.zone_type == ZoneType.RESISTANCE and open_price > zone.upper:
        return GAP_BEYOND
    return TOUCH


def excursion_from_proximal(zone: Zone, high: float, low: float) -> float:
    """Distance a non-overlapping bar travelled away from the proximal edge."""
    if zone.zone_type == ZoneType.DEMAND:
        return max(0.0, high - zone.proximal)
    return max(0.0, zone.proximal - low)


def beyond_distal_close(zone: Zone, close: float) -> bool:
    if zone.zone_type == ZoneType.DEMAND:
        return close < zone.distal
    return close > zone.distal


def evaluate_zone_history(
    zone: Zone,
    daily: pd.DataFrame,
    zone_candles: pd.DataFrame,
    atr_d: pd.Series,
    *,
    params: ScannerParams,
    asof: date,
) -> Zone:
    """Populate the zone's freshness, touches, tests, invalidation and status.

    ``daily`` is the clean daily frame (DatetimeIndex) which may include the
    current partial session; ``zone_candles`` are *completed* candles of the
    zone's own timeframe; ``atr_d`` is ATR_D aligned to ``daily`` and computed
    on completed sessions.
    """

    departure_end = date.fromisoformat(zone.formation_date)
    after = daily[daily.index.date > departure_end]
    zone.touches, zone.tests, zone.invalidation = [], [], None

    # ── Data coverage ───────────────────────────────────────────────────────
    unknown_reasons: list[str] = []
    if daily.empty or daily.index[-1].date() < departure_end:
        unknown_reasons.append("no daily data after the departure")
    else:
        dates = [departure_end, *after.index.date]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        if gaps and max(gaps) > params.max_data_gap_days:
            unknown_reasons.append(f"daily data gap of {max(gaps)} days after formation")
        last = daily.index[-1].date()
        stale = int(np.busday_count(last, asof)) if last < asof else 0
        if stale > params.max_stale_business_days:
            unknown_reasons.append(f"daily data ends {last} ({stale} business days before {asof})")

    # ── Touches and tests ───────────────────────────────────────────────────
    excursion = 0.0  # farthest move away from the proximal edge since the last entry
    rows = zip(after.index, after["open"].astype(float), after["high"].astype(float), after["low"].astype(float))
    for ts, open_, high, low in rows:
        if not zone.overlaps(low, high):
            if zone.touches:
                excursion = max(excursion, excursion_from_proximal(zone, high, low))
            continue
        kind = classify_entry(zone, open_)
        touch = {
            "date": ts.date().isoformat(),
            "kind": kind,
            "open": open_,
            "high": high,
            "low": low,
            "timing": str(TimingConfidence.EOD_INFERRED),
            "source": "daily_ohlc",
        }
        zone.touches.append(touch)
        reference_atr = _atr_before(atr_d, ts)
        new_test = not zone.tests or (
            math.isfinite(reference_atr) and excursion >= params.test_reset_atr_d * reference_atr
        )
        if new_test:
            zone.tests.append(
                {
                    "test_number": len(zone.tests) + 1,
                    "date": touch["date"],
                    "kind": kind,
                    "excursion_before_atr_d": round(excursion / reference_atr, 3)
                    if zone.tests and math.isfinite(reference_atr) and reference_atr > 0
                    else None,
                    "timing": touch["timing"],
                }
            )
        excursion = 0.0

    # ── Invalidation on the zone's own timeframe ───────────────────────────
    if not zone_candles.empty:
        later = zone_candles[[start > departure_end for start in zone_candles["start"]]]
        for candle in later.to_dict("records"):
            if beyond_distal_close(zone, float(candle["close"])):
                zone.invalidation = {
                    "date": str(candle["end"]),
                    "candle": str(candle["label"]),
                    "close": float(candle["close"]),
                    "distal": zone.distal,
                    "rule": f"completed {zone.timeframe} candle closed beyond the distal boundary",
                }
                break

    # ── Status ──────────────────────────────────────────────────────────────
    if zone.touches:
        zone.freshness = Freshness.TESTED
        zone.freshness_reason = f"first entered on {zone.touches[0]['date']} ({zone.touches[0]['kind']})"
    elif unknown_reasons:
        zone.freshness = Freshness.UNKNOWN
        zone.freshness_reason = "; ".join(unknown_reasons)
    else:
        zone.freshness = Freshness.FRESH
        zone.freshness_reason = f"no daily bar entered the zone between {departure_end} and {daily.index[-1].date()}"

    if zone.invalidation:
        zone.status = ZoneStatus.INVALIDATED
    elif zone.touches:
        zone.status = ZoneStatus.TOUCHED
    elif zone.freshness == Freshness.FRESH:
        zone.status = ZoneStatus.FRESH
    else:
        zone.status = ZoneStatus.UNVERIFIED
    zone.evaluated_through = daily.index[-1].date().isoformat() if not daily.empty else None
    return zone


def _atr_before(atr_d: pd.Series, ts: pd.Timestamp) -> float:
    """Latest ATR_D known before the session at ``ts`` (no look-ahead)."""
    position = atr_d.index.searchsorted(ts, side="left")
    prior = atr_d.iloc[:position].dropna()
    return float(prior.iloc[-1]) if not prior.empty else math.nan
