"""Objective detection of DBR / RBR / RBD / DBD zones on completed candles.

Structure = leg-in (1–2 candles) → compact base (1–4 candles) → departure
(1–2 candles).  All thresholds are measured in ATR_Z, the ATR of the zone's
own timeframe *as of the last base candle*, so the departure being evaluated
never inflates its own yardstick.

Deterministic base selection
----------------------------
The candle immediately before the base (last leg-in candle) and the candle
immediately after it (first departure candle) must not be base-like
themselves; otherwise they would belong to the base.  For every possible
departure start the smallest qualifying base is taken
(1 candle before 2, …), with the shortest qualifying leg-in and departure.
Candidates are then accepted in formation order; a later candidate whose base
overlaps the base or departure candles of an already accepted zone is
discarded.  Every selected candle is recorded so the result is reproducible.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from app.fresh_zones.models import Pattern, Zone, ZoneType
from app.fresh_zones.params import ScannerParams
from app.fresh_zones.timeframes import atr_series


CONSTRUCTION_METHOD = "native_candles"


def candle_record(candles: pd.DataFrame, idx: int) -> dict[str, Any]:
    row = candles.iloc[idx]
    open_, high, low, close = (float(row[key]) for key in ("open", "high", "low", "close"))
    candle_range = high - low
    body = abs(close - open_)
    return {
        "label": str(row["label"]),
        "start": str(row["start"]),
        "end": str(row["end"]),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "body": round(body, 4),
        "range": round(candle_range, 4),
        "body_ratio": round(body / candle_range, 4) if candle_range > 0 else None,
        "direction": "up" if close > open_ else "down" if close < open_ else "flat",
    }


def round_to_tick(value: float, tick: float, mode: str) -> float:
    """Round ``value`` to ``tick`` ('down' / 'up'), stable against float noise."""
    steps = value / tick
    nearest = round(steps)
    if math.isclose(steps, nearest, abs_tol=1e-9):
        steps = nearest
    elif mode == "down":
        steps = math.floor(steps)
    else:
        steps = math.ceil(steps)
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(steps * tick, decimals + 2)


class ZoneDetector:
    def __init__(self, params: ScannerParams):
        self.params = params

    # ── Leg / base / departure checks ──────────────────────────────────────
    @staticmethod
    def _signed_body(ohlc: dict[str, np.ndarray], start: int, stop: int) -> float:
        return float((ohlc["close"][start:stop] - ohlc["open"][start:stop]).sum())

    def _base_ok(self, ohlc: dict[str, np.ndarray], start: int, stop: int) -> tuple[bool, float]:
        ratios = []
        for idx in range(start, stop):
            candle_range = ohlc["high"][idx] - ohlc["low"][idx]
            if candle_range <= 0:
                return False, 0.0  # zero-range candle cannot be evaluated reliably
            ratio = abs(ohlc["close"][idx] - ohlc["open"][idx]) / candle_range
            if ratio > self.params.base_body_max_ratio:
                return False, 0.0
            ratios.append(float(ratio))
        return True, sum(ratios) / len(ratios)

    def _is_base_like(self, ohlc: dict[str, np.ndarray], idx: int) -> bool:
        candle_range = ohlc["high"][idx] - ohlc["low"][idx]
        return candle_range > 0 and abs(ohlc["close"][idx] - ohlc["open"][idx]) / candle_range <= self.params.base_body_max_ratio

    def _leg_in(self, ohlc: dict[str, np.ndarray], base_start: int, atr: float) -> tuple[str, int, float] | None:
        if base_start < 1 or self._is_base_like(ohlc, base_start - 1):
            return None  # a base-like candle next to the base belongs to the base
        for length in range(1, self.params.leg_in_max_candles + 1):
            first = base_start - length
            if first < 0:
                break
            body = self._signed_body(ohlc, first, base_start)
            if body >= self.params.leg_in_min_atr * atr:
                return "up", length, body
            if -body >= self.params.leg_in_min_atr * atr:
                return "down", length, -body
        return None

    def _departure(
        self, ohlc: dict[str, np.ndarray], base_stop: int, atr: float, base_high: float, base_low: float
    ) -> tuple[ZoneType, int, float] | None:
        if base_stop >= len(ohlc["close"]) or self._is_base_like(ohlc, base_stop):
            return None  # a base-like candle next to the base belongs to the base
        for length in range(1, self.params.departure_max_candles + 1):
            last = base_stop + length - 1
            if last >= len(ohlc["close"]):
                break
            body = self._signed_body(ohlc, base_stop, base_stop + length)
            close = float(ohlc["close"][last])
            if body >= self.params.departure_min_atr * atr and close > base_high:
                return ZoneType.DEMAND, length, body
            if -body >= self.params.departure_min_atr * atr and close < base_low:
                return ZoneType.RESISTANCE, length, -body
        return None

    def _fvg(
        self, candles: pd.DataFrame, zone_type: ZoneType, departure: range, atr: float
    ) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        for middle in departure:
            first, third = middle - 1, middle + 1
            if first < 0 or third >= len(candles):
                continue
            c1, c3 = candles.iloc[first], candles.iloc[third]
            if zone_type == ZoneType.DEMAND:
                gap = float(c3["low"]) - float(c1["high"])
                bounds = (float(c1["high"]), float(c3["low"]))
            else:
                gap = float(c1["low"]) - float(c3["high"])
                bounds = (float(c3["high"]), float(c1["low"]))
            if gap <= 0 or (best is not None and gap <= best["size"]):
                continue
            best = {
                "direction": "bullish" if zone_type == ZoneType.DEMAND else "bearish",
                "size": round(gap, 4),
                "size_atr_z": round(gap / atr, 4),
                "lower": bounds[0],
                "upper": bounds[1],
                "candles": [str(candles.iloc[i]["label"]) for i in (first, middle, third)],
            }
        return best

    # ── Detection ───────────────────────────────────────────────────────────
    def detect(
        self,
        candles: pd.DataFrame,
        *,
        symbol: str,
        timeframe: str,
        exchange: str = "NSE",
        data_source: str = "",
    ) -> list[Zone]:
        """Detect zones on *completed* ``timeframe`` candles (positional index)."""

        params = self.params
        if candles.empty:
            return []
        if timeframe == "Y" and len(candles) < params.yearly_min_candles:
            return []
        period = params.atr_period_for(timeframe)
        atr = atr_series(candles, period, params.atr_method).to_numpy()
        ohlc = {key: candles[key].astype(float).to_numpy() for key in ("open", "high", "low", "close")}

        candidates: list[dict[str, Any]] = []
        for departure_start in range(1, len(candles)):
            for base_len in range(params.base_min_candles, params.base_max_candles + 1):
                base_start = departure_start - base_len
                if base_start < 1:  # a leg-in candle is required before the base
                    break
                atr_z = float(atr[departure_start - 1])
                if not math.isfinite(atr_z) or atr_z <= 0:
                    break
                ok, avg_ratio = self._base_ok(ohlc, base_start, departure_start)
                if not ok:
                    continue
                departure = self._departure(
                    ohlc,
                    departure_start,
                    atr_z,
                    float(ohlc["high"][base_start:departure_start].max()),
                    float(ohlc["low"][base_start:departure_start].min()),
                )
                if departure is None:
                    continue
                leg_in = self._leg_in(ohlc, base_start, atr_z)
                if leg_in is None:
                    continue
                candidates.append(
                    {
                        "base": (base_start, departure_start),
                        "departure": departure,
                        "leg_in": leg_in,
                        "atr_z": atr_z,
                        "avg_ratio": avg_ratio,
                    }
                )
                break  # smallest qualifying base for this departure start

        zones: list[Zone] = []
        claimed: set[int] = set()
        for candidate in sorted(candidates, key=lambda c: c["base"][1] + c["departure"][1]):
            base_start, base_stop = candidate["base"]
            zone_type, dep_len, dep_body = candidate["departure"]
            if claimed.intersection(range(base_start, base_stop)):
                continue
            zone = self._build_zone(
                candles, candidate, symbol=symbol, timeframe=timeframe, exchange=exchange, data_source=data_source
            )
            if zone is None:
                continue
            claimed.update(range(base_start, base_stop + dep_len))
            zones.append(zone)
        return zones

    def _build_zone(
        self,
        candles: pd.DataFrame,
        candidate: dict[str, Any],
        *,
        symbol: str,
        timeframe: str,
        exchange: str,
        data_source: str,
    ) -> Zone | None:
        params = self.params
        base_start, base_stop = candidate["base"]
        zone_type, dep_len, dep_body = candidate["departure"]
        leg_dir, leg_len, leg_body = candidate["leg_in"]
        atr_z = candidate["atr_z"]
        base = candles.iloc[base_start:base_stop]
        body_tops = base[["open", "close"]].max(axis=1).astype(float)
        body_bottoms = base[["open", "close"]].min(axis=1).astype(float)

        if zone_type == ZoneType.DEMAND:
            pattern = Pattern.DBR if leg_dir == "down" else Pattern.RBR
            proximal_raw, distal_raw = float(body_tops.max()), float(base["low"].astype(float).min())
            tick = params.tick_size(proximal_raw)
            proximal, distal = round_to_tick(proximal_raw, tick, "up"), round_to_tick(distal_raw, tick, "down")
        else:
            pattern = Pattern.RBD if leg_dir == "up" else Pattern.DBD
            proximal_raw, distal_raw = float(body_bottoms.min()), float(base["high"].astype(float).max())
            tick = params.tick_size(proximal_raw)
            proximal, distal = round_to_tick(proximal_raw, tick, "down"), round_to_tick(distal_raw, tick, "up")

        departure_range = range(base_stop, base_stop + dep_len)
        zone = Zone(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            zone_type=zone_type,
            pattern=pattern,
            construction_method=CONSTRUCTION_METHOD,
            proximal=proximal,
            distal=distal,
            proximal_raw=proximal_raw,
            distal_raw=distal_raw,
            atr_z=round(atr_z, 4),
            atr_z_period=params.atr_period_for(timeframe),
            base_candles=[candle_record(candles, i) for i in range(base_start, base_stop)],
            leg_in_candles=[candle_record(candles, i) for i in range(base_start - leg_len, base_start)],
            departure_candles=[candle_record(candles, i) for i in departure_range],
            leg_in_body=round(leg_body, 4),
            departure_body=round(dep_body, 4),
            base_avg_body_ratio=round(candidate["avg_ratio"], 4),
            fvg=self._fvg(candles, zone_type, departure_range, atr_z),
            param_version=params.version,
            data_source=data_source,
        )
        raw_width = abs(proximal_raw - distal_raw)
        violations = []
        if raw_width > params.max_width_atr * atr_z:
            violations.append(f"width {raw_width / atr_z:.2f}×ATR_Z exceeds {params.max_width_atr}×ATR_Z")
        pct_cap = params.width_pct_cap(timeframe)
        if proximal_raw > 0 and raw_width / proximal_raw * 100 > pct_cap:
            violations.append(f"width {raw_width / proximal_raw * 100:.2f}% exceeds {pct_cap}% cap")
        if violations:
            if params.width_violation_action == "reject":
                return None
            zone.review_reasons.extend(violations)
        return zone
