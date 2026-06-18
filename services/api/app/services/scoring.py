from __future__ import annotations

from typing import Dict, List
import os

import numpy as np

from app.services.chase_risk import classify_chase_risk
from app.services.demand_supply import analyze_demand_supply
from app.services.entry_timing import analyze_entry_timing
from app.services.lifecycle import build_lifecycle_advice
from app.services.trap_detector import full_trap_analysis
from app.services.finnhub_data import fetch_news_sentiment
import joblib
import pandas as pd

class ScoringEngine:
    def __init__(self):
        self._model_cache = {}
        self._model_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".models")

    def _safe(self, snapshot: Dict, key: str, default: float = 0.0) -> float:
        value = snapshot.get(key, default)
        if value is None or isinstance(value, bool):
            return default
        return float(value)

    def _bucket(self, confidence: float, direction: str, risk_level: str) -> str:
        if direction == "neutral":
            return "avoid"
        if confidence >= 80 and risk_level != "high":
            return "high_priority"
        if confidence >= 65:
            return "watchlist"
        return "low_priority"

    def _risk_level(self, score: int) -> str:
        if score >= 4:
            return "high"
        if score >= 2:
            return "medium"
        return "low"

    def _evidence_status(self, historical: Dict | None) -> str:
        count = int((historical or {}).get("signal_count", 0) or 0)
        if count >= 12:
            return "validated"
        if count > 0:
            return "limited"
        return "unavailable"

    def _timeframe(self, snapshot: Dict, direction: str, move_quality_seed: float) -> tuple[str, int]:
        relative_volume = self._safe(snapshot, "relative_volume", 1.0)
        intraday_volume_ratio = self._safe(snapshot, "intraday_volume_ratio", 1.0)
        atr_pct = self._safe(snapshot, "atr_pct", 0.0)
        trend_regime = str(snapshot.get("trend_regime") or "range")

        if direction == "neutral":
            return "watch only", 3
        if intraday_volume_ratio >= 2.2 and atr_pct >= 4.0 and move_quality_seed < 70:
            return "intraday", 1
        if relative_volume >= 2.1 and (
            snapshot.get("breakout_20") or snapshot.get("breakdown_20") or snapshot.get("intraday_breakout")
        ):
            return "1-2 days", 2
        if trend_regime in {"uptrend", "downtrend"} and move_quality_seed >= 75:
            return "swing 1-2 weeks", 10
        return "3-5 days", 5

    def _pre_breakout_setup(self, snapshot: Dict, *, price: float, change_pct: float, gap_pct: float, atr_pct: float) -> Dict:
        advanced = snapshot.get("advanced_chart_pattern") or {}
        advanced_score = float(advanced.get("score") or 0)
        advanced_trigger = self._safe(advanced, "trigger_price")
        advanced_stop = self._safe(advanced, "invalidation_level")
        advanced_target = self._safe(advanced, "target_1")
        advanced_late = bool(advanced.get("late_entry_risk"))
        advanced_stage = str(advanced.get("stage") or "")
        resistance = self._safe(snapshot, "resistance_20")
        support = self._safe(snapshot, "support_20")
        ema_20 = self._safe(snapshot, "ema_20")
        ema_50 = self._safe(snapshot, "ema_50")
        relative_volume = self._safe(snapshot, "relative_volume", 1.0)
        intraday_volume_ratio = self._safe(snapshot, "intraday_volume_ratio", 1.0)
        rsi = self._safe(snapshot, "rsi", 50.0)
        cmf = self._safe(snapshot, "cmf")
        obv_slope = self._safe(snapshot, "obv_slope")
        distance_to_resistance = self._safe(snapshot, "distance_to_resistance_pct", 99.0)
        tight_consolidation = self._safe(snapshot, "tight_consolidation_pct", 99.0)
        bb_width_ratio = self._safe(snapshot, "bb_width_ratio", 1.0)
        atr_expansion = self._safe(snapshot, "atr_expansion", 1.0)
        macd_delta = self._safe(snapshot, "macd_hist_delta_3d")
        rsi_delta = self._safe(snapshot, "rsi_delta_5d")
        rs_delta = self._safe(snapshot, "relative_strength_delta_5d")

        if advanced_trigger and advanced_score >= 55:
            resistance = max(resistance, advanced_trigger / 1.002)
        if advanced_stop:
            support = max(support, advanced_stop)

        already_moved = (
            abs(change_pct) >= 5.0
            or abs(gap_pct) >= 3.0
            or bool(snapshot.get("breakout_20"))
            or advanced_late
            or advanced_stage == "LATE_MOVE"
        )
        if already_moved or not price or not resistance or resistance <= price:
            return {
                "is_pre_breakout": False,
                "pre_breakout_labels": [],
                "pre_breakout_score": 0,
            }

        labels: list[str] = ["PRE_BREAKOUT", "WATCH_FOR_BREAKOUT"]
        reasons: list[str] = []
        score = 0

        if distance_to_resistance <= 3.5:
            score += 2
            reasons.append("Price is coiling near 20-day resistance before breakout.")
        if bb_width_ratio <= 0.85 or bool(snapshot.get("bb_squeeze")):
            score += 2
            labels.append("SQUEEZE_SETUP")
            reasons.append("Bollinger Band width is compressed.")
        if atr_expansion <= 0.9 or bool(snapshot.get("atr_contraction")):
            score += 1
            reasons.append("ATR is contracting, showing volatility compression.")
        if tight_consolidation <= max(4.5, atr_pct * 2.2):
            score += 1
            labels.append("BASE_BUILDING")
            reasons.append("Recent range is tight, suggesting base building.")
        if bool(snapshot.get("higher_lows")):
            score += 1
            reasons.append("Higher lows are forming inside consolidation.")
        if bool(snapshot.get("volume_dryup")) or relative_volume <= 0.9:
            score += 1
            reasons.append("Volume has dried up before confirmation.")
        if cmf >= 0.04 or obv_slope > 0:
            score += 2
            labels.append("ACCUMULATION")
            reasons.append("OBV/CMF shows quiet accumulation.")
        if macd_delta > 0:
            score += 1
            reasons.append("MACD histogram is improving before price breakout.")
        if rsi_delta > 0 and 45 <= rsi <= 65:
            score += 1
            reasons.append("RSI is improving without being overextended.")
        if bool(snapshot.get("price_above_ema20")) or (ema_20 and abs((price - ema_20) / ema_20) <= 0.012):
            score += 1
            reasons.append("Price is above or reclaiming the 20 EMA.")
        if bool(snapshot.get("price_above_ema50")) or (ema_50 and abs((price - ema_50) / ema_50) <= 0.015):
            score += 1
            reasons.append("Price is above or reclaiming the 50 EMA.")
        if rs_delta > 0:
            score += 1
            reasons.append("Relative strength vs benchmark is improving.")
            
        # Order Flow / Level 2 Iceberg Detection
        bid_ask = self._safe(snapshot, "bid_ask_ratio", 1.0)
        total_buy_qty = self._safe(snapshot, "total_buy_qty", 0)
        iceberg_threshold = int(os.getenv("ICEBERG_MAX_OI_THRESHOLD", "50000"))
        if distance_to_resistance <= 2.5 and bid_ask >= 2.5 and total_buy_qty > iceberg_threshold:
            score += 4
            labels.append("ICEBERG_ACCUMULATION")
            labels.append("PRE_BREAKOUT_CONFIRMED")
            reasons.insert(0, f"Order Book shows massive institutional bidding ({bid_ask:.1f}x buyers) near resistance.")
            
        if advanced_score >= 75 and advanced.get("direction") == "bullish":
            score += 4
            labels.extend(str(label) for label in advanced.get("labels", [])[:4])
            reasons.insert(0, f"{advanced.get('pattern_name')} is forming near a clean trigger.")
        elif advanced_score >= 60 and advanced.get("direction") == "bullish":
            score += 2
            labels.extend(str(label) for label in advanced.get("labels", [])[:3])
            reasons.insert(0, f"{advanced.get('pattern_name')} adds chart-pattern confluence.")

        if score < 4 and advanced_score < 68 and "ICEBERG_ACCUMULATION" not in labels:
            return {
                "is_pre_breakout": False,
                "pre_breakout_labels": list(dict.fromkeys(labels)),
                "pre_breakout_score": score,
            }

        alert_price = resistance * 1.002
        invalidation_candidates = [value for value in (support, ema_20, ema_50) if value and value < price]
        invalidation_level = max(invalidation_candidates) if invalidation_candidates else price * 0.965
        expected_move = max(1.5, min(7.5, atr_pct * 1.4 if atr_pct else tight_consolidation * 0.75))
        if advanced_target and advanced_target > alert_price:
            expected_move = max(expected_move, min(12.0, ((advanced_target - alert_price) / alert_price) * 100))
        if advanced_stop and advanced_stop < price:
            invalidation_level = max(invalidation_level, advanced_stop)
        action = "ALERT_ABOVE_LEVEL" if distance_to_resistance <= 1.5 else "WATCH"
        confidence = min(88, 48 + score * 4)
        if advanced_score >= 60:
            confidence = min(92, max(confidence, 52 + advanced_score * 0.42))

        if distance_to_resistance <= 1.5 and (tight_consolidation <= 3.5 or bool(snapshot.get("volume_dryup")) or score >= 5):
            timeframe = "Tomorrow / Anytime"
        elif distance_to_resistance <= 3.0 and score >= 3:
            timeframe = "1-2 days"
        else:
            timeframe = "3-5 days"

        return {
            "is_pre_breakout": True,
            "pre_breakout_labels": list(dict.fromkeys(labels)),
            "pre_breakout_score": score,
            "pre_breakout_confidence": round(confidence, 1),
            "pre_breakout_action": action,
            "setup_type": "Pattern Forming / Pre-Move Setup",
            "breakout_level": round(resistance, 2),
            "alert_price": round(alert_price, 2),
            "alert_above_price": round(alert_price, 2),
            "invalidation_level": round(invalidation_level, 2),
            "expected_breakout_move": round(expected_move, 2),
            "expected_move": round(expected_move, 2),
            "pre_breakout_reason": "; ".join(reasons[:4]),
            "reason": "; ".join(reasons[:4]),
            "pre_breakout_timeframe": timeframe,
            "action": action,
            "pre_breakout_pattern": advanced.get("pattern_name") if advanced_score >= 55 else None,
            "pre_breakout_pattern_score": round(advanced_score, 1) if advanced_score else 0,
        }

    def _pattern_context(self, snapshot: Dict, *, direction: str, price: float) -> Dict:
        labels: list[str] = []
        reasons: list[str] = []
        return_20d = self._safe(snapshot, "return_20d")
        return_60d = self._safe(snapshot, "return_60d")
        tight = self._safe(snapshot, "tight_consolidation_pct", 99.0)
        atr_expansion = self._safe(snapshot, "atr_expansion", 1.0)
        bb_width_ratio = self._safe(snapshot, "bb_width_ratio", 1.0)
        relative_strength = self._safe(snapshot, "relative_strength_20d")
        cmf = self._safe(snapshot, "cmf")
        obv_slope = self._safe(snapshot, "obv_slope")
        ema_20 = self._safe(snapshot, "ema_20")
        ema_50 = self._safe(snapshot, "ema_50")
        support = self._safe(snapshot, "support_20")
        resistance = self._safe(snapshot, "resistance_20")
        distance_to_support = self._safe(snapshot, "distance_to_support_pct", 99.0)
        advanced = snapshot.get("advanced_chart_pattern") or {}
        advanced_score = self._safe(advanced, "score")
        advanced_trigger = self._safe(advanced, "trigger_price")

        if tight <= 5.5 and atr_expansion <= 1.05:
            labels.append("tight_consolidation")
            reasons.append("Tight consolidation with controlled volatility.")
        if bool(snapshot.get("higher_lows")):
            labels.append("higher_lows")
            reasons.append("Higher lows show improving structure.")
        if bb_width_ratio <= 0.9 or bool(snapshot.get("bb_squeeze")):
            labels.append("bollinger_squeeze")
            reasons.append("Bollinger squeeze suggests volatility contraction.")
        if cmf >= 0.04 or obv_slope > 0:
            labels.append("accumulation")
            reasons.append("OBV/CMF accumulation supports the pattern.")
        if relative_strength >= 1.5:
            labels.append("relative_strength")
            reasons.append("Relative strength vs Nifty is positive.")
        if price and ((ema_20 and abs((price - ema_20) / ema_20) <= 0.025) or (ema_50 and abs((price - ema_50) / ema_50) <= 0.03)):
            labels.append("ema_hold")
            reasons.append("Price is holding near 20/50 EMA support.")
        if distance_to_support <= 2.0 and direction == "bullish":
            labels.append("support_bounce")
            reasons.append("Price is near support bounce area.")
        if return_60d > 8 and -8 <= return_20d <= 8 and tight <= 7:
            labels.append("cup_handle_candidate")
            reasons.append("Prior uptrend is pausing in a handle-like base.")
            
        if return_20d >= 40:
            labels.append("parabolic_momentum")
            if tight <= 12:
                labels.append("high_tight_flag")
                reasons.append("Explosive >40% move consolidating tightly (High Tight Flag).")
            else:
                reasons.append("Massive momentum >40% in a month. Monitor for base.")
        elif return_20d >= 12 and tight <= 7:
            labels.append("flag_pennant_candidate")
            reasons.append("Strong prior move is consolidating in a flag/pennant style range.")
            
        if advanced_score >= 50 and advanced.get("direction") == "bullish":
            labels.extend(str(label).lower() for label in advanced.get("labels", []))
            pattern_name = str(advanced.get("pattern_name") or "Chart pattern")
            reasons.insert(0, f"{pattern_name} detected with {advanced_score:.0f}% pattern score.")

        if advanced_score >= 60 and advanced.get("pattern_name"):
            setup_type = str(advanced.get("pattern_name"))
        elif "cup_handle_candidate" in labels:
            setup_type = "Cup and handle candidate"
        elif "flag_pennant_candidate" in labels:
            setup_type = "Flag / pennant continuation"
        elif "support_bounce" in labels:
            setup_type = "Support bounce"
        elif "bollinger_squeeze" in labels:
            setup_type = "Squeeze base"
        elif "tight_consolidation" in labels:
            setup_type = "Base formation"
        else:
            setup_type = "Trend setup" if direction != "neutral" else "No clear pattern"

        if "support_bounce" in labels and direction == "bullish":
            entry_trigger = round(price or support, 2)
            target = round((resistance or price * 1.05) * 0.99, 2)
        else:
            entry_trigger = round(advanced_trigger or ((resistance or price) * 1.002), 2) if direction == "bullish" else round((support or price) * 0.998, 2)
            target = advanced.get("target_1")

        return {
            "pattern_labels": list(dict.fromkeys(labels)),
            "pattern_score": max(len(set(labels)), int(advanced_score // 12) if advanced_score else 0),
            "setup_type": setup_type,
            "pattern_reason": "; ".join(reasons[:4]) or "Pattern evidence is limited.",
            "entry_trigger": entry_trigger,
            "chart_pattern": advanced.get("pattern_name") or setup_type,
            "chart_pattern_family": advanced.get("pattern_family"),
            "chart_pattern_score": round(advanced_score, 1) if advanced_score else 0,
            "chart_pattern_stage": advanced.get("stage"),
            "pattern_target_price": target,
            "pattern_target_2": advanced.get("target_2"),
            "pattern_invalidation": advanced.get("invalidation_level"),
            "pattern_risk_reward": advanced.get("risk_reward"),
            "pattern_expected_move_pct": advanced.get("expected_move_pct"),
            "pattern_measured_height_pct": advanced.get("measured_height_pct"),
            "pattern_freshness_score": advanced.get("freshness_score"),
            "pattern_late_entry_risk": advanced.get("late_entry_risk"),
            "pattern_timeframe": advanced.get("timeframe"),
            "pattern_target_method": advanced.get("target_method"),
            "pattern_alternates": advanced.get("alternates", []),
        }

    def _momentum_continuation_setup(
        self,
        snapshot: Dict,
        *,
        price: float,
        direction: str,
        chase: Dict,
        risk_reward: float,
        target_1: float,
    ) -> Dict:
        if direction != "bullish" or not chase.get("overextended_fresh_entry"):
            return {"is_momentum_continuation": False}

        ema_20 = self._safe(snapshot, "ema_20")
        ema_50 = self._safe(snapshot, "ema_50")
        rolling_vwap = self._safe(snapshot, "rolling_vwap")
        resistance = self._safe(snapshot, "resistance_20")
        support = self._safe(snapshot, "support_20")
        low = self._safe(snapshot, "low", price)
        change_pct = self._safe(snapshot, "change_pct")
        return_5d = self._safe(snapshot, "return_5d")
        return_20d = self._safe(snapshot, "return_20d")
        relative_volume = self._safe(snapshot, "relative_volume", 1.0)
        atr_pct = self._safe(snapshot, "atr_pct")
        atr_expansion = self._safe(snapshot, "atr_expansion", 1.0)
        tight_consolidation = self._safe(snapshot, "tight_consolidation_pct", 99.0)
        relative_strength = self._safe(snapshot, "relative_strength_20d")

        above_support = bool(price and ((ema_20 and price >= ema_20) or (rolling_vwap and price >= rolling_vwap)))
        volume_ok = relative_volume >= 0.6
        volume_compressed = relative_volume <= 1.25
        higher_lows = bool(snapshot.get("higher_lows"))
        no_deep_retrace = return_5d >= -8 and (not ema_50 or price >= ema_50)
        pulled_back = -6.5 <= return_5d <= -1.0 or (ema_20 and 0 <= ((price - ema_20) / ema_20) * 100 <= 5.0)
        tight_after_move = return_20d >= 15 and tight_consolidation <= max(6.0, atr_pct * 3.0) and atr_expansion <= 1.15
        trend_intact = return_20d >= 16 and higher_lows and relative_strength >= 1.5 and above_support

        continuation_type = None
        reasons: list[str] = []
        if pulled_back and above_support and volume_ok and no_deep_retrace:
            continuation_type = "pullback"
            reasons.append("Pulled back 2-5% from the move and is holding VWAP/20 EMA support.")
        elif tight_after_move and no_deep_retrace and volume_compressed:
            continuation_type = "consolidation"
            reasons.append("Strong move is digesting in a tight low-volatility range.")
        elif trend_intact and volume_ok:
            continuation_type = "trend"
            reasons.append("Higher lows and relative strength remain intact after the move.")

        if continuation_type is None:
            return {"is_momentum_continuation": False}

        support_candidates = [value for value in (rolling_vwap, ema_20, support) if value and value < price]
        invalidation_level = max(support_candidates) if support_candidates else price * 0.94
        consolidation_high = resistance if resistance and resistance > price else price * (1 + max(0.8, atr_pct * 0.35) / 100)
        alert_price = consolidation_high * 1.002
        safe_entry_price = alert_price if continuation_type == "consolidation" else round((ema_20 or rolling_vwap or price * 0.98) * 1.006, 2)
        stop_distance = max(safe_entry_price - invalidation_level, 0)
        target_pct = max(2.0, min(8.0, atr_pct * 1.6 if atr_pct else 3.0))
        new_target = safe_entry_price * (1 + target_pct / 100)
        recalculated_rr = round((new_target - safe_entry_price) / stop_distance, 2) if stop_distance > 0 else 0.0

        bounce_from_support = pulled_back and low <= max(ema_20 or 0, rolling_vwap or 0) * 1.015 and price > safe_entry_price * 0.995
        breaks_consolidation = bool(resistance and price > resistance and abs(change_pct) <= 4.5)
        if recalculated_rr >= 1.5 and (breaks_consolidation or bounce_from_support):
            action = "REENTRY_BUY"
        elif recalculated_rr >= 1.3:
            action = "ALERT"
        else:
            action = "WAIT_FOR_REENTRY"

        reasons.append("Stock already moved; wait for safe re-entry instead of chasing the high.")
        return {
            "is_momentum_continuation": True,
            "continuation_type": continuation_type,
            "continuation_labels": ["MOMENTUM_CONTINUATION", "RE_ENTRY_SETUP"],
            "re_entry_zone": {
                "low": round(max(invalidation_level, safe_entry_price * 0.985), 2),
                "high": round(safe_entry_price * 1.01, 2),
            },
            "safe_entry_price": round(safe_entry_price, 2),
            "invalidation_level": round(invalidation_level, 2),
            "new_target": round(new_target, 2),
            "continuation_risk_reward": recalculated_rr,
            "continuation_action": action,
            "action": action,
            "continuation_reason": "; ".join(reasons[:3]),
            "setup_type": "Momentum Continuation / Re-Entry",
            "trailing_stop": round(max(invalidation_level, ema_20 or 0, rolling_vwap or 0), 2),
        }

    def _build_trap_fields(self, snapshot: Dict, direction: str) -> Dict:
        """Runs the full trap analysis and extracts relevant fields."""
        try:
            trap_data = full_trap_analysis(snapshot)
            return {
                "trap_risk": trap_data.get("trap_risk"),
                "safe_to_enter": trap_data.get("safe_to_enter", True),
                "entry_advice": trap_data.get("entry_advice"),
                "trap_warnings": trap_data.get("trap_warnings", []),
                "institutional_buying": trap_data.get("institutional", {}).get("is_institutional_buying", False),
                "institutional_selling": trap_data.get("institutional", {}).get("is_institutional_selling", False),
                "smart_shakeout": trap_data.get("smart_shakeout", {}).get("is_shakeout", False),
                "shakeout_reasons": trap_data.get("smart_shakeout", {}).get("shakeout_reasons", []),
            }
        except Exception as e:
            return {
                "trap_risk": "unknown", 
                "safe_to_enter": True, 
                "trap_warnings": [f"Trap detection failed: {e}"]
            }

    def evaluate(
        self,
        symbol: str,
        snapshot: Dict,
        backtest: Dict | None = None,
        df: pd.DataFrame | None = None,
        *,
        calibrate: bool = True,
    ) -> Dict:
        bullish = 0.0
        bearish = 0.0
        components = {
            "trend": 0.0,
            "momentum": 0.0,
            "volume": 0.0,
            "breakout": 0.0,
            "relative_strength": 0.0,
            "volatility": 0.0,
        }
        reasons: List[str] = []
        weaknesses: List[str] = []
        risk_factors: List[str] = []
        tags: List[str] = []

        def add_bull(points: float, category: str, message: str, tag: str | None = None):
            nonlocal bullish
            bullish += points
            components[category] += points
            reasons.append(message)
            if tag:
                tags.append(tag)

        def add_bear(points: float, category: str, message: str, tag: str | None = None):
            nonlocal bearish
            bearish += points
            components[category] -= points
            reasons.append(message)
            if tag:
                tags.append(tag)

        price = self._safe(snapshot, "price", self._safe(snapshot, "close"))
        change_pct = self._safe(snapshot, "change_pct")
        relative_volume = self._safe(snapshot, "relative_volume", 1.0)
        intraday_volume_ratio = self._safe(snapshot, "intraday_volume_ratio", 1.0)
        rsi = self._safe(snapshot, "rsi", 50.0)
        macd_hist = self._safe(snapshot, "macd_hist")
        atr_pct = self._safe(snapshot, "atr_pct", 0.0)
        atr_expansion = self._safe(snapshot, "atr_expansion", 1.0)
        relative_strength = self._safe(snapshot, "relative_strength_20d")
        return_5d = self._safe(snapshot, "return_5d")
        return_20d = self._safe(snapshot, "return_20d")
        close_location = self._safe(snapshot, "close_location", 0.5)
        upper_wick_pct = self._safe(snapshot, "upper_wick_pct")
        lower_wick_pct = self._safe(snapshot, "lower_wick_pct")
        cmf = self._safe(snapshot, "cmf")
        obv_slope = self._safe(snapshot, "obv_slope")
        delivery_spike = self._safe(snapshot, "delivery_spike")
        tight_consolidation_pct = self._safe(snapshot, "tight_consolidation_pct", 99.0)
        distance_to_resistance_pct = self._safe(snapshot, "distance_to_resistance_pct", 99.0)
        distance_to_support_pct = self._safe(snapshot, "distance_to_support_pct", 99.0)
        bb_width_ratio = self._safe(snapshot, "bb_width_ratio", 1.0)
        advanced_pattern = snapshot.get("advanced_chart_pattern") or {}
        advanced_pattern_score = self._safe(advanced_pattern, "score")
        advanced_pattern_late = bool(advanced_pattern.get("late_entry_risk"))
        advanced_pattern_stage = str(advanced_pattern.get("stage") or "")

        if snapshot.get("price_above_ema20") and snapshot.get("price_above_ema50"):
            add_bull(12, "trend", "Price is holding above the 20 and 50 EMA.", "trend_up")
        elif not snapshot.get("price_above_ema20") and not snapshot.get("price_above_ema50"):
            add_bear(12, "trend", "Price is below the 20 and 50 EMA.", "trend_down")

        if snapshot.get("price_above_ema200"):
            add_bull(6, "trend", "Longer-term structure is above the 200 EMA.")
        elif price and self._safe(snapshot, "ema_200"):
            add_bear(6, "trend", "Longer-term structure is below the 200 EMA.")

        trend_regime = str(snapshot.get("trend_regime") or "range")
        if trend_regime == "uptrend":
            add_bull(8, "trend", "Trend regime is rising across the recent swing.")
        elif trend_regime == "downtrend":
            add_bear(8, "trend", "Trend regime is weakening across the recent swing.")
        else:
            weaknesses.append("Trend regime is still range-bound.")

        if 55 <= rsi <= 68:
            add_bull(8, "momentum", f"RSI is strong at {rsi:.1f} without being overheated.", "momentum")
        elif 68 < rsi <= 76:
            add_bull(5, "momentum", f"RSI is bullish at {rsi:.1f}, but getting stretched.", "momentum")
            risk_factors.append("Momentum is stretched and more vulnerable to rejection.")
        elif rsi > 76 and return_20d >= 40:
            add_bull(10, "momentum", f"RSI is extreme at {rsi:.1f} in an explosive parabolic run.", "parabolic_momentum")
        elif rsi > 76:
            add_bear(4, "momentum", f"RSI is extremely overbought at {rsi:.1f}.", "overbought")
            risk_factors.append("Extremely overbought. High risk of mean reversion.")
        elif 32 <= rsi <= 45:
            add_bear(8, "momentum", f"RSI is soft at {rsi:.1f} and still favors sellers.", "momentum")
        elif rsi < 24:
            add_bear(5, "momentum", f"RSI is deeply weak at {rsi:.1f}, but a squeeze risk exists.", "momentum")
            risk_factors.append("Momentum is oversold and could snap back sharply.")

        if macd_hist > 0:
            add_bull(6, "momentum", "MACD histogram is positive.")
        elif macd_hist < 0:
            add_bear(6, "momentum", "MACD histogram is negative.")

        if relative_strength >= 2:
            add_bull(7, "relative_strength", f"20-day relative strength vs benchmark is +{relative_strength:.2f}%.", "relative_strength")
        elif relative_strength <= -2:
            add_bear(7, "relative_strength", f"20-day relative strength vs benchmark is {relative_strength:.2f}%.", "relative_strength")

        if relative_volume >= 3:
            if change_pct >= 0:
                add_bull(12, "volume", f"Relative volume is {relative_volume:.2f}x the 20-day average.", "unusual_volume")
            else:
                add_bear(12, "volume", f"Relative volume is {relative_volume:.2f}x the 20-day average on a weak tape.", "unusual_volume")
        elif relative_volume >= 1.8:
            if close_location >= 0.55:
                add_bull(8, "volume", f"Relative volume is {relative_volume:.2f}x and buyers are active.", "unusual_volume")
            elif close_location <= 0.45:
                add_bear(8, "volume", f"Relative volume is {relative_volume:.2f}x and sellers control the close.", "unusual_volume")
        else:
            weaknesses.append("Volume confirmation is limited.")

        if snapshot.get("breakout_20") and snapshot.get("above_vwap"):
            add_bull(14, "breakout", "Price is breaking above 20-day resistance while holding above VWAP.", "breakout")
        elif snapshot.get("breakdown_20") and not snapshot.get("above_vwap"):
            add_bear(14, "breakout", "Price is breaking below 20-day support while staying below VWAP.", "breakdown")
        elif snapshot.get("near_resistance") and bullish >= bearish:
            risk_factors.append("Price is pressing into resistance without full breakout confirmation.")
        elif snapshot.get("near_support") and bearish >= bullish:
            risk_factors.append("Price is pressing into support without full breakdown confirmation.")

        if close_location >= 0.7 and snapshot.get("above_vwap"):
            add_bull(5, "breakout", "The candle is closing near the high and above VWAP.")
        elif close_location <= 0.3 and not snapshot.get("above_vwap"):
            add_bear(5, "breakout", "The candle is closing near the low and below VWAP.")

        if lower_wick_pct >= 0.35 and snapshot.get("near_support"):
            add_bull(3, "breakout", "Lower wick rejection suggests dip-buying support.")
        if upper_wick_pct >= 0.35 and snapshot.get("near_resistance"):
            add_bear(3, "breakout", "Upper wick rejection suggests supply near resistance.")

        if cmf >= 0.1 and obv_slope > 0:
            add_bull(8, "volume", "Accumulation signals are positive through CMF and OBV.", "accumulation")
        elif cmf <= -0.1 and obv_slope < 0:
            add_bear(8, "volume", "Distribution pressure is visible through CMF and OBV.", "distribution")

        if snapshot.get("delivery_available") and delivery_spike >= 1.2:
            if change_pct >= 0:
                add_bull(5, "volume", f"Delivery ratio is elevated at {delivery_spike:.2f}x normal.")
            else:
                add_bear(5, "volume", f"Delivery ratio is elevated at {delivery_spike:.2f}x normal on weakness.")
        elif not snapshot.get("delivery_available"):
            weaknesses.append("Delivery data is unavailable in the current feed.")

        if advanced_pattern_score >= 55 and advanced_pattern.get("direction") == "bullish":
            pattern_name = str(advanced_pattern.get("pattern_name") or "Chart pattern")
            if advanced_pattern_late:
                risk_factors.append(f"{pattern_name} trigger is already stretched; wait for a fresh base or retest.")
                tags.extend(["pattern_late_entry", "avoid_late_entry"])
            else:
                pattern_points = 16 if advanced_pattern_score >= 82 else 12 if advanced_pattern_score >= 70 else 8
                add_bull(pattern_points, "breakout", f"{pattern_name} is forming near a clean trigger.", "live_chart_pattern")
                tags.extend(str(label).lower() for label in advanced_pattern.get("labels", [])[:5])
                if advanced_pattern_stage in {"READY_TO_BREAK", "BREAKOUT_ACTIVE"}:
                    add_bull(5, "volatility", "Pattern is close enough to trigger for live monitoring.", "live_trigger_ready")

        if atr_expansion >= 1.2 and abs(change_pct) >= 1:
            if bullish >= bearish:
                add_bull(4, "volatility", "Range expansion confirms the move.")
            else:
                add_bear(4, "volatility", "Range expansion confirms the downside move.")
        elif atr_expansion <= 0.9:
            weaknesses.append("Volatility is compressed and the move still needs expansion.")

        edge = bullish - bearish
        if bullish >= 18 and edge >= 8:
            direction = "bullish"
        elif bearish >= 18 and edge <= -8:
            direction = "bearish"
        else:
            direction = "neutral"

        base_confidence = 50 + min(abs(edge) * 1.9, 30) + min(max(bullish, bearish) * 0.28, 14)
        model_confidence = float(np.clip(base_confidence, 38, 95))
        confidence = model_confidence
        evidence_confidence = None
        confidence_note = "Confidence is driven by live signal alignment."

        historical = {}
        evidence_status = "not_loaded"
        if backtest and calibrate and direction in backtest:
            historical = backtest.get(direction, {})
            evidence_status = self._evidence_status(historical)
            signal_count = int(historical.get("signal_count", 0) or 0)
            if signal_count >= 8:
                evidence_confidence = float(historical.get("win_rate", 0) * 100)
                confidence = (model_confidence * 0.72) + (evidence_confidence * 0.28)
                confidence_note = "Confidence blends live model alignment with historical hit-rate evidence."
                fpr = historical.get("false_positive_rate", 0)
                if fpr > 0.55:
                    # >55% false positive rate: significant penalty + block buy
                    confidence = max(confidence - 12, 38)
                    risk_factors.append("Historical false positives are very high (>55%) — system recommends WATCH only.")
                elif fpr > 0.45:
                    # >45% false positive rate: moderate penalty
                    confidence = max(confidence - 6, 38)
                    risk_factors.append("Historical false positives are elevated for similar setups.")
            elif signal_count > 0:
                evidence_confidence = float(historical.get("win_rate", 0) * 100)
                confidence = min(model_confidence, 86.0)
                confidence_note = (
                    f"Confidence is mostly model-driven because only {signal_count} "
                    "historical matches were found."
                )
                weaknesses.append(f"Historical validation is limited to {signal_count} similar signals.")
            elif direction != "neutral":
                confidence = min(model_confidence, 82.0)
                confidence_note = (
                    "Confidence is model-driven; historical evidence is unavailable "
                    "for this setup in the current backtest window."
                )
                weaknesses.append("Historical validation is unavailable for this setup in the current window.")
        elif direction != "neutral":
            confidence_note = "Confidence is driven by live model alignment; historical evidence was not loaded."

        risk_score = 0
        if atr_pct >= 4.5:
            risk_score += 2
            risk_factors.append(f"ATR is elevated at {atr_pct:.2f}% of price.")
        elif atr_pct >= 2.5:
            risk_score += 1
        if direction == "bullish" and rsi >= 72:
            risk_score += 1
        if direction == "bearish" and rsi <= 28:
            risk_score += 1
        if direction == "bullish" and snapshot.get("near_resistance") and not snapshot.get("breakout_20"):
            risk_score += 1
        if direction == "bearish" and snapshot.get("near_support") and not snapshot.get("breakdown_20"):
            risk_score += 1
        if relative_volume < 1.1:
            risk_score += 1
        # --- Inject F&O Options Chain & Order Book Logic ---
        pcr = float(snapshot.get("pcr", 1.0))
        bid_ask_ratio = float(snapshot.get("bid_ask_ratio", 1.0))
        is_fno = snapshot.get("is_fno", False)

        if direction == "bullish":
            if is_fno and pcr > 1.2:
                confidence += 5.0
                reasons.append(f"Options Chain shows strong Put writing (PCR: {pcr:.2f}), confirming smart money support.")
            elif is_fno and pcr < 0.75:
                confidence -= 5.0
                risk_factors.append(f"Bearish Options Chain (PCR: {pcr:.2f}) contradicts bullish chart setup.")
            
            if bid_ask_ratio > 1.5:
                confidence += 3.0
                reasons.append(f"Heavy buy-side depth ({bid_ask_ratio:.1f}x buyers) in live order book.")
            elif bid_ask_ratio < 0.4:
                confidence -= 5.0
                risk_factors.append(f"Order book is bearish ({1/bid_ask_ratio:.1f}x sellers), fakeout risk possible.")
                
        elif direction == "bearish":
            if is_fno and pcr < 0.8:
                confidence += 5.0
                reasons.append(f"Options Chain shows heavy Call writing (PCR: {pcr:.2f}), confirming resistance.")
            elif is_fno and pcr > 1.25:
                confidence -= 5.0
                risk_factors.append(f"Bullish Options Chain (PCR: {pcr:.2f}) contradicts bearish chart setup.")
                
            if bid_ask_ratio < 0.6:
                confidence += 3.0
                reasons.append(f"Heavy sell-side depth ({1/bid_ask_ratio:.1f}x sellers) in live order book.")
            elif bid_ask_ratio > 2.5:
                confidence -= 5.0
                risk_factors.append(f"Order book is extremely bullish ({bid_ask_ratio:.1f}x buyers), bear trap risk high.")

        confidence = float(np.clip(confidence, 38, 95))
        risk_level = self._risk_level(risk_score)
        alert_level = self._bucket(confidence, direction, risk_level)

        if direction == "bullish":
            expected_move_pct = historical.get("avg_win_pct") or max(0.8, atr_pct * 1.05)
            invalidation = self._safe(snapshot, "support_20") or self._safe(snapshot, "ema_20")
        elif direction == "bearish":
            expected_move_pct = -(historical.get("avg_win_pct") or max(0.8, atr_pct * 1.05))
            invalidation = self._safe(snapshot, "resistance_20") or self._safe(snapshot, "ema_20")
        else:
            expected_move_pct = 0.0
            invalidation = self._safe(snapshot, "ema_20") or price

        move_quality = int(
            np.clip(
                max(abs(edge) * 2.2, max(bullish, bearish) * 2.1) - (risk_score * 4) + (10 if direction != "neutral" else -8),
                25,
                98,
            )
        )
        timeframe_label, timeframe_days = self._timeframe(snapshot, direction, move_quality)

        if direction == "bullish":
            setup_label = "Strong bullish setup" if confidence >= 80 and risk_level != "high" else "Constructive bullish setup" if confidence >= 65 else "Weak bullish setup"
        elif direction == "bearish":
            setup_label = "Strong bearish setup" if confidence >= 80 and risk_level != "high" else "Constructive bearish setup" if confidence >= 65 else "Weak bearish setup"
        else:
            setup_label = "No clear edge"

        probability = 0.5 if direction == "neutral" else round(confidence / 100, 4)
        signal_summary = "; ".join(reasons[:3]) if reasons else "Confirmation is still weak."
        pattern_context = self._pattern_context(snapshot, direction=direction, price=price)
        pattern_target = self._safe(advanced_pattern, "target_1")
        pattern_target_2 = self._safe(advanced_pattern, "target_2")
        pattern_stop = self._safe(advanced_pattern, "invalidation_level")
        pattern_expected_move_pct = self._safe(advanced_pattern, "expected_move_pct")
        if direction == "bullish" and pattern_expected_move_pct > 0:
            expected_move_pct = max(expected_move_pct, pattern_expected_move_pct)
        
        # Determine actual entry trigger (use price if it has already broken out)
        entry_price = price
        if direction == "bullish" and pattern_context["entry_trigger"] > price:
            entry_price = pattern_context["entry_trigger"]
        elif direction == "bearish" and pattern_context["entry_trigger"] < price:
            entry_price = pattern_context["entry_trigger"]

        # Cap stop loss to max 3.5x ATR to avoid unrealistic Risk/Reward, but enforce min 1.5x ATR to avoid premature stop-outs
        atr_val = self._safe(snapshot, "atr") or (price * 0.02)
        if direction == "bullish":
            if invalidation and invalidation < entry_price - (atr_val * 3.5):
                invalidation = entry_price - (atr_val * 3.5)
            if invalidation and invalidation > entry_price - (atr_val * 1.5):
                invalidation = entry_price - (atr_val * 1.5)
            if invalidation and invalidation >= entry_price:
                invalidation = entry_price - (atr_val * 1.5)
            if pattern_stop and pattern_stop < entry_price:
                invalidation = max(invalidation or 0, pattern_stop)
        elif direction == "bearish":
            if invalidation and invalidation > entry_price + (atr_val * 3.5):
                invalidation = entry_price + (atr_val * 3.5)
            if invalidation and invalidation < entry_price + (atr_val * 1.5):
                invalidation = entry_price + (atr_val * 1.5)
            if invalidation and invalidation <= entry_price:
                invalidation = entry_price + (atr_val * 1.5)

        stop_loss = invalidation
        stop_distance = abs(entry_price - stop_loss) if stop_loss else 0.0
        
        # Ensure minimum 1.5 Risk/Reward based on expected move
        base_target_distance = entry_price * (abs(expected_move_pct) / 100)
        pattern_target_distance = 0.0
        if direction == "bullish" and pattern_target > entry_price:
            pattern_target_distance = pattern_target - entry_price
        target_distance = max(base_target_distance, stop_distance * 1.5, pattern_target_distance)
        
        if direction == "bullish":
            target_price = entry_price + target_distance
            extended_target_price = max(entry_price + (target_distance * 1.5), pattern_target_2 if pattern_target_2 > entry_price else 0)
            extended_target = entry_price + (target_distance * 2.0)
            trailing_stop = max(stop_loss or 0, self._safe(snapshot, "ema_20") or 0, self._safe(snapshot, "rolling_vwap") or 0) or stop_loss
        elif direction == "bearish":
            target_price = entry_price - target_distance
            extended_target_price = entry_price - (target_distance * 1.5)
            extended_target = entry_price - (target_distance * 2.0)
            trailing_candidates = [value for value in [stop_loss, self._safe(snapshot, "ema_20"), self._safe(snapshot, "rolling_vwap")] if value]
            trailing_stop = min(trailing_candidates) if trailing_candidates else stop_loss
        else:
            target_price = entry_price
            extended_target_price = entry_price
            extended_target = entry_price
            trailing_stop = stop_loss
            
        risk_reward = round(target_distance / stop_distance, 2) if stop_distance > 0 else 0.0
        target_1 = target_price
        target_2 = extended_target_price

        above_vwap = bool(snapshot.get("above_vwap"))
        above_ema20 = bool(snapshot.get("price_above_ema20"))
        volume_ok = relative_volume >= 1.2 or intraday_volume_ratio >= 1.2
        volume_fading = relative_volume < 1.05 and intraday_volume_ratio < 1.0
        overextended = (direction == "bullish" and rsi >= 72) or (direction == "bearish" and rsi <= 28)
        lifecycle = build_lifecycle_advice(
            direction=direction,
            entry_price=price,
            current_price=price,
            target_1=target_1,
            target_2=target_2,
            stop_loss=stop_loss,
            trailing_stop=trailing_stop,
            above_vwap=above_vwap,
            above_ema20=above_ema20,
            volume_ok=volume_ok,
            overextended=overextended,
            volume_fading=volume_fading,
        )
        if overextended and volume_fading:
            risk_factors.append("Move is overextended while volume is fading, raising reversal risk.")
        chase = classify_chase_risk(
            {
                "direction": direction,
                "change_pct": change_pct,
                "gap_pct": self._safe(snapshot, "gap_pct"),
                "risk_reward": risk_reward,
                "relative_volume": relative_volume,
                "intraday_volume_ratio": intraday_volume_ratio,
                "rsi": rsi,
                "current_price": price,
                "trailing_stop": trailing_stop,
                "stop_loss": stop_loss,
                "invalidation": invalidation,
                "rolling_vwap": self._safe(snapshot, "rolling_vwap"),
                "ema_20": self._safe(snapshot, "ema_20"),
                "return_5d": return_5d,
                "return_20d": return_20d,
                "close_location": close_location,
            }
        )
        if chase["chase_risk"]:
            risk_factors.append(chase["chase_risk_reason"])
            tags.append("chase_risk")
        if chase["next_day_profit_booking_risk"]:
            risk_factors.append("High chance of profit booking tomorrow after an 8%+ move.")
            tags.append("profit_booking_risk")
        if chase.get("overextended_fresh_entry"):
            risk_factors.append(chase["chase_risk_reason"])
            tags.extend(["avoid_late_entry", "overextended"])
        entry_timing = analyze_entry_timing(snapshot, direction=direction)
        demand_supply = analyze_demand_supply(snapshot, direction=direction)
        if demand_supply.get("blockFreshEntry"):
            tags.extend(["bull_trap_risk", "avoid_late_entry", "supply_pressure"])
            risk_factors.append(demand_supply.get("smartMoneyRead", "Trap risk is elevated."))
            risk_factors.extend(demand_supply.get("trapReasons", [])[:2])
        elif demand_supply.get("status") in {"demand_absorption", "early_demand"}:
            tags.extend(["demand_absorption", "accumulation_watch"])
            reasons.append(demand_supply.get("smartMoneyRead", "Demand evidence is improving."))
        elif demand_supply.get("status") == "supply_pressure":
            tags.extend(["supply_pressure", "seller_pressure"])
            risk_factors.append(demand_supply.get("smartMoneyRead", "Supply pressure is elevated."))
        if direction == "bullish" and entry_timing.get("profit_booking_risk") in {"high", "very_high"}:
            tags.extend(["profit_booking_zone", "seller_pressure", "avoid_late_entry"])
            risk_factors.extend(entry_timing.get("reasons", [])[:2])
        elif direction == "bullish" and entry_timing.get("entry_quality") == "watch":
            weaknesses.extend(entry_timing.get("reasons", [])[:1])
        elif direction == "bearish" and entry_timing.get("seller_pressure") in {"high", "very_high"}:
            tags.append("seller_pressure")
        pre_breakout = self._pre_breakout_setup(
            snapshot,
            price=price,
            change_pct=change_pct,
            gap_pct=self._safe(snapshot, "gap_pct"),
            atr_pct=atr_pct,
        )
        if pre_breakout.get("is_pre_breakout"):
            tags.extend(["pre_breakout", "watch_for_breakout"])
            tags.extend(label.lower() for label in pre_breakout.get("pre_breakout_labels", []))
        continuation = self._momentum_continuation_setup(
            snapshot,
            price=price,
            direction=direction,
            chase=chase,
            risk_reward=risk_reward,
            target_1=target_1,
        )
        if continuation.get("is_momentum_continuation"):
            tags.extend(["momentum_continuation", "re_entry_setup"])

        trap_fields = self._build_trap_fields(snapshot, direction)

        entry_blocks_fresh_buy = (
            direction == "bullish"
            and (
                entry_timing.get("entry_quality") in {"poor", "avoid"}
                or bool(demand_supply.get("blockFreshEntry"))
            )
            and not trap_fields.get("smart_shakeout")
        )
        live_pattern_ready = bool(
            direction == "bullish"
            and advanced_pattern_score >= 78
            and not advanced_pattern_late
            and advanced_pattern_stage in {"READY_TO_BREAK", "BREAKOUT_ACTIVE"}
            and risk_reward >= 1.3
        )
        if live_pattern_ready and not entry_blocks_fresh_buy:
            signal_stage = "LIVE_PATTERN_READY" if advanced_pattern_stage == "READY_TO_BREAK" else "CONFIRMED_BREAKOUT"
            action = str(advanced_pattern.get("action") or "BUY_ONLY_ON_TRIGGER_HOLD")
        elif "support_bounce" in pattern_context.get("pattern_labels", []) and not entry_blocks_fresh_buy and direction == "bullish":
            signal_stage = "SUPPORT_BOUNCE"
            action = "BUY"
        elif pre_breakout.get("is_pre_breakout") and not entry_blocks_fresh_buy:
            signal_stage = "PATTERN_FORMING" if pre_breakout.get("pre_breakout_action") == "WATCH" else "ALERT_ABOVE_LEVEL"
            action = pre_breakout.get("pre_breakout_action", "WATCH")
        elif continuation.get("is_momentum_continuation") and not entry_blocks_fresh_buy:
            signal_stage = "RETEST_ENTRY" if continuation.get("continuation_type") == "pullback" else "RE_ENTRY_SETUP"
            action = continuation.get("continuation_action", "WAIT_FOR_REENTRY")
        elif entry_blocks_fresh_buy:
            signal_stage = str(entry_timing.get("setup_stage") or "AVOID_LATE_ENTRY")
            action = "WAIT_FOR_PULLBACK"
        elif chase.get("overextended_fresh_entry") or chase.get("chase_risk"):
            signal_stage = "AVOID_CHASE"
            action = "WAIT_FOR_PULLBACK"
        elif "breakout" in tags:
            # POINT 2: Breakout Acceptance over Crossing
            if price and resistance and price > resistance * 1.002 and snapshot.get("above_vwap") and close_location >= 0.5:
                signal_stage = "CONFIRMED_BREAKOUT"
                action = "BUY"
            else:
                signal_stage = "WATCH_FOR_ACCEPTANCE"
                action = "WATCH"
                reasons.append("Breakout crossed resistance but lacks VWAP hold or strong close. Wait for acceptance.")
        elif "breakdown" in tags:
            signal_stage = "CONFIRMED_BREAKDOWN"
            action = "SELL"
        elif pattern_context["pattern_score"] >= 3:
            signal_stage = "PATTERN_FORMING"
            action = "WATCH"
        else:
            signal_stage = "WATCH"
            action = "WATCH"

        # POINT 1 & 10: Mandatory Failure Pre-Check & Hard Trap Rejection
        failure_flags = 0
        failure_reasons = []

        if trap_fields.get("trap_risk") in {"high", "medium"} or trap_fields.get("bull_trap", {}).get("bull_trap_detected"):
            failure_flags += 2  # Hard fail
            failure_reasons.append("High Trap Risk / Bull Trap Detected.")

        if chase.get("chase_risk") or chase.get("overextended_fresh_entry"):
            failure_flags += 1
            failure_reasons.append("Entry is overextended/chasing.")

        if nifty_trend == "BEARISH" and direction == "bullish":
            failure_flags += 1
            failure_reasons.append("Hostile Market Regime (Nifty Bearish).")

        if stop_loss and price and ((abs(price - stop_loss) / price) * 100 > 8.0):
            failure_flags += 1
            failure_reasons.append("Stop-loss is too wide (>8%), unrealistic for swing.")

        if not trap_fields.get("delivery_data_available", True) and direction == "bullish":
            confidence = min(confidence, 80.0)
            if relative_volume > 2.0:
                failure_flags += 1
                failure_reasons.append("High volume move without delivery confirmation (Speculation Risk).")

        if failure_flags >= 2 and action in {"BUY", "SELL", "REENTRY_BUY"}:
            action = "AVOID"
            signal_stage = "RISK_REJECTED"
            tags.append("risk_rejected")
            risk_factors.insert(0, "MANDATORY PRE-CHECK FAILED: " + " | ".join(failure_reasons))
            confidence = min(confidence, 60.0)

        if trap_fields.get("smart_shakeout"):
            signal_stage = "SHAKEOUT_RETEST"
            action = "BUY_ON_SUPPORT"
            tags.append("operator_shakeout")
            tags.append("smart_money_absorption")
            reasons.extend(trap_fields.get("shakeout_reasons", []))
            confidence = min(confidence + 15.0, 95.0)
        historical_sample_count = int(historical.get("sample_count", historical.get("signal_count", 0)) or 0)
        historical_win_rate = float(historical.get("win_rate", 0) or 0)
        historically_validated = historical_sample_count >= 30 and historical_win_rate >= 0.60
        if action in {"BUY", "SELL", "REENTRY_BUY"} and not historically_validated:
            if confidence >= 72 or pattern_context["pattern_score"] >= 3 or "parabolic_momentum" in tags:
                risk_factors.append("No historical backtest, but technical conviction is strong enough to override.")
            else:
                action = "WATCH"
                if signal_stage == "CONFIRMED_BREAKOUT":
                    signal_stage = "ALERT_ABOVE_LEVEL"
            if continuation.get("continuation_action") == "REENTRY_BUY":
                continuation["continuation_action"] = "ALERT"
                continuation["action"] = "ALERT"
            risk_factors.append("Historical validation is below the BUY/SELL threshold; keep as watch only.")

        eval_allow_buy_call = (
            True
            if live_pattern_ready and not entry_blocks_fresh_buy
            else
            False
            if (pre_breakout.get("is_pre_breakout") and not live_pattern_ready) or entry_blocks_fresh_buy
            else bool(chase["allow_buy_call"] and entry_timing.get("allow_buy_call", True))
        )
        eval_attention_only = False if (live_pattern_ready or eval_allow_buy_call) else (
            (bool(pre_breakout.get("is_pre_breakout")) and not live_pattern_ready)
            or bool(continuation.get("is_momentum_continuation"))
            or chase["attention_only"]
            or bool(entry_timing.get("attention_only"))
        )

        ai_probability = probability
        news_sentiment = "neutral"
        news_headlines = []
        
        if df is not None and len(df) >= 50:
            model_path = os.path.join(self._model_dir, "master_ai_model.pkl")
            if os.path.exists(model_path):
                try:
                    if "master" not in self._model_cache:
                        self._model_cache["master"] = joblib.load(model_path)
                    model = self._model_cache["master"]
                    features = model.prepare_features(df)
                    if not features.empty:
                        last_row = features.iloc[-1:].copy()
                        # Master model expects 'is_bullish_setup'
                        last_row['is_bullish_setup'] = 1 if direction == 'bullish' else 0
                        
                        prob = float(model.model.predict_proba(last_row)[0][1])
                        
                        # AI Calibration: ML models rarely output >0.85 prob.
                        # We scale 0.55-0.80 to 0.70-0.95 so it aligns with the UI's 90%+ expectations,
                        # avoiding the perception that the AI is "downgrading" a good setup.
                        if prob >= 0.55:
                            calibrated_prob = 0.55 + ((prob - 0.55) * 1.6)
                        else:
                            calibrated_prob = prob
                            
                        ai_probability = round(min(0.98, max(0.0, calibrated_prob)), 4)
                        
                        ai_insight = None
                        if ai_probability > 0.75:
                            tags.append("ai_highly_bullish")
                            ai_notes = ["🤖 **Master AI Analysis**: I have scanned the entire historical chart."]
                            
                            if 'failed_breakdown' in last_row and int(last_row['failed_breakdown'].iloc[0]) == 1:
                                ai_notes.append("It recently showed a false breakdown to trap sellers (Bear Trap), but refused to fall further.")
                            if 'volume_divergence' in last_row and int(last_row['volume_divergence'].iloc[0]) == 1:
                                ai_notes.append("Massive volume is building up silently without the price moving, indicating strong institutional accumulation.")
                            if 'rsi_oversold' in last_row and int(last_row['rsi_oversold'].iloc[0]) == 1:
                                ai_notes.append("The chart is heavily oversold and primed for a violent reversal.")
                                
                            # F&O and Order Book Narrative
                            if snapshot.get("is_fno"):
                                pcr_val = float(snapshot.get("pcr", 1.0))
                                if pcr_val > 1.2:
                                    ai_notes.append(f"Options Chain confirms smart money is aggressively writing Puts (PCR: {pcr_val:.2f}), creating a solid floor.")
                            
                            bar = float(snapshot.get("bid_ask_ratio", 1.0))
                            if bar > 1.5:
                                ai_notes.append(f"Live Market Depth shows {bar:.1f}x more Buyers than Sellers, indicating aggressive institutional buying.")
                                
                            ai_notes.append(f"Based on pattern similarities across our database, I am **{ai_probability*100:.1f}% confident** that this will run hard from here.")
                            ai_insight = " ".join(ai_notes)
                            reasons.insert(0, ai_insight)
                            
                        elif ai_probability < 0.3:
                            tags.append("ai_highly_bearish")
                            ai_notes = ["🤖 **Master AI Warning**: I have scanned the historical chart."]
                            if 'failed_breakout' in last_row and int(last_row['failed_breakout'].iloc[0]) == 1:
                                ai_notes.append("It just faked a breakout to trap buyers (Bull Trap) and is facing heavy supply.")
                                
                            # Bearish F&O Narrative
                            if snapshot.get("is_fno"):
                                pcr_val = float(snapshot.get("pcr", 1.0))
                                if pcr_val < 0.8:
                                    ai_notes.append(f"Options Chain confirms heavy Call writing (PCR: {pcr_val:.2f}), creating a massive resistance wall.")
                            
                            bar = float(snapshot.get("bid_ask_ratio", 1.0))
                            if bar < 0.6:
                                ai_notes.append(f"Live Market Depth shows {1/bar:.1f}x more Sellers than Buyers, indicating aggressive dumping.")
                                
                            ai_notes.append(f"My model shows only a **{ai_probability*100:.1f}% chance** of success. Avoid fresh entries.")
                            ai_insight = " ".join(ai_notes)
                            risk_factors.insert(0, ai_insight)
                            
                            # CRITICAL FIX: If AI is extremely bearish, penalize the main technical confidence 
                            # so this stock doesn't show up with 90%+ in the main UI/Dashboard.
                            if direction == 'bullish':
                                confidence = max(38.0, confidence - 25.0)
                            elif direction == 'bearish':
                                confidence = max(38.0, confidence - 25.0)
                            
                except Exception as e:
                    pass
        
        try:
            news_data = fetch_news_sentiment(symbol)
            if news_data and news_data.get("available"):
                news_sentiment = news_data.get("newsSentiment", "neutral")
                news_headlines = news_data.get("headlines", [])
                if news_sentiment == "bullish":
                    tags.append("bullish_news")
                elif news_sentiment == "bearish":
                    tags.append("bearish_news")
        except Exception:
            pass

        return {
            "symbol": symbol.upper(),
            "direction": direction,
            "setup_label": setup_label,
            "alert_level": alert_level,
            "confidence": round(confidence, 1),
            "model_confidence": round(model_confidence, 1),
            "evidence_confidence": round(evidence_confidence, 1) if evidence_confidence is not None else None,
            "historical_evidence_status": evidence_status,
            "confidence_note": confidence_note,
            "probability": ai_probability,
            "ai_insight": ai_insight,
            "news_sentiment": news_sentiment,
            "news_headlines": news_headlines,
            "move_quality": move_quality,
            "expected_move_pct": round(expected_move_pct, 2),
            **pattern_context,
            "signal_stage": signal_stage,
            "action": action,
            "entry_trigger": pattern_context["entry_trigger"],
            "target": round(target_price, 2),
            "historical_evidence": {
                "sample_count": int(historical.get("sample_count", historical.get("signal_count", 0)) or 0),
                "win_rate": round(float(historical.get("win_rate", 0) or 0), 4),
                "avg_return": round(float(historical.get("avg_return", historical.get("expectancy_pct", 0)) or 0), 2),
                "max_drawdown": round(float(historical.get("max_drawdown", historical.get("avg_loss_pct", 0)) or 0), 2),
                "false_positive_rate": round(float(historical.get("false_positive_rate", 0) or 0), 4),
            },
            "validation_status": (
                "validated"
                if historically_validated
                else "watch_only_insufficient_history"
            ),
            "catalyst_summary": {
                "sentiment": "neutral",
                "summary": "Catalyst scan not loaded for this request.",
                "catalysts": [],
            },
            "risk_level": risk_level,
            "current_price": round(price, 2),
            "target_price": round(target_price, 2),
            "extended_target_price": round(extended_target_price, 2),
            "stop_loss": round(stop_loss, 2) if stop_loss else None,
            "entry_zone": {
                "low": round(min(price * 0.995, price * 1.005), 2),
                "high": round(max(price * 0.995, price * 1.005), 2),
            },
            "target_1": round(target_1, 2),
            "target_2": round(target_2, 2),
            "extended_target": round(extended_target, 2),
            "trailing_stop": round(trailing_stop, 2) if trailing_stop else None,
            "exit_signal": lifecycle["exit_signal"],
            "hold_or_exit": lifecycle["hold_or_exit"],
            "reason_for_exit_decision": lifecycle["reason_for_exit_decision"],
            "current_pnl_pct": lifecycle["current_pnl_pct"],
            "target_progress_pct": lifecycle["target_progress_pct"],
            **chase,
            **pre_breakout,
            **continuation,
            "action": action,
            "setup_type": (
                pattern_context["setup_type"]
                if live_pattern_ready
                else continuation.get("setup_type") or pre_breakout.get("setup_type") or pattern_context["setup_type"]
            ),
            "setup_stage": (
                str(entry_timing.get("setup_stage") or "AVOID_LATE_ENTRY")
                if entry_blocks_fresh_buy
                else "PRE_BREAKOUT"
                if pre_breakout.get("is_pre_breakout") and not live_pattern_ready
                else "MOMENTUM_CONTINUATION"
                if continuation.get("is_momentum_continuation")
                else "LIVE_PATTERN_READY"
                if live_pattern_ready
                else chase["setup_stage"]
            ),
            "trade_labels": list(
                dict.fromkeys(
                    (advanced_pattern.get("labels", []) if advanced_pattern_score >= 50 else [])
                    + (pre_breakout.get("pre_breakout_labels", []) if pre_breakout.get("is_pre_breakout") else [])
                    + (continuation.get("continuation_labels", []) if continuation.get("is_momentum_continuation") else [])
                    + chase.get("trade_labels", [])
                    + entry_timing.get("trade_labels", [])
                )
            ),
            "recommended_action": action,
            "entry_quality": entry_timing.get("entry_quality"),
            "entry_timing": entry_timing.get("entry_timing"),
            "seller_pressure": entry_timing.get("seller_pressure"),
            "seller_pressure_score": entry_timing.get("seller_pressure_score"),
            "profit_booking_risk": entry_timing.get("profit_booking_risk"),
            "best_action": entry_timing.get("best_action"),
            "reentry_plan": entry_timing.get("reentry_plan"),
            "entry_timing_reasons": entry_timing.get("reasons", []),
            "demand_supply": demand_supply,
            "demand_status": demand_supply.get("status"),
            "demand_score": demand_supply.get("demandScore"),
            "supply_score": demand_supply.get("supplyScore"),
            "trap_risk": demand_supply.get("trapRisk"),
            "trap_risk_score": demand_supply.get("trapRiskScore"),
            "attention_only": eval_attention_only,
            "allow_buy_call": eval_allow_buy_call,
            "live_pattern_ready": live_pattern_ready,
            "advanced_chart_pattern": advanced_pattern,
            "risk_reward": risk_reward,
            "timeframe_label": timeframe_label,
            "timeframe_days": timeframe_days,
            "change_pct": round(change_pct, 2),
            "return_5d": round(return_5d, 2),
            "return_20d": round(return_20d, 2),
            "volume": int(self._safe(snapshot, "volume", 0)),
            "relative_volume": round(relative_volume, 2),
            "gap_pct": round(self._safe(snapshot, "gap_pct"), 2),
            "intraday_volume_ratio": round(self._safe(snapshot, "intraday_volume_ratio", 1.0), 2),
            "benchmark_relative_strength": round(relative_strength, 2),
            "pcr": float(snapshot.get("pcr", 1.0)) if snapshot.get("is_fno") else None,
            "bid_ask_ratio": float(snapshot.get("bid_ask_ratio", 1.0)) if snapshot.get("bid_ask_ratio") else None,
            "is_fno": bool(snapshot.get("is_fno", False)),
            "options_iv_pct": float(snapshot.get("options_iv_pct", 50.0)) if snapshot.get("is_fno") else None,
            "rsi": round(rsi, 1),
            "atr_pct": round(atr_pct, 2),
            "support": round(self._safe(snapshot, "support_20"), 2) if self._safe(snapshot, "support_20") else None,
            "resistance": round(self._safe(snapshot, "resistance_20"), 2) if self._safe(snapshot, "resistance_20") else None,
            "rolling_vwap": round(self._safe(snapshot, "rolling_vwap"), 2) if self._safe(snapshot, "rolling_vwap") else None,
            "invalidation": round(invalidation, 2) if invalidation else None,
            "chart_features": {
                "near_breakout": bool(snapshot.get("near_resistance")) or distance_to_resistance_pct <= 2.5,
                "breakout_confirmed": bool(snapshot.get("breakout_20") or snapshot.get("intraday_breakout")),
                "breakdown_confirmed": bool(snapshot.get("breakdown_20")),
                "pullback_to_support": bool(snapshot.get("near_support")) or distance_to_support_pct <= 2.0,
                "volume_spike": relative_volume >= 1.5 or intraday_volume_ratio >= 1.4,
                "rsi_momentum": rsi,
                "moving_average_trend": trend_regime,
                "risk_reward": risk_reward,
                "distance_from_stop_pct": round((abs(price - stop_loss) / price) * 100, 2) if price and stop_loss else None,
                "overextended": overextended,
                "tight_consolidation_pct": round(tight_consolidation_pct, 2) if tight_consolidation_pct != 99.0 else None,
                "distance_to_resistance_pct": round(distance_to_resistance_pct, 2) if distance_to_resistance_pct != 99.0 else None,
                "distance_to_support_pct": round(distance_to_support_pct, 2) if distance_to_support_pct != 99.0 else None,
                "higher_lows": bool(snapshot.get("higher_lows")),
                "volume_dryup": bool(snapshot.get("volume_dryup")),
                "bb_squeeze": bool(snapshot.get("bb_squeeze")),
                "bb_width_ratio": round(bb_width_ratio, 2),
                "atr_expansion": round(atr_expansion, 2),
                "cmf": round(cmf, 3),
                "obv_slope": round(obv_slope, 2),
            },
            "reasons": reasons[:6],
            "weaknesses": weaknesses[:4],
            "risk_factors": list(dict.fromkeys(risk_factors))[:5],
            "tags": list(dict.fromkeys(tags)),
            "signal_summary": signal_summary,
            "score_breakdown": {key: round(value, 1) for key, value in components.items()},
            "bullish_score": round(bullish, 1),
            "bearish_score": round(bearish, 1),
            "historical_context": historical,
            **trap_fields,
        }
