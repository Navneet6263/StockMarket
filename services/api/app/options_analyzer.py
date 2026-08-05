"""
options_analyzer.py — Strict CE/PE rule engine.

Data sources (in priority order):
  1. nsepython  — NSE option chain (free, no auth)
  2. NSE public API with cookie session
  3. yfinance fallback (spot price only — labelled, no trade recommendation)

Accuracy rules:
  - rule alignment < 70/100 → NO_TRADE
  - fallback data     → NO_TRADE (clearly labelled ⚠️)
  - Only BUY CE / BUY PE — never neutral "hold" when unclear
  - direction comes from price trend; aggregate OI/PCR/VIX are context only
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from app.services.trade_plan import assess_structural_plan

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
    "MIDCPNIFTY": {"yf": "NIFTY_MID_SELECT.NS", "strike_step": 25, "label": "Midcap Nifty"},
}

CONFIDENCE_THRESHOLD = 70  # Below this → NO_TRADE always
SUPPORTED_INDEX_SYMBOLS = frozenset(INDEX_META)
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
CHAIN_MAX_AGE = timedelta(minutes=10)
MIN_EXPIRY_CALENDAR_DAYS = 16
MIN_RISK_REWARD = 1.5
MAX_OPTION_SPREAD_PCT = 5.0
MIN_OPTION_VOLUME = 100
MIN_OPTION_OPEN_INTEREST = 500


def _normalize_index_symbol(symbol: object) -> str | None:
    """Normalize a public symbol without ever defaulting to another index."""

    normalized = str(symbol or "").strip().upper()
    return normalized if normalized in SUPPORTED_INDEX_SYMBOLS else None


def _as_ist(value: datetime | None = None) -> datetime:
    current = value or datetime.now(IST)
    if current.tzinfo is None:
        return current.replace(tzinfo=IST)
    return current.astimezone(IST)


def _previous_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _market_state(as_of: datetime | None = None) -> str:
    current = _as_ist(as_of)
    if current.weekday() >= 5:
        return "closed"
    if current.time() < MARKET_OPEN:
        return "pre_open"
    if current.time() <= MARKET_CLOSE:
        return "live"
    return "post_close"


def _latest_completed_session_date(as_of: datetime | None = None) -> date:
    current = _as_ist(as_of)
    if current.weekday() < 5 and current.time() > MARKET_CLOSE:
        return current.date()
    return _previous_weekday(current.date())


def _parse_exchange_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_ist(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=IST)

    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    normalized = raw_value.replace("Z", "+00:00")
    try:
        return _as_ist(datetime.fromisoformat(normalized))
    except ValueError:
        pass
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d-%b-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(raw_value, fmt)
            return parsed.replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _assess_chain_freshness(
    timestamp_value: object,
    *,
    as_of: datetime | None = None,
) -> dict:
    """Fail-closed freshness check for an NSE option-chain snapshot."""

    current = _as_ist(as_of)
    source_time = _parse_exchange_timestamp(timestamp_value)
    state = _market_state(current)
    base = {
        "available": source_time is not None,
        "fresh": False,
        "market_state": state,
        "source_timestamp": source_time.isoformat() if source_time else None,
        "checked_at": current.isoformat(),
        "reason": "chain_timestamp_missing_or_invalid",
    }
    if source_time is None:
        return base

    age = current - source_time
    base["age_seconds"] = round(age.total_seconds(), 1)
    if age < -timedelta(minutes=2):
        base["reason"] = "chain_timestamp_is_in_future"
        return base

    if state == "live":
        fresh = source_time.date() == current.date() and age <= CHAIN_MAX_AGE
        base.update(
            {
                "fresh": fresh,
                "reason": "fresh_live_snapshot" if fresh else "stale_live_chain_snapshot",
                "expected_session_date": current.date().isoformat(),
            }
        )
        return base

    expected_date = _latest_completed_session_date(current)
    near_close = source_time.time() >= time(15, 15)
    fresh = source_time.date() == expected_date and near_close and source_time <= current
    base.update(
        {
            "fresh": fresh,
            "reason": "fresh_latest_completed_session" if fresh else "chain_not_latest_completed_session",
            "expected_session_date": expected_date.isoformat(),
        }
    )
    return base


def _coerce_bar_datetime(value: object) -> datetime | None:
    if hasattr(value, "to_pydatetime"):
        try:
            value = value.to_pydatetime()
        except Exception:
            return None
    if isinstance(value, datetime):
        return _as_ist(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=IST)
    return _parse_exchange_timestamp(value)


def _coerce_bar_date(value: object) -> date | None:
    parsed = _coerce_bar_datetime(value)
    return parsed.date() if parsed else None


def _assess_price_bar_freshness(
    latest_bar: object,
    *,
    as_of: datetime | None = None,
) -> dict:
    """Validate that technicals use the latest *completed* weekday session."""

    current = _as_ist(as_of)
    bar_date = _coerce_bar_date(latest_bar)
    expected_date = _latest_completed_session_date(current)
    fresh = bar_date == expected_date
    return {
        "available": bar_date is not None,
        "fresh": fresh,
        "source_timestamp": bar_date.isoformat() if bar_date else None,
        "expected_session_date": expected_date.isoformat(),
        "checked_at": current.isoformat(),
        "reason": "fresh_latest_completed_price_bar" if fresh else "stale_or_missing_completed_price_bar",
    }


def _assess_vix_bar_freshness(
    latest_bar: object,
    *,
    as_of: datetime | None = None,
) -> dict:
    """VIX must be current-session during live hours, latest close otherwise."""

    current = _as_ist(as_of)
    observed_at = _coerce_bar_datetime(latest_bar)
    bar_date = observed_at.date() if observed_at else None
    state = _market_state(current)
    expected_date = current.date() if state in {"live", "post_close"} else _latest_completed_session_date(current)
    if state == "live":
        age = current - observed_at if observed_at else None
        fresh = bool(
            observed_at
            and bar_date == expected_date
            and age is not None
            and -timedelta(minutes=2) <= age <= CHAIN_MAX_AGE
        )
    else:
        age = current - observed_at if observed_at else None
        fresh = bar_date == expected_date
    return {
        "available": bar_date is not None,
        "fresh": fresh,
        "market_state": state,
        "source_timestamp": observed_at.isoformat() if observed_at else None,
        "expected_session_date": expected_date.isoformat(),
        "checked_at": current.isoformat(),
        "age_seconds": round(age.total_seconds(), 1) if age is not None else None,
        "reason": "fresh_vix_bar" if fresh else "stale_or_missing_vix_bar",
    }


def _latest_confirmed_pivot(
    values: list[float],
    *,
    direction: str,
    spot: float,
) -> float | None:
    """Return the latest observed 3-bar pivot that invalidates the setup."""

    cleaned: list[float] = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = math.nan
        cleaned.append(numeric)
    if len(cleaned) < 3:
        return None

    for index in range(len(cleaned) - 2, 0, -1):
        left, middle, right = cleaned[index - 1 : index + 2]
        if not all(math.isfinite(item) and item > 0 for item in (left, middle, right)):
            continue
        if direction == "bullish" and middle <= left and middle < right and middle < spot:
            return round(middle, 2)
        if direction == "bearish" and middle >= left and middle > right and middle > spot:
            return round(middle, 2)
    return None


def _positive_number(value: object) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return numeric if math.isfinite(numeric) and numeric > 0 else 0.0


def _select_liquid_contract(
    records: list,
    *,
    expiry_date: str,
    option_action: str,
    spot: float,
) -> dict | None:
    """Choose the nearest actually quoted/liquid contract on the chosen side."""

    side = "CE" if option_action == "BUY_CE" else "PE" if option_action == "BUY_PE" else None
    if side is None:
        return None

    candidates: list[dict] = []
    for row in records or []:
        if str(row.get("expiryDate") or "").strip() != expiry_date:
            continue
        strike = _positive_number(row.get("strikePrice"))
        leg = row.get(side)
        if not strike or not isinstance(leg, dict):
            continue
        if str(leg.get("expiryDate") or "").strip() != expiry_date:
            continue

        ltp = _positive_number(leg.get("lastPrice"))
        volume = _positive_number(leg.get("totalTradedVolume"))
        open_interest = _positive_number(leg.get("openInterest"))
        if (
            not ltp
            or volume < MIN_OPTION_VOLUME
            or open_interest < MIN_OPTION_OPEN_INTEREST
        ):
            continue

        bid = _positive_number(leg.get("bidprice", leg.get("bidPrice")))
        ask = _positive_number(leg.get("askPrice", leg.get("askprice")))
        if not bid or not ask or ask <= bid:
            continue
        bid_qty_present = "bidQty" in leg or "bidqty" in leg
        ask_qty_present = "askQty" in leg or "askqty" in leg
        if bid_qty_present or ask_qty_present:
            bid_qty = _positive_number(leg.get("bidQty", leg.get("bidqty")))
            ask_qty = _positive_number(leg.get("askQty", leg.get("askqty")))
            if not bid_qty or not ask_qty:
                continue
        midpoint = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / midpoint) * 100 if midpoint else math.inf
        if spread_pct > MAX_OPTION_SPREAD_PCT:
            continue

        candidates.append(
            {
                "strike": int(strike),
                "option_type": side,
                "expiry": expiry_date,
                "last_price": round(ltp, 2),
                "volume": int(volume),
                "open_interest": int(open_interest),
                "bid": round(bid, 2) if bid else None,
                "ask": round(ask, 2) if ask else None,
                "spread_pct": round(spread_pct, 2),
                "quote_quality": "verified_two_sided_liquid_quote",
            }
        )

    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            abs(item["strike"] - spot),
            -item["volume"],
            -item["open_interest"],
        ),
    )


def _select_swing_expiry(
    expiry_dates: list,
    minimum_calendar_days: int = MIN_EXPIRY_CALENDAR_DAYS,
    as_of: date | None = None,
) -> str | None:
    """Choose an actual chain expiry with a conservative swing/theta buffer."""

    today = as_of or datetime.now().date()
    parsed: list[tuple[datetime, str]] = []
    for raw_value in expiry_dates or []:
        value = str(raw_value or "").strip()
        try:
            parsed_date = datetime.strptime(value, "%d-%b-%Y")
        except ValueError:
            continue
        if (parsed_date.date() - today).days >= minimum_calendar_days:
            parsed.append((parsed_date, value))
    return min(parsed, key=lambda item: item[0])[1] if parsed else None


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
            payload = entry[1]
            if payload.get("recommendation", {}).get("action") in {"BUY_CE", "BUY_PE"}:
                timestamps = payload.get("source_timestamps") or {}
                if not _assess_chain_freshness(timestamps.get("option_chain"))["fresh"]:
                    return None
                if not _assess_price_bar_freshness(timestamps.get("price_history"))["fresh"]:
                    return None
                if not _assess_vix_bar_freshness(timestamps.get("vix"))["fresh"]:
                    return None
            return payload
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

    def _blocked_analysis(
        self,
        symbol: str,
        reason: str,
        *,
        data_source: str,
        data_quality: dict | None = None,
        spot: float | None = None,
        selected_expiry: str | None = None,
    ) -> dict:
        normalized = _normalize_index_symbol(symbol)
        label = INDEX_META[normalized]["label"] if normalized else str(symbol or "Unknown")
        return {
            "symbol": normalized or str(symbol or "").strip().upper(),
            "label": label,
            "spot_price": round(float(spot), 2) if _positive_number(spot) else None,
            "atm_strike": None,
            "selected_expiry": selected_expiry,
            "pcr": None,
            "max_pain": None,
            "iv_proxy": None,
            "india_vix": None,
            "support": None,
            "resistance": None,
            "oi_change": {
                "available": False,
                "call_change_oi": None,
                "put_change_oi": None,
                "bias": "unknown",
            },
            "swing_prediction": {
                "direction": "neutral",
                "confidence": 0,
                "score_meaning": "Technical rule alignment, not probability",
                "option_action": "NO_TRADE",
                "signals": [reason],
                "horizon_days": 10,
                "technical_gate_passed": False,
            },
            "recommendation": {
                "action": "NO_TRADE",
                "reason": reason,
                "confidence": 0,
                "score_meaning": "Technical rule alignment, not probability",
                "safe": False,
                "trade_gate_passed": False,
            },
            "records": [],
            "data_source": data_source,
            "data_quality": data_quality or {"status": "blocked", "reason": reason},
            "timestamp": datetime.now(IST).isoformat(),
        }

    def get_full_analysis(self, symbol: str = "NIFTY") -> dict:
        """
        Returns complete options analysis including:
          - spot_price, atm_strike, pcr, max_pain, iv_proxy
          - support / resistance levels
          - 10-day swing prediction (CE or PE)
          - Final recommendation box
          - data_source: 'live' | 'fallback'
        """
        normalized = _normalize_index_symbol(symbol)
        if normalized is None:
            return self._blocked_analysis(
                str(symbol or ""),
                "Unsupported index symbol. Allowed: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY.",
                data_source="invalid_symbol",
                data_quality={"status": "blocked", "reason": "unsupported_index_symbol"},
            )

        cached = self._from_cache(normalized)
        if cached:
            return cached

        raw = self._fetch_raw_chain(normalized)
        if raw:
            result = self._analyze(normalized, raw, data_source="live")
        else:
            result = self._fallback_analysis(normalized)

        self._to_cache(normalized, result)
        return result

    # ── Core analysis ──────────────────────────────────────────────────────────

    def _analyze(
        self,
        symbol: str,
        raw: dict,
        data_source: str,
        *,
        as_of: datetime | None = None,
    ) -> dict:
        normalized = _normalize_index_symbol(symbol)
        if normalized is None:
            return self._blocked_analysis(
                str(symbol or ""),
                "Unsupported index symbol. No options action was generated.",
                data_source="invalid_symbol",
                data_quality={"status": "blocked", "reason": "unsupported_index_symbol"},
            )
        symbol = normalized
        current = _as_ist(as_of)
        records_data = raw.get("records", {}) or {}
        filtered_data = raw.get("filtered", {}) or {}
        option_data = records_data.get("data", []) or filtered_data.get("data", [])

        if not option_data:
            return self._fallback_analysis(symbol)

        spot = _positive_number(
            records_data.get("underlyingValue")
            or filtered_data.get("underlyingValue")
            or raw.get("underlyingValue", 0)
        )
        if not spot:
            return self._fallback_analysis(symbol)

        chain_timestamp = (
            records_data.get("timestamp")
            or filtered_data.get("timestamp")
            or raw.get("timestamp")
        )
        chain_quality = _assess_chain_freshness(chain_timestamp, as_of=current)
        if not chain_quality["fresh"]:
            return self._blocked_analysis(
                symbol,
                "NSE option-chain snapshot is missing or stale; directional options trade is blocked.",
                data_source="stale_chain",
                data_quality={"status": "blocked", "chain": chain_quality},
                spot=spot,
            )

        # Every calculation and recommendation must use one explicit live
        # expiry. Aggregating OI across expiries produces meaningless PCR and
        # max-pain levels, while guessing the next weekday can select a
        # non-existent contract.
        expiry_dates = records_data.get("expiryDates") or []
        selected_expiry = _select_swing_expiry(expiry_dates, as_of=current.date())
        if selected_expiry:
            option_data = [
                row for row in option_data
                if str(row.get("expiryDate") or "").strip() == selected_expiry
            ]
        if not selected_expiry or not option_data:
            return self._blocked_analysis(
                symbol,
                "No verified expiry with enough swing/theta buffer is available.",
                data_source="live_no_valid_expiry",
                data_quality={"status": "blocked", "chain": chain_quality, "expiry": "unavailable"},
                spot=spot,
                selected_expiry=selected_expiry,
            )

        step = INDEX_META[symbol]["strike_step"]
        atm = self._atm_strike(spot, option_data, step)
        pcr = self._pcr(option_data, atm)
        max_pain = self._max_pain(option_data)
        iv_proxy = self._iv_proxy(option_data, atm, spot)
        sr = self._support_resistance(option_data, spot)
        oi_change = self._oi_change_bias(option_data, atm)
        price_context = self._technical_context(INDEX_META[symbol]["yf"], spot, as_of=current)
        vix_snapshot = self._fetch_vix_snapshot(as_of=current)
        vix = vix_snapshot.get("value")

        # Flatten records for frontend OI chart
        flattened = self._flatten_records(option_data, spot)

        # 10-day swing prediction
        swing = self._swing_prediction(
            symbol,
            spot,
            pcr,
            max_pain,
            sr,
            oi_change,
            vix,
            technical_context=price_context,
        )
        contract = _select_liquid_contract(
            option_data,
            expiry_date=selected_expiry,
            option_action=swing["option_action"],
            spot=spot,
        )
        data_quality = {
            "status": "verified" if (
                chain_quality["fresh"]
                and price_context.get("freshness", {}).get("fresh")
                and vix_snapshot.get("freshness", {}).get("fresh")
                and contract is not None
            ) else "blocked",
            "chain": chain_quality,
            "price_history": price_context.get("freshness"),
            "vix": vix_snapshot.get("freshness"),
            "contract": contract,
        }

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
            expiry_date=selected_expiry,
            contract=contract,
            price_context=price_context,
            data_quality=data_quality,
        )

        return {
            "symbol": symbol,
            "label": INDEX_META[symbol]["label"],
            "spot_price": round(float(spot), 2),
            "atm_strike": atm,
            "selected_expiry": selected_expiry,
            "pcr": round(pcr, 3) if pcr is not None else None,
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
            "data_quality": data_quality,
            "source_timestamps": {
                "option_chain": chain_quality.get("source_timestamp"),
                "price_history": price_context.get("freshness", {}).get("source_timestamp"),
                "vix": vix_snapshot.get("freshness", {}).get("source_timestamp"),
            },
            "timestamp": current.isoformat(),
        }

    # ── Strike helpers ─────────────────────────────────────────────────────────

    def _atm_strike(self, spot: float, records: list, step: int) -> int:
        strikes = [
            strike
            for row in records
            if (strike := _positive_number(row.get("strikePrice")))
        ]
        if not strikes:
            return int(round(spot / step) * step)
        return int(min(strikes, key=lambda x: abs(x - spot)))

    # ── PCR ────────────────────────────────────────────────────────────────────

    def _pcr(self, records: list, atm: int, window: int = 500) -> float | None:
        put_oi = call_oi = 0
        for r in records:
            s = r.get("strikePrice", 0)
            if abs(s - atm) <= window:
                put_oi += r.get("PE", {}).get("openInterest", 0)
                call_oi += r.get("CE", {}).get("openInterest", 0)
        return round(put_oi / call_oi, 3) if call_oi else None

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
        # Use 21 trading days (~1 month) as default DTE for IV proxy
        dte_years = 21 / 252
        return round((premium / spot) / (dte_years ** 0.5), 3) if spot > 0 else 0.0

    # ── Support / Resistance ───────────────────────────────────────────────────

    def _support_resistance(self, records: list, spot: float) -> dict:
        below_put, above_call = [], []
        for r in records:
            s = _positive_number(r.get("strikePrice"))
            if not s:
                continue
            # Largest put/call OI strikes are crowding references. OI does not
            # reveal whether those contracts were bought or written.
            put_oi = _positive_number(r.get("PE", {}).get("openInterest", 0))
            call_oi = _positive_number(r.get("CE", {}).get("openInterest", 0))
            if s < spot and put_oi:
                below_put.append((s, put_oi))
            elif s > spot and call_oi:
                above_call.append((s, call_oi))

        support = max(below_put, key=lambda x: x[1]) if below_put else None
        resistance = max(above_call, key=lambda x: x[1]) if above_call else None
        return {
            "support": int(support[0]) if support else None,
            "support_oi": int(support[1]) if support else None,
            "resistance": int(resistance[0]) if resistance else None,
            "resistance_oi": int(resistance[1]) if resistance else None,
            "levels_are_observed": bool(support or resistance),
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
            bias = "put_oi_addition_heavy"
        elif call_chg > put_chg * 1.2:
            bias = "call_oi_addition_heavy"
        else:
            bias = "neutral"

        return {
            "call_change_oi": int(call_chg),
            "put_change_oi": int(put_chg),
            "bias": bias,
        }

    # ── India VIX ─────────────────────────────────────────────────────────────

    def _fetch_vix_snapshot(self, *, as_of: datetime | None = None) -> dict:
        try:
            import yfinance as yf
            live_session = _market_state(as_of) == "live"
            hist = yf.Ticker("^INDIAVIX").history(
                period="1d" if live_session else "5d",
                interval="5m" if live_session else "1d",
            )
            if not hist.empty:
                latest_bar = hist.index[-1]
                freshness = _assess_vix_bar_freshness(latest_bar, as_of=as_of)
                value = _positive_number(hist["Close"].iloc[-1])
                return {
                    "value": round(value, 2) if value else None,
                    "freshness": freshness,
                }
        except Exception as e:
            logger.debug("VIX fetch error: %s", e)
        return {
            "value": None,
            "freshness": _assess_vix_bar_freshness(None, as_of=as_of),
        }

    def _fetch_vix(self) -> Optional[float]:
        """Backward-compatible value-only VIX accessor."""

        return self._fetch_vix_snapshot().get("value")

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
        pcr: Optional[float],
        max_pain: int,
        sr: dict,
        oi_change: dict,
        vix: Optional[float],
        technical_context: dict | None = None,
    ) -> dict:
        """
        Combines technical indicators (EMA, RSI, MACD) + options data
        to predict 10-day direction with confidence score.
        Returns: direction, confidence, signals[], option_action
        """
        if technical_context is None:
            yf_symbol = INDEX_META[symbol]["yf"] if symbol in INDEX_META else ""
            tech_signals, bull_score, bear_score = (
                self._technical_scorecard(yf_symbol, spot)
                if yf_symbol
                else (["Unsupported index symbol"], 0, 0)
            )
        else:
            tech_signals = list(technical_context.get("signals") or [])
            bull_score = int(technical_context.get("bull_score") or 0)
            bear_score = int(technical_context.get("bear_score") or 0)

        signals: List[str] = list(tech_signals)

        # ── PCR ──
        if pcr is None:
            signals.append("PCR unavailable because verified call OI is absent; no neutral value was fabricated")
        elif pcr > 1.3:
            signals.append(f"PCR {pcr:.2f} → put OI is heavier; OI alone cannot identify buying, writing, or direction")
        elif pcr < 0.7:
            signals.append(f"PCR {pcr:.2f} → call OI is heavier; OI alone cannot identify buying, writing, or direction")
        else:
            signals.append(f"PCR {pcr:.2f} → balanced aggregate OI positioning")

        # ── Max pain ──
        if max_pain and spot < max_pain - (spot * 0.01):
            signals.append(f"Spot {spot:.0f} is below aggregate-payout max-pain reference {max_pain}; this is not a directional forecast")
        elif max_pain and spot > max_pain + (spot * 0.01):
            signals.append(f"Spot {spot:.0f} is above aggregate-payout max-pain reference {max_pain}; this is not a directional forecast")

        # ── OI change ──
        if oi_change["bias"] == "put_oi_addition_heavy":
            signals.append("PUT OI is increasing faster near ATM; buying/writing and direction are unknown")
        elif oi_change["bias"] == "call_oi_addition_heavy":
            signals.append("CALL OI is increasing faster near ATM; buying/writing and direction are unknown")

        # ── Support/Resistance distance ──
        support = _positive_number(sr.get("support"))
        resistance = _positive_number(sr.get("resistance"))
        dist_support = abs((spot - support) / spot) * 100 if support else None
        dist_resist = abs((resistance - spot) / spot) * 100 if resistance else None
        if dist_support is not None and dist_support < 1.5:
            signals.append(f"Spot is near put-OI reference {sr['support']}; price must confirm support")
        elif dist_resist is not None and dist_resist < 1.5:
            signals.append(f"Spot is near call-OI reference {sr['resistance']}; price must confirm resistance")

        # ── VIX regime ──
        if vix is not None:
            if vix < 13:
                signals.append(f"India VIX {vix} → very low fear, complacency — breakout possible but risky")
            elif vix <= 17:
                signals.append(f"India VIX {vix} → normal volatility range; no directional vote")
            elif vix <= 22:
                signals.append(f"India VIX {vix} → elevated fear, options expensive — prefer spreads")
            else:
                signals.append(f"India VIX {vix} → extreme fear (>22), avoid naked options — very risky")

        # ── Final direction ──
        # Preserve both sides of the technical evidence.  Normalizing the
        # winning score against zero would turn even one weak indicator into
        # 100% alignment and incorrectly pass the options trade gate.
        dominant_score = max(bull_score, bear_score)
        score_margin = abs(bull_score - bear_score)
        if bull_score > bear_score:
            direction = "bullish"
        elif bear_score > bull_score:
            direction = "bearish"
        else:
            direction = "neutral"

        # 65% absolute strength + 35% separation from the opposing case.
        # This is a bounded rule-alignment score, never a win probability.
        confidence = (
            min(
                round(
                    100
                    * (
                        0.65 * (dominant_score / 35.0)
                        + 0.35 * (score_margin / 35.0)
                    )
                ),
                85,
            )
            if direction != "neutral" and dominant_score > 0
            else 0
        )
        technical_gate_passed = bool(
            direction != "neutral"
            and dominant_score >= 26
            and score_margin >= 12
            and confidence >= CONFIDENCE_THRESHOLD
        )
        if not technical_gate_passed:
            signals.append(
                "Technical evidence is weak or conflicted; directional options trade stays blocked"
            )

        option_action = "NO_TRADE"
        if technical_gate_passed:
            option_action = "BUY_CE" if direction == "bullish" else "BUY_PE" if direction == "bearish" else "NO_TRADE"

        return {
            "direction": direction,
            "confidence": confidence,
            "score_meaning": "Technical rule alignment, not probability",
            "directional_options_oi_vote": False,
            "option_action": option_action,  # BUY_CE | BUY_PE | NO_TRADE
            "signals": signals,
            "horizon_days": 10,
            "bull_score": bull_score,
            "bear_score": bear_score,
            "technical_score_margin": score_margin,
            "technical_gate_passed": technical_gate_passed,
            "vix": vix,
        }

    # ── Technical analysis ─────────────────────────────────────────────────────

    def _technical_context(
        self,
        yf_symbol: str,
        spot: float,
        *,
        as_of: datetime | None = None,
    ) -> dict:
        """Return fresh completed-session technicals and observed pivot levels."""

        try:
            import yfinance as yf
            hist = yf.Ticker(yf_symbol).history(period="6mo", interval="1d")
            if hist.empty:
                raise ValueError("empty price history")

            expected_date = _latest_completed_session_date(as_of)
            eligible_positions = [
                index
                for index, raw_date in enumerate(hist.index)
                if (bar_date := _coerce_bar_date(raw_date)) is not None and bar_date <= expected_date
            ]
            if not eligible_positions:
                raise ValueError("no completed price bar")
            hist = hist.iloc[: eligible_positions[-1] + 1]
            freshness = _assess_price_bar_freshness(hist.index[-1], as_of=as_of)
            if not freshness["fresh"] or len(hist) < 50:
                return {
                    "signals": ["Latest completed price history is stale or insufficient"],
                    "bull_score": 0,
                    "bear_score": 0,
                    "freshness": freshness,
                    "bullish_invalidation": None,
                    "bearish_invalidation": None,
                    "bullish_targets": [],
                    "bearish_targets": [],
                }

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

            lows = list(hist["Low"].iloc[-30:]) if "Low" in hist else []
            highs = list(hist["High"].iloc[-30:]) if "High" in hist else []
            observed_swing_low = _latest_confirmed_pivot(
                lows,
                direction="bullish",
                spot=spot,
            )
            observed_swing_high = _latest_confirmed_pivot(
                highs,
                direction="bearish",
                spot=spot,
            )
            return {
                "signals": signals,
                "bull_score": min(bull, 35),
                "bear_score": min(bear, 35),
                "freshness": freshness,
                "bullish_invalidation": observed_swing_low,
                "bearish_invalidation": observed_swing_high,
                "bullish_targets": [observed_swing_high] if observed_swing_high else [],
                "bearish_targets": [observed_swing_low] if observed_swing_low else [],
                "invalidation_method": "latest_confirmed_three_bar_price_pivot",
                "target_method": "confirmed_completed_bar_price_pivot",
            }

        except Exception as e:
            logger.warning("[OPTIONS] Technical analysis failed: %s", e)
            return {
                "signals": ["Technical analysis unavailable"],
                "bull_score": 0,
                "bear_score": 0,
                "freshness": _assess_price_bar_freshness(None, as_of=as_of),
                "bullish_invalidation": None,
                "bearish_invalidation": None,
                "bullish_targets": [],
                "bearish_targets": [],
            }

    def _technical_scorecard(self, yf_symbol: str, spot: float) -> Tuple[List[str], int, int]:
        """Backward-compatible score-only view of the technical context."""

        context = self._technical_context(yf_symbol, spot)
        return (
            list(context.get("signals") or []),
            int(context.get("bull_score") or 0),
            int(context.get("bear_score") or 0),
        )

    def _technical_analysis(self, yf_symbol: str, spot: float) -> Tuple[List[str], str, int]:
        """Backward-compatible dominant-side view of the full scorecard."""

        signals, bull, bear = self._technical_scorecard(yf_symbol, spot)
        direction = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
        return signals, direction, max(bull, bear)

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
        expiry_date: str | None = None,
        contract: dict | None = None,
        price_context: dict | None = None,
        data_quality: dict | None = None,
    ) -> dict:
        """Build the final actionable recommendation card."""

        def blocked(reason: str, *, safe: bool = False, plan: dict | None = None) -> dict:
            result = {
                "action": "NO_TRADE",
                "reason": reason,
                "confidence": confidence,
                "score_meaning": "Technical rule alignment, not probability",
                "safe": safe,
                "trade_gate_passed": False,
            }
            if plan:
                result["structural_plan"] = plan
            return result

        if data_source != "live":
            return blocked("Live NSE option-chain data is unavailable; no options trade was generated.")

        quality = data_quality or {}
        if quality.get("chain", {}).get("fresh") is not True:
            return blocked("NSE option-chain snapshot is missing or stale.")
        if quality.get("price_history", {}).get("fresh") is not True:
            return blocked("Latest completed price history is missing or stale.")
        if quality.get("vix", {}).get("fresh") is not True or swing.get("vix") is None:
            return blocked("Current India VIX input is unavailable or stale; volatility risk cannot be verified.")
        if not expiry_date:
            return blocked("Live option expiry is unavailable; no contract can be selected safely.")
        if confidence < CONFIDENCE_THRESHOLD:
            return blocked(
                f"Rule alignment is only {confidence}/100 — below the 70/100 gate. Market is unclear. Sit out.",
                safe=True,
            )
        if swing["vix"] > 22:
            return blocked(
                f"India VIX {swing['vix']} is extremely high (>22). Options premium is very expensive. Risk of loss is very high. Avoid.",
                safe=True,
            )

        option_action = swing.get("option_action", "NO_TRADE")
        expected_action = (
            "BUY_CE" if direction == "bullish"
            else "BUY_PE" if direction == "bearish"
            else "NO_TRADE"
        )
        if (
            option_action not in {"BUY_CE", "BUY_PE"}
            or option_action != expected_action
            or not swing.get("technical_gate_passed")
        ):
            return blocked("Market direction is unclear or inconsistent; no trade recommended.", safe=True)

        expected_side = "CE" if option_action == "BUY_CE" else "PE"
        if not contract or contract.get("option_type") != expected_side:
            return blocked("No current, liquid, correctly dated option contract is available near ATM.")
        if contract.get("expiry") != expiry_date:
            return blocked("Selected option contract does not match the verified expiry.")

        price_context = price_context or {}
        invalidation = (
            price_context.get("bullish_invalidation")
            if direction == "bullish"
            else price_context.get("bearish_invalidation")
        )
        price_targets = (
            price_context.get("bullish_targets")
            if direction == "bullish"
            else price_context.get("bearish_targets")
        ) or []
        plan = assess_structural_plan(
            direction,
            spot,
            invalidation,
            price_targets,
            min_risk_reward=MIN_RISK_REWARD,
        )
        if not plan.get("allowed"):
            return blocked(
                plan.get("blockReason") or "Observed price structure does not provide a defensible options plan.",
                plan=plan,
            )

        is_ce = option_action == "BUY_CE"
        strike = int(contract["strike"])
        strategy = (
            f"Buy {symbol} {strike} CE (Call) | Expiry {expiry_date}"
            if is_ce else
            f"Buy {symbol} {strike} PE (Put) | Expiry {expiry_date}"
        )

        return {
            "action": option_action,
            "label": "📈 BUY CALL (CE)" if is_ce else "📉 BUY PUT (PE)",
            "symbol": symbol,
            "strike": strike,
            "expiry": expiry_date,
            "expiry_approx": None,
            "strategy": strategy,
            "confidence": confidence,
            "score_meaning": "Technical rule alignment, not probability",
            "horizon": "10 sessions maximum; exit before the expiry/theta buffer closes",
            "option_last_price_reference": contract["last_price"],
            "option_quote_quality": contract["quote_quality"],
            "option_bid": contract.get("bid"),
            "option_ask": contract.get("ask"),
            "option_spread_pct": contract.get("spread_pct"),
            "underlying_invalidation": round(float(plan["stop"]), 2),
            "underlying_structure_target": round(float(plan["target1"]), 2),
            "underlying_risk_reward": plan["riskReward"],
            "underlying_risk_pct": plan["riskPct"],
            "invalidation_method": price_context.get(
                "invalidation_method",
                "latest_confirmed_three_bar_price_pivot",
            ),
            "target_method": price_context.get(
                "target_method",
                "confirmed_completed_bar_price_pivot",
            ),
            "max_pain": max_pain,
            "reasons": list(swing.get("signals") or [])[:4],
            "vix_note": (
                f"VIX {swing['vix']:.1f} — "
                f"{'normal/low range' if swing['vix'] <= 17 else 'elevated; premium risk remains high'}"
            ),
            "warning": (
                "⚠️ These are underlying index invalidation/reference levels, not executable option-premium orders. "
                "Confirm the live premium and size risk before entry; options can expire worthless."
            ),
            "safe": False,
            "trade_gate_passed": True,
            "data_source": data_source,
            "structural_plan": plan,
            "contract_quality": contract,
        }

    # ── Fallback ───────────────────────────────────────────────────────────────

    def _fallback_analysis(self, symbol: str) -> dict:
        normalized = _normalize_index_symbol(symbol)
        if normalized is None:
            return self._blocked_analysis(
                str(symbol or ""),
                "Unsupported index symbol. No fallback index was substituted.",
                data_source="invalid_symbol",
                data_quality={"status": "blocked", "reason": "unsupported_index_symbol"},
            )
        symbol = normalized
        yf_symbol = INDEX_META[symbol]["yf"]
        spot = 0.0
        try:
            import yfinance as yf
            hist = yf.Ticker(yf_symbol).history(period="1d")
            if not hist.empty:
                spot = float(hist["Close"].iloc[-1])
        except Exception:
            pass

        step = INDEX_META[symbol]["strike_step"]
        atm = int(round(spot / step) * step) if spot > 0 else None

        tech_signals, tech_bull, tech_bear = self._technical_scorecard(yf_symbol, spot)
        tech_direction = (
            "bullish" if tech_bull > tech_bear
            else "bearish" if tech_bear > tech_bull
            else "neutral"
        )
        dominant_score = max(tech_bull, tech_bear)
        score_margin = abs(tech_bull - tech_bear)
        confidence = (
            min(
                round(
                    100
                    * (
                        0.65 * (dominant_score / 35.0)
                        + 0.35 * (score_margin / 35.0)
                    )
                ),
                85,
            )
            if tech_direction != "neutral" and dominant_score > 0
            else 0
        )

        swing_prediction = {
            "direction": tech_direction,
            "confidence": confidence,
            "score_meaning": "Technical rule alignment, not probability",
            "option_action": "NO_TRADE",
            "signals": tech_signals + ["⚠️ Using Technical Fallback (NSE Option Chain unavailable)"],
            "horizon_days": 10,
            "bull_score": tech_bull,
            "bear_score": tech_bear,
            "technical_score_margin": score_margin,
            "technical_gate_passed": False,
            "vix": self._fetch_vix(),
        }

        recommendation = {
            "action": "NO_TRADE",
            "reason": "Live NSE option-chain data is unavailable; a technical fallback cannot safely select a contract, expiry, stop, or target.",
            "confidence": confidence,
            "score_meaning": "Technical rule alignment, not probability",
            "safe": False,
        }

        return {
            "symbol": symbol,
            "label": INDEX_META[symbol]["label"],
            "spot_price": round(spot, 2),
            "atm_strike": atm,
            "pcr": None,
            "max_pain": None,
            "iv_proxy": None,
            "india_vix": swing_prediction["vix"],
            "support": None,
            "resistance": None,
            "oi_change": {"available": False, "call_change_oi": None, "put_change_oi": None, "bias": "unknown"},
            "swing_prediction": swing_prediction,
            "recommendation": recommendation,
            "records": [],
            "data_source": "fallback",
            "data_quality": "option_chain_unavailable_no_trade",
            "timestamp": datetime.now(IST).isoformat(),
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
