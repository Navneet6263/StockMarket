from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np
import pandas as pd
import yfinance as yf


class MultiTimeframeAnalyzer:
    def __init__(self):
        self.timeframes = {
            "5m": {"period": "5d", "interval": "5m"},
            "15m": {"period": "15d", "interval": "15m"},
            "1h": {"period": "2mo", "interval": "60m"},
            "1d": {"period": "1y", "interval": "1d"},
        }
        self.symbol_map = {
            "NIFTY": "^NSEI",
            "NIFTY50": "^NSEI",
            "BANKNIFTY": "^NSEBANK",
            "SENSEX": "^BSESN",
        }
        self.nifty_stocks = [
            "RELIANCE.NS",
            "TCS.NS",
            "HDFCBANK.NS",
            "INFY.NS",
            "ICICIBANK.NS",
            "HINDUNILVR.NS",
            "ITC.NS",
            "SBIN.NS",
            "BHARTIARTL.NS",
            "KOTAKBANK.NS",
            "LT.NS",
            "AXISBANK.NS",
            "ASIANPAINT.NS",
            "MARUTI.NS",
            "SUNPHARMA.NS",
            "TITAN.NS",
            "ULTRACEMCO.NS",
            "BAJFINANCE.NS",
            "NESTLEIND.NS",
            "WIPRO.NS",
        ]
        self.cache: Dict = {}
        self.cache_ttl = timedelta(minutes=3)

    def _resolve_symbol(self, symbol: str) -> str:
        clean = symbol.upper().replace(" ", "")
        if clean in self.symbol_map:
            return self.symbol_map[clean]
        if clean.startswith("^") or "." in clean:
            return clean
        return f"{clean}.NS"

    def _cache_get(self, key: str):
        cached = self.cache.get(key)
        if cached and datetime.now() - cached[0] < self.cache_ttl:
            return cached[1]
        return None

    def _cache_set(self, key: str, value):
        self.cache[key] = (datetime.now(), value)

    def _calculate_rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss + 0.0001)
        return 100 - (100 / (1 + rs))

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low = df["High"] - df["Low"]
        high_close = abs(df["High"] - df["Close"].shift())
        low_close = abs(df["Low"] - df["Close"].shift())
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def _rolling_vwap(self, df: pd.DataFrame, window: int = 20) -> pd.Series:
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        volume = df["Volume"].replace(0, np.nan)
        return (typical * volume).rolling(window).sum() / volume.rolling(window).sum()

    def get_timeframe_data(self, symbol: str, timeframe: str) -> pd.DataFrame:
        resolved = self._resolve_symbol(symbol)
        cache_key = f"{resolved}:{timeframe}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            config = self.timeframes[timeframe]
            ticker = yf.Ticker(resolved)
            df = ticker.history(period=config["period"], interval=config["interval"], auto_adjust=False)
            self._cache_set(cache_key, df)
            return df
        except Exception as exc:
            print(f"Error fetching {timeframe} data for {resolved}: {exc}")
            return pd.DataFrame()

    def analyze_timeframe(self, df: pd.DataFrame) -> Dict:
        if df.empty or len(df) < 20:
            return {
                "direction": "neutral",
                "strength": 0.5,
                "trend": "unknown",
                "rsi": 50.0,
                "volume_ratio": 1.0,
                "vwap_distance": 0.0,
                "atr_pct": 0.0,
                "structure_score": 0.0,
                "breakout": False,
            }

        latest = df.iloc[-1]
        price = float(latest["Close"])
        sma_20 = float(df["Close"].rolling(20).mean().iloc[-1])
        sma_50 = float(df["Close"].rolling(50).mean().iloc[-1] if len(df) >= 50 else df["Close"].rolling(20).mean().iloc[-1])
        vol_ma = df["Volume"].rolling(20).mean().iloc[-1]
        vol_ratio = float(latest["Volume"] / vol_ma) if vol_ma and not np.isnan(vol_ma) else 1.0
        rsi = float(self._calculate_rsi(df["Close"], 14).iloc[-1])
        atr = float(self._calculate_atr(df, 14).iloc[-1])
        atr_pct = float((atr / price) * 100) if price else 0.0
        vwap = float(self._rolling_vwap(df, 20).iloc[-1])
        vwap_distance = float(((price - vwap) / vwap) * 100) if vwap else 0.0

        prev = df.iloc[-2] if len(df) > 1 else latest
        structure_score = float(
            (1 if latest["High"] > prev["High"] else 0)
            + (1 if latest["Low"] > prev["Low"] else 0)
            - (1 if latest["High"] < prev["High"] else 0)
            - (1 if latest["Low"] < prev["Low"] else 0)
        )

        prior_high = df["High"].rolling(20).max().shift(1).iloc[-1]
        prior_low = df["Low"].rolling(20).min().shift(1).iloc[-1]
        breakout = bool(price > prior_high) if not np.isnan(prior_high) else False
        breakdown = bool(price < prior_low) if not np.isnan(prior_low) else False

        direction = "neutral"
        strength = 0.5
        if price > sma_20 > sma_50 and structure_score >= 1 and rsi >= 48:
            direction = "up"
            strength = 0.58 + min(abs(price - sma_20) / max(atr, 0.01), 0.22)
        elif price < sma_20 < sma_50 and structure_score <= -1 and rsi <= 52:
            direction = "down"
            strength = 0.58 + min(abs(price - sma_20) / max(atr, 0.01), 0.22)

        if direction != "neutral":
            if vol_ratio > 1.15:
                strength += 0.1
            if abs(vwap_distance) > 0.2:
                strength += 0.05
            if breakout or breakdown:
                strength += 0.05

        return {
            "direction": direction,
            "strength": round(float(min(strength, 0.98)), 4),
            "price": round(price, 2),
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2),
            "rsi": round(rsi, 2),
            "volume_ratio": round(vol_ratio, 3),
            "vwap_distance": round(vwap_distance, 3),
            "atr_pct": round(atr_pct, 3),
            "structure_score": round(structure_score, 2),
            "trend": "strong" if strength > 0.82 else "moderate" if strength > 0.65 else "weak",
            "breakout": breakout,
        }

    def get_market_breadth(self) -> Dict:
        cached = self._cache_get("market_breadth")
        if cached is not None:
            return cached

        green_count = 0
        red_count = 0
        total_checked = 0

        for stock in self.nifty_stocks:
            try:
                hist = yf.Ticker(stock).history(period="2d")
                if len(hist) >= 2:
                    change = hist["Close"].iloc[-1] - hist["Close"].iloc[-2]
                    if change > 0:
                        green_count += 1
                    else:
                        red_count += 1
                    total_checked += 1
            except Exception:
                continue

        if total_checked == 0:
            result = {"green": 0, "red": 0, "total": 0, "ratio": 0.5, "sentiment": "neutral"}
            self._cache_set("market_breadth", result)
            return result

        ratio = green_count / total_checked
        sentiment = "bullish" if ratio > 0.6 else "bearish" if ratio < 0.4 else "neutral"
        result = {
            "green": green_count,
            "red": red_count,
            "total": total_checked,
            "ratio": round(ratio, 3),
            "sentiment": sentiment,
        }
        self._cache_set("market_breadth", result)
        return result

    def get_pcr_simple(self, symbol: str) -> float:
        resolved = self._resolve_symbol(symbol)
        cache_key = f"pcr:{resolved}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            hist = yf.Ticker(resolved).history(period="5d")
            if len(hist) < 5:
                return 1.0

            volatility = hist["Close"].pct_change().std()
            recent_change = (hist["Close"].iloc[-1] - hist["Close"].iloc[-5]) / hist["Close"].iloc[-5]
            pcr = 1.0 + (volatility * 10) - (recent_change * 2)
            pcr = max(0.5, min(float(pcr), 2.0))
            self._cache_set(cache_key, pcr)
            return pcr
        except Exception:
            return 1.0

    def get_volatility_index(self, symbol: str) -> Dict:
        resolved = self._resolve_symbol(symbol)
        vix_symbol = "^INDIAVIX" if resolved.endswith(".NS") or resolved.startswith("^NSE") else "^VIX"
        cache_key = f"vix:{vix_symbol}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            hist = yf.Ticker(vix_symbol).history(period="10d")
            if hist.empty:
                raise ValueError("empty_vix")

            value = float(hist["Close"].iloc[-1])
            previous = float(hist["Close"].iloc[-2]) if len(hist) > 1 else value
            change_pct = ((value - previous) / previous) * 100 if previous else 0.0

            if value >= 18:
                regime = "high"
            elif value >= 14:
                regime = "elevated"
            else:
                regime = "low"

            result = {
                "symbol": vix_symbol,
                "value": round(value, 2),
                "change_pct": round(change_pct, 2),
                "regime": regime,
            }
            self._cache_set(cache_key, result)
            return result
        except Exception:
            return {"symbol": vix_symbol, "value": 0.0, "change_pct": 0.0, "regime": "unknown"}

    def analyze_multi_timeframe(self, symbol: str) -> Dict:
        results = {}
        ordered_timeframes = ["5m", "15m", "1h", "1d"]

        for timeframe in ordered_timeframes:
            df = self.get_timeframe_data(symbol, timeframe)
            results[timeframe] = self.analyze_timeframe(df)

        directions = [results[tf]["direction"] for tf in ordered_timeframes]
        up_count = directions.count("up")
        down_count = directions.count("down")
        aligned_count = max(up_count, down_count)

        if up_count == 4:
            sync_status = "STRONG_UP"
            sync_confidence = 0.95
            final_direction = "up"
        elif down_count == 4:
            sync_status = "STRONG_DOWN"
            sync_confidence = 0.95
            final_direction = "down"
        elif up_count >= 3:
            sync_status = "MODERATE_UP"
            sync_confidence = 0.78
            final_direction = "up"
        elif down_count >= 3:
            sync_status = "MODERATE_DOWN"
            sync_confidence = 0.78
            final_direction = "down"
        else:
            sync_status = "CONFLICTING"
            sync_confidence = 0.52
            final_direction = "neutral"

        breadth = self.get_market_breadth()
        pcr = self.get_pcr_simple(symbol)
        volatility_index = self.get_volatility_index(symbol)
        alignment_score = round(aligned_count / len(ordered_timeframes), 2)
        tradeable = sync_status != "CONFLICTING"

        if final_direction == "up" and breadth["sentiment"] == "bearish":
            sync_confidence *= 0.85
            tradeable = False
        elif final_direction == "down" and breadth["sentiment"] == "bullish":
            sync_confidence *= 0.85
            tradeable = False
        elif final_direction != "neutral" and breadth["sentiment"] != "neutral":
            sync_confidence = min(sync_confidence + 0.04, 0.98)

        if volatility_index["regime"] == "high":
            sync_confidence *= 0.88
            if alignment_score < 1.0:
                tradeable = False
        elif volatility_index["regime"] == "elevated":
            sync_confidence *= 0.94

        if pcr > 1.3 and final_direction == "down":
            sync_confidence *= 0.9
        elif pcr < 0.7 and final_direction == "up":
            sync_confidence *= 0.9

        position_size_factor = 1.0
        if volatility_index["regime"] == "high":
            position_size_factor = 0.4
        elif volatility_index["regime"] == "elevated":
            position_size_factor = 0.7
        if not tradeable:
            position_size_factor = min(position_size_factor, 0.25)

        final_confidence = round(float(min(sync_confidence, 0.98)), 4)
        analysis = self._generate_analysis(results, breadth, pcr, sync_status, volatility_index, tradeable)

        return {
            "timeframes": results,
            "sync_status": sync_status,
            "sync_confidence": final_confidence,
            "final_direction": final_direction,
            "market_breadth": breadth,
            "pcr": pcr,
            "volatility_index": volatility_index,
            "alignment_score": alignment_score,
            "tradeable": tradeable,
            "position_size_factor": position_size_factor,
            "analysis": analysis,
        }

    def _generate_analysis(
        self,
        timeframes: Dict,
        breadth: Dict,
        pcr: float,
        sync: str,
        volatility_index: Dict,
        tradeable: bool,
    ) -> List[str]:
        analysis = []

        if sync == "STRONG_UP":
            analysis.append("All 4 timeframes are aligned bullish.")
        elif sync == "STRONG_DOWN":
            analysis.append("All 4 timeframes are aligned bearish.")
        elif sync == "MODERATE_UP":
            analysis.append("3 of 4 timeframes are aligned bullish.")
        elif sync == "MODERATE_DOWN":
            analysis.append("3 of 4 timeframes are aligned bearish.")
        else:
            analysis.append("Timeframes are conflicting. Treat this as a no-trade zone.")

        analysis.append(
            f"Market breadth: {breadth['green']}/{breadth['total']} green, sentiment {breadth['sentiment']}."
        )
        analysis.append(f"PCR proxy: {pcr:.2f}.")
        analysis.append(
            f"Volatility index: {volatility_index['value']:.2f} ({volatility_index['regime']})."
        )
        analysis.append("Tradeable setup." if tradeable else "Do not force size until alignment returns.")

        for timeframe, data in timeframes.items():
            analysis.append(
                f"{timeframe.upper()}: {data['direction']} | RSI {data['rsi']:.1f} | "
                f"Vol {data['volume_ratio']:.2f}x | VWAP {data['vwap_distance']:.2f}%"
            )

        return analysis
