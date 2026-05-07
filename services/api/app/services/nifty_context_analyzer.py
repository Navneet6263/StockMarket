"""Nifty Context Analyzer — FII/DII data + Options PCR + Max Pain.
All data from public NSE endpoints. Returns 'unknown' gracefully when unavailable.
Feature flag: ENABLE_FII_DII=true, ENABLE_PCR=true
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Dict

import requests

logger = logging.getLogger(__name__)

ENABLE_FII_DII = os.getenv("ENABLE_FII_DII", "true").lower() == "true"
ENABLE_PCR     = os.getenv("ENABLE_PCR", "true").lower() == "true"
NSE_TIMEOUT    = int(os.getenv("NSE_TIMEOUT_SEC", "8"))

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
}

_NSE_SESSION: requests.Session | None = None


def _nse_session() -> requests.Session:
    global _NSE_SESSION
    if _NSE_SESSION is None:
        _NSE_SESSION = requests.Session()
        _NSE_SESSION.headers.update(_NSE_HEADERS)
        try:
            _NSE_SESSION.get("https://www.nseindia.com", timeout=NSE_TIMEOUT)
        except Exception:
            pass
    return _NSE_SESSION


def _get(url: str) -> dict | None:
    try:
        resp = _nse_session().get(url, timeout=NSE_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.debug("[NIFTY_CTX] GET failed url=%s err=%s", url, exc)
    return None


# ── FII / DII ─────────────────────────────────────────────────────────────────
def fetch_fii_dii() -> Dict:
    """Fetch latest FII/DII net buy/sell from NSE."""
    if not ENABLE_FII_DII:
        return _unknown_fii("fii_dii_disabled")
    try:
        data = _get("https://www.nseindia.com/api/fiidiiTradeReact")
        if not data:
            return _unknown_fii("nse_api_unavailable")

        # NSE returns list; last entry = latest date
        rows = data if isinstance(data, list) else data.get("data", [])
        if not rows:
            return _unknown_fii("empty_response")

        latest = rows[-1]
        fii_net = float(latest.get("fiiNet") or latest.get("FII_NET") or 0)
        dii_net = float(latest.get("diiNet") or latest.get("DII_NET") or 0)
        date_str = latest.get("date") or latest.get("DATE") or ""

        if fii_net > 500 and dii_net > 0:
            mood = "strongly_bullish"
        elif fii_net > 0 or dii_net > 500:
            mood = "bullish"
        elif fii_net < -500 and dii_net < 0:
            mood = "strongly_bearish"
        elif fii_net < 0:
            mood = "bearish"
        else:
            mood = "neutral"

        logger.debug("[FII_DII] fii_net=%.0f dii_net=%.0f mood=%s", fii_net, dii_net, mood)
        return {
            "fiiNet": round(fii_net, 2),
            "diiNet": round(dii_net, 2),
            "combinedNet": round(fii_net + dii_net, 2),
            "mood": mood,
            "date": date_str,
            "available": True,
        }
    except Exception as exc:
        logger.warning("[FII_DII] fetch failed: %s", exc)
        return _unknown_fii(f"error: {exc}")


def _unknown_fii(reason: str) -> Dict:
    return {"fiiNet": None, "diiNet": None, "combinedNet": None,
            "mood": "unknown", "date": None, "available": False, "reason": reason}


# ── PCR + Max Pain ────────────────────────────────────────────────────────────
def fetch_pcr_max_pain(symbol: str = "NIFTY") -> Dict:
    """Fetch Put-Call Ratio and Max Pain from NSE options chain."""
    if not ENABLE_PCR:
        return _unknown_pcr("pcr_disabled")
    try:
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol.upper()}"
        data = _get(url)
        if not data:
            return _unknown_pcr("nse_api_unavailable")

        records = data.get("records", {})
        expiry_dates = records.get("expiryDates", [])
        if not expiry_dates:
            return _unknown_pcr("no_expiry_dates")

        nearest_expiry = expiry_dates[0]
        all_data = records.get("data", [])

        total_ce_oi = 0
        total_pe_oi = 0
        pain_map: dict[float, float] = {}

        for item in all_data:
            if item.get("expiryDate") != nearest_expiry:
                continue
            strike = float(item.get("strikePrice", 0))
            ce = item.get("CE", {})
            pe = item.get("PE", {})
            ce_oi = float(ce.get("openInterest", 0))
            pe_oi = float(pe.get("openInterest", 0))
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            pain_map[strike] = ce_oi + pe_oi

        pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 0.0
        max_pain = min(pain_map, key=pain_map.get) if pain_map else None

        if pcr >= 1.3:
            pcr_signal = "bullish"       # heavy put writing = market expects up
        elif pcr <= 0.7:
            pcr_signal = "bearish"       # heavy call writing = market expects down
        else:
            pcr_signal = "neutral"

        logger.debug("[PCR] symbol=%s pcr=%.3f max_pain=%s signal=%s", symbol, pcr, max_pain, pcr_signal)
        return {
            "pcr": pcr,
            "pcrSignal": pcr_signal,
            "maxPain": max_pain,
            "totalCeOi": int(total_ce_oi),
            "totalPeOi": int(total_pe_oi),
            "nearestExpiry": nearest_expiry,
            "available": True,
        }
    except Exception as exc:
        logger.warning("[PCR] fetch failed symbol=%s: %s", symbol, exc)
        return _unknown_pcr(f"error: {exc}")


def _unknown_pcr(reason: str) -> Dict:
    return {"pcr": None, "pcrSignal": "unknown", "maxPain": None,
            "totalCeOi": None, "totalPeOi": None, "nearestExpiry": None,
            "available": False, "reason": reason}


# ── Combined context ──────────────────────────────────────────────────────────
def get_nifty_context(symbol: str = "NIFTY") -> Dict:
    """Return combined FII/DII + PCR context. Safe — never crashes."""
    fii_dii = fetch_fii_dii()
    pcr = fetch_pcr_max_pain(symbol)

    # Combine moods
    moods = [fii_dii.get("mood", "unknown"), pcr.get("pcrSignal", "unknown")]
    bullish_count = sum(1 for m in moods if "bullish" in m)
    bearish_count = sum(1 for m in moods if "bearish" in m)

    if bullish_count >= 2:
        combined_mood = "bullish"
    elif bearish_count >= 2:
        combined_mood = "bearish"
    elif bullish_count > bearish_count:
        combined_mood = "leaning_bullish"
    elif bearish_count > bullish_count:
        combined_mood = "leaning_bearish"
    else:
        combined_mood = "neutral"

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "combinedMood": combined_mood,
        "fiiDii": fii_dii,
        "optionsPcr": pcr,
    }
