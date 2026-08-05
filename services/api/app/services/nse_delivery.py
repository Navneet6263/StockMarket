"""Official NSE end-of-day delivery archive support.

The broker candle endpoint exposes *traded volume*, not deliverable quantity.
This module keeps that distinction explicit and enriches daily frames only when
the official ``sec_bhavdata_full`` archive actually supplies ``DELIV_QTY`` and
``DELIV_PER``.  Refreshing is deliberately asynchronous so the live scanner is
never held up by an exchange/archive outage.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from io import StringIO
import logging
from pathlib import Path
import threading
import time
from typing import Any

import pandas as pd
import requests

from app.core.settings import Settings


logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{stamp}.csv"
REQUIRED_COLUMNS = {"SYMBOL", "DATE1", "TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER"}


def _normalise_columns(frame: pd.DataFrame) -> pd.DataFrame:
    cleaned = frame.copy()
    cleaned.columns = [str(column).strip().upper() for column in cleaned.columns]
    return cleaned


def parse_delivery_csv(raw: str) -> pd.DataFrame:
    """Parse one official NSE security-wise bhavdata CSV.

    An empty frame is returned for HTML/error bodies or formats that do not
    contain real delivery fields.  This prevents total volume from ever being
    silently treated as delivery volume.
    """

    if not raw or "<html" in raw[:500].lower():
        return pd.DataFrame()
    try:
        frame = _normalise_columns(pd.read_csv(StringIO(raw), skipinitialspace=True))
    except Exception:
        return pd.DataFrame()
    if not REQUIRED_COLUMNS.issubset(frame.columns):
        return pd.DataFrame()

    frame["SYMBOL"] = frame["SYMBOL"].astype(str).str.strip().str.upper()
    if "SERIES" in frame.columns:
        frame["SERIES"] = frame["SERIES"].astype(str).str.strip().str.upper()
        frame = frame[frame["SERIES"].isin({"EQ", "BE", "BZ", "SM", "ST"})]
    frame["TRADE_DATE"] = pd.to_datetime(frame["DATE1"].astype(str).str.strip(), errors="coerce", dayfirst=True)
    for column in ("TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER"):
        frame[column] = pd.to_numeric(frame[column].astype(str).str.strip().replace({"-": None}), errors="coerce")
    frame = frame.dropna(subset=["SYMBOL", "TRADE_DATE", "DELIV_QTY", "DELIV_PER"])
    frame = frame[(frame["DELIV_PER"] >= 0) & (frame["DELIV_PER"] <= 100)]
    return frame[["SYMBOL", "TRADE_DATE", "TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER"]].copy()


class NSEDeliveryArchive:
    """Non-blocking cache of official NSE EOD delivery observations."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = bool(settings.enable_nse_delivery_archive)
        self.lookback_days = max(10, int(settings.nse_delivery_lookback_days))
        self.timeout = max(1.0, float(settings.nse_delivery_timeout_sec))
        self.refresh_interval = max(900, int(settings.nse_delivery_refresh_sec))
        self.cache_dir = Path(__file__).resolve().parents[3] / "cache" / "nse_delivery"
        self._lock = threading.RLock()
        self._refreshing = False
        self._last_refresh_at: str | None = None
        self._last_refresh_started_epoch = 0.0
        self._last_error: str | None = None
        self._rows = pd.DataFrame()
        if self.enabled:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._load_cached_files()
            except Exception as exc:
                # Delivery enrichment is optional. A read-only cache path must
                # never prevent the scanner or live feed from starting.
                self._last_error = f"delivery_cache_unavailable: {exc}"
                logger.warning("NSE delivery cache unavailable: %s", exc)

    def _candidate_dates(self) -> list[date]:
        today = datetime.now().date()
        return [today - timedelta(days=offset) for offset in range(1, self.lookback_days + 1)]

    def _cache_path(self, trading_date: date) -> Path:
        return self.cache_dir / f"sec_bhavdata_full_{trading_date.strftime('%d%m%Y')}.csv"

    def _load_cached_files(self) -> None:
        frames: list[pd.DataFrame] = []
        def archive_date(path: Path) -> datetime:
            stamp = path.stem.rsplit("_", 1)[-1]
            try:
                return datetime.strptime(stamp, "%d%m%Y")
            except ValueError:
                return datetime.min

        cached_paths = sorted(
            self.cache_dir.glob("sec_bhavdata_full_*.csv"),
            key=archive_date,
        )[-self.lookback_days :]
        for path in cached_paths:
            try:
                parsed = parse_delivery_csv(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if not parsed.empty:
                frames.append(parsed)
        if frames:
            self._replace_rows(pd.concat(frames, ignore_index=True))

    def _replace_rows(self, rows: pd.DataFrame) -> None:
        if rows.empty:
            return
        combined = rows.drop_duplicates(subset=["SYMBOL", "TRADE_DATE"], keep="last")
        combined = combined.sort_values(["SYMBOL", "TRADE_DATE"])
        with self._lock:
            self._rows = combined.reset_index(drop=True)

    def _download_day(self, trading_date: date) -> pd.DataFrame:
        path = self._cache_path(trading_date)
        if path.exists():
            try:
                return parse_delivery_csv(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                return pd.DataFrame()

        url = ARCHIVE_URL.format(stamp=trading_date.strftime("%d%m%Y"))
        response = requests.get(
            url,
            timeout=self.timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MarketIntelligence/1.0)"},
        )
        if response.status_code != 200:
            return pd.DataFrame()
        parsed = parse_delivery_csv(response.text)
        if parsed.empty:
            return parsed
        try:
            path.write_text(response.text, encoding="utf-8")
        except Exception:
            logger.debug("Could not persist NSE delivery cache %s", path, exc_info=True)
        return parsed

    def refresh_async(self) -> bool:
        """Start one best-effort background refresh and return immediately."""

        if not self.enabled:
            return False
        with self._lock:
            now = time.time()
            if self._refreshing or now - self._last_refresh_started_epoch < self.refresh_interval:
                return False
            self._refreshing = True
            self._last_refresh_started_epoch = now

        def refresh() -> None:
            try:
                frames: list[pd.DataFrame] = []
                dates = self._candidate_dates()
                with ThreadPoolExecutor(max_workers=6) as executor:
                    futures = {executor.submit(self._download_day, value): value for value in dates}
                    for future in as_completed(futures):
                        try:
                            parsed = future.result()
                        except Exception:
                            continue
                        if not parsed.empty:
                            frames.append(parsed)
                if frames:
                    existing = self._rows.copy()
                    if not existing.empty:
                        frames.append(existing)
                    self._replace_rows(pd.concat(frames, ignore_index=True))
                    with self._lock:
                        self._last_refresh_at = datetime.now().isoformat()
                        self._last_error = None
                else:
                    with self._lock:
                        self._last_error = "official_delivery_archive_unavailable"
            except Exception as exc:
                logger.warning("NSE delivery archive refresh failed: %s", exc)
                with self._lock:
                    self._last_error = str(exc)
            finally:
                with self._lock:
                    self._refreshing = False

        threading.Thread(target=refresh, name="nse-delivery-refresh", daemon=True).start()
        return True

    def enrich_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """Attach official delivery columns to matching daily rows, if known."""

        if not self.enabled:
            return frames
        with self._lock:
            rows = self._rows.copy()
        if rows.empty:
            return frames

        by_symbol = {symbol: group for symbol, group in rows.groupby("SYMBOL", sort=False)}
        enriched: dict[str, pd.DataFrame] = {}
        for raw_symbol, source in frames.items():
            symbol = str(raw_symbol).upper().replace(".NS", "")
            delivery = by_symbol.get(symbol)
            if delivery is None or source is None or source.empty:
                enriched[raw_symbol] = source
                continue

            frame = source.copy()
            dates = pd.to_datetime(frame.index, errors="coerce").normalize()
            delivery_map = delivery.set_index(delivery["TRADE_DATE"].dt.normalize())
            frame["DELIV_PER"] = [
                delivery_map.at[value, "DELIV_PER"] if pd.notna(value) and value in delivery_map.index else float("nan")
                for value in dates
            ]
            frame["Deliverable Volume"] = [
                delivery_map.at[value, "DELIV_QTY"] if pd.notna(value) and value in delivery_map.index else float("nan")
                for value in dates
            ]
            frame.attrs["delivery_source"] = "nse_sec_bhavdata_full"
            enriched[raw_symbol] = frame
        return enriched

    def status(self) -> dict[str, Any]:
        with self._lock:
            rows = self._rows
            return {
                "enabled": self.enabled,
                "refreshing": self._refreshing,
                "source": "NSE sec_bhavdata_full",
                "observations": int(len(rows)),
                "symbols": int(rows["SYMBOL"].nunique()) if not rows.empty else 0,
                "last_refresh_at": self._last_refresh_at,
                "last_refresh_attempt_epoch": self._last_refresh_started_epoch or None,
                "refresh_interval_sec": self.refresh_interval,
                "last_error": self._last_error,
                "data_quality": "official_eod" if not rows.empty else "unavailable",
            }
