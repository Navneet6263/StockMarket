from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier

try:
    from xgboost import XGBClassifier

    XGBOOST_AVAILABLE = True
except ImportError:
    XGBClassifier = None
    XGBOOST_AVAILABLE = False

try:
    from .pattern_analyzer import AdvancedPatternAnalyzer
except ImportError:
    from pattern_analyzer import AdvancedPatternAnalyzer


class StockPredictor:
    def __init__(self):
        if XGBOOST_AVAILABLE:
            self.model = XGBClassifier(
                n_estimators=320,
                max_depth=5,
                learning_rate=0.05,
                subsample=0.85,
                colsample_bytree=0.85,
                reg_lambda=1.0,
                random_state=42,
                eval_metric="logloss",
                objective="binary:logistic",
            )
            self.model_name = "xgboost"
        else:
            self.model = GradientBoostingClassifier(
                n_estimators=250,
                max_depth=5,
                learning_rate=0.05,
                subsample=0.85,
                random_state=42,
            )
            self.model_name = "gradient_boosting"

        self.is_trained = False
        self.pattern_analyzer = AdvancedPatternAnalyzer()
        self.pattern_performance: Dict = {}
        self.backtest_metrics: Dict = {}
        self.feature_importances: List[Dict] = []
        self.training_summary: Dict = {}
        self.feature_columns: List[str] = []

    def prepare_features(self, df: pd.DataFrame) -> pd.DataFrame:
        features = pd.DataFrame(index=df.index)

        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        open_ = df["Open"]
        volume = df["Volume"].replace(0, np.nan)

        typical_price = (high + low + close) / 3
        vwap_20 = (typical_price * volume).rolling(20).sum() / volume.rolling(20).sum()
        atr = self.calculate_atr(df, 14)
        sma_5 = close.rolling(5).mean()
        sma_20 = close.rolling(20).mean()
        sma_50 = close.rolling(50).mean()
        sma_200 = close.rolling(200).mean()
        hv_20 = close.pct_change().rolling(20).std() * np.sqrt(252)
        hv_60 = close.pct_change().rolling(60).std() * np.sqrt(252)

        higher_high = (high > high.shift(1)).astype(int)
        lower_low = (low < low.shift(1)).astype(int)
        higher_low = (low > low.shift(1)).astype(int)
        lower_high = (high < high.shift(1)).astype(int)

        prev_high_20 = high.rolling(20).max().shift(1)
        prev_low_20 = low.rolling(20).min().shift(1)
        delivery_pct = self._extract_delivery_pct(df)

        features["price_change"] = close.pct_change()
        features["price_change_2d"] = close.pct_change(2)
        features["price_change_5d"] = close.pct_change(5)
        features["momentum_5"] = close.pct_change(5)
        features["momentum_20"] = close.pct_change(20)
        features["mom_3m"] = close.pct_change(63)
        features["mom_6m"] = close.pct_change(126)

        features["intraday_body_pct"] = (close - open_) / open_.replace(0, np.nan)
        features["range_pct"] = (high - low) / close.replace(0, np.nan)
        features["upper_wick_pct"] = (high - open_.where(open_ > close, close)) / close.replace(0, np.nan)
        features["lower_wick_pct"] = (open_.where(open_ < close, close) - low) / close.replace(0, np.nan)
        features["high_low_ratio"] = high / low.replace(0, np.nan)
        features["close_open_ratio"] = close / open_.replace(0, np.nan)

        features["volume_ratio"] = volume / volume.rolling(20).mean()
        features["volume_spike"] = (volume > volume.rolling(20).mean() * 1.5).astype(int)
        features["volume_trend"] = volume.rolling(5).mean() / volume.rolling(20).mean()
        features["delivery_pct"] = delivery_pct
        features["delivery_spike"] = (delivery_pct > delivery_pct.rolling(20).mean()).astype(int)
        features["vwap_distance"] = (close - vwap_20) / vwap_20.replace(0, np.nan)

        features["rsi"] = self.calculate_rsi(close, 14)
        features["rsi_oversold"] = (features["rsi"] < 30).astype(int)
        features["rsi_overbought"] = (features["rsi"] > 70).astype(int)

        features["sma_5"] = sma_5
        features["sma_20"] = sma_20
        features["sma_50"] = sma_50
        features["sma_200"] = sma_200
        features["ma_spread_20_50"] = (sma_20 - sma_50) / sma_50.replace(0, np.nan)
        features["ma_spread_50_200"] = (sma_50 - sma_200) / sma_200.replace(0, np.nan)
        features["sma_20_slope"] = sma_20.pct_change(5)
        features["sma_50_slope"] = sma_50.pct_change(10)
        features["price_above_sma20"] = (close > sma_20).astype(int)
        features["price_above_sma50"] = (close > sma_50).astype(int)
        features["price_above_vwap"] = (close > vwap_20).astype(int)
        features["sma_cross_up"] = ((sma_5 > sma_20) & (sma_5.shift(1) <= sma_20.shift(1))).astype(int)
        features["sma_cross_down"] = ((sma_5 < sma_20) & (sma_5.shift(1) >= sma_20.shift(1))).astype(int)

        features["atr"] = atr
        features["atr_pct"] = atr / close.replace(0, np.nan)
        features["hv_20"] = hv_20
        features["hv_60"] = hv_60
        features["volatility_ratio"] = hv_20 / hv_60.replace(0, np.nan)
        features["range_expansion"] = features["range_pct"] / features["range_pct"].rolling(20).mean()
        features["boll_perc"] = (close - sma_20) / (2 * close.rolling(20).std() + 1e-6)
        features["vix_proxy"] = hv_20 * 100

        features["higher_high"] = higher_high
        features["lower_low"] = lower_low
        features["higher_low"] = higher_low
        features["lower_high"] = lower_high
        features["structure_score"] = (higher_high + higher_low - lower_low - lower_high).rolling(5).sum()
        features["breakout_strength"] = (close - prev_high_20) / atr.replace(0, np.nan)
        features["breakdown_strength"] = (prev_low_20 - close) / atr.replace(0, np.nan)
        features["distance_to_resistance"] = (prev_high_20 - close) / close.replace(0, np.nan)
        features["distance_to_support"] = (close - prev_low_20) / close.replace(0, np.nan)
        features["trend_efficiency"] = abs(close - close.shift(10)) / (
            close.diff().abs().rolling(10).sum() + 1e-6
        )

        rolling_peak = close.cummax()
        features["drawdown"] = (close - rolling_peak) / rolling_peak.replace(0, np.nan)

        features["hammer"] = self.detect_hammer_feature(df)
        features["shooting_star"] = self.detect_shooting_star_feature(df)
        features["doji"] = self.detect_doji_feature(df)
        features["engulfing_bull"] = self.detect_engulfing_bull_feature(df)
        features["engulfing_bear"] = self.detect_engulfing_bear_feature(df)

        features["gap_up"] = (
            (open_ > close.shift(1))
            & ((open_ - close.shift(1)) / close.shift(1).replace(0, np.nan) > 0.01)
        ).astype(int)
        features["gap_down"] = (
            (open_ < close.shift(1))
            & ((close.shift(1) - open_) / close.shift(1).replace(0, np.nan) > 0.01)
        ).astype(int)

        cleaned = features.replace([np.inf, -np.inf], np.nan).fillna(0)
        self.feature_columns = list(cleaned.columns)
        return cleaned

    def calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss + 0.0001)
        return 100 - (100 / (1 + rs))

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low = df["High"] - df["Low"]
        high_close = abs(df["High"] - df["Close"].shift())
        low_close = abs(df["Low"] - df["Close"].shift())
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def detect_hammer_feature(self, df: pd.DataFrame) -> pd.Series:
        hammer = pd.Series(0, index=df.index)
        for i in range(1, len(df)):
            current = df.iloc[i]
            body = abs(current["Close"] - current["Open"])
            lower_shadow = min(current["Open"], current["Close"]) - current["Low"]
            upper_shadow = current["High"] - max(current["Open"], current["Close"])
            if lower_shadow > 2 * body and upper_shadow < 0.1 * max(body, 0.01):
                hammer.iloc[i] = 1
        return hammer

    def detect_shooting_star_feature(self, df: pd.DataFrame) -> pd.Series:
        shooting_star = pd.Series(0, index=df.index)
        for i in range(1, len(df)):
            current = df.iloc[i]
            body = abs(current["Close"] - current["Open"])
            lower_shadow = min(current["Open"], current["Close"]) - current["Low"]
            upper_shadow = current["High"] - max(current["Open"], current["Close"])
            if upper_shadow > 2 * body and lower_shadow < 0.1 * max(body, 0.01):
                shooting_star.iloc[i] = 1
        return shooting_star

    def detect_doji_feature(self, df: pd.DataFrame) -> pd.Series:
        doji = pd.Series(0, index=df.index)
        for i in range(len(df)):
            current = df.iloc[i]
            body = abs(current["Close"] - current["Open"])
            range_size = current["High"] - current["Low"]
            if body < 0.1 * max(range_size, 0.01):
                doji.iloc[i] = 1
        return doji

    def detect_engulfing_bull_feature(self, df: pd.DataFrame) -> pd.Series:
        engulfing = pd.Series(0, index=df.index)
        for i in range(1, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i - 1]
            if (
                prev["Close"] < prev["Open"]
                and current["Close"] > current["Open"]
                and current["Open"] < prev["Close"]
                and current["Close"] > prev["Open"]
            ):
                engulfing.iloc[i] = 1
        return engulfing

    def detect_engulfing_bear_feature(self, df: pd.DataFrame) -> pd.Series:
        engulfing = pd.Series(0, index=df.index)
        for i in range(1, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i - 1]
            if (
                prev["Close"] > prev["Open"]
                and current["Close"] < current["Open"]
                and current["Open"] > prev["Close"]
                and current["Close"] < prev["Open"]
            ):
                engulfing.iloc[i] = 1
        return engulfing

    def train_model(self, df: pd.DataFrame) -> Dict:
        features = self.prepare_features(df)
        target = (df["Close"].shift(-1) > df["Close"]).astype(int)

        self._learn_pattern_performance(df, features)

        valid_idx = features.index[:-1]
        X = features.loc[valid_idx]
        y = target.loc[valid_idx]

        split_idx = int(len(X) * 0.8)
        split_idx = min(max(split_idx, 60), len(X) - 20)

        X_train = X.iloc[:split_idx]
        y_train = y.iloc[:split_idx]
        X_test = X.iloc[split_idx:]
        y_test = y.iloc[split_idx:]

        self.model.fit(X_train, y_train)
        train_score = float(self.model.score(X_train, y_train))
        test_score = float(self.model.score(X_test, y_test))

        self.backtest_metrics = self._run_backtest(df.loc[valid_idx], X, split_idx)
        self.feature_importances = self._get_feature_importances()
        self.training_summary = {
            "train_accuracy": round(train_score, 4),
            "test_accuracy": round(test_score, 4),
            "model_type": self.model_name,
            "feature_importance": self.feature_importances[:10],
            "backtest": self.backtest_metrics,
        }

        self.is_trained = True
        return {
            "train_accuracy": train_score,
            "test_accuracy": test_score,
            "pattern_stats": self._get_pattern_stats(),
            "feature_importance": self.feature_importances[:10],
            "backtest_metrics": self.backtest_metrics,
            "model_type": self.model_name,
        }

    def _run_backtest(self, price_df: pd.DataFrame, features: pd.DataFrame, split_idx: int) -> Dict:
        test_features = features.iloc[split_idx:]
        if len(test_features) < 10:
            return {}

        probabilities = self.model.predict_proba(test_features)
        future_returns = price_df["Close"].pct_change().shift(-1).iloc[split_idx:]
        volume_ratio = features["volume_ratio"].iloc[split_idx:]
        structure_score = features["structure_score"].iloc[split_idx:]
        volatility_ratio = features["volatility_ratio"].iloc[split_idx:]

        strategy_returns: List[float] = []
        trade_returns: List[float] = []

        for idx, prob in zip(test_features.index, probabilities):
            prob_down = float(prob[0])
            prob_up = float(prob[1])
            edge = prob_up - prob_down
            next_ret = future_returns.loc[idx]
            if pd.isna(next_ret):
                continue

            aligned = abs(structure_score.loc[idx]) >= 1
            liquid = volume_ratio.loc[idx] >= 0.9
            stable = volatility_ratio.loc[idx] <= 1.8 if volatility_ratio.loc[idx] != 0 else True

            signal = 0
            if prob_up >= 0.58 and edge > 0.08 and aligned and liquid and stable:
                signal = 1
            elif prob_down >= 0.58 and edge < -0.08 and aligned and liquid and stable:
                signal = -1

            strategy_return = float(signal * next_ret) if signal != 0 else 0.0
            strategy_returns.append(strategy_return)
            if signal != 0:
                trade_returns.append(strategy_return)

        if not strategy_returns:
            return {}

        returns_series = pd.Series(strategy_returns)
        equity_curve = (1 + returns_series).cumprod()
        running_peak = equity_curve.cummax()
        drawdown = (equity_curve / running_peak) - 1

        wins = [ret for ret in trade_returns if ret > 0]
        losses = [ret for ret in trade_returns if ret < 0]
        gross_profit = float(sum(wins))
        gross_loss = abs(float(sum(losses)))
        profit_factor = gross_profit / gross_loss if gross_loss else (gross_profit if gross_profit else 0.0)

        return {
            "trade_count": int(len(trade_returns)),
            "signal_rate": round(len(trade_returns) / max(len(strategy_returns), 1), 4),
            "win_rate": round(len(wins) / max(len(trade_returns), 1), 4),
            "avg_trade_return": round(float(np.mean(trade_returns)) if trade_returns else 0.0, 4),
            "profit_factor": round(float(profit_factor), 4),
            "max_drawdown": round(float(drawdown.min()) if not drawdown.empty else 0.0, 4),
            "net_return": round(float(equity_curve.iloc[-1] - 1), 4),
            "out_of_sample_days": int(len(strategy_returns)),
        }

    def _get_feature_importances(self) -> List[Dict]:
        if not hasattr(self.model, "feature_importances_"):
            return []

        pairs = []
        for name, score in zip(self.feature_columns, self.model.feature_importances_):
            pairs.append({"feature": name, "importance": round(float(score), 6)})

        pairs.sort(key=lambda item: item["importance"], reverse=True)
        return pairs

    def _extract_delivery_pct(self, df: pd.DataFrame) -> pd.Series:
        delivery_candidates = [
            "DELIV_PER",
            "DELIVERY_PCT",
            "DELIVERY_PERCENT",
        ]
        for column in delivery_candidates:
            if column in df.columns:
                series = pd.to_numeric(df[column], errors="coerce")
                return (series / 100).clip(lower=0, upper=1)

        if "Deliverable Volume" in df.columns:
            base = df["Volume"].replace(0, np.nan)
            series = pd.to_numeric(df["Deliverable Volume"], errors="coerce") / base
            return series.clip(lower=0, upper=1)

        if "DELIVERABLE_VOLUME" in df.columns:
            base = df["Volume"].replace(0, np.nan)
            series = pd.to_numeric(df["DELIVERABLE_VOLUME"], errors="coerce") / base
            return series.clip(lower=0, upper=1)

        return pd.Series(0.0, index=df.index)

    def _learn_pattern_performance(self, df: pd.DataFrame, features: pd.DataFrame):
        pattern_cols = ["hammer", "shooting_star", "doji", "engulfing_bull", "engulfing_bear"]
        df_reset = df.reset_index(drop=True)
        features_reset = features.reset_index(drop=True)

        for pattern in pattern_cols:
            if pattern in features_reset.columns:
                pattern_indices = features_reset[features_reset[pattern] == 1].index.tolist()
                if len(pattern_indices) > 10:
                    perf = self.pattern_analyzer.analyze_pattern_performance(df_reset, pattern, pattern_indices)
                    self.pattern_performance[pattern] = perf

    def _get_pattern_stats(self) -> Dict:
        stats = {}
        for pattern, perf in self.pattern_performance.items():
            stats[pattern] = {
                "success_rate": f"{perf.success_rate * 100:.1f}%",
                "occurrences": perf.total_occurrences,
                "avg_gain": f"{perf.avg_gain * 100:.2f}%",
            }
        return stats

    def predict(self, df: pd.DataFrame) -> Dict:
        if not self.is_trained:
            raise ValueError("Model not trained")

        features = self.prepare_features(df)
        latest_features = features.iloc[-1:]
        probability = self.model.predict_proba(latest_features)[0]

        market_regime = self.pattern_analyzer.get_market_regime(df)
        latest = df.iloc[-1]
        current_price = float(latest["Close"])

        pattern_warnings = []
        pattern_confirmations = []
        latest_idx = len(df) - 1
        for pattern_name, perf in self.pattern_performance.items():
            if pattern_name in features.columns and features[pattern_name].iloc[-1] == 1:
                should_trust, _, reasons = self.pattern_analyzer.should_trust_pattern(
                    df, pattern_name, latest_idx, perf
                )
                if should_trust:
                    pattern_confirmations.extend(reasons)
                else:
                    pattern_warnings.extend(reasons)

        resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
        support = df["Low"].rolling(20).min().shift(1).iloc[-1]
        if np.isnan(resistance):
            resistance = df["High"].tail(20).max()
        if np.isnan(support):
            support = df["Low"].tail(20).min()

        support = float(support)
        resistance = float(resistance)

        prob_down = float(probability[0])
        prob_up = float(probability[1])
        raw_edge = prob_up - prob_down
        base_confidence = max(prob_up, prob_down)

        volume_ratio = float(features["volume_ratio"].iloc[-1])
        volume_trend = float(features["volume_trend"].iloc[-1])
        delivery_pct = float(features["delivery_pct"].iloc[-1])
        vwap_distance = float(features["vwap_distance"].iloc[-1])
        rsi = float(features["rsi"].iloc[-1])
        atr = float(features["atr"].iloc[-1])
        atr_pct = float(features["atr_pct"].iloc[-1] * 100)
        structure_score = float(features["structure_score"].iloc[-1])
        breakout_strength = float(features["breakout_strength"].iloc[-1])
        breakdown_strength = float(features["breakdown_strength"].iloc[-1])
        trend_efficiency = float(features["trend_efficiency"].iloc[-1])

        volume_strength = float(
            np.clip(
                0.45
                + max(volume_ratio - 1.0, -0.4) * 0.35
                + max(volume_trend - 1.0, -0.4) * 0.15
                + max(delivery_pct - 0.45, 0) * 0.35,
                0.2,
                1.0,
            )
        )

        trend_strength = float(market_regime.get("trend_strength", 0.5))
        direction = "neutral"
        if raw_edge > 0.08:
            direction = "up"
        elif raw_edge < -0.08:
            direction = "down"

        adjusted_confidence = base_confidence
        regime_name = market_regime.get("regime", "sideways")
        if regime_name == "sideways":
            adjusted_confidence *= 0.82
        elif regime_name == "panic":
            adjusted_confidence *= 0.75
        elif regime_name.startswith("trending"):
            adjusted_confidence *= 1.03

        breakout_valid = current_price > resistance and volume_ratio >= 1.2 and structure_score > 0
        breakdown_valid = current_price < support and volume_ratio >= 1.2 and structure_score < 0
        near_resistance = resistance > 0 and ((resistance - current_price) / current_price) * 100 < 1.2
        near_support = support > 0 and ((current_price - support) / current_price) * 100 < 1.2

        reasons: List[str] = []
        if direction == "up":
            reasons.append(f"Directional bias up with probability spread {raw_edge * 100:.1f}%.")
        elif direction == "down":
            reasons.append(f"Directional bias down with probability spread {abs(raw_edge) * 100:.1f}%.")
        else:
            reasons.append("Probabilities are balanced, edge is weak.")

        reasons.append(f"Regime: {regime_name}.")
        reasons.append(f"Structure score: {structure_score:.1f}.")
        reasons.append(f"Volume ratio: {volume_ratio:.2f}x, delivery ratio: {delivery_pct * 100:.1f}%.")
        reasons.append(f"VWAP distance: {vwap_distance * 100:.2f}%, RSI: {rsi:.1f}.")

        if breakout_valid:
            reasons.append("Breakout has volume and structure support.")
        elif breakdown_valid:
            reasons.append("Breakdown has volume and structure support.")
        elif near_resistance and direction == "up":
            reasons.append("Long setup is too close to resistance.")
        elif near_support and direction == "down":
            reasons.append("Short setup is too close to support.")

        regime_alignment = 0.4
        if direction == "up" and market_regime.get("trend") in {"uptrend", "strong_uptrend"}:
            regime_alignment = 1.0
        elif direction == "down" and market_regime.get("trend") in {"downtrend", "strong_downtrend"}:
            regime_alignment = 1.0
        elif direction == "neutral":
            regime_alignment = 0.55

        structure_alignment = 0.4
        if direction == "up" and structure_score > 0:
            structure_alignment = 1.0
        elif direction == "down" and structure_score < 0:
            structure_alignment = 1.0
        elif direction == "neutral":
            structure_alignment = 0.55

        quality_score = int(
            round(
                100
                * (
                    0.4 * adjusted_confidence
                    + 0.2 * volume_strength
                    + 0.2 * trend_strength
                    + 0.1 * regime_alignment
                    + 0.1 * structure_alignment
                )
            )
        )
        if pattern_confirmations:
            quality_score = min(quality_score + 4, 99)
        if pattern_warnings:
            quality_score = max(quality_score - 6, 0)

        quality_grade = self._quality_grade(quality_score)
        quality_badge = self._quality_badge(quality_grade)

        no_trade_reason = ""
        action = "WATCH"
        setup = "Wait for clarity"
        signal_bias = direction

        if direction == "neutral":
            no_trade_reason = "Directional probabilities are too balanced."
        elif adjusted_confidence < 0.58:
            no_trade_reason = "Model confidence is below the trade threshold."
        elif regime_name == "sideways" and volume_ratio < 1.15:
            no_trade_reason = "Sideways regime with no expansion in volume."
        elif regime_name == "panic" and quality_score < 82:
            no_trade_reason = "Panic regime requires only top-quality setups."
        elif direction == "up" and near_resistance and not breakout_valid:
            no_trade_reason = "Upside is capped by nearby resistance without breakout confirmation."
        elif direction == "down" and near_support and not breakdown_valid:
            no_trade_reason = "Downside is capped by nearby support without breakdown confirmation."

        if no_trade_reason:
            action = "NO_TRADE"
            setup = "No Trade Zone"
        elif direction == "up":
            if breakout_valid:
                action = "BUY"
                setup = "Breakout Continuation Long"
            elif regime_name == "trending_up" and 42 <= rsi <= 68 and vwap_distance > -0.01:
                action = "BUY"
                setup = "Trend Pullback Long"
            else:
                action = "BUY"
                setup = "Momentum Long"
        elif direction == "down":
            if breakdown_valid:
                action = "SELL"
                setup = "Breakdown Continuation Short"
            elif regime_name == "trending_down" and 32 <= rsi <= 58 and vwap_distance < 0.01:
                action = "SELL"
                setup = "Trend Pullback Short"
            else:
                action = "SELL"
                setup = "Momentum Short"

        expected_move_pct = 0.0
        if signal_bias == "up":
            expected_move_pct = max(atr_pct * (0.55 + adjusted_confidence), 0.35)
        elif signal_bias == "down":
            expected_move_pct = -max(atr_pct * (0.55 + adjusted_confidence), 0.35)

        stop_distance_pct = max(atr_pct * 0.7, 0.45)
        target_1_pct = max(abs(expected_move_pct), stop_distance_pct * 1.5)
        target_2_pct = max(target_1_pct * 1.5, stop_distance_pct * 2.2)
        risk_reward_ratio = round(target_1_pct / stop_distance_pct, 2) if stop_distance_pct else 0.0

        entry_price = current_price
        stop_loss = current_price
        target_1 = current_price
        target_2 = current_price
        if signal_bias == "up":
            stop_loss = current_price * (1 - stop_distance_pct / 100)
            target_1 = current_price * (1 + target_1_pct / 100)
            target_2 = current_price * (1 + target_2_pct / 100)
        elif signal_bias == "down":
            stop_loss = current_price * (1 + stop_distance_pct / 100)
            target_1 = current_price * (1 - target_1_pct / 100)
            target_2 = current_price * (1 - target_2_pct / 100)

        position_size_factor = 1.0
        if regime_name == "panic":
            position_size_factor = 0.35
        elif market_regime.get("volatility") == "high_volatility":
            position_size_factor = 0.6
        elif action == "NO_TRADE":
            position_size_factor = 0.0

        risk_management = [
            f"Action: {action}.",
            f"Setup: {setup}.",
            f"Trade quality: {quality_grade} ({quality_score}/100).",
        ]
        if action == "NO_TRADE":
            risk_management.append(f"No-trade reason: {no_trade_reason}")
        else:
            risk_management.extend(
                [
                    f"Entry: {entry_price:.2f}",
                    f"Stop loss: {stop_loss:.2f}",
                    f"Target 1: {target_1:.2f}",
                    f"Target 2: {target_2:.2f}",
                    f"Risk/reward: 1:{risk_reward_ratio:.2f}",
                    f"Suggested size factor: {position_size_factor:.2f}x base risk.",
                ]
            )

        return {
            "direction": signal_bias,
            "confidence": round(float(np.clip(adjusted_confidence, 0.3, 0.95)), 4),
            "probability_up": round(prob_up, 4),
            "probability_down": round(prob_down, 4),
            "reasons": reasons,
            "risk_management": risk_management,
            "support": support,
            "resistance": resistance,
            "pattern_warnings": pattern_warnings[:3],
            "pattern_confirmations": pattern_confirmations[:3],
            "market_regime": market_regime,
            "market_structure": {
                "score": round(structure_score, 2),
                "breakout_strength": round(breakout_strength, 2),
                "breakdown_strength": round(breakdown_strength, 2),
                "trend_efficiency": round(trend_efficiency, 3),
            },
            "volume_strength": round(volume_strength, 3),
            "trend_strength": round(trend_strength, 3),
            "volume_analysis": {
                "volume_ratio": round(volume_ratio, 3),
                "volume_trend": round(volume_trend, 3),
                "delivery_pct": round(delivery_pct, 3),
                "vwap_distance_pct": round(vwap_distance * 100, 3),
            },
            "volatility_context": {
                "atr": round(atr, 4),
                "atr_pct": round(atr_pct, 3),
                "vix_proxy": round(float(features["vix_proxy"].iloc[-1]), 3),
                "regime": market_regime.get("volatility", "normal_volatility"),
            },
            "strategy": {
                "action": action,
                "setup": setup,
                "bias": signal_bias,
                "is_no_trade": action == "NO_TRADE",
                "no_trade_reason": no_trade_reason,
                "quality_score": quality_score,
                "quality_grade": quality_grade,
                "quality_badge": quality_badge,
                "position_size_factor": round(position_size_factor, 2),
            },
            "trade_plan": {
                "entry_price": round(entry_price, 2),
                "stop_loss": round(stop_loss, 2),
                "target_1": round(target_1, 2),
                "target_2": round(target_2, 2),
                "expected_move_pct": round(expected_move_pct, 2),
                "expected_move_points": round(current_price * expected_move_pct / 100, 2),
                "risk_reward_ratio": risk_reward_ratio,
                "stop_distance_pct": round(stop_distance_pct, 2),
                "position_size_factor": round(position_size_factor, 2),
            },
            "expected_move_pct": round(expected_move_pct, 2),
            "risk_reward_ratio": risk_reward_ratio,
            "quality_score": quality_score,
            "quality_grade": quality_grade,
            "quality_badge": quality_badge,
            "backtest": self.backtest_metrics,
            "model_type": self.model_name,
            "feature_importance": self.feature_importances[:10],
        }

    def _quality_grade(self, score: int) -> str:
        if score >= 80:
            return "A"
        if score >= 65:
            return "B"
        if score >= 50:
            return "C"
        return "D"

    def _quality_badge(self, grade: str) -> str:
        if grade == "A":
            return "HIGH_CONVICTION"
        if grade == "B":
            return "TRADEABLE"
        if grade == "C":
            return "CAUTION"
        return "AVOID"
