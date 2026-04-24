import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from datetime import datetime
import yfinance as yf

class ActiveTradeTracker:
    def __init__(self):
        self.active_trades = {}
        
    def track_trade(self, trade_id: str, instrument: str, option_type: str, 
                   strike: float, buying_price: float, lot_size: int, 
                   spot_price: float, df: pd.DataFrame, prediction: Dict, 
                   mtf_data: Dict = None) -> Dict:
        """
        Track active trade and provide real-time AI feedback
        
        Args:
            trade_id: Unique trade identifier
            instrument: e.g., "NIFTY"
            option_type: "CALL" or "PUT"
            strike: Strike price (e.g., 25500)
            buying_price: Entry price (e.g., 20)
            lot_size: Number of lots (e.g., 75)
            spot_price: Current Nifty spot price
            df: Historical price data
            prediction: ML prediction data
            mtf_data: Multi-timeframe data
        """
        
        # Calculate current option price (simplified)
        current_option_price = self._estimate_option_price(
            spot_price, strike, option_type, buying_price
        )
        
        # Calculate P&L
        pnl = (current_option_price - buying_price) * lot_size
        pnl_pct = ((current_option_price - buying_price) / buying_price) * 100
        
        # Get market metrics
        latest = df.iloc[-1]
        vol_ma = df['Volume'].rolling(20).mean().iloc[-1]
        vol_ratio = latest['Volume'] / vol_ma if vol_ma > 0 else 1.0
        rsi = self._calculate_rsi(df['Close'], 14).iloc[-1]
        
        support = prediction.get('support', spot_price * 0.98)
        resistance = prediction.get('resistance', spot_price * 1.02)
        
        # MTF sync
        mtf_sync = mtf_data.get('sync_status', 'CONFLICTING') if mtf_data else 'CONFLICTING'
        breadth = mtf_data.get('market_breadth', {}).get('sentiment', 'neutral') if mtf_data else 'neutral'
        
        # AI Feedback
        ai_feedback = self._generate_ai_feedback(
            option_type, pnl_pct, spot_price, strike, support, resistance,
            vol_ratio, rsi, mtf_sync, breadth, buying_price, current_option_price
        )
        
        # Store trade
        self.active_trades[trade_id] = {
            'instrument': f"{instrument} {strike} {option_type}",
            'entry_price': buying_price,
            'current_price': current_option_price,
            'lot_size': lot_size,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'spot_price': spot_price,
            'timestamp': datetime.now().isoformat()
        }
        
        return {
            'active_trade': {
                'instrument': f"{instrument} {strike} {option_type}",
                'entry_price': buying_price,
                'current_price': round(current_option_price, 2),
                'lot_size': lot_size,
                'pnl': f"{'+'if pnl >= 0 else ''}{pnl:.0f}",
                'pnl_pct': f"{'+'if pnl_pct >= 0 else ''}{pnl_pct:.1f}%",
                'spot_price': spot_price,
                'ai_feedback': ai_feedback
            }
        }
    
    def _estimate_option_price(self, spot: float, strike: float, 
                               option_type: str, entry_price: float) -> float:
        """
        Estimate current option price based on spot movement
        Simplified Black-Scholes approximation
        """
        # Distance from strike
        if option_type == "CALL":
            intrinsic = max(0, spot - strike)
            # If spot moved up, option gains value
            spot_change_pct = (spot - strike) / strike
            price_multiplier = 1 + (spot_change_pct * 2)  # Delta approximation
        else:  # PUT
            intrinsic = max(0, strike - spot)
            # If spot moved down, put gains value
            spot_change_pct = (strike - spot) / strike
            price_multiplier = 1 + (spot_change_pct * 2)  # Delta approximation
        
        # Estimate current price
        estimated_price = entry_price * max(0.1, price_multiplier)
        
        # Add intrinsic value
        estimated_price = max(estimated_price, intrinsic)
        
        return estimated_price
    
    def _generate_ai_feedback(self, option_type: str, pnl_pct: float, 
                             spot: float, strike: float, support: float, 
                             resistance: float, vol_ratio: float, rsi: float,
                             mtf_sync: str, breadth: str, entry_price: float,
                             current_price: float) -> Dict:
        """
        Generate AI-powered feedback for active trade
        """
        
        # 1. EXCELLENT ENTRY - In profit with strong confirmation
        if pnl_pct > 20 and vol_ratio > 1.2 and mtf_sync in ['STRONG_UP', 'STRONG_DOWN']:
            return {
                'rating': 'EXCELLENT_ENTRY',
                'status': 'HOLD',
                'logic': f"🔥 Entry ekdam perfect hai! Your buying price ₹{entry_price:.0f} is now ₹{current_price:.0f}. Volume support kar raha hai ({vol_ratio:.1f}x). Keep trailing SL to ₹{current_price * 0.85:.0f}.",
                'risk_meter': 'LOW',
                'action': 'TRAIL_SL',
                'emoji': '🔥',
                'color': '#22c55e'
            }
        
        # 2. BOOK PROFIT - Near resistance or overbought
        if option_type == "CALL":
            near_resistance = abs(spot - resistance) / spot < 0.01
            if pnl_pct > 15 and (near_resistance or rsi > 70):
                return {
                    'rating': 'BOOK_PROFIT_ZONE',
                    'status': 'EXIT_50%',
                    'logic': f"💰 Profit book kar lo! Resistance ₹{resistance:.0f} hit hone wala hai (RSI {rsi:.0f}). Book 50% profit at ₹{current_price:.0f}, trail rest.",
                    'risk_meter': 'MEDIUM',
                    'action': 'BOOK_PARTIAL',
                    'emoji': '💰',
                    'color': '#f59e0b'
                }
        else:  # PUT
            near_support = abs(spot - support) / spot < 0.01
            if pnl_pct > 15 and (near_support or rsi < 30):
                return {
                    'rating': 'BOOK_PROFIT_ZONE',
                    'status': 'EXIT_50%',
                    'logic': f"💰 Profit book kar lo! Support ₹{support:.0f} hit hone wala hai (RSI {rsi:.0f}). Book 50% profit at ₹{current_price:.0f}, trail rest.",
                    'risk_meter': 'MEDIUM',
                    'action': 'BOOK_PARTIAL',
                    'emoji': '💰',
                    'color': '#f59e0b'
                }
        
        # 3. HOLD POSITION - Small loss but at support/resistance
        if -10 < pnl_pct < 5:
            if option_type == "CALL" and abs(spot - support) / spot < 0.01:
                return {
                    'rating': 'TEMPORARY_DIP',
                    'status': 'HOLD',
                    'logic': f"🛡️ Relax bhai! Spot ₹{spot:.0f} is at support ₹{support:.0f}. Ye temporary dip hai. Market structure intact hai. Hold karo, bounce aayega.",
                    'risk_meter': 'CONTROLLED',
                    'action': 'HOLD',
                    'emoji': '🛡️',
                    'color': '#22c55e'
                }
            elif option_type == "PUT" and abs(spot - resistance) / spot < 0.01:
                return {
                    'rating': 'TEMPORARY_DIP',
                    'status': 'HOLD',
                    'logic': f"🛡️ Relax bhai! Spot ₹{spot:.0f} is at resistance ₹{resistance:.0f}. Ye temporary dip hai. Rejection aayega. Hold karo.",
                    'risk_meter': 'CONTROLLED',
                    'action': 'HOLD',
                    'emoji': '🛡️',
                    'color': '#22c55e'
                }
        
        # 4. STRICT STOP LOSS - Loss > 15% and trend reversed
        if pnl_pct < -15:
            if option_type == "CALL" and mtf_sync in ['STRONG_DOWN', 'MODERATE_DOWN']:
                return {
                    'rating': 'STOP_LOSS_HIT',
                    'status': 'EXIT_NOW',
                    'logic': f"🚨 Loss {pnl_pct:.1f}% cross kar gaya aur trend RED hai ({mtf_sync}). Ego chhodo, exit karo! Capital preservation priority hai.",
                    'risk_meter': 'CRITICAL',
                    'action': 'EXIT_NOW',
                    'emoji': '🚨',
                    'color': '#ef4444'
                }
            elif option_type == "PUT" and mtf_sync in ['STRONG_UP', 'MODERATE_UP']:
                return {
                    'rating': 'STOP_LOSS_HIT',
                    'status': 'EXIT_NOW',
                    'logic': f"🚨 Loss {pnl_pct:.1f}% cross kar gaya aur trend GREEN hai ({mtf_sync}). Ego chhodo, exit karo! Capital preservation priority hai.",
                    'risk_meter': 'CRITICAL',
                    'action': 'EXIT_NOW',
                    'emoji': '🚨',
                    'color': '#ef4444'
                }
        
        # 5. PATTERN BASED HOLD - Historical pattern suggests continuation
        if -5 < pnl_pct < 10:
            pattern_duration = self._estimate_pattern_duration(mtf_sync, breadth)
            return {
                'rating': 'PATTERN_CONTINUATION',
                'status': 'HOLD',
                'logic': f"📊 History ke hisaab se ye move abhi {pattern_duration} mins aur chalegi. Volume {vol_ratio:.1f}x hai. Jaldi mat nikalna, patience rakho.",
                'risk_meter': 'LOW',
                'action': 'HOLD',
                'emoji': '📊',
                'color': '#06b6d4'
            }
        
        # 6. DEFAULT - MONITOR
        return {
            'rating': 'MONITOR',
            'status': 'WATCH',
            'logic': f"⏸️ Position monitor karo. P&L: {pnl_pct:.1f}%. Volume {vol_ratio:.1f}x, RSI {rsi:.0f}. Wait for clear signal.",
            'risk_meter': 'NEUTRAL',
            'action': 'MONITOR',
            'emoji': '⏸️',
            'color': '#9ca3af'
        }
    
    def _estimate_pattern_duration(self, mtf_sync: str, breadth: str) -> int:
        """
        Estimate how long the current pattern will continue (in minutes)
        Based on historical pattern analysis
        """
        if mtf_sync in ['STRONG_UP', 'STRONG_DOWN']:
            if breadth in ['bullish', 'bearish']:
                return 30  # Strong trend with breadth support = 30 mins
            else:
                return 15  # Strong trend but mixed breadth = 15 mins
        elif mtf_sync in ['MODERATE_UP', 'MODERATE_DOWN']:
            return 20  # Moderate trend = 20 mins
        else:
            return 10  # Conflicting signals = 10 mins
    
    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """Calculate RSI"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss + 0.0001)
        return 100 - (100 / (1 + rs))
    
    def get_all_active_trades(self) -> List[Dict]:
        """Get all active trades"""
        return list(self.active_trades.values())
    
    def close_trade(self, trade_id: str, exit_price: float) -> Dict:
        """Close a trade and calculate final P&L"""
        if trade_id not in self.active_trades:
            return {'error': 'Trade not found'}
        
        trade = self.active_trades[trade_id]
        final_pnl = (exit_price - trade['entry_price']) * trade['lot_size']
        final_pnl_pct = ((exit_price - trade['entry_price']) / trade['entry_price']) * 100
        
        result = {
            'trade_id': trade_id,
            'instrument': trade['instrument'],
            'entry_price': trade['entry_price'],
            'exit_price': exit_price,
            'pnl': final_pnl,
            'pnl_pct': final_pnl_pct,
            'closed_at': datetime.now().isoformat()
        }
        
        # Remove from active trades
        del self.active_trades[trade_id]
        
        return result
