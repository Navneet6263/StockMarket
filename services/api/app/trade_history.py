from typing import Dict, List
from datetime import datetime
import json
import os

class TradeHistory:
    def __init__(self, history_file: str = "trade_history.json"):
        self.history_file = history_file
        self.trades = self._load_history()
    
    def _load_history(self) -> List[Dict]:
        """Load trade history from file"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []
    
    def _save_history(self):
        """Save trade history to file"""
        try:
            with open(self.history_file, 'w') as f:
                json.dump(self.trades, f, indent=2)
        except Exception as e:
            print(f"Error saving history: {e}")
    
    def add_trade(self, trade_data: Dict):
        """Add completed trade to history"""
        trade_record = {
            'trade_id': trade_data.get('trade_id'),
            'instrument': trade_data.get('instrument'),
            'option_type': trade_data.get('option_type'),
            'strike': trade_data.get('strike'),
            'entry_price': trade_data.get('entry_price'),
            'exit_price': trade_data.get('exit_price'),
            'lot_size': trade_data.get('lot_size'),
            'pnl': trade_data.get('pnl'),
            'pnl_pct': trade_data.get('pnl_pct'),
            'entry_time': trade_data.get('entry_time'),
            'exit_time': datetime.now().isoformat(),
            'duration_mins': trade_data.get('duration_mins', 0),
            'ai_advice_followed': trade_data.get('ai_advice_followed', 'unknown'),
            'peak_profit': trade_data.get('peak_profit', 0),
            'peak_profit_pct': trade_data.get('peak_profit_pct', 0)
        }
        
        self.trades.append(trade_record)
        self._save_history()
        
        return trade_record
    
    def get_history(self, limit: int = 50) -> List[Dict]:
        """Get recent trade history"""
        return self.trades[-limit:]
    
    def get_stats(self) -> Dict:
        """Calculate trading statistics"""
        if not self.trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0,
                'avg_profit': 0,
                'avg_loss': 0,
                'total_pnl': 0,
                'best_trade': None,
                'worst_trade': None
            }
        
        winning = [t for t in self.trades if t.get('pnl', 0) > 0]
        losing = [t for t in self.trades if t.get('pnl', 0) < 0]
        
        total_pnl = sum(t.get('pnl', 0) for t in self.trades)
        avg_profit = sum(t.get('pnl', 0) for t in winning) / len(winning) if winning else 0
        avg_loss = sum(t.get('pnl', 0) for t in losing) / len(losing) if losing else 0
        
        best_trade = max(self.trades, key=lambda x: x.get('pnl', 0))
        worst_trade = min(self.trades, key=lambda x: x.get('pnl', 0))
        
        return {
            'total_trades': len(self.trades),
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'win_rate': (len(winning) / len(self.trades) * 100) if self.trades else 0,
            'avg_profit': avg_profit,
            'avg_loss': avg_loss,
            'total_pnl': total_pnl,
            'best_trade': best_trade,
            'worst_trade': worst_trade,
            'ai_accuracy': self._calculate_ai_accuracy()
        }
    
    def _calculate_ai_accuracy(self) -> Dict:
        """Calculate how accurate AI advice was"""
        followed_correct = 0
        followed_wrong = 0
        ignored_correct = 0
        ignored_wrong = 0
        
        for trade in self.trades:
            advice = trade.get('ai_advice_followed', 'unknown')
            pnl = trade.get('pnl', 0)
            
            if advice == 'EXIT' and pnl > 0:
                followed_correct += 1
            elif advice == 'EXIT' and pnl < 0:
                followed_wrong += 1
            elif advice == 'HOLD' and pnl > 0:
                followed_correct += 1
            elif advice == 'HOLD' and pnl < 0:
                followed_wrong += 1
        
        total_followed = followed_correct + followed_wrong
        accuracy = (followed_correct / total_followed * 100) if total_followed > 0 else 0
        
        return {
            'ai_followed_correct': followed_correct,
            'ai_followed_wrong': followed_wrong,
            'ai_accuracy_pct': accuracy,
            'message': f"AI ne {followed_correct}/{total_followed} baar sahi bola ({accuracy:.1f}%)"
        }
