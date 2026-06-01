import asyncio
import logging
import time
from typing import Dict, List, Callable
import pandas as pd

from app.services.data_provider import MarketDataService
from app.core.settings import get_settings

logger = logging.getLogger(__name__)

class InstitutionalDetector:
    def __init__(self, data_service: MarketDataService):
        self.data_service = data_service
        self.context_cache: Dict[str, Dict] = {}

    def get_context(self, symbol: str) -> Dict:
        now = time.time()
        cached = self.context_cache.get(symbol)
        if cached and (now - cached['timestamp'] < 3600):  # 1 hour cache
            return cached

        try:
            # Fetch 20d daily history to get avg volume, support, resistance
            df = self.data_service.fetch_history(symbol, period="1mo", interval="1d")
            if df.empty or len(df) < 5:
                return {}

            avg_vol = df['Volume'].tail(20).mean()
            resistance = df['High'].tail(20).max()
            support = df['Low'].tail(20).min()

            ctx = {
                'avg_volume_20d': avg_vol,
                'resistance': resistance,
                'support': support,
                'timestamp': now
            }
            self.context_cache[symbol] = ctx
            return ctx
        except Exception as e:
            logger.warning(f"Failed to fetch context for {symbol}: {e}")
            return {}

    def detect(self, symbol: str, recent_candles: List[Dict]) -> List[Dict]:
        if len(recent_candles) < 3:
            return []

        ctx = self.get_context(symbol)
        if not ctx:
            return []

        avg_vol = ctx['avg_volume_20d']
        resistance = ctx['resistance']
        support = ctx['support']

        current = recent_candles[-1]
        prev1 = recent_candles[-2]
        prev2 = recent_candles[-3]

        patterns = []

        # Convert simple dict to comparable format
        def is_bullish(c): return c['close'] > c['open']
        def is_bearish(c): return c['close'] < c['open']

        # 1. Volume Anomaly
        if current.get('volume', 0) > (avg_vol * 3):
            patterns.append({"type": "VOLUME_ANOMALY", "time": current['time'], "message": "Massive Volume Surge"})

        # 2. Bull Trap
        if prev1['close'] > resistance and current['close'] < resistance:
            patterns.append({"type": "BULL_TRAP", "time": current['time'], "message": "Bull Trap Detected"})

        # 3. Bear Trap
        if prev1['close'] < support and current['close'] > support:
            patterns.append({"type": "BEAR_TRAP", "time": current['time'], "message": "Bear Trap Detected"})

        # 4. Institutional Trap
        spike_up = is_bullish(prev2) and prev2.get('volume', 0) > (avg_vol * 2)
        sharp_reversal = current['close'] < prev2['low']
        if spike_up and sharp_reversal:
            patterns.append({"type": "INSTITUTIONAL_TRAP", "time": current['time'], "message": "Inst. Reversal Trap"})

        # 5. Accumulation (simplification without EOD delivery info, focusing on tight spread + volume)
        body = abs(current['close'] - current['open'])
        tight_action = body / current['open'] < 0.005 if current['open'] > 0 else False
        if tight_action and current.get('volume', 0) > avg_vol:
            patterns.append({"type": "ACCUMULATION", "time": current['time'], "message": "Heavy Accumulation"})

        return patterns


class OHLCAggregator:
    def __init__(self, interval_seconds=900):  # 15m default
        self.interval = interval_seconds
        self.current_candles: Dict[str, Dict] = {}
        self.candle_history: Dict[str, List[Dict]] = {}
        self.subscribers: Dict[str, List[asyncio.Queue]] = {}
        self.detector = InstitutionalDetector(MarketDataService(get_settings()))
        self.last_volume: Dict[str, int] = {}

    def subscribe(self, symbol: str) -> asyncio.Queue:
        if symbol not in self.subscribers:
            self.subscribers[symbol] = []
        queue = asyncio.Queue()
        self.subscribers[symbol].append(queue)
        
        # Optionally, seed with current forming candle if exists
        if symbol in self.current_candles:
            queue.put_nowait({
                "type": "CANDLE_UPDATE",
                "symbol": symbol,
                "data": self.current_candles[symbol]
            })
            
        return queue

    def unsubscribe(self, symbol: str, queue: asyncio.Queue):
        if symbol in self.subscribers and queue in self.subscribers[symbol]:
            self.subscribers[symbol].remove(queue)

    def process_tick(self, symbol: str, tick: dict):
        now = time.time()
        candle_start = now - (now % self.interval)
        
        if symbol not in self.candle_history:
            self.candle_history[symbol] = []

        # AngelOne sends cumulative daily volume. 
        # Calculate incremental tick volume.
        cumulative_vol = tick.get("volume", 0)
        last_cum_vol = self.last_volume.get(symbol, cumulative_vol)
        tick_vol = max(0, cumulative_vol - last_cum_vol)
        self.last_volume[symbol] = cumulative_vol

        is_new_candle = False
        if symbol not in self.current_candles or self.current_candles[symbol]["time"] != candle_start:
            # Finalize previous candle if exists
            if symbol in self.current_candles:
                completed_candle = self.current_candles[symbol].copy()
                self.candle_history[symbol].append(completed_candle)
                
                # Run institutional detection on completed candles
                patterns = self.detector.detect(symbol, self.candle_history[symbol][-5:])
                if patterns:
                    self._broadcast(symbol, {
                        "type": "PATTERN_DETECTED",
                        "patterns": patterns
                    })
                    
            # Start a new candle period
            self.current_candles[symbol] = {
                "time": candle_start,
                "open": tick["ltp"],
                "high": tick["ltp"],
                "low": tick["ltp"],
                "close": tick["ltp"],
                "volume": tick_vol,
                "is_new_candle": True
            }
            is_new_candle = True
        else:
            # Update existing candle
            c = self.current_candles[symbol]
            c["high"] = max(c["high"], tick["ltp"])
            c["low"] = min(c["low"], tick["ltp"])
            c["close"] = tick["ltp"]
            c["volume"] += tick_vol
            c["is_new_candle"] = False
            
        # Broadcast the candle update
        self._broadcast(symbol, {
            "type": "CANDLE_UPDATE",
            "symbol": symbol,
            "data": self.current_candles[symbol]
        })

    def _broadcast(self, symbol: str, payload: dict):
        if symbol in self.subscribers:
            for queue in list(self.subscribers[symbol]):
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

# Singleton
_aggregator = None

def get_ohlc_aggregator() -> OHLCAggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = OHLCAggregator(interval_seconds=900)  # 15 minutes
    return _aggregator
