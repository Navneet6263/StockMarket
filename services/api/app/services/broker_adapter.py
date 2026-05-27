"""Broker Adapter — pluggable interface for AngelOne / Upstox / Zerodha.
yfinance remains the fallback. Set BROKER_PROVIDER=angelone to activate.
AngelOne API key will be injected via env vars when ready.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Dict, Protocol

import pandas as pd

logger = logging.getLogger(__name__)

# ── Provider selection ─────────────────────────────────────────────────────────
BROKER_PROVIDER = os.getenv("BROKER_PROVIDER", "yfinance").lower()  # angelone | yfinance

# ── AngelOne env vars ──────────────────────────────────────────────────────────
ANGELONE_API_KEY         = os.getenv("ANGELONE_API_KEY", "")
ANGELONE_CLIENT_ID       = os.getenv("ANGELONE_CLIENT_ID", "")
ANGELONE_PASSWORD        = os.getenv("ANGELONE_PASSWORD", "")
ANGELONE_TOTP_KEY        = os.getenv("ANGELONE_TOTP_KEY", "")
ANGELONE_CA_BUNDLE       = os.getenv("ANGELONE_CA_BUNDLE", "certifi").strip()
ANGELONE_DISABLE_SSL_VERIFY = os.getenv("ANGELONE_DISABLE_SSL_VERIFY", "false").strip().lower() in {"1", "true", "yes", "on"}

# ── Exchange constants used by AngelOne API ────────────────────────────────────
_AO_EXCHANGE_NSE = "NSE"
_AO_EXCHANGE_BSE = "BSE"

# Suffix used in Yahoo Finance raw_symbol to identify exchange
_YF_SUFFIX_NSE = ".NS"
_YF_SUFFIX_BSE = ".BO"

# AngelOne exchange type codes for WebSocket subscriptions
_AO_WS_EXCHANGE_NSE = 1   # NSE cash market
_AO_WS_EXCHANGE_BSE = 3   # BSE cash market


def _configure_tls() -> None:
    if ANGELONE_DISABLE_SSL_VERIFY:
        logger.warning("[BROKER] AngelOne SSL verification is disabled by ANGELONE_DISABLE_SSL_VERIFY=true")
        return
    if not ANGELONE_CA_BUNDLE:
        return
    if ANGELONE_CA_BUNDLE.lower() == "certifi":
        try:
            import certifi  # type: ignore
            bundle = certifi.where()
        except Exception as exc:
            logger.warning("[BROKER] certifi CA bundle unavailable: %s", exc)
            return
    else:
        bundle = ANGELONE_CA_BUNDLE
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
    os.environ.setdefault("SSL_CERT_FILE", bundle)


def _requests_verify():
    if ANGELONE_DISABLE_SSL_VERIFY:
        return False
    return os.getenv("REQUESTS_CA_BUNDLE") or True


def _exchange_segment_for_symbol(symbol: str) -> str:
    """Determine AngelOne exchange segment from a raw symbol string.

    Rules:
      - If raw_symbol ends with .BO  →  BSE
      - Anything else (incl. .NS)    →  NSE   (default / safest fallback)
    """
    upper = (symbol or "").upper().strip()
    if upper.endswith(_YF_SUFFIX_BSE):
        return _AO_EXCHANGE_BSE
    return _AO_EXCHANGE_NSE


def _ws_exchange_type(exchange_segment: str) -> int:
    """Convert AngelOne exchange segment string to WebSocket exchangeType integer."""
    return _AO_WS_EXCHANGE_BSE if exchange_segment == _AO_EXCHANGE_BSE else _AO_WS_EXCHANGE_NSE


class BrokerAdapter(Protocol):
    """Minimal interface every broker adapter must implement."""

    def fetch_history(self, symbol: str, period: str, interval: str) -> pd.DataFrame: ...
    def fetch_live_quote(self, symbol: str) -> Dict: ...
    def is_available(self) -> bool: ...


# ── AngelOne Adapter ──────────────────────────────────────────────────────────
class AngelOneAdapter:
    """
    AngelOne SmartAPI adapter.
    Requires: pip install smartapi-python pyotp
    Docs: https://smartapi.angelbroking.com/docs
    """

    def __init__(self):
        self._api = None
        self._session_token: str | None = None
        self._connected = False
        self._instrument_master: list[Dict] | None = None
        self._instrument_cache: dict[str, Dict] = {}  # "{exchange}:{clean_symbol}" -> instrument dict
        self._connect()

    def _connect(self):
        if not all([ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PASSWORD]):
            logger.info("[BROKER] AngelOne credentials not set — adapter inactive")
            return
        try:
            _configure_tls()
            from SmartApi import SmartConnect  # type: ignore
            import pyotp                        # type: ignore

            self._api = SmartConnect(api_key=ANGELONE_API_KEY, disable_ssl=ANGELONE_DISABLE_SSL_VERIFY)
            totp = pyotp.TOTP(ANGELONE_TOTP_KEY).now() if ANGELONE_TOTP_KEY else ""
            data = self._api.generateSession(ANGELONE_CLIENT_ID, ANGELONE_PASSWORD, totp)
            if data.get("status"):
                self._session_token = data["data"]["jwtToken"]
                self._connected = True
                logger.info("[BROKER] AngelOne connected client=%s", ANGELONE_CLIENT_ID)
            else:
                logger.warning("[BROKER] AngelOne session failed: %s", data.get("message"))
        except ImportError:
            logger.warning("[BROKER] smartapi-python not installed — pip install smartapi-python pyotp")
        except Exception as exc:
            logger.warning("[BROKER] AngelOne connect error: %s", exc)

    def is_available(self) -> bool:
        return self._connected and self._api is not None

    def _load_instruments(self) -> list[Dict]:
        if self._instrument_master is not None:
            return self._instrument_master
        try:
            import requests
            resp = requests.get(
                "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
                timeout=10,
                verify=_requests_verify(),
            )
            self._instrument_master = resp.json()
        except Exception as exc:
            logger.warning("[BROKER] instrument master lookup failed: %s", exc)
            self._instrument_master = []
        return self._instrument_master

    def _resolve_instrument(self, symbol: str, exchange: str = _AO_EXCHANGE_NSE) -> Dict | None:
        """Resolve any NSE or BSE equity instrument using AngelOne instrument list.

        Args:
            symbol:   Normalised ticker without exchange suffix (e.g. "RELIANCE").
            exchange: "NSE" or "BSE" — determines which exchange segment to search.
        """
        clean = symbol.upper().replace(_YF_SUFFIX_NSE, "").replace(_YF_SUFFIX_BSE, "")
        cache_key = f"{exchange}:{clean}"
        if cache_key in self._instrument_cache:
            return self._instrument_cache[cache_key]

        for item in self._load_instruments():
            if item.get("exch_seg") != exchange or item.get("name") != clean:
                continue
            trading_symbol = str(item.get("symbol") or "")
            instrument_type = str(item.get("instrumenttype") or "")
            # Accept EQ instruments (or blank type on BSE)
            if trading_symbol.endswith("-EQ") or instrument_type in {"EQ", ""}:
                self._instrument_cache[cache_key] = item
                return item
        return None

    # Legacy helper kept for backward compatibility (always NSE)
    def _nse_instrument(self, symbol: str) -> Dict | None:
        return self._resolve_instrument(symbol, _AO_EXCHANGE_NSE)

    def _nse_token(self, symbol: str) -> str | None:
        instrument = self._nse_instrument(symbol)
        return str(instrument.get("token")) if instrument and instrument.get("token") else None

    def _resolve_token(self, symbol: str, exchange: str = _AO_EXCHANGE_NSE) -> str | None:
        """Return AngelOne token for the given symbol on the given exchange."""
        instrument = self._resolve_instrument(symbol, exchange)
        return str(instrument.get("token")) if instrument and instrument.get("token") else None

    def _trading_symbol(self, symbol: str, exchange: str = _AO_EXCHANGE_NSE) -> str | None:
        instrument = self._resolve_instrument(symbol, exchange)
        return str(instrument.get("symbol")) if instrument and instrument.get("symbol") else None

    # ── History fetch ─────────────────────────────────────────────────────────

    def fetch_history(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        if not self.is_available():
            return pd.DataFrame()
        try:
            # Detect exchange from raw symbol suffix; default to NSE
            exchange = _exchange_segment_for_symbol(symbol)
            clean = symbol.upper().replace(_YF_SUFFIX_NSE, "").replace(_YF_SUFFIX_BSE, "")
            token = self._resolve_token(clean, exchange)
            if not token:
                logger.debug("[BROKER] token not found symbol=%s exchange=%s", symbol, exchange)
                return pd.DataFrame()

            interval_map = {
                "1d": "ONE_DAY",
                "1h": "ONE_HOUR",
                "15m": "FIFTEEN_MINUTE",
                "5m": "FIVE_MINUTE",
            }
            ao_interval = interval_map.get(interval, "ONE_DAY")
            from_date, to_date = _period_to_dates(period)
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": ao_interval,
                "fromdate": from_date,
                "todate": to_date,
            }
            resp = self._api.getCandleData(params)
            if not resp.get("status") or not resp.get("data"):
                return pd.DataFrame()

            rows = resp["data"]
            df = pd.DataFrame(rows, columns=["Datetime", "Open", "High", "Low", "Close", "Volume"])
            df["Datetime"] = pd.to_datetime(df["Datetime"])
            df = df.set_index("Datetime")
            df = df.astype({"Open": float, "High": float, "Low": float, "Close": float, "Volume": float})
            logger.info("[BROKER] AngelOne history symbol=%s exchange=%s rows=%d", symbol, exchange, len(df))
            return df
        except Exception as exc:
            logger.warning("[BROKER] AngelOne fetch_history failed symbol=%s: %s", symbol, exc)
            return pd.DataFrame()

    # ── Live quote ────────────────────────────────────────────────────────────

    def fetch_live_quote(self, symbol: str) -> Dict:
        if not self.is_available():
            return {}
        try:
            exchange = _exchange_segment_for_symbol(symbol)
            clean = symbol.upper().replace(_YF_SUFFIX_NSE, "").replace(_YF_SUFFIX_BSE, "")
            instrument = self._resolve_instrument(clean, exchange)
            if not instrument:
                return {}
            token = str(instrument.get("token") or "")
            trading_sym = str(instrument.get("symbol") or clean)
            if not token:
                return {}

            resp = self._api.ltpData(exchange, trading_sym, token)
            if not resp.get("status"):
                return {}
            data = resp.get("data", {})
            price = float(data.get("ltp", 0))
            return {
                "symbol": clean,
                "price": price,
                "previous_close": float(data.get("close") or price),
                "open": float(data.get("open") or 0),
                "high": float(data.get("high") or 0),
                "low": float(data.get("low") or 0),
                "volume": int(data.get("tradedQty", 0)),
                "exchange": exchange,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data_provider": "angelone",
            }
        except Exception as exc:
            logger.warning("[BROKER] AngelOne fetch_live_quote failed symbol=%s: %s", symbol, exc)
            return {}


# ── yfinance Adapter (fallback) ───────────────────────────────────────────────
class YFinanceAdapter:
    def is_available(self) -> bool:
        return True

    def fetch_history(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        return pd.DataFrame()  # handled by existing data_provider logic

    def fetch_live_quote(self, symbol: str) -> Dict:
        return {}  # handled by existing data_provider logic


# ── Factory ───────────────────────────────────────────────────────────────────
_adapter: BrokerAdapter | None = None


def get_broker_adapter() -> BrokerAdapter:
    global _adapter
    if _adapter is None:
        if BROKER_PROVIDER == "angelone" and ANGELONE_API_KEY:
            _adapter = AngelOneAdapter()
            if not _adapter.is_available():
                logger.warning("[BROKER] AngelOne unavailable — falling back to yfinance")
                _adapter = YFinanceAdapter()
        else:
            _adapter = YFinanceAdapter()
        logger.info("[BROKER] Active provider=%s", BROKER_PROVIDER)
    return _adapter


def _period_to_dates(period: str) -> tuple[str, str]:
    """Convert yfinance-style period string to AngelOne fromdate/todate."""
    from datetime import timedelta
    now = datetime.now()
    period_map = {
        "1d": 1, "5d": 5, "1mo": 30, "3mo": 90,
        "6mo": 180, "1y": 365, "2y": 730,
    }
    days = period_map.get(period, 365)
    from_dt = now - timedelta(days=days)
    fmt = "%Y-%m-%d %H:%M"
    return from_dt.strftime(fmt), now.strftime(fmt)
