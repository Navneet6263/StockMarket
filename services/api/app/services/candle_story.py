"""
Candle Story Analyzer - Sequential Pattern Recognition

Reads 10-15 days of price action to detect higher-level narratives:
1. Trap-then-Breakout (operator shake → reversal)
2. Pullback-to-Base (30% up → 10% down → base → re-entry)
3. Climax-then-Fade (exhaustion → distribution)
4. Accumulation Base (stealth buying before breakout)
5. Squeeze-then-Expansion (volatility contraction → explosion)
"""

from __future__ import annotations

import logging
from typing import Dict, List
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


def analyze_candle_story(history: pd.DataFrame, current_snapshot: Dict) -> Dict:
    """
    Comprehensive candle story analysis - reads last 15 candles.
    
    Args:
        history: OHLCV DataFrame with at least 15 rows
        current_snapshot: Current indicator snapshot from indicators engine
        
    Returns:
        Dict with pattern, stage, trap_detected, base_quality, accumulation_score
    """
    try:
        if history is None or history.empty or len(history) < 15:
            return _empty_story()
        
        # Ensure we have clean data
        df = history.tail(30).copy()  # Look at last 30 days for context
        df = df.dropna(subset=['Close', 'Volume'])
        
        if len(df) < 15:
            return _empty_story()
        
        # Calculate daily returns and metrics
        df['returns'] = df['Close'].pct_change() * 100
        df['vol_ma_20'] = df['Volume'].rolling(20, min_periods=5).mean()
        df['vol_ratio'] = df['Volume'] / df['vol_ma_20']
        df['range_pct'] = ((df['High'] - df['Low']) / df['Close']) * 100
        df['body_pct'] = (abs(df['Close'] - df['Open']) / df['Close']) * 100
        df['close_location'] = (df['Close'] - df['Low']) / (df['High'] - df['Low'])
        df['close_location'] = df['close_location'].fillna(0.5)
        
        # Get current price and extract from snapshot
        current_price = float(current_snapshot.get('price', df['Close'].iloc[-1]))
        current_volume = float(current_snapshot.get('volume', df['Volume'].iloc[-1]))
        relative_volume = float(current_snapshot.get('relative_volume') or 1.0)
        gap_pct = float(current_snapshot.get('gap_pct') or 0.0)
        change_pct = float(current_snapshot.get('change_pct') or 0.0)
        trend_status = current_snapshot.get('trend_status', 'unknown')
        resistance = float(current_snapshot.get('resistance_20') or 0.0)
        obv_slope = float(current_snapshot.get('obv_slope', 0))
        cmf = float(current_snapshot.get('cmf', 0))
        rsi = float(current_snapshot.get('rsi', 50))
        ema_20 = float(current_snapshot.get('ema_20', current_price))
        
        # Calculate key metrics for pattern detection
        returns_5d = ((current_price / df['Close'].iloc[-6]) - 1) * 100 if len(df) >= 6 else 0
        returns_10d = ((current_price / df['Close'].iloc[-11]) - 1) * 100 if len(df) >= 11 else 0
        returns_20d = ((current_price / df['Close'].iloc[-21]) - 1) * 100 if len(df) >= 21 else 0
        
        # Find highest high and lowest low in periods
        high_20d = df['High'].tail(20).max() if len(df) >= 20 else current_price
        low_20d = df['Low'].tail(20).min() if len(df) >= 20 else current_price
        high_10d = df['High'].tail(10).max() if len(df) >= 10 else current_price
        low_10d = df['Low'].tail(10).min() if len(df) >= 10 else current_price
        
        # Detect patterns
        story = _detect_patterns(
            df=df,
            current_price=current_price,
            returns_5d=returns_5d,
            returns_10d=returns_10d,
            returns_20d=returns_20d,
            high_20d=high_20d,
            low_20d=low_20d,
            high_10d=high_10d,
            low_10d=low_10d,
            relative_volume=relative_volume,
            obv_slope=obv_slope,
            cmf=cmf,
            rsi=rsi,
            ema_20=ema_20,
        )
        
        return story
        
    except Exception as e:
        logger.warning("Candle story analysis failed: %s", e, exc_info=True)
        return _empty_story()


def _detect_patterns(
    df: pd.DataFrame,
    current_price: float,
    returns_5d: float,
    returns_10d: float,
    returns_20d: float,
    high_20d: float,
    low_20d: float,
    high_10d: float,
    low_10d: float,
    relative_volume: float,
    obv_slope: float,
    cmf: float,
    rsi: float,
    ema_20: float,
) -> Dict:
    """Detect specific candlestick patterns in the story."""
    
    patterns_detected: List[str] = []
    reasons: List[str] = []
    stage = "unknown"
    trap_detected = False
    base_quality = 0
    accumulation_score = 0
    direction = "neutral"
    confidence = 0
    
    # Get last 10 candles for detailed analysis
    recent = df.tail(10)
    
    # Calculate consolidation tightness
    range_20d = ((high_20d - low_20d) / low_20d) * 100 if low_20d else 99
    range_10d = ((high_10d - low_10d) / low_10d) * 100 if low_10d else 99
    
    # --- PATTERN 1: Trap-then-Breakout ---
    # Operator trap: Sharp drop → Volume spike → Reversal
    if len(recent) >= 5:
        # Find if there was a trap (big down move with volume)
        down_days = recent[recent['returns'] < -3]
        if len(down_days) >= 1:
            trap_day = down_days.iloc[-1]
            trap_volume = float(trap_day.get('vol_ratio', 0))
            
            # Check if we're now recovering
            if (trap_volume >= 1.5 and 
                returns_5d < -3 and 
                df['returns'].iloc[-1] > 0 and
                relative_volume > 1.2):
                
                patterns_detected.append("trap_then_breakout")
                direction = "bullish"
                stage = "reversal"
                trap_detected = True
                confidence += 25
                reasons.append(
                    f"Classic operator trap: Stock dropped {abs(returns_5d):.1f}% over 5 days "
                    f"with volume spike, now reversing with {relative_volume:.1f}x volume today"
                )
    
    # --- PATTERN 2: Pullback-to-Base (The Golden Setup) ---
    # 20%+ rally → 5-12% pullback → Base at EMA → BUY
    distance_from_ema = ((current_price - ema_20) / ema_20) * 100 if ema_20 else 0
    
    if (returns_20d > 18 and  # Had a strong run
        -12 <= returns_5d <= -3 and  # Pulled back healthy amount
        abs(distance_from_ema) < 5 and  # Near EMA support
        range_10d < 8):  # Building tight base
        
        patterns_detected.append("pullback_to_base")
        direction = "bullish"
        stage = "reentry_zone"
        base_quality += 30
        confidence += 30
        
        # Check for accumulation signals
        if obv_slope > 0 or cmf > 0.05:
            accumulation_score += 20
            reasons.append(
                f"GOLDEN SETUP: Stock rallied {returns_20d:.1f}% then pulled back {abs(returns_5d):.1f}%, "
                f"now building base near 20-EMA. OBV/CMF shows accumulation."
            )
        else:
            reasons.append(
                f"Pullback-to-Base: {returns_20d:.1f}% rally corrected {abs(returns_5d):.1f}%, "
                f"consolidating near EMA. Watch for volume confirmation."
            )
    
    # --- PATTERN 3: Climax-then-Fade (Distribution) ---
    # Parabolic move → Exhaustion candle → Fade
    if (returns_5d > 12 and  # Big recent move
        relative_volume > 1.3 and  # High volume
        df['returns'].iloc[-1] < 0 and  # Today fading
        float(df['close_location'].iloc[-1]) < 0.4):  # Weak close
        
        patterns_detected.append("climax_then_fade")
        direction = "bearish"
        stage = "exhaustion"
        confidence += 20
        reasons.append(
            f"CLIMAX TOP: Explosive {returns_5d:.1f}% rally with high volume is fading "
            f"with weak close near low — distribution pattern"
        )
    
    # --- PATTERN 4: Accumulation Base (Stealth Buying) ---
    # Tight range + OBV rising + Dry volume = Accumulation
    if (range_10d < 6 and  # Tight consolidation
        (obv_slope > 0 or cmf > 0.08) and  # Money flow positive
        relative_volume < 1.2 and  # Volume dry
        45 <= rsi <= 65):  # RSI neutral
        
        patterns_detected.append("accumulation_base")
        direction = "bullish"
        stage = "accumulation"
        base_quality += 25
        accumulation_score += 25
        confidence += 25
        reasons.append(
            f"STEALTH ACCUMULATION: Tight {range_10d:.1f}% range with dry volume, "
            f"but OBV/CMF rising — institutions quietly buying"
        )
    
    # --- PATTERN 5: Squeeze-then-Expansion (Volatility Breakout) ---
    # Very tight range → Volume compression → Ready to explode
    atr_ratio = _calculate_atr_ratio(recent)
    
    if (range_10d < 5 and  # Very tight
        atr_ratio < 0.85 and  # ATR contracting
        relative_volume < 1.1):  # Volume dried up
        
        patterns_detected.append("squeeze_setup")
        direction = "neutral"  # Can break either way
        stage = "coiled"
        base_quality += 20
        confidence += 20
        reasons.append(
            f"SQUEEZE SETUP: Price compressed in {range_10d:.1f}% range with ATR contraction "
            f"— volatility breakout imminent"
        )
    
    # --- PATTERN 6: Higher Lows + Accumulation (Bullish Structure) ---
    if len(recent) >= 3:
        lows = recent['Low'].tail(5).values
        if _has_higher_lows(lows) and (obv_slope > 0 or cmf > 0):
            if "pullback_to_base" not in patterns_detected:  # Don't double-count
                patterns_detected.append("higher_lows_accumulation")
                direction = "bullish"
                stage = "building_strength"
                base_quality += 15
                accumulation_score += 15
                confidence += 15
                reasons.append(
                    "Higher lows forming with positive OBV/CMF — bullish structure building"
                )
    
    # --- PATTERN 7: Failed Breakout / Bull Trap ---
    # Broke resistance → Immediately failed → Trap
    if len(recent) >= 3:
        # Check if price recently hit high then fell back
        peak_idx = recent['High'].tail(5).idxmax()
        peak_price = recent.loc[peak_idx, 'High']
        
        if (peak_price > high_10d * 0.99 and  # Was at or near highs
            current_price < peak_price * 0.96 and  # Fell 4%+ from peak
            returns_5d < -2):  # Confirmed downtrend
            
            patterns_detected.append("failed_breakout")
            direction = "bearish"
            stage = "bull_trap"
            trap_detected = True
            confidence += 20
            reasons.append(
                f"BULL TRAP: Broke above resistance at {peak_price:.2f} "
                f"but failed immediately, now down {abs(returns_5d):.1f}%"
            )
    
    # If no clear pattern, check for generic trends
    if not patterns_detected:
        if returns_20d > 15 and returns_5d > 0:
            patterns_detected.append("strong_uptrend")
            direction = "bullish"
            stage = "trending"
            confidence += 10
        elif returns_20d < -15 and returns_5d < 0:
            patterns_detected.append("strong_downtrend")
            direction = "bearish"
            stage = "trending"
            confidence += 10
        else:
            patterns_detected.append("range_bound")
            stage = "consolidation"
    
    # Calculate final scores
    base_quality = min(100, base_quality)
    accumulation_score = min(100, accumulation_score)
    confidence = min(95, confidence)
    
    return {
        "candle_story_pattern": patterns_detected[0] if patterns_detected else None,
        "all_patterns": patterns_detected,
        "candle_story_direction": direction,
        "candle_story_stage": stage,
        "candle_story_reasons": reasons,
        "trap_detected": trap_detected,
        "base_quality_score": base_quality,
        "accumulation_score": accumulation_score,
        "pattern_confidence": confidence,
        "is_pullback_base": "pullback_to_base" in patterns_detected,
        "is_trap_reversal": "trap_then_breakout" in patterns_detected,
        "is_accumulation": accumulation_score >= 20,
        "is_squeeze": "squeeze_setup" in patterns_detected,
        "is_distribution": "climax_then_fade" in patterns_detected,
    }


def _calculate_atr_ratio(df: pd.DataFrame, period: int = 10) -> float:
    """Calculate ATR expansion/contraction ratio."""
    try:
        tr = pd.DataFrame({
            'hl': df['High'] - df['Low'],
            'hc': abs(df['High'] - df['Close'].shift()),
            'lc': abs(df['Low'] - df['Close'].shift())
        }).max(axis=1)
        
        atr = tr.rolling(period, min_periods=5).mean()
        if len(atr) >= period:
            current_atr = atr.iloc[-1]
            avg_atr = atr.iloc[-period:].mean()
            return current_atr / avg_atr if avg_atr else 1.0
        return 1.0
    except Exception:
        return 1.0


def _has_higher_lows(lows: np.ndarray) -> bool:
    """Check if array has higher lows pattern."""
    if len(lows) < 3:
        return False
    
    # Check if each low is higher than previous (with small tolerance)
    for i in range(1, len(lows)):
        if lows[i] < lows[i-1] * 0.99:  # Allow 1% tolerance
            return False
    return True


def _empty_story() -> Dict:
    """Return empty story when analysis fails or insufficient data."""
    return {
        "candle_story_pattern": None,
        "all_patterns": [],
        "candle_story_direction": "neutral",
        "candle_story_stage": "unknown",
        "candle_story_reasons": [],
        "trap_detected": False,
        "base_quality_score": 0,
        "accumulation_score": 0,
        "pattern_confidence": 0,
        "is_pullback_base": False,
        "is_trap_reversal": False,
        "is_accumulation": False,
        "is_squeeze": False,
        "is_distribution": False,
    }
