"""Price position relative to a zone, and ATR_D-based proximity categories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.fresh_zones.models import Proximity, Zone, ZoneType
from app.fresh_zones.params import ScannerParams


APPROACHING = "approaching"  # on the correct side, outside the zone
INSIDE = "inside"            # at the proximal boundary or within the zone
BEYOND = "beyond"            # past the distal boundary


@dataclass(frozen=True)
class ProximityReading:
    position: str
    price: float
    distance: float | None
    distance_pct: float | None
    distance_atr_d: float | None
    category: Proximity | None
    approach_direction: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "price": self.price,
            "distance_rupees": None if self.distance is None else round(self.distance, 2),
            "distance_pct": None if self.distance_pct is None else round(self.distance_pct, 3),
            "distance_atr_d": None if self.distance_atr_d is None else round(self.distance_atr_d, 3),
            "category": None if self.category is None else str(self.category),
            "approach_direction": self.approach_direction,
        }


def position_of(zone: Zone, price: float) -> str:
    if zone.zone_type == ZoneType.DEMAND:
        if price > zone.proximal:
            return APPROACHING
        return INSIDE if price >= zone.distal else BEYOND
    if price < zone.proximal:
        return APPROACHING
    return INSIDE if price <= zone.distal else BEYOND


def categorize(distance_atr_d: float, params: ScannerParams) -> Proximity:
    if distance_atr_d <= params.imminent_atr_d:
        return Proximity.IMMINENT
    if distance_atr_d <= params.near_atr_d:
        return Proximity.NEAR
    return Proximity.NOT_PRIORITY


def measure(zone: Zone, price: float, atr_d: float, params: ScannerParams) -> ProximityReading:
    direction = "falling toward demand" if zone.zone_type == ZoneType.DEMAND else "rising toward resistance"
    position = position_of(zone, price)
    if position != APPROACHING:
        return ProximityReading(position, price, None, None, None, None, direction)
    distance = price - zone.proximal if zone.zone_type == ZoneType.DEMAND else zone.proximal - price
    distance_atr = distance / atr_d if atr_d > 0 else float("inf")
    return ProximityReading(
        position,
        price,
        distance,
        distance / price * 100 if price > 0 else None,
        distance_atr,
        categorize(distance_atr, params),
        direction,
    )


def rank_key(category: Proximity | None, score: int | None, distance_atr_d: float | None) -> tuple:
    """IMMINENT before NEAR, then higher quality score, then smaller distance."""
    order = {Proximity.IMMINENT: 0, Proximity.NEAR: 1, Proximity.NOT_PRIORITY: 2}
    return (
        order.get(category, 3) if category is not None else 3,
        -(score or 0),
        distance_atr_d if distance_atr_d is not None else float("inf"),
    )
