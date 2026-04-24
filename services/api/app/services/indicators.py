from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


class IndicatorEngine:
    def _rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
        rs = gain / (loss.replace(0, np.nan))
        return (100 - (100 / (1 + rs))).fillna(50)

    def _atr(self, frame: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low = frame["High"] - frame["Low"]
        high_close = (frame["High"] - frame["Close"].shift()).abs()
        low_close = (frame["Low"] - frame["Close"].shift()).abs()
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return true_range.rolling(period).mean()

    def build_feature_frame(self, frame: pd.DataFrame, benchmark_frame: pd.DataFrame | None = None) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame()

        close = frame["Close"].astype(float)
        open_ = frame["Open"].astype(float)
        high = frame["High"].astype(float)
        low = frame["Low"].astype(float)
        volume = frame["Volume"].astype(float).replace(0, np.nan)

        features = pd.DataFrame(index=frame.index)
        features["price"] = close
        features["prev_close"] = close.shift(1)
        features["change_pct"] = close.pct_change() * 100
        features["gap_pct"] = ((open_ - close.shift(1)) / close.shift(1).replace(0, np.nan)) * 100

        ema_20 = close.ewm(span=20, adjust=False).mean()
        ema_50 = close.ewm(span=50, adjust=False).mean()
        ema_200 = close.ewm(span=200, adjust=False).mean()
        features["ema_20"] = ema_20
        features["ema_50"] = ema_50
        features["ema_200"] = ema_200
        features["ema_20_slope"] = ema_20.pct_change(5) * 100
        features["ema_50_slope"] = ema_50.pct_change(10) * 100
        features["price_above_ema20"] = (close > ema_20).astype(int)
        features["price_above_ema50"] = (close > ema_50).astype(int)
        features["price_above_ema200"] = (close > ema_200).astype(int)

        volume_avg_20 = volume.rolling(20).mean()
        features["volume_avg_20"] = volume_avg_20
        features["relative_volume"] = volume / volume_avg_20

        typical_price = (high + low + close) / 3
        rolling_vwap = (typical_price * volume).rolling(20).sum() / volume.rolling(20).sum()
        features["rolling_vwap"] = rolling_vwap
        features["vwap_distance_pct"] = ((close - rolling_vwap) / rolling_vwap.replace(0, np.nan)) * 100
        features["above_vwap"] = (close > rolling_vwap).astype(int)

        atr = self._atr(frame, 14)
        features["atr"] = atr
        features["atr_pct"] = (atr / close.replace(0, np.nan)) * 100
        features["atr_expansion"] = atr / atr.rolling(50).mean()

        features["return_5d"] = close.pct_change(5) * 100
        features["return_20d"] = close.pct_change(20) * 100
        features["return_60d"] = close.pct_change(60) * 100

        features["rsi"] = self._rsi(close, 14)
        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        features["macd_line"] = macd_line
        features["macd_signal"] = macd_signal
        features["macd_hist"] = macd_line - macd_signal

        resistance_20 = high.rolling(20).max().shift(1)
        support_20 = low.rolling(20).min().shift(1)
        features["resistance_20"] = resistance_20
        features["support_20"] = support_20
        features["breakout_20"] = (close > resistance_20).astype(int)
        features["breakdown_20"] = (close < support_20).astype(int)
        features["distance_to_resistance_pct"] = ((resistance_20 - close) / close.replace(0, np.nan)) * 100
        features["distance_to_support_pct"] = ((close - support_20) / close.replace(0, np.nan)) * 100

        candle_range = (high - low).replace(0, np.nan)
        features["close_location"] = ((close - low) / candle_range).clip(0, 1)
        features["upper_wick_pct"] = ((high - np.maximum(open_, close)) / candle_range).clip(0, 1)
        features["lower_wick_pct"] = ((np.minimum(open_, close) - low) / candle_range).clip(0, 1)

        direction = np.sign(close.diff()).fillna(0.0)
        obv = (direction * volume.fillna(0)).cumsum()
        money_flow_multiplier = (((close - low) - (high - close)) / candle_range).fillna(0.0)
        ad_line = (money_flow_multiplier * volume.fillna(0)).cumsum()
        features["obv_slope"] = obv.diff(5) / volume_avg_20.replace(0, np.nan)
        features["cmf"] = (money_flow_multiplier * volume.fillna(0)).rolling(20).sum() / volume.rolling(20).sum()
        features["ad_slope"] = ad_line.diff(5) / volume_avg_20.replace(0, np.nan)

        if benchmark_frame is not None and not benchmark_frame.empty:
            benchmark_close = benchmark_frame["Close"].astype(float).reindex(frame.index).ffill()
            benchmark_return_20d = benchmark_close.pct_change(20) * 100
            benchmark_change_pct = benchmark_close.pct_change() * 100
            features["benchmark_return_20d"] = benchmark_return_20d
            features["benchmark_change_pct"] = benchmark_change_pct
            features["relative_strength_20d"] = features["return_20d"] - benchmark_return_20d
        else:
            features["benchmark_return_20d"] = np.nan
            features["benchmark_change_pct"] = np.nan
            features["relative_strength_20d"] = np.nan

        delivery_candidates = ["DELIV_PER", "DELIVERY_PCT", "DELIVERY_PERCENT"]
        delivery_series = None
        for column in delivery_candidates:
            if column in frame.columns:
                delivery_series = pd.to_numeric(frame[column], errors="coerce") / 100
                break
        if delivery_series is not None:
            features["delivery_ratio"] = delivery_series.clip(lower=0, upper=1)
            features["delivery_spike"] = features["delivery_ratio"] / features["delivery_ratio"].rolling(20).mean()
        else:
            features["delivery_ratio"] = np.nan
            features["delivery_spike"] = np.nan

        features["trend_regime"] = np.select(
            [
                (close > ema_20) & (ema_20 > ema_50) & (features["ema_20_slope"] > 0) & (features["return_20d"] > 0),
                (close < ema_20) & (ema_20 < ema_50) & (features["ema_20_slope"] < 0) & (features["return_20d"] < 0),
            ],
            ["uptrend", "downtrend"],
            default="range",
        )
        features["volatility_regime"] = np.select(
            [features["atr_expansion"] >= 1.2, features["atr_expansion"] <= 0.9],
            ["expanded", "compressed"],
            default="normal",
        )

        return features.replace([np.inf, -np.inf], np.nan)

    def build_intraday_snapshot(self, intraday_frame: pd.DataFrame | None) -> Dict:
        if intraday_frame is None or intraday_frame.empty or len(intraday_frame) < 20:
            return {
                "intraday_change_pct": 0.0,
                "intraday_vwap_distance_pct": 0.0,
                "intraday_volume_ratio": 1.0,
                "intraday_above_vwap": False,
                "intraday_breakout": False,
            }

        frame = intraday_frame.dropna(subset=["Close"]).copy()
        close = frame["Close"].astype(float)
        high = frame["High"].astype(float)
        low = frame["Low"].astype(float)
        volume = frame["Volume"].astype(float).replace(0, np.nan)
        typical = (high + low + close) / 3
        cumulative_vwap = (typical * volume).cumsum() / volume.cumsum()
        volume_ratio = volume.iloc[-1] / volume.rolling(20).mean().iloc[-1]
        prior_high = high.rolling(20).max().shift(1).iloc[-1]

        return {
            "intraday_change_pct": round(((close.iloc[-1] / close.iloc[0]) - 1) * 100, 4),
            "intraday_vwap_distance_pct": round(((close.iloc[-1] - cumulative_vwap.iloc[-1]) / cumulative_vwap.iloc[-1]) * 100, 4),
            "intraday_volume_ratio": round(float(volume_ratio) if pd.notna(volume_ratio) else 1.0, 4),
            "intraday_above_vwap": bool(close.iloc[-1] > cumulative_vwap.iloc[-1]),
            "intraday_breakout": bool(close.iloc[-1] > prior_high) if pd.notna(prior_high) else False,
        }

    def build_snapshot(
        self,
        symbol: str,
        frame: pd.DataFrame,
        feature_frame: pd.DataFrame,
        intraday_frame: pd.DataFrame | None = None,
    ) -> Dict:
        latest = feature_frame.iloc[-1].replace([np.inf, -np.inf], np.nan)
        snapshot = {
            key: (None if pd.isna(value) else float(value) if isinstance(value, (int, float, np.number)) else value)
            for key, value in latest.items()
        }
        snapshot.update(self.build_intraday_snapshot(intraday_frame))
        snapshot["symbol"] = symbol.upper()
        snapshot["volume"] = int(frame["Volume"].iloc[-1])
        snapshot["open"] = float(frame["Open"].iloc[-1])
        snapshot["high"] = float(frame["High"].iloc[-1])
        snapshot["low"] = float(frame["Low"].iloc[-1])
        snapshot["close"] = float(frame["Close"].iloc[-1])
        snapshot["as_of"] = frame.index[-1].isoformat() if hasattr(frame.index[-1], "isoformat") else str(frame.index[-1])
        snapshot["near_resistance"] = bool((snapshot.get("distance_to_resistance_pct") or 99) <= 1.2)
        snapshot["near_support"] = bool((snapshot.get("distance_to_support_pct") or 99) <= 1.2)
        snapshot["delivery_available"] = snapshot.get("delivery_ratio") is not None
        return snapshot
