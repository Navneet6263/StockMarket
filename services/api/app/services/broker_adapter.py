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

BROKER_PROVIDER = os.getenv("BROKER_PROVIDER", "yfinance").lower()  # angelone | yfinance

# ── AngelOne env vars (set when API key is ready) ─────────────────────────────
ANGELONE_API_KEY    = os.getenv("ANGELONE_API_KEY", "")
ANGELONE_CLIENT_ID  = os.getenv("ANGELONE_CLIENT_ID", "")
ANGELONE_PASSWORD   = os.getenv("ANGELONE_PASSWORD", "")
ANGELONE_TOTP_KEY   = os.getenv("ANGELONE_TOTP_KEY", "")   # for TOTP-based login


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
        self._connect()

    def _connect(self):
        if not all([ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PASSWORD]):
            logger.info("[BROKER] AngelOne credentials not set — adapter inactive")
            return
        try:
            from SmartApi import SmartConnect  # type: ignore
            import pyotp                        # type: ignore

            self._api = SmartConnect(api_key=ANGELONE_API_KEY)
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

    def _nse_token(self, symbol: str) -> str | None:
        """Resolve NSE token for a symbol using AngelOne instrument list."""
        try:
            import requests
            resp = requests.get(
                "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
                timeout=10,
            )
            instruments = resp.json()
            clean = symbol.upper().replace(".NS", "")
            for item in instruments:
                if item.get("exch_seg") == "NSE" and item.get("name") == clean and item.get("instrumenttype") == "EQ":
                    return item.get("token")
        except Exception as exc:
            logger.warning("[BROKER] token lookup failed symbol=%s: %s", symbol, exc)
        return None

    def fetch_history(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        if not self.is_available():
            return pd.DataFrame()
        try:
            token = self._nse_token(symbol)
            if not token:
                return pd.DataFrame()

            # Map period/interval to AngelOne format
            interval_map = {"1d": "ONE_DAY", "1h": "ONE_HOUR", "15m": "FIFTEEN_MINUTE", "5m": "FIVE_MINUTE"}
            ao_interval = interval_map.get(interval, "ONE_DAY")

            from_date, to_date = _period_to_dates(period)
            params = {
                "exchange": "NSE",
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
            logger.info("[BROKER] AngelOne history symbol=%s rows=%d", symbol, len(df))
            return df
        except Exception as exc:
            logger.warning("[BROKER] AngelOne fetch_history failed symbol=%s: %s", symbol, exc)
            return pd.DataFrame()

    def fetch_live_quote(self, symbol: str) -> Dict:
        if not self.is_available():
            return {}
        try:
            token = self._nse_token(symbol)
            if not token:
                return {}
            resp = self._api.ltpData("NSE", symbol.upper().replace(".NS", ""), token)
            if not resp.get("status"):
                return {}
            data = resp.get("data", {})
            price = float(data.get("ltp", 0))
            return {
                "symbol": symbol.upper().replace(".NS", ""),
                "price": price,
                "volume": int(data.get("tradedQty", 0)),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data_provider": "angelone",
            }
        except Exception as exc:
            logger.warning("[BROKER] AngelOne fetch_live_quote failed symbol=%s: %s", symbol, exc)
            return {}


# ── yfinance Adapter (existing fallback) ─────────────────────────────────────
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
