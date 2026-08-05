import pandas as pd
import numpy as np
from typing import Dict, Tuple
from datetime import datetime

class TradingPsychology:
    def __init__(self):
        self.alert_types = {
            'FALSE_BREAKOUT': '⚠️ FAKE MOVE ALERT',
            'DONT_EXIT': '🛡️ STOP STILL INTACT',
            'HARD_EXIT': '🚨 STRUCTURAL INVALIDATION',
            'SAFE_ENTRY': '✅ CONFIRMATION CANDIDATE',
            'WAIT': '⏸️ WAIT & WATCH'
        }
    
    def analyze_psychology(self, df: pd.DataFrame, prediction: Dict, mtf_data: Dict = None) -> Dict:
        """
        Rule-based behaviour guard. Scores are evidence alignment, not probabilities.
        """
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        current_price = latest['Close']
        
        # Get key metrics
        vol_ma = df['Volume'].rolling(20).mean().iloc[-1]
        vol_ratio = latest['Volume'] / vol_ma if vol_ma > 0 else 1.0
        
        support = prediction.get('support', current_price * 0.98)
        resistance = prediction.get('resistance', current_price * 1.02)
        
        rsi = self._calculate_rsi(df['Close'], 14).iloc[-1]
        
        # Distance from support/resistance
        dist_to_support = ((current_price - support) / current_price) * 100
        dist_to_resistance = ((resistance - current_price) / current_price) * 100
        
        # Market breadth from MTF
        breadth_sentiment = 'neutral'
        if mtf_data and 'market_breadth' in mtf_data:
            breadth_sentiment = mtf_data['market_breadth'].get('sentiment', 'neutral')
        
        # MTF sync status
        mtf_sync = 'CONFLICTING'
        if mtf_data:
            mtf_sync = mtf_data.get('sync_status', 'CONFLICTING')
        
        # 1. FALSE BREAKOUT DETECTION
        if self._is_false_breakout(current_price, prev['Close'], vol_ratio, breadth_sentiment, resistance, support):
            return self._false_breakout_alert(current_price, vol_ratio, breadth_sentiment)
        
        # 2. Structural invalidation always outranks a hold suggestion.
        if self._should_hard_exit(current_price, support, resistance, mtf_sync, vol_ratio):
            return self._hard_exit_alert(current_price, mtf_sync)

        # 3. HOLD/REVIEW LOGIC
        if self._should_hold_position(current_price, support, rsi, breadth_sentiment, mtf_sync):
            return self._dont_exit_alert(current_price, support, rsi, breadth_sentiment)
        
        # 4. SAFE ENTRY LOGIC
        if self._is_safe_entry(vol_ratio, breadth_sentiment, mtf_sync, rsi):
            return self._safe_entry_alert(current_price, vol_ratio, breadth_sentiment)
        
        # 5. DEFAULT - WAIT & WATCH
        return self._wait_alert()
    
    def _is_false_breakout(self, price: float, prev_price: float, vol_ratio: float, 
                          breadth: str, resistance: float, support: float) -> bool:
        """
        Detect false breakout:
        - Price moving up but volume dying
        - Price moving up but market breadth bearish
        """
        price_up = price > prev_price * 1.005  # 0.5% up
        price_down = price < prev_price * 0.995  # 0.5% down
        
        low_volume = vol_ratio < 0.8
        
        # Breakout above resistance with low volume
        if price > resistance and low_volume:
            return True
        
        # Breakdown below support with low volume
        if price < support and low_volume:
            return True
        
        # Price up but breadth bearish
        if price_up and breadth == 'bearish' and low_volume:
            return True
        
        # Price down but breadth bullish
        if price_down and breadth == 'bullish' and low_volume:
            return True
        
        return False
    
    def _should_hold_position(self, price: float, support: float, rsi: float, 
                             breadth: str, mtf_sync: str) -> bool:
        """
        Don't exit when:
        - At strong support
        - Oversold RSI
        - Market breadth still bullish
        """
        near_support = abs(price - support) / price < 0.01  # Within 1% of support
        oversold = rsi < 35
        bullish_breadth = breadth == 'bullish'
        
        # At support + oversold = HOLD
        if near_support and oversold:
            return True
        
        # At support + bullish breadth = HOLD
        if near_support and bullish_breadth:
            return True
        
        # MTF still bullish despite small dip
        if mtf_sync in ['STRONG_UP', 'MODERATE_UP'] and oversold:
            return True
        
        return False
    
    def _should_hard_exit(self, price: float, support: float, resistance: float,
                         mtf_sync: str, vol_ratio: float) -> bool:
        """
        Hard exit when:
        - Stop loss hit
        - All timeframes flipped
        - High volume breakdown
        """
        # Below support with high volume
        below_support = price < support * 0.995
        high_volume = vol_ratio > 1.3
        
        if below_support and high_volume:
            return True
        
        # All timeframes bearish
        if mtf_sync in ['STRONG_DOWN']:
            return True
        
        # Above resistance but failing (rejection)
        above_resistance = price > resistance * 1.005
        if above_resistance and vol_ratio < 0.7:
            return True
        
        return False
    
    def _is_safe_entry(self, vol_ratio: float, breadth: str, mtf_sync: str, rsi: float) -> bool:
        """
        Safe entry when:
        - Good volume
        - Bullish breadth
        - MTF aligned
        - RSI not overbought
        """
        good_volume = vol_ratio > 1.2
        bullish_breadth = breadth == 'bullish'
        mtf_aligned = mtf_sync in ['STRONG_UP', 'MODERATE_UP']
        not_overbought = rsi < 70
        
        return good_volume and bullish_breadth and mtf_aligned and not_overbought
    
    def _false_breakout_alert(self, price: float, vol_ratio: float, breadth: str) -> Dict:
        """Generate false breakout alert"""
        return {
            'action': 'WAIT',
            'alert_type': 'FALSE_BREAKOUT_DETECTED',
            'risk_status': 'HIGH_RISK',
            'advice': f"⚠️ Possible failed move at ₹{price:.2f}: volume is {vol_ratio:.1f}x and breadth is {breadth}. Block a fresh entry; manage an existing position only by its structural stop.",
            'confidence': 0.85,
            'confidence_is_probability': False,
            'emoji': '⚠️',
            'color': '#f97316'
        }
    
    def _dont_exit_alert(self, price: float, support: float, rsi: float, breadth: str) -> Dict:
        """Generate don't exit alert"""
        return {
            'action': 'HOLD_IF_STOP_INTACT',
            'alert_type': 'SUPPORT_REACTION_WATCH',
            'risk_status': 'CONTROLLED',
            'advice': f"🛡️ Price ₹{price:.2f} is near support ₹{support:.2f} with RSI {rsi:.0f}. A bounce is not guaranteed; hold only while the planned structural stop remains intact and wait for a reclaim candle.",
            'confidence': 0.75,
            'confidence_is_probability': False,
            'emoji': '🛡️',
            'color': '#22c55e'
        }
    
    def _hard_exit_alert(self, price: float, mtf_sync: str) -> Dict:
        """Generate hard exit alert"""
        return {
            'action': 'EXIT_OR_REDUCE',
            'alert_type': 'STRUCTURAL_INVALIDATION',
            'risk_status': 'CRITICAL',
            'advice': f"🚨 Price ₹{price:.2f} has met the configured structural-exit rule ({mtf_sync}). Do not average down; execute the position's predefined exit/reduction plan.",
            'confidence': 0.90,
            'confidence_is_probability': False,
            'emoji': '🚨',
            'color': '#ef4444'
        }
    
    def _safe_entry_alert(self, price: float, vol_ratio: float, breadth: str) -> Dict:
        """Generate safe entry alert"""
        return {
            'action': 'WAIT_FOR_LIVE_CONFIRMATION',
            'alert_type': 'ENTRY_CONFIRMATION_CANDIDATE',
            'risk_status': 'LOW_RISK',
            'advice': f"✅ Entry candidate near ₹{price:.2f}: volume is {vol_ratio:.1f}x and breadth is {breadth}. Enter only after the live trigger confirms and the structural stop/target pass the risk gate.",
            'confidence': 0.80,
            'confidence_is_probability': False,
            'emoji': '✅',
            'color': '#22c55e'
        }
    
    def _wait_alert(self) -> Dict:
        """Generate wait alert"""
        return {
            'action': 'WAIT',
            'alert_type': 'WAIT_FOR_CLARITY',
            'risk_status': 'NEUTRAL',
            'advice': "⏸️ WAIT & WATCH. Signals are mixed. No clear edge right now. Patience is key. Wait for volume confirmation or timeframe alignment.",
            'confidence': 0.50,
            'confidence_is_probability': False,
            'emoji': '⏸️',
            'color': '#9ca3af'
        }
    
    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """Calculate RSI"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss + 0.0001)
        return 100 - (100 / (1 + rs))
    
    def get_position_advice(self, entry_price: float, current_price: float, 
                           stop_loss: float, target: float, psychology: Dict) -> Dict:
        """
        Get advice for existing position
        """
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        
        # Check if stop loss hit
        if current_price <= stop_loss:
            return {
                'action': 'EXIT_NOW',
                'reason': f"Stop Loss hit! Exit at ₹{current_price:.2f}. Loss: {pnl_pct:.2f}%",
                'urgency': 'CRITICAL'
            }
        
        # Check if target hit
        if current_price >= target:
            return {
                'action': 'BOOK_PROFIT',
                'reason': f"Target achieved! Book profit at ₹{current_price:.2f}. Gain: {pnl_pct:.2f}%",
                'urgency': 'HIGH'
            }
        
        # In profit but psychology says exit
        if pnl_pct > 0 and psychology['action'] == 'EXIT_NOW':
            return {
                'action': 'EXIT_NOW',
                'reason': f"Exit with profit {pnl_pct:.2f}%. {psychology['advice']}",
                'urgency': 'HIGH'
            }
        
        # In loss but psychology says hold
        if pnl_pct < 0 and psychology['action'] == 'HOLD':
            return {
                'action': 'HOLD',
                'reason': f"Current loss {pnl_pct:.2f}%. {psychology['advice']}",
                'urgency': 'LOW'
            }
        
        # Default
        return {
            'action': psychology['action'],
            'reason': psychology['advice'],
            'urgency': 'MEDIUM'
        }
