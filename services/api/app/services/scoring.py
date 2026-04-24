from __future__ import annotations

from typing import Dict, List

import numpy as np


class ScoringEngine:
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

    def evaluate(
        self,
        symbol: str,
        snapshot: Dict,
        backtest: Dict | None = None,
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
        rsi = self._safe(snapshot, "rsi", 50.0)
        macd_hist = self._safe(snapshot, "macd_hist")
        atr_pct = self._safe(snapshot, "atr_pct", 0.0)
        atr_expansion = self._safe(snapshot, "atr_expansion", 1.0)
        relative_strength = self._safe(snapshot, "relative_strength_20d")
        close_location = self._safe(snapshot, "close_location", 0.5)
        upper_wick_pct = self._safe(snapshot, "upper_wick_pct")
        lower_wick_pct = self._safe(snapshot, "lower_wick_pct")
        cmf = self._safe(snapshot, "cmf")
        obv_slope = self._safe(snapshot, "obv_slope")
        delivery_spike = self._safe(snapshot, "delivery_spike")

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
                if historical.get("false_positive_rate", 0) > 0.45:
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
        if historical and historical.get("signal_count", 0) < 12:
            risk_score += 1

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
        if direction == "bullish":
            target_price = price * (1 + abs(expected_move_pct) / 100)
            extended_target_price = price * (1 + abs(expected_move_pct) * 1.35 / 100)
        elif direction == "bearish":
            target_price = price * (1 - abs(expected_move_pct) / 100)
            extended_target_price = price * (1 - abs(expected_move_pct) * 1.35 / 100)
        else:
            target_price = price
            extended_target_price = price

        stop_loss = invalidation
        stop_distance = abs(price - stop_loss) if stop_loss else 0.0
        target_distance = abs(target_price - price)
        risk_reward = round(target_distance / stop_distance, 2) if stop_distance > 0 else 0.0

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
            "probability": probability,
            "move_quality": move_quality,
            "expected_move_pct": round(expected_move_pct, 2),
            "risk_level": risk_level,
            "current_price": round(price, 2),
            "target_price": round(target_price, 2),
            "extended_target_price": round(extended_target_price, 2),
            "stop_loss": round(stop_loss, 2) if stop_loss else None,
            "risk_reward": risk_reward,
            "timeframe_label": timeframe_label,
            "timeframe_days": timeframe_days,
            "change_pct": round(change_pct, 2),
            "volume": int(self._safe(snapshot, "volume", 0)),
            "relative_volume": round(relative_volume, 2),
            "gap_pct": round(self._safe(snapshot, "gap_pct"), 2),
            "intraday_volume_ratio": round(self._safe(snapshot, "intraday_volume_ratio", 1.0), 2),
            "benchmark_relative_strength": round(relative_strength, 2),
            "rsi": round(rsi, 1),
            "atr_pct": round(atr_pct, 2),
            "support": round(self._safe(snapshot, "support_20"), 2) if self._safe(snapshot, "support_20") else None,
            "resistance": round(self._safe(snapshot, "resistance_20"), 2) if self._safe(snapshot, "resistance_20") else None,
            "rolling_vwap": round(self._safe(snapshot, "rolling_vwap"), 2) if self._safe(snapshot, "rolling_vwap") else None,
            "invalidation": round(invalidation, 2) if invalidation else None,
            "reasons": reasons[:6],
            "weaknesses": weaknesses[:4],
            "risk_factors": list(dict.fromkeys(risk_factors))[:5],
            "tags": list(dict.fromkeys(tags)),
            "signal_summary": signal_summary,
            "score_breakdown": {key: round(value, 1) for key, value in components.items()},
            "bullish_score": round(bullish, 1),
            "bearish_score": round(bearish, 1),
            "historical_context": historical,
        }
