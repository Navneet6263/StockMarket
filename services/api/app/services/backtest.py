from __future__ import annotations

from typing import Dict

import pandas as pd

from app.core.cache import TTLCache
from app.core.settings import Settings
from app.services.indicators import IndicatorEngine
from app.services.scoring import ScoringEngine


class BacktestService:
    def __init__(self, settings: Settings, indicators: IndicatorEngine, scoring: ScoringEngine):
        self.settings = settings
        self.indicators = indicators
        self.scoring = scoring
        self.cache: TTLCache[Dict] = TTLCache(settings.backtest_cache_ttl_sec)

    def _aggregate(self, records: list[Dict]) -> Dict:
        if not records:
            return {
                "signal_count": 0,
                "win_rate": 0.0,
                "false_positive_rate": 0.0,
                "avg_win_pct": 0.0,
                "avg_loss_pct": 0.0,
                "profit_factor": 0.0,
                "expectancy_pct": 0.0,
            }

        wins = [item for item in records if item["win"]]
        losses = [item for item in records if not item["win"]]
        avg_win = sum(item["favorable_pct"] for item in wins) / len(wins) if wins else 0.0
        avg_loss = sum(item["adverse_pct"] for item in losses) / len(losses) if losses else 0.0
        gross_win = sum(item["favorable_pct"] for item in wins)
        gross_loss = sum(item["adverse_pct"] for item in losses)
        win_rate = len(wins) / len(records)
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        return {
            "signal_count": len(records),
            "win_rate": round(win_rate, 4),
            "false_positive_rate": round(1 - win_rate, 4),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else round(gross_win, 2),
            "expectancy_pct": round(expectancy, 2),
        }

    def evaluate(self, symbol: str, frame: pd.DataFrame, benchmark_frame: pd.DataFrame | None = None) -> Dict:
        cache_key = f"{symbol.upper()}:{len(frame)}:{self.settings.hold_days}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        if frame.empty or len(frame) < max(self.settings.min_history_bars, 140):
            return {}

        features = self.indicators.build_feature_frame(frame, benchmark_frame)
        hold_days = self.settings.hold_days
        start = max(80, features.dropna(subset=["price"]).index.get_loc(features.dropna(subset=["price"]).index[0]))

        bullish_records: list[Dict] = []
        bearish_records: list[Dict] = []
        calibration: dict[str, list[bool]] = {}

        for position in range(start, len(frame) - hold_days):
            row = features.iloc[position].to_dict()
            if pd.isna(row.get("price")):
                continue

            signal = self.scoring.evaluate(symbol, row, calibrate=False)
            direction = signal["direction"]
            if direction == "neutral" or signal["alert_level"] == "avoid":
                continue

            current_close = float(frame["Close"].iloc[position])
            future_window = frame.iloc[position + 1 : position + 1 + hold_days]
            if future_window.empty:
                continue

            threshold = max(0.6, float(row.get("atr_pct") or 0) * 0.4)
            if direction == "bullish":
                favorable = ((future_window["High"].max() / current_close) - 1) * 100
                adverse = ((current_close / future_window["Low"].min()) - 1) * 100
                win = favorable >= threshold
                bullish_records.append({"win": win, "favorable_pct": favorable, "adverse_pct": adverse})
            else:
                favorable = ((current_close / future_window["Low"].min()) - 1) * 100
                adverse = ((future_window["High"].max() / current_close) - 1) * 100
                win = favorable >= threshold
                bearish_records.append({"win": win, "favorable_pct": favorable, "adverse_pct": adverse})

            bucket_floor = int(signal["confidence"] // 10) * 10
            bucket = f"{bucket_floor}-{bucket_floor + 9}"
            calibration.setdefault(bucket, []).append(win)

        payload = {
            "window_days": hold_days,
            "bullish": self._aggregate(bullish_records),
            "bearish": self._aggregate(bearish_records),
            "calibration": [
                {
                    "bucket": bucket,
                    "signal_count": len(results),
                    "win_rate": round(sum(results) / len(results), 4),
                }
                for bucket, results in sorted(calibration.items())
            ],
        }
        self.cache.set(cache_key, payload)
        return payload
