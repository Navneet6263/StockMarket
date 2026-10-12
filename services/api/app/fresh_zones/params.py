"""Versioned parameter set for the fresh-zone scanner.

Every threshold below is an initial hypothesis taken from the specification,
not a proven optimum.  The parameter-set version is a content hash, so every
stored zone, event and alert can be traced back to the exact rules that
produced it.  Overrides come from a JSON file named by ``FRESH_ZONE_PARAMS_FILE``
(only keys that exist on :class:`ScannerParams` are accepted).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields, replace
from functools import lru_cache
from typing import Any


SCHEMA_VERSION = "fz1"


@dataclass(frozen=True)
class ScannerParams:
    # ── Timeframes ──────────────────────────────────────────────────────────
    timeframes: tuple[str, ...] = ("W", "M", "Y")
    # "calendar" (Jan–Dec) or "financial" (Apr–Mar).  OWNER DECISION.
    year_definition: str = "calendar"
    # Weeks run Monday–Sunday; the candle is complete once its last calendar
    # day's session has closed.
    atr_period: int = 14
    yearly_atr_period: int = 5
    yearly_min_candles: int = 6
    # "wilder" (RMA, the usual ATR) or "sma".
    atr_method: str = "wilder"

    # ── Base formation ──────────────────────────────────────────────────────
    base_min_candles: int = 1
    base_max_candles: int = 4
    base_body_max_ratio: float = 0.50

    # ── Leg-in / departure ──────────────────────────────────────────────────
    leg_in_max_candles: int = 2
    leg_in_min_atr: float = 1.0
    departure_max_candles: int = 2
    departure_min_atr: float = 1.5

    # ── Zone width ──────────────────────────────────────────────────────────
    max_width_atr: float = 1.5
    max_width_pct: tuple[tuple[str, float], ...] = (("W", 6.0), ("M", 10.0), ("Y", 15.0))
    # "reject" drops an over-wide zone; "review" keeps it, flagged, but never
    # lets it raise an approach alert.
    width_violation_action: str = "reject"

    # ── Tick size ───────────────────────────────────────────────────────────
    # (upper price bound exclusive, tick).  The last band applies above all
    # bounds.  NSE's current price-band tick table must be confirmed with the
    # data provider; a flat ₹0.05 is used until then.
    tick_bands: tuple[tuple[float, float], ...] = ((float("inf"), 0.05),)

    # ── Freshness / testing ─────────────────────────────────────────────────
    test_reset_atr_d: float = 0.5
    # A gap between consecutive daily bars longer than this (calendar days)
    # after the departure makes freshness UNKNOWN rather than FRESH.
    max_data_gap_days: int = 15
    # Daily data older than this many business days relative to the evaluation
    # date makes freshness UNKNOWN.
    max_stale_business_days: int = 3

    # ── Proximity ───────────────────────────────────────────────────────────
    imminent_atr_d: float = 0.5
    near_atr_d: float = 1.5

    # ── Reaction (Release 2) ────────────────────────────────────────────────
    reaction_window_sessions: int = 2
    reaction_candle_minutes: int = 15
    reaction_threshold_atr_d: float = 0.5
    under_pressure_fraction: float = 0.5

    # ── Price-path clarity (Release 2) ──────────────────────────────────────
    swing_lookaround: int = 10
    swing_window_days: int = 250

    # ── Breakout lifecycle (Release 3) ──────────────────────────────────────
    enable_daily_close_provisional: bool = False  # OWNER DECISION
    continuation_atr_d: float = 1.0
    # "no_daily_close_inside": a retest holds while no completed daily candle
    # closes back inside the original zone.
    retest_holding_rule: str = "no_daily_close_inside"

    # ── Alerts ──────────────────────────────────────────────────────────────
    cooldown_minutes: int = 60
    daily_alert_cap: int = 20
    rearm_enabled: bool = True
    rearm_distance_atr_d: float = 2.0
    # Allow approach alerts back toward an already touched (non-fresh) zone.
    # Such alerts are never labelled fresh.
    allow_post_touch_approach: bool = False

    # ── Scoring ─────────────────────────────────────────────────────────────
    clarity_conflict_atr_z: float = 1.0

    # ── Session ─────────────────────────────────────────────────────────────
    session_open: str = "09:15"
    session_close: str = "15:30"
    timezone: str = "Asia/Kolkata"

    # ── Universe (OWNER DECISIONS) ──────────────────────────────────────────
    min_avg_traded_value_cr: float = 10.0  # ₹ crore, 20-session average
    min_history_sessions: int = 500
    exclude_asm_gsm: bool = True

    # ── Helpers ─────────────────────────────────────────────────────────────
    def width_pct_cap(self, timeframe: str) -> float:
        return dict(self.max_width_pct).get(timeframe, float("inf"))

    def tick_size(self, price: float) -> float:
        for upper, tick in self.tick_bands:
            if price < upper:
                return tick
        return self.tick_bands[-1][1]

    def atr_period_for(self, timeframe: str) -> int:
        return self.yearly_atr_period if timeframe == "Y" else self.atr_period

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # JSON cannot hold infinity; keep the hash stable and the record readable.
        data["tick_bands"] = [["inf" if upper == float("inf") else upper, tick] for upper, tick in self.tick_bands]
        return data

    @property
    def version(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return f"{SCHEMA_VERSION}-{hashlib.sha256(payload.encode()).hexdigest()[:10]}"


def _coerce(name: str, value: Any) -> Any:
    if name == "tick_bands":
        return tuple((float("inf") if str(upper) == "inf" else float(upper), float(tick)) for upper, tick in value)
    if name == "max_width_pct":
        items = value.items() if isinstance(value, dict) else value
        return tuple((str(tf), float(pct)) for tf, pct in items)
    if isinstance(value, list):
        return tuple(value)
    return value


def params_from_overrides(overrides: dict[str, Any], base: ScannerParams | None = None) -> ScannerParams:
    known = {f.name for f in fields(ScannerParams)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise ValueError(f"Unknown scanner parameter(s): {', '.join(unknown)}")
    params = replace(base or ScannerParams(), **{k: _coerce(k, v) for k, v in overrides.items()})
    validate_params(params)
    return params


def validate_params(params: ScannerParams) -> None:
    if params.year_definition not in {"calendar", "financial"}:
        raise ValueError("year_definition must be 'calendar' or 'financial'")
    if params.width_violation_action not in {"reject", "review"}:
        raise ValueError("width_violation_action must be 'reject' or 'review'")
    if params.atr_method not in {"wilder", "sma"}:
        raise ValueError("atr_method must be 'wilder' or 'sma'")
    if not 1 <= params.base_min_candles <= params.base_max_candles:
        raise ValueError("base candle bounds are inconsistent")
    if not 0 < params.imminent_atr_d <= params.near_atr_d:
        raise ValueError("proximity thresholds must satisfy 0 < imminent <= near")
    if params.rearm_distance_atr_d <= params.near_atr_d:
        raise ValueError("re-arm distance must exceed the NEAR threshold")
    if set(params.timeframes) - {"W", "M", "Y"}:
        raise ValueError("timeframes must be drawn from W, M, Y")


@lru_cache
def get_default_params() -> ScannerParams:
    path = os.getenv("FRESH_ZONE_PARAMS_FILE", "").strip()
    if not path:
        return ScannerParams()
    with open(path, encoding="utf-8") as handle:
        return params_from_overrides(json.load(handle))
