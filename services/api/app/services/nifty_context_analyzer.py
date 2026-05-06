"""
Nifty Context Analyzer - Financial Expert Level Index Analysis

A financial expert does NOT just look at green/red count.
They look at:
1. Where is Nifty in its trend structure? (uptrend, downtrend, range)
2. What are the key support/resistance levels?
3. Is Nifty near a breakout or breakdown zone?
4. What is the quality of the current move? (volume, breadth, momentum)
5. What does this mean for individual stocks?
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

NIFTY_SYMBOL = "^NSEI"
BANKNIFTY_SYMBOL = "^NSEBANK"
INDIA_VIX_SYMBOL = "^INDIAVIX"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return default
        return float(val)
    except Exception:
        return default


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / (loss.replace(0, np.nan))
    return (100 - (100 / (1 + rs))).fillna(50)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(period).mean()


def _find_key_levels(high: pd.Series, low: pd.Series, close: pd.Series, lookback: int = 60) -> Dict:
    """
    Find key support/resistance levels using swing highs/lows.
    This is how a real technical analyst reads a chart.
    """
    recent_high = high.tail(lookback)
    recent_low = low.tail(lookback)

    swing_highs: List[float] = []
    swing_lows: List[float] = []
    h = recent_high.values
    l = recent_low.values
    for i in range(3, len(h) - 3):
        if h[i] == max(h[i - 3:i + 4]):
            swing_highs.append(float(h[i]))
        if l[i] == min(l[i - 3:i + 4]):
            swing_lows.append(float(l[i]))

    def cluster(levels: List[float], tolerance_pct: float = 0.5) -> List[float]:
        if not levels:
            return []
        levels = sorted(levels)
        clusters = [[levels[0]]]
        for level in levels[1:]:
            if (level - clusters[-1][-1]) / clusters[-1][-1] * 100 <= tolerance_pct:
                clusters[-1].append(level)
            else:
                clusters.append([level])
        return [round(sum(c) / len(c), 2) for c in clusters]

    resistance_levels = cluster(swing_highs)[-4:]
    support_levels = cluster(swing_lows)[:4]

    current_price = float(close.iloc[-1])

    above = [r for r in resistance_levels if r > current_price * 1.001]
    below = [s for s in support_levels if s < current_price * 0.999]

    nearest_resistance = min(above) if above else round(current_price * 1.03, 2)
    nearest_support = max(below) if below else round(current_price * 0.97, 2)

    distance_to_resistance_pct = round((nearest_resistance - current_price) / current_price * 100, 2)
    distance_to_support_pct = round((current_price - nearest_support) / current_price * 100, 2)

    return {
        "nearest_resistance": nearest_resistance,
        "nearest_support": nearest_support,
        "distance_to_resistance_pct": distance_to_resistance_pct,
        "distance_to_support_pct": distance_to_support_pct,
        "resistance_levels": resistance_levels,
        "support_levels": support_levels,
    }


def _trend_structure(close: pd.Series, high: pd.Series, low: pd.Series) -> Dict:
    """
    Determine trend structure using higher highs/lows methodology.
    """
    ema_20 = close.ewm(span=20, adjust=False).mean()
    ema_50 = close.ewm(span=50, adjust=False).mean()
    ema_200 = close.ewm(span=200, adjust=False).mean()

    price = float(close.iloc[-1])
    e20 = float(ema_20.iloc[-1])
    e50 = float(ema_50.iloc[-1])
    e200 = float(ema_200.iloc[-1])

    e20_slope = float(ema_20.pct_change(5).iloc[-1] * 100)
    e50_slope = float(ema_50.pct_change(10).iloc[-1] * 100)

    recent_highs = high.tail(20).values
    recent_lows = low.tail(20).values
    hh = int(recent_highs[-1] > recent_highs[-10])
    hl = int(recent_lows[-1] > recent_lows[-10])
    lh = int(recent_highs[-1] < recent_highs[-10])
    ll = int(recent_lows[-1] < recent_lows[-10])

    ret_20d = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) > 21 else 0.0
    ret_60d = float((close.iloc[-1] / close.iloc[-61] - 1) * 100) if len(close) > 61 else 0.0

    if price > e20 > e50 > e200 and e20_slope > 0 and hh and hl:
        regime = "strong_uptrend"
        regime_label = "Strong Uptrend"
    elif price > e20 > e50 and e20_slope > 0:
        regime = "uptrend"
        regime_label = "Uptrend"
    elif price < e20 < e50 < e200 and e20_slope < 0 and lh and ll:
        regime = "strong_downtrend"
        regime_label = "Strong Downtrend"
    elif price < e20 < e50 and e20_slope < 0:
        regime = "downtrend"
        regime_label = "Downtrend"
    elif abs(ret_20d) < 3 and abs(e20_slope) < 0.3:
        regime = "sideways"
        regime_label = "Sideways / Consolidation"
    else:
        regime = "mixed"
        regime_label = "Mixed / Transitioning"

    return {
        "regime": regime,
        "regime_label": regime_label,
        "price": round(price, 2),
        "ema_20": round(e20, 2),
        "ema_50": round(e50, 2),
        "ema_200": round(e200, 2),
        "ema_20_slope": round(e20_slope, 3),
        "ema_50_slope": round(e50_slope, 3),
        "above_ema20": price > e20,
        "above_ema50": price > e50,
        "above_ema200": price > e200,
        "higher_highs": bool(hh),
        "higher_lows": bool(hl),
        "lower_highs": bool(lh),
        "lower_lows": bool(ll),
        "return_20d": round(ret_20d, 2),
        "return_60d": round(ret_60d, 2),
    }


def _momentum_quality(close: pd.Series, volume: pd.Series, df: pd.DataFrame) -> Dict:
    """
    Assess the quality of the current move.
    A financial expert asks: Is this move backed by real participation?
    """
    rsi_series = _rsi(close, 14)
    rsi_val = float(rsi_series.iloc[-1])
    rsi_5d_ago = float(rsi_series.iloc[-6]) if len(rsi_series) > 6 else rsi_val
    rsi_delta = rsi_val - rsi_5d_ago

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    macd_positive = bool(hist.iloc[-1] > 0)
    macd_improving = bool(hist.iloc[-1] > hist.iloc[-3])

    vol_avg = volume.rolling(20).mean()
    vol_ratio = float(volume.iloc[-1] / vol_avg.iloc[-1]) if vol_avg.iloc[-1] else 1.0

    atr_series = _atr(df, 14)
    atr_expansion = float(
        atr_series.iloc[-1] / atr_series.rolling(50).mean().iloc[-1]
    ) if len(atr_series) > 50 else 1.0

    score = 50
    if 50 <= rsi_val <= 70:
        score += 15
    elif rsi_val > 70:
        score += 5
    elif rsi_val < 40:
        score -= 15
    if rsi_delta > 3:
        score += 10
    elif rsi_delta < -3:
        score -= 10
    if macd_positive:
        score += 10
    if macd_improving:
        score += 5
    if vol_ratio > 1.2:
        score += 10
    elif vol_ratio < 0.8:
        score -= 10
    if atr_expansion > 1.1:
        score += 5

    score = max(10, min(int(score), 95))

    return {
        "rsi": round(rsi_val, 1),
        "rsi_delta_5d": round(rsi_delta, 1),
        "macd_positive": macd_positive,
        "macd_improving": macd_improving,
        "volume_ratio": round(vol_ratio, 2),
        "atr_expansion": round(atr_expansion, 2),
        "momentum_quality_score": score,
        "momentum_quality_label": "Strong" if score >= 70 else "Moderate" if score >= 50 else "Weak",
    }


def _nifty_narrative(trend: Dict, levels: Dict, momentum: Dict, vix_val: float) -> Dict:
    """
    Generate plain-language narrative like a financial expert would explain.
    Not just numbers - MEANING.
    """
    regime = trend["regime"]
    price = trend["price"]
    resistance = levels["nearest_resistance"]
    support = levels["nearest_support"]
    dist_res = levels["distance_to_resistance_pct"]
    dist_sup = levels["distance_to_support_pct"]

    if regime == "strong_uptrend":
        market_state = "Nifty is in a strong uptrend with higher highs and higher lows intact."
        bias = "bullish"
    elif regime == "uptrend":
        market_state = "Nifty is in an uptrend but the move is not yet confirmed as strong."
        bias = "bullish"
    elif regime == "strong_downtrend":
        market_state = "Nifty is in a strong downtrend. Avoid fresh longs until structure repairs."
        bias = "bearish"
    elif regime == "downtrend":
        market_state = "Nifty is in a downtrend. Be selective and keep position sizes small."
        bias = "bearish"
    elif regime == "sideways":
        market_state = "Nifty is consolidating in a range. Wait for a directional breakout before adding risk."
        bias = "neutral"
    else:
        market_state = "Nifty is in a mixed/transitioning phase. Reduce size and wait for clarity."
        bias = "neutral"

    if dist_res <= 1.5:
        level_context = f"Nifty is very close to resistance at {resistance}. A breakout here would be significant."
        level_signal = "near_resistance_breakout"
    elif dist_res <= 3.0:
        level_context = f"Nifty is approaching resistance at {resistance} ({dist_res:.1f}% away). Watch for reaction."
        level_signal = "approaching_resistance"
    elif dist_sup <= 1.5:
        level_context = f"Nifty is sitting on support at {support}. A bounce here would be constructive."
        level_signal = "on_support"
    elif dist_sup <= 3.0:
        level_context = f"Nifty has support at {support} ({dist_sup:.1f}% below). Pullbacks to this zone are buyable in uptrend."
        level_signal = "near_support"
    else:
        level_context = f"Nifty is in the middle of its range. Support at {support}, resistance at {resistance}."
        level_signal = "mid_range"

    if bias == "bullish" and level_signal in ("near_resistance_breakout", "approaching_resistance"):
        stock_implication = (
            "Stocks that have already broken out or are near their own breakout levels are the best candidates. "
            "If Nifty breaks resistance, it will pull many stocks up with it."
        )
        stock_action = "focus_on_breakouts"
    elif bias == "bullish" and level_signal in ("on_support", "near_support"):
        stock_implication = (
            "Nifty is near support in an uptrend - this is a good zone to look for quality stocks "
            "that are also holding their own support levels. Risk/reward is favorable here."
        )
        stock_action = "buy_support_bounces"
    elif bias == "bullish":
        stock_implication = (
            "Nifty trend is supportive. Focus on stocks with strong relative strength - "
            "those that are outperforming Nifty are the real leaders."
        )
        stock_action = "focus_on_relative_strength"
    elif bias == "bearish":
        stock_implication = (
            "Avoid fresh longs in a weak Nifty environment. "
            "Only the strongest stocks with very tight setups deserve attention."
        )
        stock_action = "reduce_exposure"
    else:
        stock_implication = (
            "In a sideways Nifty, only stocks with their own independent catalyst or breakout setup "
            "will move. Do not rely on index tailwind."
        )
        stock_action = "catalyst_driven_only"

    if vix_val >= 20:
        vix_note = f"India VIX is elevated at {vix_val:.1f}. Reduce position sizes and widen stops."
        vix_regime = "high_fear"
    elif vix_val >= 15:
        vix_note = f"India VIX is at {vix_val:.1f} - moderate caution. Normal position sizing is fine."
        vix_regime = "moderate"
    elif vix_val > 0:
        vix_note = f"India VIX is low at {vix_val:.1f}. Market is calm - good environment for swing trades."
        vix_regime = "low_fear"
    else:
        vix_note = "India VIX data unavailable."
        vix_regime = "unknown"

    market_score = 50
    if bias == "bullish":
        market_score += 20
    elif bias == "bearish":
        market_score -= 20
    if level_signal in ("on_support", "near_support") and bias == "bullish":
        market_score += 10
    if level_signal == "near_resistance_breakout":
        market_score += 5
    if momentum["momentum_quality_score"] >= 70:
        market_score += 10
    elif momentum["momentum_quality_score"] < 40:
        market_score -= 10
    if vix_regime == "high_fear":
        market_score -= 15
    elif vix_regime == "low_fear":
        market_score += 5
    market_score = max(10, min(int(market_score), 95))

    return {
        "bias": bias,
        "market_state": market_state,
        "level_context": level_context,
        "level_signal": level_signal,
        "stock_implication": stock_implication,
        "stock_action": stock_action,
        "vix_note": vix_note,
        "vix_regime": vix_regime,
        "market_score": market_score,
        "market_score_label": "Favorable" if market_score >= 65 else "Neutral" if market_score >= 45 else "Unfavorable",
        "full_narrative": f"{market_state} {level_context} {stock_implication}",
    }


def analyze_nifty_context(force_refresh: bool = False) -> Dict:
    """
    Main entry point. Returns comprehensive Nifty context analysis
    that a financial expert would use to make stock selection decisions.
    """
    try:
        nifty = yf.Ticker(NIFTY_SYMBOL)
        df = nifty.history(period="1y", interval="1d", auto_adjust=False)
        if df.empty or len(df) < 60:
            return _fallback_context("insufficient_nifty_data")

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        volume = df["Volume"].astype(float).replace(0, np.nan)

        trend = _trend_structure(close, high, low)
        levels = _find_key_levels(high, low, close, lookback=60)
        momentum = _momentum_quality(close, volume, df)

        vix_val = 0.0
        try:
            vix_df = yf.Ticker(INDIA_VIX_SYMBOL).history(period="5d")
            if not vix_df.empty:
                vix_val = float(vix_df["Close"].iloc[-1])
        except Exception:
            pass

        narrative = _nifty_narrative(trend, levels, momentum, vix_val)

        banknifty_bias = "unknown"
        try:
            bn_df = yf.Ticker(BANKNIFTY_SYMBOL).history(period="60d", interval="1d", auto_adjust=False)
            if not bn_df.empty and len(bn_df) >= 20:
                bn_close = bn_df["Close"].astype(float)
                bn_ema20 = float(bn_close.ewm(span=20, adjust=False).mean().iloc[-1])
                bn_ema50 = float(bn_close.ewm(span=50, adjust=False).mean().iloc[-1])
                bn_price = float(bn_close.iloc[-1])
                if bn_price > bn_ema20 > bn_ema50:
                    banknifty_bias = "bullish"
                elif bn_price < bn_ema20 < bn_ema50:
                    banknifty_bias = "bearish"
                else:
                    banknifty_bias = "neutral"
        except Exception:
            pass

        if (narrative["bias"] == "bullish" and banknifty_bias == "bullish") or \
           (narrative["bias"] == "bearish" and banknifty_bias == "bearish"):
            confluence = "aligned"
        elif narrative["bias"] != "neutral" and banknifty_bias not in ("unknown", "neutral") and \
                narrative["bias"] != banknifty_bias:
            confluence = "diverging"
        else:
            confluence = "partial"

        return {
            "nifty_price": trend["price"],
            "nifty_regime": trend["regime"],
            "nifty_regime_label": trend["regime_label"],
            "nifty_bias": narrative["bias"],
            "nifty_return_20d": trend["return_20d"],
            "nifty_return_60d": trend["return_60d"],
            "nifty_rsi": momentum["rsi"],
            "nifty_volume_ratio": momentum["volume_ratio"],
            "nifty_momentum_quality": momentum["momentum_quality_score"],
            "nifty_momentum_label": momentum["momentum_quality_label"],
            "nearest_resistance": levels["nearest_resistance"],
            "nearest_support": levels["nearest_support"],
            "distance_to_resistance_pct": levels["distance_to_resistance_pct"],
            "distance_to_support_pct": levels["distance_to_support_pct"],
            "resistance_levels": levels["resistance_levels"],
            "support_levels": levels["support_levels"],
            "level_signal": narrative["level_signal"],
            "market_state": narrative["market_state"],
            "level_context": narrative["level_context"],
            "stock_implication": narrative["stock_implication"],
            "stock_action": narrative["stock_action"],
            "vix": vix_val,
            "vix_note": narrative["vix_note"],
            "vix_regime": narrative["vix_regime"],
            "banknifty_bias": banknifty_bias,
            "index_confluence": confluence,
            "market_score": narrative["market_score"],
            "market_score_label": narrative["market_score_label"],
            "full_narrative": narrative["full_narrative"],
            "ema_20": trend["ema_20"],
            "ema_50": trend["ema_50"],
            "ema_200": trend["ema_200"],
            "above_ema20": trend["above_ema20"],
            "above_ema50": trend["above_ema50"],
            "above_ema200": trend["above_ema200"],
            "higher_highs": trend["higher_highs"],
            "higher_lows": trend["higher_lows"],
            "data_available": True,
        }

    except Exception as exc:
        logger.warning("[NIFTY_CONTEXT] analysis failed: %s", exc)
        return _fallback_context(f"error: {exc}")


def _fallback_context(reason: str) -> Dict:
    return {
        "nifty_price": None,
        "nifty_regime": "unknown",
        "nifty_regime_label": "Unknown",
        "nifty_bias": "neutral",
        "nifty_return_20d": None,
        "nifty_return_60d": None,
        "nifty_rsi": None,
        "nifty_volume_ratio": None,
        "nifty_momentum_quality": 50,
        "nifty_momentum_label": "Unknown",
        "nearest_resistance": None,
        "nearest_support": None,
        "distance_to_resistance_pct": None,
        "distance_to_support_pct": None,
        "resistance_levels": [],
        "support_levels": [],
        "level_signal": "unknown",
        "market_state": "Nifty context unavailable.",
        "level_context": "",
        "stock_implication": "Cannot assess market environment without index data.",
        "stock_action": "wait",
        "vix": 0.0,
        "vix_note": "VIX data unavailable.",
        "vix_regime": "unknown",
        "banknifty_bias": "unknown",
        "index_confluence": "unknown",
        "market_score": 50,
        "market_score_label": "Neutral",
        "full_narrative": "Market context unavailable.",
        "ema_20": None,
        "ema_50": None,
        "ema_200": None,
        "above_ema20": None,
        "above_ema50": None,
        "above_ema200": None,
        "higher_highs": None,
        "higher_lows": None,
        "data_available": False,
        "unavailable_reason": reason,
    }


def stock_nifty_alignment_score(signal: Dict, nifty_context: Dict) -> Dict:
    """
    Given a stock signal and Nifty context, compute how well they align.
    A financial expert always checks: Is the stock moving WITH the market or AGAINST it?
    """
    stock_direction = signal.get("direction", "neutral")
    nifty_bias = nifty_context.get("nifty_bias", "neutral")
    nifty_regime = nifty_context.get("nifty_regime", "unknown")
    level_signal = nifty_context.get("level_signal", "unknown")
    market_score = nifty_context.get("market_score", 50)
    vix_regime = nifty_context.get("vix_regime", "unknown")

    score = 50
    reasons: List[str] = []
    warnings: List[str] = []

    if stock_direction == "bullish" and nifty_bias == "bullish":
        score += 20
        reasons.append("Stock direction aligns with Nifty uptrend.")
    elif stock_direction == "bearish" and nifty_bias == "bearish":
        score += 20
        reasons.append("Stock direction aligns with Nifty downtrend.")
    elif stock_direction == "bullish" and nifty_bias == "bearish":
        score -= 25
        warnings.append("Stock is bullish but Nifty is in downtrend - counter-trend risk is high.")
    elif stock_direction == "bearish" and nifty_bias == "bullish":
        score -= 20
        warnings.append("Stock is bearish but Nifty is in uptrend - short setups are harder to sustain.")

    if nifty_regime in ("strong_uptrend", "uptrend") and stock_direction == "bullish":
        score += 10
        reasons.append("Nifty regime supports bullish stock setups.")
    elif nifty_regime in ("strong_downtrend", "downtrend") and stock_direction == "bearish":
        score += 10

    if level_signal == "on_support" and stock_direction == "bullish":
        score += 10
        reasons.append("Nifty is bouncing from support - good timing for bullish entries.")
    elif level_signal == "near_resistance_breakout" and stock_direction == "bullish":
        score += 5
        reasons.append("Nifty near breakout - if it breaks, stocks will follow.")
    elif level_signal == "approaching_resistance" and stock_direction == "bullish":
        score -= 5
        warnings.append("Nifty approaching resistance - wait for breakout confirmation before adding.")

    if vix_regime == "high_fear":
        score -= 15
        warnings.append("High VIX - reduce position size regardless of setup quality.")
    elif vix_regime == "low_fear":
        score += 5

    if market_score >= 70:
        score += 5
    elif market_score <= 35:
        score -= 10

    score = max(10, min(int(score), 98))
    alignment = "strong" if score >= 75 else "moderate" if score >= 55 else "weak" if score >= 35 else "against_market"

    return {
        "nifty_alignment_score": score,
        "nifty_alignment": alignment,
        "nifty_alignment_reasons": reasons,
        "nifty_alignment_warnings": warnings,
        "nifty_bias": nifty_bias,
        "nifty_regime": nifty_regime,
        "vix_regime": vix_regime,
    }
