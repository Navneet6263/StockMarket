"""Finnhub Data Layer — News, Earnings, Insider Activity, Analyst Ratings.
Feature flag: ENABLE_FINNHUB=true
Requires: FINNHUB_API_KEY in .env
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

ENABLE_FINNHUB = os.getenv("ENABLE_FINNHUB", "true").lower() == "true"
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE = "https://finnhub.io/api/v1"
FINNHUB_TIMEOUT = int(os.getenv("FINNHUB_TIMEOUT_SEC", "8"))
FINNHUB_CACHE_TTL = int(os.getenv("FINNHUB_CACHE_TTL_SEC", "300"))  # 5 min cache

_cache: Dict[str, tuple[float, Dict]] = {}
_cache_lock = threading.Lock()


def _cached(key: str) -> Dict | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[0] < FINNHUB_CACHE_TTL:
            return entry[1]
    return None


def _set_cache(key: str, data: Dict):
    with _cache_lock:
        _cache[key] = (time.time(), data)


def _get(endpoint: str, params: Dict = None) -> Dict | List | None:
    if not ENABLE_FINNHUB or not FINNHUB_API_KEY:
        return None
    try:
        p = {"token": FINNHUB_API_KEY, **(params or {})}
        resp = requests.get(f"{FINNHUB_BASE}{endpoint}", params=p, timeout=FINNHUB_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        logger.debug("[FINNHUB] %s returned %d", endpoint, resp.status_code)
    except Exception as exc:
        logger.debug("[FINNHUB] %s failed: %s", endpoint, exc)
    return None


def _nse_to_finnhub(symbol: str) -> str:
    """Convert NSE symbol to Finnhub format (NSE:SYMBOL)."""
    clean = symbol.upper().replace(".NS", "").replace(" ", "")
    return f"NSE:{clean}"


# ── News Sentiment ────────────────────────────────────────────────────────────

def fetch_news_sentiment(symbol: str) -> Dict:
    """Fetch company news and compute sentiment score."""
    cache_key = f"news:{symbol}"
    cached = _cached(cache_key)
    if cached:
        return cached

    if not ENABLE_FINNHUB or not FINNHUB_API_KEY:
        return _empty_news("finnhub_disabled_or_no_key")

    finnhub_sym = _nse_to_finnhub(symbol)
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    data = _get("/company-news", {"symbol": finnhub_sym, "from": week_ago, "to": today})
    if not data or not isinstance(data, list):
        # Try without NSE prefix
        data = _get("/company-news", {"symbol": symbol.upper(), "from": week_ago, "to": today})

    if not data or not isinstance(data, list):
        result = _empty_news("no_news_found")
        _set_cache(cache_key, result)
        return result

    headlines = []
    positive = 0
    negative = 0
    neutral = 0

    for article in data[:15]:
        headline = article.get("headline", "")
        sentiment = article.get("sentiment") or _simple_sentiment(headline)
        headlines.append({
            "title": headline,
            "source": article.get("source", ""),
            "datetime": article.get("datetime", 0),
            "sentiment": sentiment,
            "url": article.get("url", ""),
        })
        if sentiment == "positive":
            positive += 1
        elif sentiment == "negative":
            negative += 1
        else:
            neutral += 1

    total = positive + negative + neutral or 1
    sentiment_score = round((positive - negative) / total, 3)

    if sentiment_score >= 0.3:
        overall = "bullish"
    elif sentiment_score <= -0.3:
        overall = "bearish"
    else:
        overall = "neutral"

    result = {
        "newsSentiment": overall,
        "sentimentScore": sentiment_score,
        "positiveCount": positive,
        "negativeCount": negative,
        "neutralCount": neutral,
        "totalArticles": len(data),
        "headlines": headlines[:5],
        "available": True,
    }
    _set_cache(cache_key, result)
    return result


def _simple_sentiment(headline: str) -> str:
    """VADER sentiment analysis for Finnhub headlines."""
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()
        scores = analyzer.polarity_scores(headline)
        compound = scores['compound']
        if compound >= 0.05:
            return "positive"
        elif compound <= -0.05:
            return "negative"
        else:
            return "neutral"
    except ImportError:
        logger.warning("vaderSentiment not installed. Falling back to keyword sentiment.")
        h = headline.lower()
        bullish = ["surge", "jump", "rally", "beat", "profit", "growth", "upgrade", "buy", "strong", "record", "high"]
        bearish = ["fall", "drop", "crash", "loss", "downgrade", "sell", "weak", "cut", "decline", "miss"]
        if any(w in h for w in bullish):
            return "positive"
        if any(w in h for w in bearish):
            return "negative"
        return "neutral"


# ── Earnings Calendar ─────────────────────────────────────────────────────────

def fetch_earnings(symbol: str) -> Dict:
    """Check upcoming/recent earnings for a stock."""
    cache_key = f"earnings:{symbol}"
    cached = _cached(cache_key)
    if cached:
        return cached

    if not ENABLE_FINNHUB or not FINNHUB_API_KEY:
        return {"available": False, "reason": "disabled"}

    finnhub_sym = _nse_to_finnhub(symbol)
    today = datetime.now().strftime("%Y-%m-%d")
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    data = _get("/calendar/earnings", {"symbol": finnhub_sym, "from": past, "to": future})
    if not data:
        data = _get("/calendar/earnings", {"symbol": symbol.upper(), "from": past, "to": future})

    earnings = (data or {}).get("earningsCalendar", [])
    if not earnings:
        result = {"available": False, "reason": "no_earnings_data", "upcoming": None, "recent": None}
        _set_cache(cache_key, result)
        return result

    upcoming = [e for e in earnings if e.get("date", "") >= today]
    recent = [e for e in earnings if e.get("date", "") < today]

    result = {
        "available": True,
        "upcoming": upcoming[0] if upcoming else None,
        "recent": recent[0] if recent else None,
        "earningsNear": bool(upcoming and upcoming[0].get("date", "") <= (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")),
        "recentBeat": recent[0].get("epsActual", 0) > recent[0].get("epsEstimate", 0) if recent else None,
    }
    _set_cache(cache_key, result)
    return result


# ── Insider Activity ──────────────────────────────────────────────────────────

def fetch_insider_activity(symbol: str) -> Dict:
    """Fetch insider/promoter transactions."""
    cache_key = f"insider:{symbol}"
    cached = _cached(cache_key)
    if cached:
        return cached

    if not ENABLE_FINNHUB or not FINNHUB_API_KEY:
        return {"available": False, "reason": "disabled"}

    finnhub_sym = _nse_to_finnhub(symbol)
    data = _get("/stock/insider-transactions", {"symbol": finnhub_sym})
    if not data:
        data = _get("/stock/insider-transactions", {"symbol": symbol.upper()})

    transactions = (data or {}).get("data", [])
    if not transactions:
        result = {"available": False, "reason": "no_insider_data", "signal": "unknown"}
        _set_cache(cache_key, result)
        return result

    recent = transactions[:10]
    buys = sum(1 for t in recent if (t.get("transactionType") or "").lower() in {"buy", "p-purchase", "acquisition"})
    sells = sum(1 for t in recent if (t.get("transactionType") or "").lower() in {"sell", "s-sale", "disposal"})

    if buys > sells + 1:
        signal = "insider_buying"
    elif sells > buys + 1:
        signal = "insider_selling"
    else:
        signal = "neutral"

    result = {
        "available": True,
        "signal": signal,
        "recentBuys": buys,
        "recentSells": sells,
        "totalTransactions": len(recent),
        "latestTransaction": recent[0] if recent else None,
    }
    _set_cache(cache_key, result)
    return result


# ── Analyst Recommendations ───────────────────────────────────────────────────

def fetch_recommendations(symbol: str) -> Dict:
    """Fetch analyst buy/sell/hold recommendations."""
    cache_key = f"reco:{symbol}"
    cached = _cached(cache_key)
    if cached:
        return cached

    if not ENABLE_FINNHUB or not FINNHUB_API_KEY:
        return {"available": False, "reason": "disabled"}

    finnhub_sym = _nse_to_finnhub(symbol)
    data = _get("/stock/recommendation", {"symbol": finnhub_sym})
    if not data:
        data = _get("/stock/recommendation", {"symbol": symbol.upper()})

    if not data or not isinstance(data, list) or not data:
        result = {"available": False, "reason": "no_recommendations"}
        _set_cache(cache_key, result)
        return result

    latest = data[0]
    total = (latest.get("buy", 0) + latest.get("hold", 0) + latest.get("sell", 0) +
             latest.get("strongBuy", 0) + latest.get("strongSell", 0)) or 1
    buy_pct = round((latest.get("buy", 0) + latest.get("strongBuy", 0)) / total * 100, 1)

    if buy_pct >= 60:
        consensus = "strong_buy"
    elif buy_pct >= 40:
        consensus = "buy"
    elif buy_pct <= 20:
        consensus = "sell"
    else:
        consensus = "hold"

    result = {
        "available": True,
        "consensus": consensus,
        "buyPct": buy_pct,
        "strongBuy": latest.get("strongBuy", 0),
        "buy": latest.get("buy", 0),
        "hold": latest.get("hold", 0),
        "sell": latest.get("sell", 0),
        "strongSell": latest.get("strongSell", 0),
        "period": latest.get("period", ""),
    }
    _set_cache(cache_key, result)
    return result


# ── Combined Intelligence ─────────────────────────────────────────────────────

def get_stock_intelligence(symbol: str) -> Dict:
    """Get all Finnhub data for a stock in one call."""
    return {
        "symbol": symbol.upper(),
        "news": fetch_news_sentiment(symbol),
        "earnings": fetch_earnings(symbol),
        "insider": fetch_insider_activity(symbol),
        "recommendations": fetch_recommendations(symbol),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def _empty_news(reason: str) -> Dict:
    return {
        "newsSentiment": "unknown",
        "sentimentScore": None,
        "positiveCount": 0,
        "negativeCount": 0,
        "neutralCount": 0,
        "totalArticles": 0,
        "headlines": [],
        "available": False,
        "reason": reason,
    }
