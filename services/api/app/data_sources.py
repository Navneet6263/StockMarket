from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
import io
import zipfile

import pandas as pd
import requests


NSE_BASE = "https://archives.nseindia.com"
BSE_BASE = "https://www.bseindia.com"

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv,application/zip,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://www.nseindia.com/",
}

BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv,application/zip,application/octet-stream,*/*",
}


@dataclass
class BhavcopyResult:
    exchange: str
    trade_date: date
    df: pd.DataFrame
    source_url: str


class BhavcopyError(RuntimeError):
    pass


def _nse_url(d: date) -> str:
    mon = d.strftime("%b").upper()
    return (
        f"{NSE_BASE}/content/historical/EQUITIES/{d.year}/{mon}/"
        f"cm{d.strftime('%d')}{mon}{d.strftime('%Y')}bhav.csv.zip"
    )


def _bse_url(d: date) -> str:
    return f"{BSE_BASE}/download/BhavCopy/Equity/EQ{d.strftime('%d%m%y')}_CSV.ZIP"


def _read_zip_csv(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise BhavcopyError("zip_missing_csv")
        with zf.open(names[0]) as f:
            return pd.read_csv(f, low_memory=False)


def _download_with_session(session: requests.Session, url: str) -> bytes:
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise BhavcopyError(f"download_failed_{resp.status_code}")
    return resp.content


def _download_nse(url: str) -> bytes:
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except requests.RequestException:
        pass
    return _download_with_session(session, url)


def _download_bse(url: str) -> bytes:
    session = requests.Session()
    session.headers.update(BSE_HEADERS)
    return _download_with_session(session, url)


def _standardize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().upper() for c in df.columns]
    return df


def _normalize_nse(df: pd.DataFrame, trade_date: date) -> pd.DataFrame:
    df = _standardize(df)
    mapping = {
        "SYMBOL": "symbol",
        "SERIES": "series",
        "OPEN": "open",
        "HIGH": "high",
        "LOW": "low",
        "CLOSE": "close",
        "PREVCLOSE": "prev_close",
        "TOTTRDQTY": "volume",
        "TOTTRDVAL": "value",
        "TOTALTRADES": "trades",
        "ISIN": "isin",
        "TIMESTAMP": "timestamp",
    }
    df = df.rename(columns=mapping)
    df["trade_date"] = trade_date
    df["exchange"] = "NSE"

    for col in ["open", "high", "low", "close", "prev_close", "value"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["volume", "trades"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce", downcast="integer")

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str).str.strip()

    return df


def _normalize_bse(df: pd.DataFrame, trade_date: date) -> pd.DataFrame:
    df = _standardize(df)
    mapping = {
        "SC_CODE": "bse_code",
        "SC_NAME": "symbol",
        "OPEN": "open",
        "HIGH": "high",
        "LOW": "low",
        "CLOSE": "close",
        "PREVCLOSE": "prev_close",
        "NO_TRADES": "trades",
        "NO_SHRS": "volume",
        "NET_TURNOV": "value",
    }
    df = df.rename(columns=mapping)
    df["trade_date"] = trade_date
    df["exchange"] = "BSE"

    for col in ["open", "high", "low", "close", "prev_close", "value"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["volume", "trades"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce", downcast="integer")

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str).str.strip()

    return df


def fetch_bhavcopy(exchange: str, trade_date: date) -> BhavcopyResult:
    ex = exchange.upper()
    if ex == "NSE":
        url = _nse_url(trade_date)
        content = _download_nse(url)
        df = _read_zip_csv(content)
        df = _normalize_nse(df, trade_date)
        return BhavcopyResult(exchange=ex, trade_date=trade_date, df=df, source_url=url)

    if ex == "BSE":
        url = _bse_url(trade_date)
        content = _download_bse(url)
        df = _read_zip_csv(content)
        df = _normalize_bse(df, trade_date)
        return BhavcopyResult(exchange=ex, trade_date=trade_date, df=df, source_url=url)

    raise BhavcopyError("unsupported_exchange")


def fetch_latest_bhavcopy(
    exchange: str,
    as_of: Optional[date] = None,
    max_lookback: int = 10,
) -> BhavcopyResult:
    current = as_of or date.today()
    last_error: Optional[Exception] = None

    for _ in range(max_lookback):
        try:
            return fetch_bhavcopy(exchange, current)
        except Exception as exc:
            last_error = exc
            current -= timedelta(days=1)

    raise BhavcopyError("latest_bhavcopy_not_found") from last_error
