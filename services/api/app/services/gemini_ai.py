"""Gemini AI Reasoning Layer — combines technicals + news + fundamentals into smart analysis.
Feature flag: ENABLE_GEMINI_AI=true
Requires: GEMINI_API_KEY in .env
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict

import requests

logger = logging.getLogger(__name__)

ENABLE_GEMINI_AI = os.getenv("ENABLE_GEMINI_AI", "true").lower() == "true"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT_SEC", "15"))
GEMINI_CACHE_TTL = int(os.getenv("GEMINI_CACHE_TTL_SEC", "600"))  # 10 min

_cache: Dict[str, tuple[float, Dict]] = {}
_cache_lock = threading.Lock()


def _cached(key: str) -> Dict | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[0] < GEMINI_CACHE_TTL:
            return entry[1]
    return None


def _set_cache(key: str, data: Dict):
    with _cache_lock:
        _cache[key] = (time.time(), data)


def analyze_stock_with_ai(
    symbol: str,
    technical: Dict,
    news: Dict,
    earnings: Dict,
    insider: Dict,
    recommendations: Dict,
    market_context: Dict = None,
) -> Dict:
    """
    Send all stock data to Gemini AI for reasoning.
    Returns structured analysis with conviction score, explanation, and action.
    """
    cache_key = f"gemini:{symbol}"
    cached = _cached(cache_key)
    if cached:
        return cached

    if not ENABLE_GEMINI_AI or not GEMINI_API_KEY:
        return _unavailable("gemini_disabled_or_no_key")

    try:
        prompt = _build_prompt(symbol, technical, news, earnings, insider, recommendations, market_context)
        response = _call_gemini(prompt)
        if not response:
            return _unavailable("gemini_api_failed")

        result = _parse_response(response, symbol)
        _set_cache(cache_key, result)
        return result

    except Exception as exc:
        logger.warning("[GEMINI] analyze failed symbol=%s: %s", symbol, exc)
        return _unavailable(f"error: {exc}")


def _build_prompt(
    symbol: str,
    technical: Dict,
    news: Dict,
    earnings: Dict,
    insider: Dict,
    recommendations: Dict,
    market_context: Dict = None,
) -> str:
    """Build a structured prompt for Gemini."""

    # Technical summary
    direction = technical.get("direction", "neutral")
    confidence = technical.get("confidence", 0)
    rsi = technical.get("rsi", 50)
    volume_ratio = technical.get("relative_volume", 1)
    change_pct = technical.get("change_pct", 0)
    setup = technical.get("setup_label", "")
    rr = technical.get("risk_reward", 0)
    entry = technical.get("entry_trigger") or technical.get("current_price", 0)
    target = technical.get("target_1") or technical.get("target_price", 0)
    stop = technical.get("stop_loss") or technical.get("invalidation", 0)
    pattern = technical.get("chart_pattern") or technical.get("setup_type", "")
    reasons = technical.get("reasons", [])[:3]

    # News summary
    news_sentiment = news.get("newsSentiment", "unknown")
    headlines = [h.get("title", "") for h in news.get("headlines", [])[:3]]

    # Earnings
    earnings_near = earnings.get("earningsNear", False)
    recent_beat = earnings.get("recentBeat")

    # Insider
    insider_signal = insider.get("signal", "unknown")

    # Analyst
    consensus = recommendations.get("consensus", "unknown")
    buy_pct = recommendations.get("buyPct", 0)

    # Market
    market_mood = (market_context or {}).get("marketMood", "unknown")

    prompt = f"""You are a senior Indian stock market analyst. Analyze this stock and give a clear recommendation.

STOCK: {symbol}

TECHNICAL ANALYSIS:
- Direction: {direction}
- Setup: {setup}
- Pattern: {pattern}
- Confidence: {confidence}%
- RSI: {rsi}
- Volume ratio: {volume_ratio}x
- Today's change: {change_pct}%
- Risk/Reward: 1:{rr}
- Entry: ₹{entry} | Target: ₹{target} | Stop: ₹{stop}
- Reasons: {'; '.join(reasons)}

NEWS SENTIMENT: {news_sentiment}
Headlines: {'; '.join(headlines) if headlines else 'No recent news'}

EARNINGS: {'Near (within 7 days)' if earnings_near else 'Not near'}
Recent result: {'Beat estimates' if recent_beat else 'Missed' if recent_beat is False else 'Unknown'}

INSIDER ACTIVITY: {insider_signal}

ANALYST CONSENSUS: {consensus} ({buy_pct}% buy)

MARKET MOOD (Nifty): {market_mood}

INSTRUCTIONS:
1. Give a CONVICTION SCORE (0-100). Above 75 = strong buy candidate.
2. Give ACTION: BUY / WATCH / AVOID / WAIT_FOR_PULLBACK
3. Give a 2-3 line REASON explaining why.
4. List 1-2 RISKS.
5. Give TIMEFRAME: intraday / 2-3 days / 1 week / 2 weeks
6. If news is negative or earnings are near, warn about it.
7. If market mood is bearish, be cautious about fresh buys.

FORMAT your response EXACTLY like this:
CONVICTION: [number]
ACTION: [action]
REASON: [explanation]
RISKS: [risk1] | [risk2]
TIMEFRAME: [timeframe]
NEWS_IMPACT: [positive/negative/neutral/unknown]
"""
    return prompt


def _call_gemini(prompt: str) -> str | None:
    """Call Gemini API and return text response."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 300,
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("[GEMINI] API returned %d: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                return parts[0].get("text", "")
    except Exception as exc:
        logger.warning("[GEMINI] API call failed: %s", exc)
    return None


def _parse_response(text: str, symbol: str) -> Dict:
    """Parse Gemini's structured response into a dict."""
    lines = text.strip().split("\n")
    result = {
        "symbol": symbol,
        "aiConviction": 50,
        "aiAction": "WATCH",
        "aiReason": "",
        "aiRisks": [],
        "aiTimeframe": "3-5 days",
        "aiNewsImpact": "unknown",
        "rawResponse": text,
        "available": True,
    }

    for line in lines:
        line = line.strip()
        if line.startswith("CONVICTION:"):
            try:
                result["aiConviction"] = int(line.split(":", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif line.startswith("ACTION:"):
            result["aiAction"] = line.split(":", 1)[1].strip().upper()
        elif line.startswith("REASON:"):
            result["aiReason"] = line.split(":", 1)[1].strip()
        elif line.startswith("RISKS:"):
            risks = line.split(":", 1)[1].strip()
            result["aiRisks"] = [r.strip() for r in risks.split("|") if r.strip()]
        elif line.startswith("TIMEFRAME:"):
            result["aiTimeframe"] = line.split(":", 1)[1].strip()
        elif line.startswith("NEWS_IMPACT:"):
            result["aiNewsImpact"] = line.split(":", 1)[1].strip().lower()

    return result


def _unavailable(reason: str) -> Dict:
    return {
        "aiConviction": None,
        "aiAction": None,
        "aiReason": None,
        "aiRisks": [],
        "aiTimeframe": None,
        "aiNewsImpact": None,
        "rawResponse": None,
        "available": False,
        "reason": reason,
    }
