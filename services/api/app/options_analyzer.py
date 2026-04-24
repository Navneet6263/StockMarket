import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import requests

try:
    from nsepython import nse_optionchain_scrapper
    NSE_PYTHON_AVAILABLE = True
except ImportError:
    NSE_PYTHON_AVAILABLE = False
    print("nsepython not installed, using requests method")

class OptionsAnalyzer:
    def __init__(self):
        self.nse_options_url = "https://www.nseindia.com/api/option-chain-indices"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'DNT': '1',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
        }
        self.session = None
        self.cache = {}
        self.cache_ttl = timedelta(minutes=5)
    
    def _get_session(self):
        if self.session is None:
            self.session = requests.Session()
            try:
                print("Getting NSE cookies...")
                home_response = self.session.get(
                    "https://www.nseindia.com", 
                    headers=self.headers, 
                    timeout=10
                )
                print(f"Homepage status: {home_response.status_code}")
                self.session.get(
                    "https://www.nseindia.com/option-chain",
                    headers=self.headers,
                    timeout=10
                )
            except Exception as e:
                print(f"Session creation error: {e}")
        return self.session
    
    def get_nifty_options_chain(self, symbol: str = "NIFTY") -> Dict:
        cached = self.cache.get(symbol)
        if cached and datetime.now() - cached[0] < self.cache_ttl:
            return cached[1]
        
        if NSE_PYTHON_AVAILABLE:
            try:
                print(f"Fetching {symbol} via NSEPython...")
                data = nse_optionchain_scrapper(symbol)
                if data and isinstance(data, dict) and len(data) > 0:
                    result = self.analyze_options_data(data)
                    if result and result.get('spot_price', 0) > 0:
                        self.cache[symbol] = (datetime.now(), result)
                        return result
            except Exception as e:
                print(f"NSEPython failed: {e}")
        
        try:
            session = self._get_session()
            url = f"{self.nse_options_url}?symbol={symbol}"
            api_headers = self.headers.copy()
            api_headers['Referer'] = 'https://www.nseindia.com/option-chain'

            for attempt in range(2):
                try:
                    response = session.get(url, headers=api_headers, timeout=15)
                    if response.status_code == 200:
                        data = response.json()
                        result = self.analyze_options_data(data)
                        if result and result.get('spot_price', 0) > 0:
                            self.cache[symbol] = (datetime.now(), result)
                            return result
                except Exception as inner:
                    print(f"Attempt {attempt + 1} failed: {inner}")
        except Exception as e:
            print(f"Requests error: {e}")
        
        result = self.get_fallback_analysis(symbol)
        self.cache[symbol] = (datetime.now(), result)
        return result
    
    def analyze_options_data(self, data: Dict) -> Dict:
        try:
            records_data = data.get('records', {}) or {}
            filtered_data = data.get('filtered', {}) or {}
            option_data = records_data.get('data', [])

            if not option_data and filtered_data:
                option_data = filtered_data.get('data', [])

            if not option_data:
                return {}
            
            spot_price = records_data.get('underlyingValue', 0) or filtered_data.get('underlyingValue', 0) or data.get('underlyingValue', 0)
            if spot_price == 0:
                return {}
            
            atm_strike = self.find_atm_strike(spot_price, option_data)
            pcr = self.calculate_pcr(option_data, atm_strike)
            max_pain = self.calculate_max_pain(option_data)
            iv_proxy = self.estimate_iv(option_data, atm_strike, spot_price)
            support_resistance = self.find_support_resistance(option_data, spot_price)
            oi_change = self.calculate_oi_change_bias(option_data, atm_strike)
            signal = self.generate_options_signal(pcr, spot_price, max_pain, support_resistance, oi_change)
            
            return {
                'spot_price': spot_price,
                'atm_strike': atm_strike,
                'pcr': pcr,
                'max_pain': max_pain,
                'iv_proxy': iv_proxy,
                'oi_change': oi_change,
                'support': support_resistance['support'],
                'resistance': support_resistance['resistance'],
                'signal': signal,
                'timestamp': datetime.now().isoformat(),
                'data_source': 'live'
            }
        except Exception as e:
            print(f"Analysis error: {e}")
            return {}
    
    def find_atm_strike(self, spot: float, records: List) -> int:
        strikes = [r['strikePrice'] for r in records if 'strikePrice' in r]
        if not strikes:
            return int(round(spot / 50) * 50)
        return min(strikes, key=lambda x: abs(x - spot))
    
    def calculate_pcr(self, records: List, atm_strike: int) -> float:
        total_put_oi = 0
        total_call_oi = 0
        
        for record in records:
            strike = record.get('strikePrice', 0)
            if abs(strike - atm_strike) <= 500:
                if 'PE' in record:
                    total_put_oi += record['PE'].get('openInterest', 0)
                if 'CE' in record:
                    total_call_oi += record['CE'].get('openInterest', 0)
        
        if total_call_oi == 0:
            return 1.0
        return total_put_oi / total_call_oi
    
    def calculate_max_pain(self, records: List) -> int:
        pain_dict = {}
        
        for record in records:
            strike = record.get('strikePrice', 0)
            if strike == 0:
                continue
            
            total_pain = 0
            for r in records:
                s = r.get('strikePrice', 0)
                if s == 0:
                    continue
                
                if 'CE' in r:
                    ce_oi = r['CE'].get('openInterest', 0)
                    if s < strike:
                        total_pain += (strike - s) * ce_oi
                
                if 'PE' in r:
                    pe_oi = r['PE'].get('openInterest', 0)
                    if s > strike:
                        total_pain += (s - strike) * pe_oi
            
            pain_dict[strike] = total_pain
        
        if not pain_dict:
            return 0
        return min(pain_dict, key=pain_dict.get)

    def calculate_oi_change_bias(self, records: List, atm_strike: int) -> Dict:
        call_change_oi = 0
        put_change_oi = 0

        for record in records:
            strike = record.get('strikePrice', 0)
            if abs(strike - atm_strike) <= 300:
                if 'CE' in record:
                    call_change_oi += record['CE'].get('changeinOpenInterest', 0)
                if 'PE' in record:
                    put_change_oi += record['PE'].get('changeinOpenInterest', 0)

        bias = 'neutral'
        if put_change_oi > call_change_oi * 1.15:
            bias = 'bullish_support'
        elif call_change_oi > put_change_oi * 1.15:
            bias = 'bearish_resistance'

        return {
            'call_change_oi': int(call_change_oi),
            'put_change_oi': int(put_change_oi),
            'bias': bias,
        }
    
    def find_support_resistance(self, records: List, spot: float) -> Dict:
        strikes_below = []
        strikes_above = []
        
        for record in records:
            strike = record.get('strikePrice', 0)
            if strike == 0:
                continue
            
            total_oi = 0
            if 'PE' in record:
                total_oi += record['PE'].get('openInterest', 0)
            if 'CE' in record:
                total_oi += record['CE'].get('openInterest', 0)
            
            if strike < spot:
                strikes_below.append((strike, total_oi))
            elif strike > spot:
                strikes_above.append((strike, total_oi))
        
        support = max(strikes_below, key=lambda x: x[1])[0] if strikes_below else spot - 200
        resistance = max(strikes_above, key=lambda x: x[1])[0] if strikes_above else spot + 200
        
        return {'support': support, 'resistance': resistance}
    
    def generate_options_signal(self, pcr: float, spot: float, max_pain: int, sr: Dict, oi_change: Optional[Dict] = None) -> Dict:
        signals = []
        direction = "neutral"
        confidence = 0.5
        
        if pcr > 1.3:
            signals.append("High PCR - Bullish (More Puts)")
            direction = "bullish"
            confidence += 0.15
        elif pcr < 0.7:
            signals.append("Low PCR - Bearish (More Calls)")
            direction = "bearish"
            confidence += 0.15
        else:
            signals.append(f"Neutral PCR ({pcr:.2f})")
        
        if spot < max_pain - 50:
            signals.append(f"Below Max Pain ({max_pain}) - Likely to move up")
            if direction != "bearish":
                direction = "bullish"
                confidence += 0.1
        elif spot > max_pain + 50:
            signals.append(f"Above Max Pain ({max_pain}) - Likely to move down")
            if direction != "bullish":
                direction = "bearish"
                confidence += 0.1
        
        distance_to_support = ((spot - sr['support']) / spot) * 100
        distance_to_resistance = ((sr['resistance'] - spot) / spot) * 100
        
        if distance_to_support < 1:
            signals.append(f"Near Support ({sr['support']}) - Bounce expected")
            direction = "bullish"
            confidence += 0.1
        elif distance_to_resistance < 1:
            signals.append(f"Near Resistance ({sr['resistance']}) - Rejection expected")
            direction = "bearish"
            confidence += 0.1

        if oi_change:
            if oi_change.get('bias') == 'bullish_support':
                signals.append("Put OI build-up is stronger than call OI build-up")
                if direction != "bearish":
                    direction = "bullish"
                    confidence += 0.1
            elif oi_change.get('bias') == 'bearish_resistance':
                signals.append("Call OI build-up is stronger than put OI build-up")
                if direction != "bullish":
                    direction = "bearish"
                    confidence += 0.1
        
        confidence = min(confidence, 0.95)
        strategy = self.suggest_strategy(direction, pcr, spot, max_pain)
        if confidence < 0.55:
            strategy = "No-trade: confidence too low"
        return {
            'direction': direction,
            'confidence': confidence,
            'signals': signals,
            'strategy': strategy,
            'oi_bias': oi_change.get('bias', 'neutral') if oi_change else 'neutral',
        }

    def estimate_iv(self, records: List, atm: int, spot: float) -> float:
        atm_rec = next((r for r in records if r.get('strikePrice') == atm), None)
        if not atm_rec or 'CE' not in atm_rec or 'PE' not in atm_rec:
            return 0.0
        ce_ltp = atm_rec['CE'].get('lastPrice', 0)
        pe_ltp = atm_rec['PE'].get('lastPrice', 0)
        premium = ce_ltp + pe_ltp
        if spot <= 0:
            return 0.0
        return round((premium / spot) / np.sqrt(0.04), 3)
    
    def suggest_strategy(self, direction: str, pcr: float, spot: float, max_pain: int) -> str:
        if direction == "bullish":
            if pcr > 1.5:
                return "Buy ATM Call | Sell OTM Put (Bull Call Spread)"
            else:
                return "Buy ITM Call | Hold till resistance"
        elif direction == "bearish":
            if pcr < 0.6:
                return "Buy ATM Put | Sell OTM Call (Bear Put Spread)"
            else:
                return "Buy ITM Put | Hold till support"
        else:
            return "Iron Condor | Sell both Call & Put OTM"
    
    def get_fallback_analysis(self, symbol: str) -> Dict:
        import yfinance as yf
        ticker_symbol = '^NSEI' if symbol == 'NIFTY' else '^NSEBANK'
        hist = yf.Ticker(ticker_symbol).history(period='1d')
        spot_price = float(hist['Close'].iloc[-1]) if not hist.empty else (24500 if symbol == 'NIFTY' else 51000)

        atm_strike = int(round(spot_price / 100) * 100)
        support = int((spot_price * 0.99) / 100) * 100
        resistance = int((spot_price * 1.01) / 100) * 100

        cached = self.cache.get(symbol)
        pcr = cached[1]['pcr'] if cached else 1.0
        max_pain = cached[1]['max_pain'] if cached else atm_strike

        direction = 'neutral'
        if pcr > 1.15:
            direction = 'bullish'
        elif pcr < 0.95:
            direction = 'bearish'

        return {
            'spot_price': float(spot_price),
            'atm_strike': atm_strike,
            'pcr': pcr,
            'max_pain': max_pain,
            'support': support,
            'resistance': resistance,
            'signal': {
                'direction': direction,
                'confidence': 0.55,
                'signals': ['Fallback data: live chain unavailable'],
                'strategy': 'No-trade: using fallback data'
            },
            'timestamp': datetime.now().isoformat(),
            'data_source': 'fallback'
        }
