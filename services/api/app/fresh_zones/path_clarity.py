"""Price-path clarity between the current price and a destination zone.

Only the structures named in the specification are considered:
  * weekly / monthly / yearly zones (fresh or previously tested, not
    invalidated, flipped zones included) that would oppose the move, and
  * confirmed daily swing highs / lows from the last ``swing_window_days``
    completed sessions.

"Opposing" is read as *opposing the direction of travel*: on the way down to
a demand zone the obstacles are supports (other demand zones, swing lows);
on the way up to a resistance zone they are resistances (other resistance
zones, swing highs).  A structure counts when its near edge lies strictly
between the current price and the destination's proximal boundary.

CLEAR means no qualifying barrier was detected; it does not guarantee an
unobstructed move.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pandas as pd

from app.fresh_zones.models import Zone, ZoneStatus, ZoneType
from app.fresh_zones.params import ScannerParams


CLEAR = "CLEAR"
PARTIALLY_OBSTRUCTED = "PARTIALLY_OBSTRUCTED"
HEAVILY_OBSTRUCTED = "HEAVILY_OBSTRUCTED"


def swing_points(daily_completed: pd.DataFrame, params: ScannerParams) -> list[dict[str, Any]]:
    """Confirmed swing highs/lows; a swing needs ``lookaround`` completed bars
    on both sides, so nothing is confirmed with look-ahead."""

    k = params.swing_lookaround
    if len(daily_completed) < 2 * k + 1:
        return []
    window = daily_completed.tail(params.swing_window_days)
    highs = window["high"].astype(float).to_numpy()
    lows = window["low"].astype(float).to_numpy()
    dates = window.index
    points: list[dict[str, Any]] = []
    for i in range(k, len(window) - k):
        neighbours_high = [*highs[i - k:i], *highs[i + 1:i + k + 1]]
        neighbours_low = [*lows[i - k:i], *lows[i + 1:i + k + 1]]
        confirmed_on = dates[i + k].date().isoformat()
        if highs[i] > max(neighbours_high):
            points.append({"type": "swing_high", "date": dates[i].date().isoformat(), "price": float(highs[i]), "confirmed_on": confirmed_on})
        if lows[i] < min(neighbours_low):
            points.append({"type": "swing_low", "date": dates[i].date().isoformat(), "price": float(lows[i]), "confirmed_on": confirmed_on})
    return points


def assess(
    target: Zone,
    price: float,
    zones: Iterable[Zone],
    swings: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    falling = target.zone_type == ZoneType.DEMAND
    lo, hi = (target.proximal, price) if falling else (price, target.proximal)
    barriers: list[dict[str, Any]] = []
    for zone in zones:
        if zone.zone_id == target.zone_id or zone.status == ZoneStatus.INVALIDATED:
            continue
        if zone.zone_type != target.zone_type:
            continue
        edge = zone.proximal
        if lo < edge < hi:
            barriers.append(
                {
                    "kind": "zone",
                    "zone_id": zone.zone_id,
                    "type": str(zone.zone_type),
                    "timeframe": zone.timeframe,
                    "status": str(zone.status),
                    "flipped": zone.parent_zone_id is not None,
                    "proximal": zone.proximal,
                    "distal": zone.distal,
                    "distance_rupees": round(abs(price - edge), 2),
                    "distance_pct": round(abs(price - edge) / price * 100, 3) if price > 0 else None,
                }
            )
    wanted = "swing_low" if falling else "swing_high"
    for point in swings:
        if point["type"] == wanted and lo < point["price"] < hi:
            barriers.append(
                {
                    "kind": point["type"],
                    "timeframe": "D",
                    "date": point["date"],
                    "price": point["price"],
                    "distance_rupees": round(abs(price - point["price"]), 2),
                    "distance_pct": round(abs(price - point["price"]) / price * 100, 3) if price > 0 else None,
                }
            )
    barriers.sort(key=lambda b: b["distance_rupees"])
    count = len(barriers)
    classification = CLEAR if count == 0 else PARTIALLY_OBSTRUCTED if count == 1 else HEAVILY_OBSTRUCTED
    return {
        "classification": classification,
        "barrier_count": count,
        "barriers": barriers,
        "scope": "W/M/Y zones and confirmed daily swing points only; CLEAR is not a guarantee of an unobstructed move.",
    }


def next_opposing_zone(zone: Zone, price: float, zones: Iterable[Zone], direction: str) -> dict[str, Any] | None:
    """Nearest non-invalidated zone that would oppose a move in ``direction``
    ('up' looks for resistance above ``price``, 'down' for demand below)."""

    best: Zone | None = None
    for other in zones:
        if other.zone_id == zone.zone_id or other.status == ZoneStatus.INVALIDATED:
            continue
        if direction == "up" and other.zone_type == ZoneType.RESISTANCE and other.proximal > price:
            if best is None or other.proximal < best.proximal:
                best = other
        if direction == "down" and other.zone_type == ZoneType.DEMAND and other.proximal < price:
            if best is None or other.proximal > best.proximal:
                best = other
    if best is None:
        return None
    return {
        "zone_id": best.zone_id,
        "type": str(best.zone_type),
        "timeframe": best.timeframe,
        "status": str(best.status),
        "proximal": best.proximal,
        "distal": best.distal,
    }
