import asyncio
import logging
import time
from statistics import median
from typing import Dict, List

logger = logging.getLogger(__name__)

class VisibleFootprintDetector:
    def detect(self, symbol: str, recent_candles: List[Dict]) -> List[Dict]:
        if len(recent_candles) < 6:
            return []

        # Use only completed 15-minute candles already observed by the live
        # feed. Comparing a 15-minute bar with average *daily* volume was both
        # dimensionally wrong and forced network I/O into the tick callback.
        prior_structure = recent_candles[:-2][-20:]
        prior_volume = [float(candle.get('volume') or 0) for candle in recent_candles[:-1][-20:]]
        positive_volume = [value for value in prior_volume if value > 0]
        if len(prior_structure) < 3 or len(positive_volume) < 5:
            return []

        baseline_vol = median(positive_volume)
        resistance = max(float(candle['high']) for candle in prior_structure)
        support = min(float(candle['low']) for candle in prior_structure)

        current = recent_candles[-1]
        prev1 = recent_candles[-2]
        prev2 = recent_candles[-3]

        patterns = []

        # Convert simple dict to comparable format
        def is_bullish(c): return c['close'] > c['open']
        def is_bearish(c): return c['close'] < c['open']

        # 1. Volume Anomaly
        if current.get('volume', 0) > (baseline_vol * 3):
            patterns.append({"type": "VOLUME_ANOMALY", "time": current['time'], "message": "Massive Volume Surge"})

        # 2. Bull Trap
        if prev1['close'] > resistance and current['close'] < resistance:
            patterns.append({"type": "BULL_TRAP", "time": current['time'], "message": "Bull Trap Detected"})

        # 3. Bear Trap
        if prev1['close'] < support and current['close'] > support:
            patterns.append({"type": "BEAR_TRAP", "time": current['time'], "message": "Bear Trap Detected"})

        # 4. High-volume reversal trap. Candle data cannot identify who traded.
        spike_up = is_bullish(prev2) and prev2.get('volume', 0) > (baseline_vol * 2)
        sharp_reversal = current['close'] < prev2['low']
        if spike_up and sharp_reversal:
            patterns.append({
                "type": "VISIBLE_REVERSAL_TRAP",
                "time": current['time'],
                "message": "High-volume reversal trap",
            })

        # 5. Accumulation (simplification without EOD delivery info, focusing on tight spread + volume)
        body = abs(current['close'] - current['open'])
        tight_action = body / current['open'] < 0.005 if current['open'] > 0 else False
        if tight_action and current.get('volume', 0) > baseline_vol:
            patterns.append({
                "type": "ABSORPTION_PROXY",
                "time": current['time'],
                "message": "Tight-range high-volume absorption proxy",
            })

        return patterns


class OHLCAggregator:
    def __init__(self, interval_seconds=900):  # 15m default
        self.interval = interval_seconds
        self.current_candles: Dict[str, Dict] = {}
        self.candle_history: Dict[str, List[Dict]] = {}
        self.subscribers: Dict[str, List[asyncio.Queue]] = {}
        self.detector = VisibleFootprintDetector()
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
        candle_start = int(now - (now % self.interval))
        
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
                
                # Run visible price/volume pattern detection on completed candles.
                patterns = self.detector.detect(symbol, self.candle_history[symbol][-40:])
                if patterns:
                    self._broadcast(symbol, {
                        "type": "PATTERN_DETECTED",
                        "patterns": patterns
                    })
                    
            # Start a new candle period
            self.current_candles[symbol] = {
                "time": int(candle_start),
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
