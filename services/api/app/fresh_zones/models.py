"""Records shared by the fresh-zone scanner.

Observed data (prices, candles, timestamps), algorithmic classifications
(zone status, proximity category) and limitations are kept in separate
fields so a stored record never blurs what was seen with what was inferred.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class StrEnum(str, Enum):
    """``enum.StrEnum`` equivalent that also runs on Python 3.10."""

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self.value), spec)


class ZoneType(StrEnum):
    DEMAND = "demand"
    RESISTANCE = "resistance"


class Pattern(StrEnum):
    DBR = "DBR"
    RBR = "RBR"
    RBD = "RBD"
    DBD = "DBD"


class ZoneStatus(StrEnum):
    """Zone lifecycle: FRESH → TOUCHED → INVALIDATED.

    ``UNVERIFIED`` is used while freshness cannot be established from the
    available data; such zones never raise fresh-zone alerts.
    """

    UNVERIFIED = "UNVERIFIED"
    FRESH = "FRESH"
    TOUCHED = "TOUCHED"
    INVALIDATED = "INVALIDATED"


class Freshness(StrEnum):
    FRESH = "FRESH"
    TESTED = "TESTED"
    UNKNOWN = "UNKNOWN"


class Proximity(StrEnum):
    IMMINENT = "IMMINENT"
    NEAR = "NEAR"
    NOT_PRIORITY = "NOT_PRIORITY"


class EventState(StrEnum):
    """Alert/event lifecycle, tracked separately from the zone lifecycle."""

    FRESH = "FRESH"
    APPROACHING = "APPROACHING"
    TOUCH_DETECTED = "TOUCH_DETECTED"
    REACTION_UNDER_OBSERVATION = "REACTION_UNDER_OBSERVATION"
    REACTION_CONFIRMED = "REACTION_CONFIRMED"
    CONSOLIDATING = "CONSOLIDATING"
    UNDER_PRESSURE = "UNDER_PRESSURE"
    FAILED_REACTION = "FAILED_REACTION"
    PROVISIONAL_BREACH = "PROVISIONAL_BREACH"
    CONFIRMED_BREAK = "CONFIRMED_BREAK"
    FALSE_BREAK = "FALSE_BREAK"
    RETEST = "RETEST"
    CONTINUATION_CONFIRMED = "CONTINUATION_CONFIRMED"


class TimingConfidence(StrEnum):
    LIVE_TRADE = "LIVE_TRADE"            # observed traded price with its own timestamp
    DELAYED_QUOTE = "DELAYED_QUOTE"      # observed price, known to be delayed
    CANDLE_INFERRED = "CANDLE_INFERRED"  # inferred from an OHLC candle's high/low
    SESSION_OPEN = "SESSION_OPEN"        # observed session open print
    EOD_INFERRED = "EOD_INFERRED"        # inferred from a completed daily candle
    UNOBSERVED = "UNOBSERVED"            # must have happened, time/price unknown


class DataMode(StrEnum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    END_OF_DAY = "END_OF_DAY"


@dataclass(frozen=True)
class DataQuality:
    source: str
    mode: DataMode
    delay_seconds: int | None = None
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "mode": str(self.mode),
            "delay_seconds": self.delay_seconds,
            "is_live": self.mode == DataMode.LIVE,
            "limitations": list(self.limitations),
        }


@dataclass
class Zone:
    symbol: str
    exchange: str
    timeframe: str
    zone_type: ZoneType
    pattern: Pattern
    construction_method: str
    proximal: float
    distal: float
    proximal_raw: float
    distal_raw: float
    atr_z: float
    atr_z_period: int
    base_candles: list[dict[str, Any]]
    leg_in_candles: list[dict[str, Any]]
    departure_candles: list[dict[str, Any]]
    leg_in_body: float
    departure_body: float
    base_avg_body_ratio: float
    fvg: dict[str, Any] | None
    param_version: str
    review_reasons: list[str] = field(default_factory=list)
    status: ZoneStatus = ZoneStatus.UNVERIFIED
    freshness: Freshness = Freshness.UNKNOWN
    freshness_reason: str = ""
    touches: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    invalidation: dict[str, Any] | None = None
    score: int | None = None
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    data_source: str = ""
    evaluated_through: str | None = None
    parent_zone_id: str | None = None
    flipped_zone_ids: list[str] = field(default_factory=list)
    zone_id: str = ""

    def __post_init__(self) -> None:
        if not self.zone_id:
            self.zone_id = make_zone_id(
                self.symbol,
                self.exchange,
                self.timeframe,
                str(self.zone_type),
                self.construction_method,
                self.base_candles[0]["start"] if self.base_candles else "",
                self.base_candles[-1]["end"] if self.base_candles else "",
                self.param_version,
            )

    # ── Derived measurements ───────────────────────────────────────────────
    @property
    def upper(self) -> float:
        return max(self.proximal, self.distal)

    @property
    def lower(self) -> float:
        return min(self.proximal, self.distal)

    @property
    def width(self) -> float:
        return abs(self.proximal - self.distal)

    @property
    def width_atr_z(self) -> float:
        return self.width / self.atr_z if self.atr_z > 0 else float("inf")

    @property
    def width_pct(self) -> float:
        return self.width / self.proximal * 100 if self.proximal > 0 else float("inf")

    @property
    def departure_atr(self) -> float:
        return self.departure_body / self.atr_z if self.atr_z > 0 else 0.0

    @property
    def leg_in_atr(self) -> float:
        return self.leg_in_body / self.atr_z if self.atr_z > 0 else 0.0

    @property
    def formation_date(self) -> str:
        """Date the zone became known: the end of its qualifying departure."""
        return self.departure_candles[-1]["end"]

    @property
    def review_required(self) -> bool:
        return bool(self.review_reasons)

    @property
    def test_count(self) -> int:
        return len(self.tests)

    @property
    def is_alert_eligible(self) -> bool:
        """Only original (non-flipped), verified-fresh, in-spec zones can raise
        fresh-zone approach alerts."""
        return (
            self.status == ZoneStatus.FRESH
            and self.freshness == Freshness.FRESH
            and not self.review_required
            and self.parent_zone_id is None
        )

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def overlaps(self, low: float, high: float) -> bool:
        return low <= self.upper and high >= self.lower

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("zone_type", "pattern", "status", "freshness"):
            data[key] = str(data[key])
        data.update(
            {
                "width": round(self.width, 4),
                "width_atr_z": round(self.width_atr_z, 4),
                "width_pct": round(self.width_pct, 4),
                "departure_atr": round(self.departure_atr, 4),
                "leg_in_atr": round(self.leg_in_atr, 4),
                "formation_date": self.formation_date,
                "review_required": self.review_required,
                "test_count": self.test_count,
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Zone":
        derived = {
            "width", "width_atr_z", "width_pct", "departure_atr", "leg_in_atr",
            "formation_date", "review_required", "test_count",
        }
        payload = {k: v for k, v in data.items() if k not in derived}
        payload["zone_type"] = ZoneType(payload["zone_type"])
        payload["pattern"] = Pattern(payload["pattern"])
        payload["status"] = ZoneStatus(payload.get("status", ZoneStatus.UNVERIFIED))
        payload["freshness"] = Freshness(payload.get("freshness", Freshness.UNKNOWN))
        return cls(**payload)


def make_zone_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return f"Z-{parts[0]}-{parts[2]}-{digest}"
