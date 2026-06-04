"""Gemini AI reasoning layer.

Combines scanner technicals, news, earnings, insider, analyst, and market
context into a compact structured verdict. Gemini enriches the scanner output;
the scanner remains the source of truth for prices, triggers, targets, and
stops.

Feature flag: ENABLE_GEMINI_AI=true
Requires: GEMINI_API_KEY in .env
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

ENABLE_GEMINI_AI = os.getenv("GEMINI_ENABLED", os.getenv("ENABLE_GEMINI_AI", "true")).lower() == "true"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT_SEC", "15"))
GEMINI_CACHE_TTL = int(os.getenv("GEMINI_CACHE_TTL_SEC", "600"))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "512"))
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.15"))
GEMINI_THINKING_BUDGET = int(os.getenv("GEMINI_THINKING_BUDGET", "0"))

_cache: Dict[str, tuple[float, Dict]] = {}
_last_success_by_symbol: Dict[str, tuple[float, Dict]] = {}
_cache_lock = threading.Lock()
_consecutive_429_errors = 0
_cooldown_until = 0.0


def _cached(key: str) -> Dict | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[0] < GEMINI_CACHE_TTL:
            return entry[1]
    return None


def _set_cache(key: str, data: Dict):
    with _cache_lock:
        _cache[key] = (time.time(), data)


def _set_last_success(symbol: str, data: Dict):
    with _cache_lock:
        _last_success_by_symbol[symbol.upper()] = (time.time(), dict(data))


def _stale_success(symbol: str, reason: str) -> Dict | None:
    with _cache_lock:
        entry = _last_success_by_symbol.get(symbol.upper())
    if not entry:
        return None
    age_sec = int(time.time() - entry[0])
    data = dict(entry[1])
    data["aiStale"] = True
    data["aiStatus"] = f"stale_after_{reason}"
    data["aiStaleAgeSec"] = age_sec
    return data


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
    Send scanner facts to Gemini and return a structured enrichment verdict.
    """
    cache_key = _cache_key(symbol, technical, news, earnings, insider, recommendations, market_context)
    cached = _cached(cache_key)
    if cached:
        return cached

    if not ENABLE_GEMINI_AI or not GEMINI_API_KEY:
        return _unavailable("gemini_disabled_or_no_key")

    with _cache_lock:
        if time.time() < _cooldown_until:
            return _stale_success(symbol, "gemini_in_cooldown") or _unavailable("gemini_in_cooldown")

    try:
        prompt = _build_prompt(symbol, technical, news, earnings, insider, recommendations, market_context)
        response = _call_gemini(prompt)
        if not response or not response.get("text"):
            reason = (response or {}).get("errorReason", "gemini_api_failed")
            return _stale_success(symbol, reason) or _unavailable(reason)

        result = _parse_response(response["text"], symbol)
        result["aiModel"] = GEMINI_MODEL
        result["aiFinishReason"] = response.get("finishReason")
        result["aiTokenUsage"] = _token_usage(response.get("usageMetadata"))
        if response.get("finishReason") == "MAX_TOKENS":
            logger.warning("[GEMINI] response truncated symbol=%s usage=%s", symbol, result["aiTokenUsage"])
        _set_cache(cache_key, result)
        _set_last_success(symbol, result)
        return result

    except Exception as exc:
        logger.warning("[GEMINI] analyze failed symbol=%s: %s", symbol, exc)
        return _stale_success(symbol, "error") or _unavailable(f"error: {exc}")


def _cache_key(
    symbol: str,
    technical: Dict,
    news: Dict,
    earnings: Dict,
    insider: Dict,
    recommendations: Dict,
    market_context: Dict | None,
) -> str:
    """Cache by the facts Gemini sees, not just by symbol."""
    fingerprint = {
        "symbol": symbol.upper(),
        "direction": technical.get("direction"),
        "confidenceBucket": int(float(technical.get("confidence") or 0) // 5) * 5,
        "setup": technical.get("setup_label") or technical.get("setup_type"),
        "trigger": _rounded_level(technical.get("entry_trigger") or technical.get("safe_entry_price")),
        "target": _rounded_level(technical.get("target_1") or technical.get("target_price")),
        "stop": _rounded_level(technical.get("stop_loss") or technical.get("invalidation")),
        "news": news.get("newsSentiment"),
        "earningsNear": earnings.get("earningsNear"),
        "insider": insider.get("signal"),
        "analyst": recommendations.get("consensus"),
        "market": (market_context or {}).get("marketMood"),
    }
    digest = hashlib.sha1(json.dumps(fingerprint, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    return f"gemini:{symbol.upper()}:{digest}"


def _rounded_level(value: Any) -> float | None:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _build_prompt(
    symbol: str,
    technical: Dict,
    news: Dict,
    earnings: Dict,
    insider: Dict,
    recommendations: Dict,
    market_context: Dict = None,
) -> str:
    """Build a compact structured prompt for Gemini."""
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
    reasons = [str(reason)[:140] for reason in (technical.get("reasons") or [])[:3]]

    news_sentiment = news.get("newsSentiment", "unknown")
    headlines = [str(h.get("title", ""))[:120] for h in (news.get("headlines") or [])[:2]]

    input_data = {
        "symbol": symbol.upper(),
        "technical": {
            "direction": direction,
            "setup": setup,
            "pattern": pattern,
            "confidence": confidence,
            "rsi": rsi,
            "relativeVolume": volume_ratio,
            "changePct": change_pct,
            "riskReward": rr,
            "entry": entry,
            "target": target,
            "stop": stop,
            "scannerAction": technical.get("action") or technical.get("recommended_action") or "WATCH",
            "reasons": reasons,
        },
        "context": {
            "newsSentiment": news_sentiment,
            "headlines": headlines,
            "earningsNear": earnings.get("earningsNear", False),
            "recentBeat": earnings.get("recentBeat"),
            "insiderSignal": insider.get("signal", "unknown"),
            "analystConsensus": recommendations.get("consensus", "unknown"),
            "buyPct": recommendations.get("buyPct", 0),
            "marketMood": (market_context or {}).get("marketMood", "unknown"),
        },
    }

    return (
        "Act as a senior Indian equity swing-trading analyst. Use only the supplied JSON; "
        "do not invent news or fundamentals. Keep output short and practical for a trading dashboard. "
        "Respect scanner levels; do not create new entry/target/stop prices. "
        "If marketMood is bearish, avoid aggressive fresh-buy wording unless evidence is very strong. "
        "Return valid compact JSON only with keys: "
        "conviction integer 0-100, action one of BUY/WATCH/AVOID/WAIT_FOR_PULLBACK, "
        "reason max 260 chars, risks array of 1-2 strings max 100 chars each, "
        "timeframe one of intraday/2-3 days/1 week/2 weeks, "
        "newsImpact positive/negative/neutral/unknown, tradePlan max 180 chars.\n"
        f"DATA={json.dumps(input_data, separators=(',', ':'), ensure_ascii=True)}"
    )


def _call_gemini(prompt: str) -> Dict[str, Any] | None:
    """Call Gemini API and return text plus small diagnostics."""
    global _consecutive_429_errors, _cooldown_until
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

    generation_config: Dict[str, Any] = {
        "temperature": GEMINI_TEMPERATURE,
        "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
        "responseMimeType": "application/json",
    }
    if "2.5" in GEMINI_MODEL:
        generation_config["thinkingConfig"] = {"thinkingBudget": GEMINI_THINKING_BUDGET}

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }

    try:
        resp = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT)
        
        if resp.status_code == 429:
            with _cache_lock:
                _consecutive_429_errors += 1
                if _consecutive_429_errors >= 3:
                    _cooldown_until = time.time() + 60.0
                    logger.warning("[GEMINI] 3 consecutive 429 errors. Pausing all calls for 60 seconds.")
                else:
                    logger.warning(f"[GEMINI] 429 quota exceeded. Error count: {_consecutive_429_errors}")
            return {"text": "", "errorReason": "gemini_http_429_quota_exceeded"}

        if resp.status_code != 200:
            logger.warning("[GEMINI] API returned %d: %s", resp.status_code, resp.text[:200])
            return {"text": "", "errorReason": f"gemini_http_{resp.status_code}"}
            
        with _cache_lock:
            _consecutive_429_errors = 0
            
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates:
            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts", [])
            if parts:
                return {
                    "text": parts[0].get("text", ""),
                    "finishReason": candidate.get("finishReason"),
                    "usageMetadata": data.get("usageMetadata"),
                }
    except Exception as exc:
        logger.warning("[GEMINI] API call failed: %s", _redact_api_key(str(exc)))
    return None


def _parse_response(text: str, symbol: str) -> Dict:
    """Parse Gemini's JSON response, with legacy line-format fallback."""
    result = {
        "symbol": symbol,
        "aiConviction": 50,
        "aiAction": "WATCH",
        "aiReason": "",
        "aiRisks": [],
        "aiTimeframe": "3-5 days",
        "aiNewsImpact": "unknown",
        "aiTradePlan": "",
        "aiStale": False,
        "aiStatus": "active",
        "rawResponse": text,
        "available": True,
    }

    parsed = _json_from_response(text)
    if isinstance(parsed, dict):
        result["aiConviction"] = _bounded_int(parsed.get("conviction"), 50)
        result["aiAction"] = _normalize_action(parsed.get("action"))
        result["aiReason"] = str(parsed.get("reason") or "").strip()[:500]
        result["aiRisks"] = _normalize_risks(parsed.get("risks"))
        result["aiTimeframe"] = str(parsed.get("timeframe") or "3-5 days").strip()
        result["aiNewsImpact"] = str(parsed.get("newsImpact") or "unknown").strip().lower()
        result["aiTradePlan"] = str(parsed.get("tradePlan") or "").strip()[:260]
        return result

    for line in text.strip().split("\n"):
        line = line.strip()
        upper = line.upper()
        if upper.startswith("CONVICTION:"):
            result["aiConviction"] = _bounded_int(line.split(":", 1)[1].strip().split()[0], 50)
        elif upper.startswith("ACTION:"):
            result["aiAction"] = _normalize_action(line.split(":", 1)[1].strip())
        elif upper.startswith("REASON:"):
            result["aiReason"] = line.split(":", 1)[1].strip()
        elif upper.startswith("RISKS:"):
            result["aiRisks"] = _normalize_risks(line.split(":", 1)[1].strip())
        elif upper.startswith("TIMEFRAME:"):
            result["aiTimeframe"] = line.split(":", 1)[1].strip()
        elif upper.startswith("NEWS_IMPACT:"):
            result["aiNewsImpact"] = line.split(":", 1)[1].strip().lower()
        elif upper.startswith("TRADE_PLAN:"):
            result["aiTradePlan"] = line.split(":", 1)[1].strip()

    return result


def _json_from_response(text: str) -> Dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _bounded_int(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return default


def _normalize_action(value: Any) -> str:
    action = str(value or "WATCH").strip().upper()
    allowed = {"BUY", "WATCH", "AVOID", "WAIT_FOR_PULLBACK"}
    return action if action in allowed else "WATCH"


def _normalize_risks(value: Any) -> list[str]:
    if isinstance(value, list):
        risks = [str(item).strip() for item in value if str(item).strip()]
    else:
        risks = [risk.strip() for risk in str(value or "").split("|") if risk.strip()]
    return risks[:2]


def _token_usage(metadata: Any) -> Dict[str, int]:
    if not isinstance(metadata, dict):
        return {}
    keys = ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount")
    return {key: int(metadata[key]) for key in keys if isinstance(metadata.get(key), int)}


def _redact_api_key(message: str) -> str:
    if not GEMINI_API_KEY:
        return message
    return message.replace(GEMINI_API_KEY, "<redacted>")


def gemini_runtime_status() -> Dict[str, Any]:
    with _cache_lock:
        cache_size = len(_cache)
        last_success_count = len(_last_success_by_symbol)
    return {
        "enabled": ENABLE_GEMINI_AI,
        "apiKeyConfigured": bool(GEMINI_API_KEY),
        "model": GEMINI_MODEL,
        "timeoutSec": GEMINI_TIMEOUT,
        "cacheTtlSec": GEMINI_CACHE_TTL,
        "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
        "temperature": GEMINI_TEMPERATURE,
        "thinkingBudget": GEMINI_THINKING_BUDGET,
        "cacheSize": cache_size,
        "lastSuccessCount": last_success_count,
    }


def _unavailable(reason: str) -> Dict:
    return {
        "aiConviction": None,
        "aiAction": None,
        "aiReason": None,
        "aiRisks": [],
        "aiTimeframe": None,
        "aiNewsImpact": None,
        "aiTradePlan": None,
        "aiModel": GEMINI_MODEL,
        "aiFinishReason": None,
        "aiTokenUsage": {},
        "aiStale": False,
        "aiStatus": reason,
        "rawResponse": None,
        "available": False,
        "reason": reason,
    }
