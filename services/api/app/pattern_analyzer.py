import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
from dataclasses import dataclass

@dataclass
class PatternPerformance:
    pattern_name: str
    total_occurrences: int
    success_rate: float
    avg_gain: float
    avg_loss: float
    best_conditions: Dict
    failure_conditions: Dict

class AdvancedPatternAnalyzer:
    def __init__(self):
        self.pattern_history = {}
        self.context_weights = {}
        
    def analyze_pattern_performance(self, df: pd.DataFrame, pattern_name: str, pattern_indices: List[int]) -> PatternPerformance:
        """Analyze historical performance of a pattern"""
        successes = 0
        failures = 0
        gains = []
        losses = []
        
        success_contexts = {'volume': [], 'rsi': [], 'trend': [], 'volatility': []}
        failure_contexts = {'volume': [], 'rsi': [], 'trend': [], 'volatility': []}
        
        # Work strictly with positional indices to avoid ambiguity
        df_reset = df.reset_index(drop=True)
        
        for pos in pattern_indices:
            if isinstance(pos, str):
                continue
            if pos >= len(df_reset) - 5 or pos < 0:
                continue
                
            current = df_reset.iloc[pos]
            next_5d = df_reset.iloc[pos+1:pos+6]
            
            # Check if pattern worked (price moved in predicted direction)
            if pattern_name in ['hammer', 'bullish_engulfing', 'morning_star', 'three_white_soldiers', 'engulfing_bull']:
                # Bullish patterns
                max_gain = (next_5d['High'].max() - current['Close']) / current['Close']
                if max_gain > 0.02:  # 2% gain
                    successes += 1
                    gains.append(max_gain)
                    self._record_context(df, pos, success_contexts)
                else:
                    failures += 1
                    losses.append((current['Close'] - next_5d['Low'].min()) / current['Close'])
                    self._record_context(df, pos, failure_contexts)
                    
            elif pattern_name in ['shooting_star', 'bearish_engulfing', 'evening_star', 'three_black_crows', 'hanging_man', 'engulfing_bear']:
                # Bearish patterns
                max_loss = (current['Close'] - next_5d['Low'].min()) / current['Close']
                if max_loss > 0.02:  # 2% drop
                    successes += 1
                    gains.append(max_loss)
                    self._record_context(df, pos, success_contexts)
                else:
                    failures += 1
                    losses.append((next_5d['High'].max() - current['Close']) / current['Close'])
                    self._record_context(df, pos, failure_contexts)
        
        total = successes + failures
        success_rate = successes / total if total > 0 else 0
        
        return PatternPerformance(
            pattern_name=pattern_name,
            total_occurrences=total,
            success_rate=success_rate,
            avg_gain=np.mean(gains) if gains else 0,
            avg_loss=np.mean(losses) if losses else 0,
            best_conditions=self._analyze_conditions(success_contexts),
            failure_conditions=self._analyze_conditions(failure_contexts)
        )
    
    def _record_context(self, df: pd.DataFrame, idx: int, context_dict: Dict):
        """Record market context when pattern occurred"""
        current = df.iloc[idx]
        
        # Volume context
        vol_ratio = current['Volume'] / df['Volume'].rolling(20).mean().iloc[idx]
        context_dict['volume'].append(vol_ratio)
        
        # RSI context
        rsi = self._calculate_rsi(df['Close'][:idx+1], 14).iloc[-1]
        context_dict['rsi'].append(rsi)
        
        # Trend context (price vs SMA)
        sma_50 = df['Close'].rolling(50).mean().iloc[idx]
        trend = 1 if current['Close'] > sma_50 else -1
        context_dict['trend'].append(trend)
        
        # Volatility context
        volatility = df['Close'].rolling(20).std().iloc[idx] / df['Close'].iloc[idx]
        context_dict['volatility'].append(volatility)
    
    def _analyze_conditions(self, context_dict: Dict) -> Dict:
        """Analyze what conditions led to success/failure"""
        if not context_dict['volume']:
            return {}
            
        return {
            'avg_volume_ratio': np.mean(context_dict['volume']),
            'avg_rsi': np.mean(context_dict['rsi']),
            'uptrend_ratio': sum(1 for t in context_dict['trend'] if t > 0) / len(context_dict['trend']),
            'avg_volatility': np.mean(context_dict['volatility'])
        }
    
    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        delta = prices.diff()
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
    
    def should_trust_pattern(self, df: pd.DataFrame, pattern_name: str, current_idx: int, performance: PatternPerformance) -> Tuple[bool, float, List[str]]:
        """Decide if current pattern should be trusted based on historical performance"""
        if performance.success_rate < 0.5:
            return False, 0.0, ["Pattern has low historical success rate"]
        
        current = df.iloc[current_idx]
        reasons = []
        trust_score = performance.success_rate
        
        # Check current context vs successful context
        vol_ratio = current['Volume'] / df['Volume'].rolling(20).mean().iloc[current_idx]
        rsi = self._calculate_rsi(df['Close'][:current_idx+1], 14).iloc[-1]
        sma_50 = df['Close'].rolling(50).mean().iloc[current_idx]
        in_uptrend = current['Close'] > sma_50
        
        best = performance.best_conditions
        worst = performance.failure_conditions
        
        if not best or not worst:
            return True, trust_score, ["Insufficient historical data"]
        
        # Volume check
        if abs(vol_ratio - best['avg_volume_ratio']) < abs(vol_ratio - worst['avg_volume_ratio']):
            trust_score += 0.1
            reasons.append(f"Volume matches successful patterns ({vol_ratio:.1f}x)")
        else:
            trust_score -= 0.15
            reasons.append(f"Volume matches failed patterns ({vol_ratio:.1f}x)")
        
        # RSI check
        if abs(rsi - best['avg_rsi']) < abs(rsi - worst['avg_rsi']):
            trust_score += 0.1
            reasons.append(f"RSI favorable ({rsi:.0f})")
        else:
            trust_score -= 0.1
            reasons.append(f"RSI unfavorable ({rsi:.0f})")
        
        # Trend check
        if pattern_name in ['hammer', 'bullish_engulfing', 'morning_star']:
            if not in_uptrend and best['uptrend_ratio'] < 0.5:
                trust_score += 0.15
                reasons.append("Bullish reversal in downtrend (ideal)")
            elif in_uptrend:
                trust_score -= 0.1
                reasons.append("Bullish pattern in uptrend (risky)")
        
        elif pattern_name in ['shooting_star', 'bearish_engulfing', 'evening_star']:
            if in_uptrend and best['uptrend_ratio'] > 0.5:
                trust_score += 0.15
                reasons.append("Bearish reversal in uptrend (ideal)")
            elif not in_uptrend:
                trust_score -= 0.1
                reasons.append("Bearish pattern in downtrend (risky)")
        
        # Final decision
        should_trust = trust_score > 0.6
        
        if not should_trust:
            reasons.append(f"⚠️ Pattern failed {int((1-performance.success_rate)*100)}% historically")
        else:
            reasons.append(f"✓ Pattern succeeded {int(performance.success_rate*100)}% historically")
        
        return should_trust, min(trust_score, 1.0), reasons

    def get_market_regime(self, df: pd.DataFrame) -> Dict:
        """Identify current market regime"""
        latest = df.iloc[-1]
        
        # Trend
        sma_20 = df['Close'].rolling(20).mean().iloc[-1]
        sma_50 = df['Close'].rolling(50).mean().iloc[-1]
        sma_200 = df['Close'].rolling(200).mean().iloc[-1]
        atr = self._calculate_atr(df, 14).iloc[-1]
        atr_pct = atr / max(latest['Close'], 0.0001)
        vol_20 = df['Close'].pct_change().rolling(20).std().iloc[-1]
        vol_60 = df['Close'].pct_change().rolling(60).std().iloc[-1]
        volatility_ratio = vol_20 / (vol_60 + 0.0001)
        trend_spread = abs(sma_20 - sma_50) / max(latest['Close'], 0.0001)
        trend_strength = float(np.clip((trend_spread / max(atr_pct, 0.002)) / 4, 0, 1))
        efficiency = abs(df['Close'].iloc[-1] - df['Close'].iloc[-10]) / (
            df['Close'].diff().abs().tail(10).sum() + 0.0001
        ) if len(df) >= 10 else 0.5
        efficiency = float(np.clip(efficiency, 0, 1))
        
        if latest['Close'] > sma_20 > sma_50 > sma_200:
            trend = "strong_uptrend"
        elif latest['Close'] > sma_50:
            trend = "uptrend"
        elif latest['Close'] < sma_20 < sma_50 < sma_200:
            trend = "strong_downtrend"
        elif latest['Close'] < sma_50:
            trend = "downtrend"
        else:
            trend = "sideways"
        
        # Volatility
        volatility = df['Close'].rolling(20).std().iloc[-1] / df['Close'].iloc[-1]
        if volatility > 0.03 or atr_pct > 0.025 or volatility_ratio > 1.35:
            vol_regime = "high_volatility"
        elif volatility < 0.015 and volatility_ratio < 0.9:
            vol_regime = "low_volatility"
        else:
            vol_regime = "normal_volatility"
        
        # Volume
        vol_ratio = latest['Volume'] / df['Volume'].rolling(50).mean().iloc[-1]
        if vol_ratio > 1.5:
            vol_state = "high_volume"
        elif vol_ratio < 0.7:
            vol_state = "low_volume"
        else:
            vol_state = "normal_volume"

        prev = df.iloc[-2] if len(df) > 1 else latest
        higher_high = latest['High'] > prev['High']
        higher_low = latest['Low'] > prev['Low']
        lower_high = latest['High'] < prev['High']
        lower_low = latest['Low'] < prev['Low']

        if higher_high and higher_low:
            structure = "bullish"
        elif lower_high and lower_low:
            structure = "bearish"
        else:
            structure = "mixed"

        if vol_regime == "high_volatility" and trend in {"downtrend", "strong_downtrend"}:
            regime = "panic"
        elif trend in {"uptrend", "strong_uptrend"} and trend_strength >= 0.45 and efficiency >= 0.35:
            regime = "trending_up"
        elif trend in {"downtrend", "strong_downtrend"} and trend_strength >= 0.45 and efficiency >= 0.35:
            regime = "trending_down"
        else:
            regime = "sideways"
        
        return {
            'trend': trend,
            'volatility': vol_regime,
            'volume': vol_state,
            'rsi': self._calculate_rsi(df['Close'], 14).iloc[-1],
            'regime': regime,
            'structure': structure,
            'atr_pct': float(round(atr_pct, 4)),
            'trend_strength': float(round(max(trend_strength, efficiency * 0.8), 4)),
            'volatility_ratio': float(round(volatility_ratio, 4)),
            'volume_ratio': float(round(vol_ratio, 4)),
        }
