from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List


class ZoneDetector:
    """Detect price-action supply and demand zones.

    Zone formation and zone lifecycle are deliberately kept separate.  A
    zone is not eligible to be tested until its complete, contiguous leg-out
    has ended.  The last candle in the supplied history is treated as the
    current candle, so its *first* touch can be returned as
    ``currently_testing`` instead of being mistaken for an already-consumed
    historical zone.
    """

    ACTIVE_STATUSES = frozenset({"fresh", "currently_testing"})

    def __init__(self):
        self.body_threshold = 0.5  # Base candle if body <= 50% of range.

    @staticmethod
    def _raw_records(history: Any, max_lookback: int) -> list[Mapping[str, Any]]:
        """Return the latest candle records without requiring pandas at import.

        Production callers pass a DataFrame, while accepting a sequence of
        mappings keeps the detector deterministic and easy to test in
        isolation.
        """

        if history is None:
            return []

        limit = max(3, int(max_lookback))
        if hasattr(history, "tail"):
            window = history.tail(limit)
            if bool(getattr(window, "empty", False)):
                return []
            try:
                records = window.to_dict(orient="records")
            except TypeError:
                records = window.to_dict("records")
            return [record for record in records if isinstance(record, Mapping)]

        if isinstance(history, Mapping):
            return []
        if isinstance(history, Sequence):
            return [record for record in list(history)[-limit:] if isinstance(record, Mapping)]
        return []

    def _classify_candles(self, history: Any, max_lookback: int = 150) -> list[dict[str, Any]]:
        candles: list[dict[str, Any]] = []
        for source_idx, raw in enumerate(self._raw_records(history, max_lookback)):
            normalised = {str(key).lower(): value for key, value in raw.items()}
            try:
                open_price = float(normalised["open"])
                high = float(normalised["high"])
                low = float(normalised["low"])
                close = float(normalised["close"])
            except (KeyError, TypeError, ValueError):
                continue

            if not all(math.isfinite(value) for value in (open_price, high, low, close)):
                continue
            if high < low:
                continue

            candle_range = max(high - low, 0.0001)
            body = abs(close - open_price)
            body_pct = body / candle_range
            candles.append(
                {
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "range": candle_range,
                    "body": body,
                    "body_pct": body_pct,
                    "is_exciting": body_pct > self.body_threshold,
                    "is_base": body_pct <= self.body_threshold,
                    "color": "green" if close >= open_price else "red",
                    "source_idx": source_idx,
                }
            )
        return candles

    @staticmethod
    def _touches_zone(zone: Mapping[str, Any], candle: Mapping[str, Any]) -> bool:
        if zone["type"] == "demand":
            return float(candle["low"]) <= float(zone["proximal"]) and float(candle["high"]) >= float(zone["distal"])
        return float(candle["high"]) >= float(zone["proximal"]) and float(candle["low"]) <= float(zone["distal"])

    @staticmethod
    def _breaks_zone(zone: Mapping[str, Any], candle: Mapping[str, Any]) -> bool:
        # A trade through the distal/invalidation edge makes the zone unsafe.
        # Exact contact is still a test; it is not a break until price crosses.
        if zone["type"] == "demand":
            return float(candle["low"]) < float(zone["distal"])
        return float(candle["high"]) > float(zone["distal"])

    def _classify_zone_status(self, zone: dict[str, Any], candles: list[dict[str, Any]]) -> dict[str, Any]:
        """Attach a mutually-exclusive lifecycle state to ``zone``.

        Historical candles stop at ``len(candles) - 2``.  The final candle is
        evaluated separately as the current candle, which is the key to
        exposing a first live retest.
        """

        first_eligible_idx = int(zone["leg_out_end_idx"]) + 1
        current_idx = len(candles) - 1
        prior_touch_count = 0
        first_touch_idx: int | None = None
        broken_idx: int | None = None

        for idx in range(first_eligible_idx, current_idx):
            candle = candles[idx]
            if self._breaks_zone(zone, candle):
                broken_idx = idx
                break
            if self._touches_zone(zone, candle):
                prior_touch_count += 1
                if first_touch_idx is None:
                    first_touch_idx = idx

        current_touch = False
        if broken_idx is None and current_idx >= first_eligible_idx:
            current = candles[current_idx]
            if self._breaks_zone(zone, current):
                broken_idx = current_idx
            else:
                current_touch = self._touches_zone(zone, current)
                if current_touch and first_touch_idx is None:
                    first_touch_idx = current_idx

        if broken_idx is not None:
            status = "broken"
        elif prior_touch_count:
            status = "consumed"
        elif current_touch:
            status = "currently_testing"
        else:
            status = "fresh"

        zone.update(
            {
                "status": status,
                "zone_status": status,
                "is_fresh": status == "fresh",
                "currently_testing": status == "currently_testing",
                "is_consumed": status == "consumed",
                "is_broken": status == "broken",
                # Compatibility with the former binary field.  A current
                # first touch is tested, but remains active/actionable context.
                "is_tested": status != "fresh",
                "is_active": status in self.ACTIVE_STATUSES,
                "prior_touch_count": prior_touch_count,
                "test_count": prior_touch_count + int(current_touch),
                "first_touch_idx": first_touch_idx,
                "broken_idx": broken_idx,
            }
        )
        return zone

    def detect_zones(
        self,
        history: Any,
        max_lookback: int = 150,
        include_inactive: bool = False,
    ) -> List[Dict]:
        candles = self._classify_candles(history, max_lookback=max_lookback)
        if len(candles) < 3:
            return []

        zones: list[dict[str, Any]] = []
        i = 1
        while i < len(candles) - 1:
            if not candles[i]["is_base"]:
                i += 1
                continue

            base_start = i
            base_end = i
            while base_end + 1 < len(candles) and candles[base_end + 1]["is_base"]:
                base_end += 1

            # A zone needs an exciting leg-in and at least one exciting
            # leg-out candle after the complete base.
            if base_start > 0 and base_end + 1 < len(candles):
                leg_in = candles[base_start - 1]
                leg_out_start = base_end + 1
                leg_out = candles[leg_out_start]

                if leg_in["is_exciting"] and leg_out["is_exciting"]:
                    base_candles = candles[base_start : base_end + 1]
                    if len(base_candles) <= 6:
                        direction_color = leg_out["color"]
                        leg_out_end = leg_out_start
                        while (
                            leg_out_end + 1 < len(candles)
                            and candles[leg_out_end + 1]["is_exciting"]
                            and candles[leg_out_end + 1]["color"] == direction_color
                        ):
                            leg_out_end += 1
                        leg_out_count = leg_out_end - leg_out_start + 1

                        base_range_max = max(candle["high"] - candle["low"] for candle in base_candles)
                        has_displacement = leg_out_count > 1 or leg_out["body"] >= base_range_max * 1.2

                        if has_displacement and direction_color == "green":
                            highest_body = max(max(candle["open"], candle["close"]) for candle in base_candles)
                            base_lowest_wick = min(candle["low"] for candle in base_candles)
                            lowest_wick = min(base_lowest_wick, leg_in["low"], leg_out["low"])
                            pattern = "DBR" if leg_in["color"] == "red" else "RBR"
                            base_strength = 2 if pattern == "DBR" else 1

                            close_strength = (leg_out["close"] - leg_out["low"]) / max(leg_out["range"], 0.0001)
                            if close_strength > 0.8:
                                base_strength += 2
                            if leg_out["open"] > max(candle["high"] for candle in base_candles):
                                base_strength += 3

                            zones.append(
                                {
                                    "type": "demand",
                                    "pattern": pattern,
                                    "proximal": highest_body,
                                    "distal": lowest_wick,
                                    "start_idx": base_start,
                                    # ``end_idx`` now means the true formation
                                    # end.  Keep explicit base indices as well.
                                    "end_idx": leg_out_end,
                                    "base_start_idx": base_start,
                                    "base_end_idx": base_end,
                                    "leg_out_start_idx": leg_out_start,
                                    "leg_out_end_idx": leg_out_end,
                                    "leg_out_candle_count": leg_out_count,
                                    "strength": base_strength + leg_out_count,
                                }
                            )

                        elif has_displacement and direction_color == "red":
                            lowest_body = min(min(candle["open"], candle["close"]) for candle in base_candles)
                            base_highest_wick = max(candle["high"] for candle in base_candles)
                            highest_wick = max(base_highest_wick, leg_in["high"], leg_out["high"])
                            pattern = "RBD" if leg_in["color"] == "green" else "DBD"
                            base_strength = 2 if pattern == "RBD" else 1

                            close_strength = (leg_out["high"] - leg_out["close"]) / max(leg_out["range"], 0.0001)
                            if close_strength > 0.8:
                                base_strength += 2
                            if leg_out["open"] < min(candle["low"] for candle in base_candles):
                                base_strength += 3

                            zones.append(
                                {
                                    "type": "supply",
                                    "pattern": pattern,
                                    "proximal": lowest_body,
                                    "distal": highest_wick,
                                    "start_idx": base_start,
                                    "end_idx": leg_out_end,
                                    "base_start_idx": base_start,
                                    "base_end_idx": base_end,
                                    "leg_out_start_idx": leg_out_start,
                                    "leg_out_end_idx": leg_out_end,
                                    "leg_out_candle_count": leg_out_count,
                                    "strength": base_strength + leg_out_count,
                                }
                            )

            i = base_end + 1

        classified = [self._classify_zone_status(zone, candles) for zone in zones]
        if include_inactive:
            return classified
        return [zone for zone in classified if zone["is_active"]]

    def apply_lotl_merging(self, zones: List[Dict], threshold_pct: float = 1.5) -> List[Dict]:
        """
        Level Over The Level (LOTL): Merge adjacent zones of the same type if they are within threshold_pct of each other.
        """
        if not zones:
            return []

        demand = sorted([z for z in zones if z['type'] == 'demand'], key=lambda x: x['proximal'])
        supply = sorted([z for z in zones if z['type'] == 'supply'], key=lambda x: x['proximal'])

        merged_zones = []

        def _merge_list(zone_list, is_demand):
            if not zone_list:
                return []
            merged = [zone_list[0]]
            for current in zone_list[1:]:
                prev = merged[-1]
                # For demand, prev is lower. Check if current's distal is close to prev's proximal
                if is_demand:
                    dist = abs(current['distal'] - prev['proximal']) / prev['proximal'] * 100
                    if dist <= threshold_pct:
                        # Merge: proximal is the higher one (current), distal is the lower one (prev)
                        prev['proximal'] = max(prev['proximal'], current['proximal'])
                        prev['distal'] = min(prev['distal'], current['distal'])
                        prev['strength'] += current['strength']
                    else:
                        merged.append(current)
                else:
                    # For supply, check if current's proximal is close to prev's distal
                    dist = abs(current['proximal'] - prev['distal']) / prev['distal'] * 100
                    if dist <= threshold_pct:
                        prev['proximal'] = min(prev['proximal'], current['proximal'])
                        prev['distal'] = max(prev['distal'], current['distal'])
                        prev['strength'] += current['strength']
                    else:
                        merged.append(current)
            return merged

        merged_zones.extend(_merge_list(demand, True))
        merged_zones.extend(_merge_list(supply, False))

        return merged_zones
