"""
options_analyzer.py — Accurate CE/PE recommendation engine.

Data sources (in priority order):
  1. nsepython  — NSE option chain (free, no auth)
  2. NSE public API with cookie session
  3. yfinance fallback (spot price only — labelled, no trade recommendation)

Accuracy rules:
  - confidence < 70% → NO_TRADE (never show a bad call)
  - fallback data     → NO_TRADE (clearly labelled ⚠️)
  - Only BUY CE / BUY PE — never neutral "hold" when unclear
  - 10-day swing prediction uses EMA + RSI + MACD + VIX + PCR + OI bias
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

try:
    from nsepython import nse_optionchain_scrapper
    NSE_PYTHON_AVAILABLE = True
except ImportError:
    NSE_PYTHON_AVAILABLE = False
    logger.warning("nsepython not installed — will use direct NSE API")

# ─── Index meta ────────────────────────────────────────────────────────────────
INDEX_META = {
    "NIFTY":      {"yf": "^NSEI",    "strike_step": 50,  "label": "Nifty 50"},
    "BANKNIFTY":  {"yf": "^NSEBANK", "strike_step": 100, "label": "Bank Nifty"},
    "FINNIFTY":   {"yf": "NIFTY_FIN_SERVICE.NS", "strike_step": 50, "label": "Fin Nifty"},
    "MIDCPNIFTY": {"yf": "^NSEI",    "strike_step": 25,  "label": "Midcap Nifty"},
}

CONFIDENCE_THRESHOLD = 70  # Below this → NO_TRADE always


class OptionsAnalyzer:
    def __init__(self):
        self.nse_options_url = "https://www.nseindia.com/api/option-chain-indices"
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "DNT": "1",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }
        self._session: Optional[requests.Session] = None
        self._cache: Dict[str, Tuple[datetime, dict]] = {}
        self._cache_ttl = timedelta(minutes=5)

    # ── Session / cookie management ────────────────────────────────────────────

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            try:
                self._session.get("https://www.nseindia.com", headers=self.headers, timeout=10)
                self._session.get("https://www.nseindia.com/option-chain", headers=self.headers, timeout=10)
            except Exception as e:
                logger.warning("NSE session init error: %s", e)
        return self._session

    # ── Cache helpers ──────────────────────────────────────────────────────────

    def _from_cache(self, key: str) -> Optional[dict]:
        entry = self._cache.get(key)
        if entry and datetime.now() - entry[0] < self._cache_ttl:
            return entry[1]
        return None

    def _to_cache(self, key: str, value: dict) -> None:
        self._cache[key] = (datetime.now(), value)

    # ── Raw option chain fetch ─────────────────────────────────────────────────

    def _fetch_raw_chain(self, symbol: str) -> Optional[dict]:
        """Fetch raw NSE option chain data. Returns raw dict or None."""
        # 1. nsepython
        if NSE_PYTHON_AVAILABLE:
            try:
                data = nse_optionchain_scrapper(symbol)
                if data and isinstance(data, dict) and data.get("records", {}).get("data"):
                    logger.info("[OPTIONS] %s chain fetched via nsepython", symbol)
                    return data
            except Exception as e:
                logger.warning("[OPTIONS] nsepython failed for %s: %s", symbol, e)

        # 2. Direct NSE API
        try:
            session = self._get_session()
            url = f"{self.nse_options_url}?symbol={symbol}"
            hdrs = {**self.headers, "Referer": "https://www.nseindia.com/option-chain"}
            for attempt in range(2):
                try:
                    resp = session.get(url, headers=hdrs, timeout=15)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("records", {}).get("data"):
                            logger.info("[OPTIONS] %s chain fetched via direct NSE API", symbol)
                            return data
                except Exception as inner:
                    logger.warning("[OPTIONS] NSE API attempt %d failed: %s", attempt + 1, inner)
                    if attempt == 0:
                        # Reset session and retry
                        self._session = None
                        self._get_session()
        except Exception as e:
            logger.warning("[OPTIONS] NSE direct API error: %s", e)

        return None

    # ── Main public method ─────────────────────────────────────────────────────

    def get_full_analysis(self, symbol: str = "NIFTY") -> dict:
        """
        Returns complete options analysis including:
          - spot_price, atm_strike, pcr, max_pain, iv_proxy
          - support / resistance levels
          - 10-day swing prediction (CE or PE)
          - Final recommendation box
          - data_source: 'live' | 'fallback'
        """
        cached = self._from_cache(symbol)
        if cached:
            return cached

        raw = self._fetch_raw_chain(symbol)
        if raw:
            result = self._analyze(symbol, raw, data_source="live")
        else:
            result = self._fallback_analysis(symbol)

        self._to_cache(symbol, result)
        return result

    # ── Core analysis ──────────────────────────────────────────────────────────

    def _analyze(self, symbol: str, raw: dict, data_source: str) -> dict:
        records_data = raw.get("records", {}) or {}
        filtered_data = raw.get("filtered", {}) or {}
        option_data = records_data.get("data", []) or filtered_data.get("data", [])

        if not option_data:
            return self._fallback_analysis(symbol)

        spot = (
            records_data.get("underlyingValue")
            or filtered_data.get("underlyingValue")
            or raw.get("underlyingValue", 0)
        )
        if not spot or spot <= 0:
            return self._fallback_analysis(symbol)

        step = INDEX_META.get(symbol, {}).get("strike_step", 50)
        atm = self._atm_strike(spot, option_data, step)
        pcr = self._pcr(option_data, atm)
        max_pain = self._max_pain(option_data)
        iv_proxy = self._iv_proxy(option_data, atm, spot)
        sr = self._support_resistance(option_data, spot)
        oi_change = self._oi_change_bias(option_data, atm)
        vix = self._fetch_vix()

        # Flatten records for frontend OI chart
        flattened = self._flatten_records(option_data, spot)

        # 10-day swing prediction
        swing = self._swing_prediction(symbol, spot, pcr, max_pain, sr, oi_change, vix)

        # Final recommendation
        recommendation = self._build_recommendation(
            symbol=symbol,
            direction=swing["direction"],
            confidence=swing["confidence"],
            spot=spot,
            atm=atm,
            step=step,
            swing=swing,
            sr=sr,
            max_pain=max_pain,
            data_source=data_source,
        )

        return {
            "symbol": symbol,
            "label": INDEX_META.get(symbol, {}).get("label", symbol),
            "spot_price": round(float(spot), 2),
            "atm_strike": atm,
            "pcr": round(pcr, 3),
            "max_pain": max_pain,
            "iv_proxy": round(iv_proxy, 3),
            "india_vix": vix,
            "support": sr["support"],
            "resistance": sr["resistance"],
            "oi_change": oi_change,
            "swing_prediction": swing,
            "recommendation": recommendation,
            "records": flattened,
            "data_source": data_source,
            "timestamp": datetime.now().isoformat(),
        }

    # ── Strike helpers ─────────────────────────────────────────────────────────

    def _atm_strike(self, spot: float, records: list, step: int) -> int:
        strikes = [r["strikePrice"] for r in records if "strikePrice" in r]
        if not strikes:
            return int(round(spot / step) * step)
        return int(min(strikes, key=lambda x: abs(x - spot)))

    # ── PCR ────────────────────────────────────────────────────────────────────

    def _pcr(self, records: list, atm: int, window: int = 500) -> float:
        put_oi = call_oi = 0
        for r in records:
            s = r.get("strikePrice", 0)
            if abs(s - atm) <= window:
                put_oi += r.get("PE", {}).get("openInterest", 0)
                call_oi += r.get("CE", {}).get("openInterest", 0)
        return round(put_oi / call_oi, 3) if call_oi else 1.0

    # ── Max pain ───────────────────────────────────────────────────────────────

    def _max_pain(self, records: list) -> int:
        pain: Dict[int, float] = {}
        for r in records:
            strike = r.get("strikePrice", 0)
            if not strike:
                continue
            total = 0.0
            for other in records:
                s = other.get("strikePrice", 0)
                if not s:
                    continue
                ce_oi = other.get("CE", {}).get("openInterest", 0)
                pe_oi = other.get("PE", {}).get("openInterest", 0)
                if s < strike:
                    total += (strike - s) * ce_oi
                if s > strike:
                    total += (s - strike) * pe_oi
            pain[strike] = total
        return int(min(pain, key=pain.get)) if pain else 0

    # ── IV proxy ───────────────────────────────────────────────────────────────

    def _iv_proxy(self, records: list, atm: int, spot: float) -> float:
        rec = next((r for r in records if r.get("strikePrice") == atm), None)
        if not rec:
            return 0.0
        ce_ltp = rec.get("CE", {}).get("lastPrice", 0)
        pe_ltp = rec.get("PE", {}).get("lastPrice", 0)
        premium = ce_ltp + pe_ltp
        return round((premium / spot) / (0.04 ** 0.5), 3) if spot > 0 else 0.0

    # ── Support / Resistance ───────────────────────────────────────────────────

    def _support_resistance(self, records: list, spot: float) -> dict:
        below, above = [], []
        for r in records:
            s = r.get("strikePrice", 0)
            if not s:
                continue
            oi = r.get("PE", {}).get("openInterest", 0) + r.get("CE", {}).get("openInterest", 0)
            (below if s < spot else above).append((s, oi))

        support = max(below, key=lambda x: x[1]) if below else (spot - 200, 0)
        resistance = max(above, key=lambda x: x[1]) if above else (spot + 200, 0)
        return {
            "support": int(support[0]),
            "support_oi": int(support[1]),
            "resistance": int(resistance[0]),
            "resistance_oi": int(resistance[1]),
        }

    # ── OI change bias ─────────────────────────────────────────────────────────

    def _oi_change_bias(self, records: list, atm: int, window: int = 300) -> dict:
        call_chg = put_chg = 0
        for r in records:
            s = r.get("strikePrice", 0)
            if abs(s - atm) <= window:
                call_chg += r.get("CE", {}).get("changeinOpenInterest", 0)
                put_chg += r.get("PE", {}).get("changeinOpenInterest", 0)

        if put_chg > call_chg * 1.2:
            bias = "bullish_support"
        elif call_chg > put_chg * 1.2:
            bias = "bearish_resistance"
        else:
            bias = "neutral"

        return {
            "call_change_oi": int(call_chg),
            "put_change_oi": int(put_chg),
            "bias": bias,
        }

    # ── India VIX ─────────────────────────────────────────────────────────────

    def _fetch_vix(self) -> Optional[float]:
        try:
            import yfinance as yf
            hist = yf.Ticker("^INDIAVIX").history(period="1d")
            if not hist.empty:
                return round(float(hist["Close"].iloc[-1]), 2)
        except Exception as e:
            logger.debug("VIX fetch error: %s", e)
        return None

    # ── Flatten records for OI chart ───────────────────────────────────────────

    def _flatten_records(self, records: list, spot: float) -> list:
        out = []
        for r in records:
            s = r.get("strikePrice")
            if not s:
                continue
            if abs(s - spot) > spot * 0.04:
                continue
            out.append({
                "strikePrice": s,
                "ce_oi": r.get("CE", {}).get("openInterest", 0),
                "pe_oi": r.get("PE", {}).get("openInterest", 0),
                "ce_change_oi": r.get("CE", {}).get("changeinOpenInterest", 0),
                "pe_change_oi": r.get("PE", {}).get("changeinOpenInterest", 0),
                "ce_ltp": r.get("CE", {}).get("lastPrice", 0),
                "pe_ltp": r.get("PE", {}).get("lastPrice", 0),
                "ce_iv": r.get("CE", {}).get("impliedVolatility", 0),
                "pe_iv": r.get("PE", {}).get("impliedVolatility", 0),
            })
        out.sort(key=lambda x: x["strikePrice"])
        return out

    # ── 10-day swing prediction ────────────────────────────────────────────────

    def _swing_prediction(
        self,
        symbol: str,
        spot: float,
        pcr: float,
        max_pain: int,
        sr: dict,
        oi_change: dict,
        vix: Optional[float],
    ) -> dict:
        """
        Combines technical indicators (EMA, RSI, MACD) + options data
        to predict 10-day direction with confidence score.
        Returns: direction, confidence, signals[], option_action
        """
        yf_symbol = INDEX_META.get(symbol, {}).get("yf", "^NSEI")
        tech_signals, tech_direction, tech_score = self._technical_analysis(yf_symbol, spot)

        signals: List[str] = list(tech_signals)
        bull_score = 0
        bear_score = 0

        # ── Technical base ──
        if tech_direction == "bullish":
            bull_score += tech_score
        elif tech_direction == "bearish":
            bear_score += tech_score

        # ── PCR ──
        if pcr > 1.3:
            signals.append(f"PCR {pcr:.2f} → bullish (hedgers buying puts = smart money protected)")
            bull_score += 15
        elif pcr < 0.7:
            signals.append(f"PCR {pcr:.2f} → bearish (call writing heavy)")
            bear_score += 15
        else:
            signals.append(f"PCR {pcr:.2f} → neutral")

        # ── Max pain ──
        mp_gap_pct = ((spot - max_pain) / spot) * 100
        if spot < max_pain - (spot * 0.01):
            signals.append(f"Spot {spot:.0f} below max pain {max_pain} → gravity pulls UP")
            bull_score += 10
        elif spot > max_pain + (spot * 0.01):
            signals.append(f"Spot {spot:.0f} above max pain {max_pain} → gravity pulls DOWN")
            bear_score += 10

        # ── OI change ──
        if oi_change["bias"] == "bullish_support":
            signals.append("Strong PUT OI addition near ATM → support building (bullish)")
            bull_score += 12
        elif oi_change["bias"] == "bearish_resistance":
            signals.append("Strong CALL OI addition near ATM → resistance building (bearish)")
            bear_score += 12

        # ── Support/Resistance distance ──
        dist_support = abs((spot - sr["support"]) / spot) * 100
        dist_resist  = abs((sr["resistance"] - spot) / spot) * 100
        if dist_support < 1.5:
            signals.append(f"Spot near strong OI support {sr['support']} → likely bounce")
            bull_score += 10
        elif dist_resist < 1.5:
            signals.append(f"Spot near strong OI resistance {sr['resistance']} → likely rejection")
            bear_score += 10

        # ── VIX regime ──
        if vix is not None:
            if vix < 13:
                signals.append(f"India VIX {vix} → very low fear, complacency — breakout possible but risky")
                bull_score += 5
            elif vix <= 17:
                signals.append(f"India VIX {vix} → normal range, good for directional plays")
                bull_score += 8
            elif vix <= 22:
                signals.append(f"India VIX {vix} → elevated fear, options expensive — prefer spreads")
                bear_score += 8
            else:
                signals.append(f"India VIX {vix} → extreme fear (>22), avoid naked options — very risky")
                bear_score += 15

        # ── Final direction ──
        total = bull_score + bear_score
        if total == 0:
            total = 1  # guard divide by zero

        if bull_score > bear_score * 1.2:
            direction = "bullish"
            raw_conf = (bull_score / total) * 100
        elif bear_score > bull_score * 1.2:
            direction = "bearish"
            raw_conf = (bear_score / total) * 100
        else:
            direction = "neutral"
            raw_conf = 50.0

        # Map raw_conf (0-100 relative score) to a clean confidence
        # Never inflate beyond 85 — options is never 100% certain
        confidence = min(round(raw_conf * 0.85 + 15), 85) if direction != "neutral" else 50

        option_action = "NO_TRADE"
        if confidence >= CONFIDENCE_THRESHOLD:
            option_action = "BUY_CE" if direction == "bullish" else "BUY_PE" if direction == "bearish" else "NO_TRADE"

        return {
            "direction": direction,
            "confidence": confidence,
            "option_action": option_action,  # BUY_CE | BUY_PE | NO_TRADE
            "signals": signals,
            "horizon_days": 10,
            "bull_score": bull_score,
            "bear_score": bear_score,
            "vix": vix,
        }

    # ── Technical analysis ─────────────────────────────────────────────────────

    def _technical_analysis(self, yf_symbol: str, spot: float) -> Tuple[List[str], str, int]:
        """Returns (signals, direction, score 0-35)"""
        try:
            import yfinance as yf
            hist = yf.Ticker(yf_symbol).history(period="3mo", interval="1d")
            if hist.empty or len(hist) < 20:
                return ["Insufficient price history for technical analysis"], "neutral", 0

            close = hist["Close"]
            signals: List[str] = []
            bull = 0
            bear = 0

            # EMA 20/50
            ema20 = close.ewm(span=20).mean().iloc[-1]
            ema50 = close.ewm(span=50).mean().iloc[-1]
            if ema20 > ema50:
                signals.append(f"EMA20 ({ema20:.0f}) > EMA50 ({ema50:.0f}) → uptrend")
                bull += 10
            else:
                signals.append(f"EMA20 ({ema20:.0f}) < EMA50 ({ema50:.0f}) → downtrend")
                bear += 10

            # Spot vs EMA20
            if spot > ema20 * 1.005:
                signals.append(f"Spot {spot:.0f} above EMA20 → momentum up")
                bull += 5
            elif spot < ema20 * 0.995:
                signals.append(f"Spot {spot:.0f} below EMA20 → momentum down")
                bear += 5

            # RSI
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            rsi_val = rsi.iloc[-1]
            if rsi_val > 60:
                signals.append(f"RSI {rsi_val:.1f} → bullish momentum")
                bull += 8
            elif rsi_val < 40:
                signals.append(f"RSI {rsi_val:.1f} → bearish momentum")
                bear += 8
            else:
                signals.append(f"RSI {rsi_val:.1f} → neutral momentum")

            # MACD
            ema12 = close.ewm(span=12).mean()
            ema26 = close.ewm(span=26).mean()
            macd = ema12 - ema26
            signal_line = macd.ewm(span=9).mean()
            hist_macd = macd - signal_line
            if hist_macd.iloc[-1] > 0 and hist_macd.iloc[-2] <= 0:
                signals.append("MACD bullish crossover → strong buy signal")
                bull += 12
            elif hist_macd.iloc[-1] < 0 and hist_macd.iloc[-2] >= 0:
                signals.append("MACD bearish crossover → strong sell signal")
                bear += 12
            elif hist_macd.iloc[-1] > 0:
                signals.append("MACD positive → trend up")
                bull += 6
            else:
                signals.append("MACD negative → trend down")
                bear += 6

            direction = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
            score = max(bull, bear)
            return signals, direction, min(score, 35)

        except Exception as e:
            logger.warning("[OPTIONS] Technical analysis failed: %s", e)
            return ["Technical analysis unavailable"], "neutral", 0

    # ── Recommendation box ─────────────────────────────────────────────────────

    def _build_recommendation(
        self,
        symbol: str,
        direction: str,
        confidence: int,
        spot: float,
        atm: int,
        step: int,
        swing: dict,
        sr: dict,
        max_pain: int,
        data_source: str,
    ) -> dict:
        """Build the final actionable recommendation card."""

        # ── Safety: never recommend on fallback or low confidence ──
        if data_source == "fallback":
            return {
                "action": "NO_TRADE",
                "reason": "⚠️ Live NSE data unavailable — Do NOT trade options based on this",
                "confidence": confidence,
                "safe": False,
            }

        if confidence < CONFIDENCE_THRESHOLD:
            return {
                "action": "NO_TRADE",
                "reason": f"Confidence only {confidence}% — below 70% threshold. Market is unclear. Sit out.",
                "confidence": confidence,
                "safe": True,
            }

        if swing.get("vix") and swing["vix"] > 22:
            return {
                "action": "NO_TRADE",
                "reason": f"India VIX {swing['vix']} is extremely high (>22). Options premium is very expensive. Risk of loss is very high. Avoid.",
                "confidence": confidence,
                "safe": True,
            }

        option_action = swing.get("option_action", "NO_TRADE")
        if option_action == "NO_TRADE":
            return {
                "action": "NO_TRADE",
                "reason": "Market direction unclear — no trade recommended",
                "confidence": confidence,
                "safe": True,
            }

        is_ce = option_action == "BUY_CE"
        strike = atm  # ATM strike for simplicity and best liquidity

        # Next weekly/monthly expiry (approximation — actual from chain is better)
        today = datetime.now()
        # Next Thursday for weekly
        days_to_thursday = (3 - today.weekday()) % 7
        if days_to_thursday == 0:
            days_to_thursday = 7
        expiry_date = (today + timedelta(days=days_to_thursday)).strftime("%d %b %Y")

        # Stop loss = underlying moves against you by 1%
        stop_loss_underlying = round(spot * 0.99, 0) if is_ce else round(spot * 1.01, 0)
        # Target = next OI resistance / support
        target_underlying = sr["resistance"] if is_ce else sr["support"]

        # Strategy text
        strategy = (
            f"Buy {symbol} {strike} CE (Call) | Expiry ~{expiry_date}"
            if is_ce else
            f"Buy {symbol} {strike} PE (Put) | Expiry ~{expiry_date}"
        )

        reason_parts = []
        if swing["signals"]:
            reason_parts = swing["signals"][:4]  # top 4 reasons

        return {
            "action": option_action,           # BUY_CE or BUY_PE
            "label": "📈 BUY CALL (CE)" if is_ce else "📉 BUY PUT (PE)",
            "symbol": symbol,
            "strike": strike,
            "expiry_approx": expiry_date,
            "strategy": strategy,
            "confidence": confidence,
            "horizon": "10 days (swing)",
            "stop_loss_underlying": int(stop_loss_underlying),
            "target_underlying": int(target_underlying),
            "max_pain": max_pain,
            "reasons": reason_parts,
            "vix_note": (
                f"VIX {swing['vix']:.1f} — {'safe range' if swing.get('vix', 15) <= 17 else 'elevated, buy spreads'}"
                if swing.get("vix") else "VIX data unavailable"
            ),
            "warning": (
                "⚠️ Always use stop loss. Options can expire worthless. Only risk what you can afford to lose."
            ),
            "safe": True,
            "data_source": data_source,
        }

    # ── Fallback ───────────────────────────────────────────────────────────────

    def _fallback_analysis(self, symbol: str) -> dict:
        yf_symbol = INDEX_META.get(symbol, {}).get("yf", "^NSEI")
        spot = 0.0
        try:
            import yfinance as yf
            hist = yf.Ticker(yf_symbol).history(period="1d")
            if not hist.empty:
                spot = float(hist["Close"].iloc[-1])
        except Exception:
            pass

        if spot <= 0:
            defaults = {"NIFTY": 24500, "BANKNIFTY": 52000, "FINNIFTY": 23000, "MIDCPNIFTY": 12000}
            spot = defaults.get(symbol, 24500)

        step = INDEX_META.get(symbol, {}).get("strike_step", 50)
        atm = int(round(spot / step) * step)

        return {
            "symbol": symbol,
            "label": INDEX_META.get(symbol, {}).get("label", symbol),
            "spot_price": round(spot, 2),
            "atm_strike": atm,
            "pcr": 1.0,
            "max_pain": atm,
            "iv_proxy": 0.0,
            "india_vix": self._fetch_vix(),
            "support": atm - (step * 5),
            "resistance": atm + (step * 5),
            "oi_change": {"call_change_oi": 0, "put_change_oi": 0, "bias": "neutral"},
            "swing_prediction": {
                "direction": "neutral",
                "confidence": 0,
                "option_action": "NO_TRADE",
                "signals": ["⚠️ Live NSE option chain data unavailable — do not trade based on this"],
                "horizon_days": 10,
            },
            "recommendation": {
                "action": "NO_TRADE",
                "reason": "⚠️ Live NSE data unavailable — Do NOT trade options based on this. Wait for live data.",
                "confidence": 0,
                "safe": False,
            },
            "records": [],
            "data_source": "fallback",
            "timestamp": datetime.now().isoformat(),
        }

    # ── Backwards compat (old endpoint called this) ────────────────────────────

    def get_nifty_options_chain(self, symbol: str = "NIFTY") -> dict:
        return self.get_full_analysis(symbol)

    def analyze_options_data(self, data: dict) -> dict:
        """Backwards compat — if already analysed, return as-is."""
        if "recommendation" in data:
            return data
        return data


# ── Index ticker bar data ──────────────────────────────────────────────────────

def get_index_quotes() -> list:
    """Returns live quotes for the index ticker bar."""
    import yfinance as yf

    indices = [
        {"symbol": "^NSEI",    "name": "Nifty 50",    "key": "NIFTY"},
        {"symbol": "^NSEBANK", "name": "BankNifty",   "key": "BANKNIFTY"},
        {"symbol": "^BSESN",   "name": "Sensex",      "key": "SENSEX"},
        {"symbol": "NIFTY_FIN_SERVICE.NS", "name": "FinNifty", "key": "FINNIFTY"},
        {"symbol": "NIFTY_MID_SELECT.NS",  "name": "MidCapNifty", "key": "MIDCPNIFTY"},
        {"symbol": "^INDIAVIX","name": "India VIX",   "key": "VIX"},
    ]

    results = []
    for idx in indices:
        try:
            ticker = yf.Ticker(idx["symbol"])
            hist = ticker.history(period="2d", interval="1d")
            if hist.empty:
                raise ValueError("no data")
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else float(hist["Close"].iloc[-1])
            current = float(hist["Close"].iloc[-1])
            chg = ((current - prev_close) / prev_close) * 100 if prev_close else 0

            # Try fast_info for intraday
            try:
                fi = ticker.fast_info
                if hasattr(fi, "last_price") and fi.last_price:
                    current = float(fi.last_price)
                    chg = ((current - prev_close) / prev_close) * 100
            except Exception:
                pass

            results.append({
                "key": idx["key"],
                "name": idx["name"],
                "price": round(current, 2),
                "change_pct": round(chg, 2),
                "is_up": chg >= 0,
            })
        except Exception as e:
            logger.debug("Index quote failed for %s: %s", idx["symbol"], e)
            results.append({
                "key": idx["key"],
                "name": idx["name"],
                "price": None,
                "change_pct": None,
                "is_up": None,
            })
    return results
