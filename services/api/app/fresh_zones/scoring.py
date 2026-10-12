"""Static zone-quality score (0–80).

Freshness and validity are gates, never score components: a tested zone
cannot buy its way back with points.  The score describes the structure only;
it is not a probability of success and carries no predictive claim until
validated against history (demand and resistance separately).

Structural clarity (8 points) is the developer's interpretation of the
specification and is documented in ``docs/FRESH_ZONE_SCANNER.md``:
  * clean departure — 4 points when the departure needed one candle,
    2 points when it needed two;
  * no conflicting structure — 4 points when no opposite-type zone of any
    scanned timeframe, formed before this zone's departure, overlaps or lies
    within ``clarity_conflict_atr_z`` × ATR_Z of this zone's boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from app.fresh_zones.models import Zone
from app.fresh_zones.params import ScannerParams


TIMEFRAME_POINTS = {"Y": 7, "M": 5, "W": 3}
MAX_SCORE = 80


def _departure_points(departure_atr: float, minimum: float) -> float:
    if departure_atr < minimum:
        return 0.0
    if departure_atr >= 3.0:
        return 30.0
    span = 3.0 - minimum
    return 15.0 + 15.0 * (departure_atr - minimum) / span if span > 0 else 30.0


def conflicting_zones(zone: Zone, others: Iterable[Zone], params: ScannerParams) -> list[str]:
    margin = params.clarity_conflict_atr_z * zone.atr_z
    formed = date.fromisoformat(zone.formation_date)
    conflicts = []
    for other in others:
        if other.zone_id == zone.zone_id or other.zone_type == zone.zone_type:
            continue
        if date.fromisoformat(other.formation_date) > formed:
            continue
        if other.overlaps(zone.lower - margin, zone.upper + margin):
            conflicts.append(other.zone_id)
    return conflicts


def score_zone(zone: Zone, others: Iterable[Zone], params: ScannerParams) -> Zone:
    base_len = len(zone.base_candles)
    base_length_pts = 10 if base_len <= 2 else 5
    body_pts = 8 if zone.base_avg_body_ratio <= 0.30 else 4
    width_pts = 7 if zone.width_atr_z <= 0.75 else 3 if zone.width_atr_z <= params.max_width_atr else 0

    departure_pts = _departure_points(zone.departure_atr, params.departure_min_atr)

    tf_pts = TIMEFRAME_POINTS.get(zone.timeframe, 0)
    clean_departure_pts = 4 if len(zone.departure_candles) == 1 else 2
    conflicts = conflicting_zones(zone, others, params)
    no_conflict_pts = 0 if conflicts else 4

    if zone.fvg is None:
        fvg_pts = 0
    elif zone.fvg["size_atr_z"] >= 0.5:
        fvg_pts = 10
    else:
        fvg_pts = 5

    breakdown: dict[str, Any] = {
        "base_quality": {
            "points": base_length_pts + body_pts + width_pts,
            "max": 25,
            "base_length": {"candles": base_len, "points": base_length_pts},
            "base_body_ratio": {"average": zone.base_avg_body_ratio, "points": body_pts},
            "zone_width": {"atr_z": round(zone.width_atr_z, 3), "points": width_pts},
        },
        "departure_strength": {
            "points": round(departure_pts, 2),
            "max": 30,
            "departure_atr_z": round(zone.departure_atr, 3),
        },
        "timeframe_and_clarity": {
            "points": tf_pts + clean_departure_pts + no_conflict_pts,
            "max": 15,
            "timeframe": {"timeframe": zone.timeframe, "points": tf_pts},
            "clean_departure": {"departure_candles": len(zone.departure_candles), "points": clean_departure_pts},
            "no_conflicting_zones": {"conflicts": conflicts, "points": no_conflict_pts},
        },
        "fvg": {
            "points": fvg_pts,
            "max": 10,
            "size_atr_z": zone.fvg["size_atr_z"] if zone.fvg else None,
        },
        "note": "Structural quality score; not a probability of success.",
    }
    total = (
        breakdown["base_quality"]["points"]
        + departure_pts
        + breakdown["timeframe_and_clarity"]["points"]
        + fvg_pts
    )
    zone.score = int(round(total))
    breakdown["total"] = zone.score
    breakdown["max"] = MAX_SCORE
    zone.score_breakdown = breakdown
    return zone
