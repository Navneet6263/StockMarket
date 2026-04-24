import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List
from dataclasses import dataclass

@dataclass
class LiveQuote:
    symbol: str
    price: float
    change: float
    change_percent: float
    volume: int
    timestamp: datetime

@dataclass
class PatternSignal:
    pattern: str
    strength: float
    direction: str
    confidence: float

class LiveDataService:
    def __init__(self):
        self.nse_symbols = {
            'RELIANCE': 'RELIANCE.NS', 'TCS': 'TCS.NS', 'INFY': 'INFY.NS', 'HDFCBANK': 'HDFCBANK.NS',
            'ICICIBANK': 'ICICIBANK.NS', 'ADANIPOWER': 'ADANIPOWER.NS', 'TATAMOTORS': 'TATAMOTORS.NS',
            'SBIN': 'SBIN.NS', 'BHARTIARTL': 'BHARTIARTL.NS', 'ITC': 'ITC.NS', 'WIPRO': 'WIPRO.NS',
            'LT': 'LT.NS', 'AXISBANK': 'AXISBANK.NS', 'MARUTI': 'MARUTI.NS', 'SUNPHARMA': 'SUNPHARMA.NS',
            'NIFTY': '^NSEI', 'BANKNIFTY': '^NSEBANK', 'NIFTY50': '^NSEI', 'SENSEX': '^BSESN'
        }
    
    def get_live_quote(self, symbol: str) -> LiveQuote:
        clean_symbol = symbol.upper().replace(' ', '')
        yf_symbol = self.nse_symbols.get(clean_symbol, f"{clean_symbol}.NS")
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info
        
        return LiveQuote(
            symbol=clean_symbol,
            price=info.get('currentPrice', info.get('regularMarketPrice', 0)),
            change=info.get('regularMarketChange', 0),
            change_percent=info.get('regularMarketChangePercent', 0),
            volume=info.get('volume', 0),
            timestamp=datetime.now()
        )
    
    def get_historical_data(self, symbol: str, period: str = "10y") -> pd.DataFrame:
        clean_symbol = symbol.upper().replace(' ', '')
        yf_symbol = self.nse_symbols.get(clean_symbol, f"{clean_symbol}.NS")
        ticker = yf.Ticker(yf_symbol)
        data = ticker.history(period=period)
        
        if data.empty:
            raise ValueError(f"No data found for {clean_symbol}")
        
        data['SMA_20'] = data['Close'].rolling(20).mean()
        data['Volume_MA'] = data['Volume'].rolling(20).mean()
        return data

class PatternRecognition:
    def detect_patterns(self, df: pd.DataFrame) -> List[PatternSignal]:
        patterns = []
        patterns.extend(self.detect_hammer(df))
        patterns.extend(self.detect_shooting_star(df))
        patterns.extend(self.detect_doji(df))
        patterns.extend(self.detect_engulfing(df))
        patterns.extend(self.detect_morning_evening_star(df))
        patterns.extend(self.detect_three_white_soldiers(df))
        patterns.extend(self.detect_hanging_man(df))
        return patterns
    
    def detect_hammer(self, df: pd.DataFrame) -> List[PatternSignal]:
        signals = []
        for i in range(2, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i-1]
            body = abs(current['Close'] - current['Open'])
            lower_shadow = min(current['Open'], current['Close']) - current['Low']
            upper_shadow = current['High'] - max(current['Open'], current['Close'])
            
            if lower_shadow > 2 * body and upper_shadow < 0.1 * body and current['Close'] > prev['Close']:
                signals.append(PatternSignal(pattern="hammer", strength=min(lower_shadow / (body + 0.01), 5.0), direction="bullish", confidence=0.75))
        return signals
    
    def detect_shooting_star(self, df: pd.DataFrame) -> List[PatternSignal]:
        signals = []
        for i in range(2, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i-1]
            body = abs(current['Close'] - current['Open'])
            lower_shadow = min(current['Open'], current['Close']) - current['Low']
            upper_shadow = current['High'] - max(current['Open'], current['Close'])
            
            if upper_shadow > 2 * body and lower_shadow < 0.1 * body and current['Close'] < prev['Close']:
                signals.append(PatternSignal(pattern="shooting_star", strength=min(upper_shadow / (body + 0.01), 5.0), direction="bearish", confidence=0.75))
        return signals
    
    def detect_hanging_man(self, df: pd.DataFrame) -> List[PatternSignal]:
        signals = []
        for i in range(2, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i-1]
            body = abs(current['Close'] - current['Open'])
            lower_shadow = min(current['Open'], current['Close']) - current['Low']
            upper_shadow = current['High'] - max(current['Open'], current['Close'])
            
            if lower_shadow > 2 * body and upper_shadow < 0.1 * body and prev['Close'] > prev['Open']:
                signals.append(PatternSignal(pattern="hanging_man", strength=2.5, direction="bearish", confidence=0.7))
        return signals
    
    def detect_doji(self, df: pd.DataFrame) -> List[PatternSignal]:
        signals = []
        for i in range(1, len(df)):
            current = df.iloc[i]
            body = abs(current['Close'] - current['Open'])
            range_size = current['High'] - current['Low']
            if body < 0.1 * range_size:
                signals.append(PatternSignal(pattern="doji", strength=1.0 - (body / (range_size + 0.01)), direction="neutral", confidence=0.65))
        return signals
    
    def detect_engulfing(self, df: pd.DataFrame) -> List[PatternSignal]:
        signals = []
        for i in range(1, len(df)):
            current = df.iloc[i]
            prev = df.iloc[i-1]
            
            if (prev['Close'] < prev['Open'] and current['Close'] > current['Open'] and 
                current['Open'] < prev['Close'] and current['Close'] > prev['Open']):
                signals.append(PatternSignal(pattern="bullish_engulfing", strength=3.0, direction="bullish", confidence=0.85))
            
            elif (prev['Close'] > prev['Open'] and current['Close'] < current['Open'] and 
                  current['Open'] > prev['Close'] and current['Close'] < prev['Open']):
                signals.append(PatternSignal(pattern="bearish_engulfing", strength=3.0, direction="bearish", confidence=0.85))
        return signals
    
    def detect_morning_evening_star(self, df: pd.DataFrame) -> List[PatternSignal]:
        signals = []
        for i in range(2, len(df)):
            first = df.iloc[i-2]
            second = df.iloc[i-1]
            third = df.iloc[i]
            
            # Morning Star (Bullish)
            if (first['Close'] < first['Open'] and 
                abs(second['Close'] - second['Open']) < (first['High'] - first['Low']) * 0.3 and
                third['Close'] > third['Open'] and third['Close'] > (first['Open'] + first['Close']) / 2):
                signals.append(PatternSignal(pattern="morning_star", strength=4.0, direction="bullish", confidence=0.9))
            
            # Evening Star (Bearish)
            elif (first['Close'] > first['Open'] and 
                  abs(second['Close'] - second['Open']) < (first['High'] - first['Low']) * 0.3 and
                  third['Close'] < third['Open'] and third['Close'] < (first['Open'] + first['Close']) / 2):
                signals.append(PatternSignal(pattern="evening_star", strength=4.0, direction="bearish", confidence=0.9))
        return signals
    
    def detect_three_white_soldiers(self, df: pd.DataFrame) -> List[PatternSignal]:
        signals = []
        for i in range(2, len(df)):
            first = df.iloc[i-2]
            second = df.iloc[i-1]
            third = df.iloc[i]
            
            # Three White Soldiers (Bullish)
            if (first['Close'] > first['Open'] and second['Close'] > second['Open'] and third['Close'] > third['Open'] and
                second['Close'] > first['Close'] and third['Close'] > second['Close']):
                signals.append(PatternSignal(pattern="three_white_soldiers", strength=4.5, direction="bullish", confidence=0.88))
            
            # Three Black Crows (Bearish)
            elif (first['Close'] < first['Open'] and second['Close'] < second['Open'] and third['Close'] < third['Open'] and
                  second['Close'] < first['Close'] and third['Close'] < second['Close']):
                signals.append(PatternSignal(pattern="three_black_crows", strength=4.5, direction="bearish", confidence=0.88))
        return signals
