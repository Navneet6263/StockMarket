"""Market breadth gate based on benchmark structure and the full scan universe."""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Mapping


logger = logging.getLogger(__name__)

ENABLE_BREADTH_GATE: bool = os.getenv("ENABLE_BREADTH_GATE", "true").lower() == "true"
BEARISH_BLOCK_FRESH_BUY: bool = os.getenv("BEARISH_BLOCK_FRESH_BUY", "true").lower() == "true"


def _close_values(frame: Any) -> list[float]:
    """Extract finite closes from either a DataFrame or a light test mapping."""

    if frame is None:
        return []
    try:
        raw = frame["Close"]
    except (KeyError, TypeError):
        try:
            raw = frame["close"]
        except (KeyError, TypeError):
            return []
    try:
        values = raw.tolist()
    except AttributeError:
        try:
            values = list(raw)
        except TypeError:
            return []

    clean: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            clean.append(number)
    return clean


def _ema(values: list[float], span: int) -> list[float]:
    """Return values equivalent to pandas ``ewm(adjust=False)``."""

    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    output = [values[0]]
    for value in values[1:]:
        output.append((alpha * value) + ((1.0 - alpha) * output[-1]))
    return output


def compute_full_universe_breadth(
    daily_frames: Mapping[str, Any] | None,
    expected_universe_count: int | None = None,
) -> Dict:
    """Compute A/D and EMA participation from every usable daily frame."""

    frames = daily_frames or {}
    universe_count = max(len(frames), int(expected_universe_count or 0))
    advancing = 0
    declining = 0
    unchanged = 0
    breadth_sample = 0
    above_ema20 = 0
    ema20_sample = 0

    for frame in frames.values():
        closes = _close_values(frame)
        if len(closes) < 2:
            continue
        breadth_sample += 1
        if closes[-1] > closes[-2]:
            advancing += 1
        elif closes[-1] < closes[-2]:
            declining += 1
        else:
            unchanged += 1

        # A partial history may contribute to A/D, but it must not be called
        # above/below the 20 EMA until 20 completed observations exist.
        if len(closes) >= 20:
            ema20_sample += 1
            if closes[-1] > _ema(closes, 20)[-1]:
                above_ema20 += 1

    return {
        "breadthSource": "full_daily_frames" if breadth_sample else "unavailable",
        "breadthUniverseCount": universe_count,
        "breadthSampleCount": breadth_sample,
        "breadthCoveragePct": round((breadth_sample / universe_count) * 100, 1) if universe_count else 0.0,
        "ema20SampleCount": ema20_sample,
        "ema20CoveragePct": round((ema20_sample / universe_count) * 100, 1) if universe_count else 0.0,
        "advancingCount": advancing,
        "decliningCount": declining,
        "unchangedCount": unchanged,
        "advanceDeclineRatio": round(advancing / max(declining, 1), 2),
        "pctStocksAboveEma20": round((above_ema20 / ema20_sample) * 100, 1) if ema20_sample else None,
    }


def _selected_signal_breadth(all_signals: List[Dict]) -> Dict:
    """Compatibility fallback, explicitly labelled as a shortlist sample."""

    signal_count = len(all_signals)
    advancing = sum(1 for signal in all_signals if (signal.get("change_pct") or 0) > 0)
    declining = sum(1 for signal in all_signals if (signal.get("change_pct") or 0) < 0)
    unchanged = signal_count - advancing - declining
    above_ema20 = sum(
        1
        for signal in all_signals
        if signal.get("price_above_ema20")
        or signal.get("chart_features", {}).get("moving_average_trend") == "uptrend"
    )
    denominator = signal_count or 1
    return {
        "breadthSource": "selected_signals_fallback",
        "breadthUniverseCount": signal_count,
        "breadthSampleCount": signal_count,
        "breadthCoveragePct": 100.0 if signal_count else 0.0,
        "ema20SampleCount": signal_count,
        "ema20CoveragePct": 100.0 if signal_count else 0.0,
        "advancingCount": advancing,
        "decliningCount": declining,
        "unchangedCount": unchanged,
        "advanceDeclineRatio": round(advancing / max(declining, 1), 2),
        "pctStocksAboveEma20": round((above_ema20 / denominator) * 100, 1),
    }


def compute_market_breadth(
    benchmark_frame: Any,
    all_signals: List[Dict],
    daily_frames: Mapping[str, Any] | None = None,
    expected_universe_count: int | None = None,
) -> Dict:
    """Compute market mood without letting a ranked shortlist define breadth."""

    if not ENABLE_BREADTH_GATE:
        return _unknown_context("breadth_gate_disabled")

    benchmark_closes = _close_values(benchmark_frame)
    if len(benchmark_closes) < 50:
        logger.debug("[BREADTH] Insufficient benchmark history - returning unknown")
        return _unknown_context("insufficient_benchmark_history")

    try:
        ema_20 = _ema(benchmark_closes, 20)
        ema_50 = _ema(benchmark_closes, 50)
        latest_close = benchmark_closes[-1]
        prev_close = benchmark_closes[-2]
        daily_change_pct = round((latest_close / prev_close - 1) * 100, 2)
        above_ema20 = latest_close > ema_20[-1]
        above_ema50 = latest_close > ema_50[-1]
        ema20_slope = (
            ((ema_20[-1] / ema_20[-6]) - 1) * 100
            if len(ema_20) >= 6 and ema_20[-6]
            else 0.0
        )

        breadth = compute_full_universe_breadth(
            daily_frames,
            expected_universe_count=expected_universe_count,
        )
        if not breadth["breadthSampleCount"]:
            breadth = _selected_signal_breadth(all_signals)

        advancing = int(breadth["advancingCount"])
        declining = int(breadth["decliningCount"])
        total = int(breadth["breadthSampleCount"])
        ad_ratio = float(breadth["advanceDeclineRatio"])
        pct_above_ema20 = breadth["pctStocksAboveEma20"]

        bearish_signals = 0
        bullish_signals = 0

        # Preserve benchmark gating. Index weakness remains deliberately
        # asymmetric because fresh longs are most vulnerable below the 20 EMA.
        if above_ema20:
            bullish_signals += 1
        else:
            bearish_signals += 2
        if above_ema50:
            bullish_signals += 1
        else:
            bearish_signals += 1
        if daily_change_pct >= 0.3:
            bullish_signals += 1
        elif daily_change_pct <= -0.5:
            bearish_signals += 1
        if ema20_slope > 0:
            bullish_signals += 1
        elif ema20_slope < -0.1:
            bearish_signals += 1

        # A reliable broad sample gets enough weight to beat a cherry-picked
        # shortlist. Sparse samples retain the previous one-vote behaviour.
        breadth_is_reliable = bool(
            total >= 5
            and float(breadth.get("breadthCoveragePct") or 0) >= 50.0
        )
        if breadth_is_reliable and pct_above_ema20 is not None:
            if pct_above_ema20 >= 55:
                bullish_signals += 2
            elif pct_above_ema20 <= 35:
                bearish_signals += 2
            if ad_ratio >= 1.5:
                bullish_signals += 2
            elif ad_ratio <= 0.67:
                bearish_signals += 2
        elif pct_above_ema20 is not None:
            if pct_above_ema20 >= 55:
                bullish_signals += 1
            elif pct_above_ema20 <= 35:
                bearish_signals += 1

        if bearish_signals >= 4:
            mood = "bearish"
        elif bullish_signals >= 4:
            mood = "bullish"
        else:
            mood = "neutral"

        logger.debug(
            "[BREADTH] mood=%s source=%s sample=%d coverage=%.1f above20=%.1f",
            mood,
            breadth["breadthSource"],
            total,
            breadth["breadthCoveragePct"],
            float(pct_above_ema20 or 0),
        )

        return {
            "marketMood": mood,
            "benchmarkAboveEma20": above_ema20,
            "benchmarkAboveEma50": above_ema50,
            "benchmarkDailyChangePct": daily_change_pct,
            "ema20Slope": round(ema20_slope, 3),
            **breadth,
            "totalScanned": total,
            "bullishVotes": bullish_signals,
            "bearishVotes": bearish_signals,
            "breadthGateEnabled": True,
            "freshBuyBlocked": mood == "bearish" and BEARISH_BLOCK_FRESH_BUY,
            "blockedReason": (
                "Market breadth weak; fresh buy calls blocked."
                if mood == "bearish" and BEARISH_BLOCK_FRESH_BUY
                else None
            ),
        }
    except Exception as exc:
        logger.warning("[BREADTH] compute failed: %s", exc)
        return _unknown_context(f"compute_error: {exc}")


def should_block_buy(market_context: Dict) -> bool:
    """Return whether the breadth gate blocks fresh long entries."""

    if not ENABLE_BREADTH_GATE or not BEARISH_BLOCK_FRESH_BUY:
        return False
    return bool(market_context.get("freshBuyBlocked"))


def _unknown_context(reason: str) -> Dict:
    return {
        "marketMood": "unknown",
        "benchmarkAboveEma20": None,
        "benchmarkAboveEma50": None,
        "benchmarkDailyChangePct": None,
        "ema20Slope": None,
        "advanceDeclineRatio": None,
        "pctStocksAboveEma20": None,
        "advancingCount": None,
        "decliningCount": None,
        "unchangedCount": None,
        "totalScanned": None,
        "breadthSource": "unavailable",
        "breadthUniverseCount": 0,
        "breadthSampleCount": 0,
        "breadthCoveragePct": 0.0,
        "ema20SampleCount": 0,
        "ema20CoveragePct": 0.0,
        "breadthGateEnabled": ENABLE_BREADTH_GATE,
        "freshBuyBlocked": False,
        "blockedReason": None,
        "unavailableReason": reason,
    }
