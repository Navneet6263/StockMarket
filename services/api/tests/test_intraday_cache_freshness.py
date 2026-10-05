"""No network or real parquet writes: persisted-history freshness regressions."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from app.core.cache import TTLCache
from app.services.data_provider import MarketDataService


def _utc(value):
    stamp = pd.Timestamp(value)
    if stamp.tz is None:
        stamp = stamp.tz_localize("Asia/Kolkata")
    return stamp.tz_convert("UTC")


def _frame(local_bar="2026-09-15 11:45", aware=False):
    stamp = _utc(local_bar)
    if not aware:
        stamp = stamp.tz_localize(None)
    return pd.DataFrame({"Open": [100], "High": [102], "Low": [99], "Close": [101], "Volume": [1000]},
                        index=pd.DatetimeIndex([stamp]))


class IntradayParquetFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(MarketDataService)
        self.service.settings = SimpleNamespace(intraday_cache_ttl_sec=60, history_cache_ttl_sec=3600,
                                                invalid_symbols=[], yahoo_timeout_sec=8, yahoo_batch_chunk_size=20)
        self.service.history_cache = TTLCache(3600)
        self.service.symbol_map = {}
        self.service.use_broker_history = False

    def _load(self, *, bar="2026-09-15 11:45", now="2026-09-15 12:00:30",
              written="2026-09-15 12:00:00", interval="15m", aware=False):
        path = Mock(spec=Path)
        path.exists.return_value = True
        path.stat.return_value = SimpleNamespace(st_mtime=_utc(written).timestamp())
        frame = _frame(bar, aware=aware)
        with patch("app.services.data_provider.pd.read_parquet", return_value=frame) as read:
            result = self.service._load_from_parquet(path, interval=interval, now=_utc(now).to_pydatetime())
        return result, read

    def test_fresh_utc_naive_intraday_cache_is_reused(self):
        result, read = self._load()
        self.assertIsNotNone(result)
        read.assert_called_once()

    def test_aware_index_is_equivalent_to_provider_utc_naive_index(self):
        naive, _ = self._load()
        aware, _ = self._load(aware=True)
        self.assertIsNotNone(naive)
        self.assertIsNotNone(aware)

    def test_expired_file_is_rejected_before_reading_parquet(self):
        result, read = self._load(written="2026-09-15 11:59:30")
        self.assertIsNone(result)
        read.assert_not_called()

    def test_recently_rewritten_yesterday_bars_are_not_current_session_data(self):
        result, _ = self._load(bar="2026-09-14 15:15")
        self.assertIsNone(result)

    def test_fresh_file_with_stale_morning_bars_is_rejected(self):
        result, _ = self._load(bar="2026-09-15 09:15")
        self.assertIsNone(result)

    def test_recent_bar_age_allowance_scales_with_interval(self):
        accepted, _ = self._load(bar="2026-09-15 11:57", interval="1m")
        rejected, _ = self._load(bar="2026-09-15 11:54", interval="1m")
        self.assertIsNotNone(accepted)
        self.assertIsNone(rejected)

    def test_future_bar_or_future_file_cannot_refresh_cache(self):
        future_bar, _ = self._load(bar="2026-09-15 12:15")
        future_file, read = self._load(written="2026-09-15 13:00")
        self.assertIsNone(future_bar)
        self.assertIsNone(future_file)
        read.assert_not_called()

    def test_completed_session_reused_overnight_without_extra_downloads(self):
        result, _ = self._load(bar="2026-09-14 15:15", written="2026-09-14 15:31",
                               now="2026-09-15 08:00")
        self.assertIsNotNone(result)

    def test_completed_friday_session_reused_over_weekend(self):
        result, _ = self._load(bar="2026-09-11 15:15", written="2026-09-11 15:31",
                               now="2026-09-13 12:00")
        self.assertIsNotNone(result)

    def test_morning_cache_is_not_assumed_complete_after_market_close(self):
        result, _ = self._load(bar="2026-09-15 09:15", written="2026-09-15 09:30",
                               now="2026-09-15 18:00")
        self.assertIsNone(result)

    def test_last_bar_file_written_before_close_is_not_complete_eod_cache(self):
        result, _ = self._load(bar="2026-09-15 15:15", written="2026-09-15 15:20",
                               now="2026-09-15 18:00")
        self.assertIsNone(result)

    def test_yesterday_eod_cache_is_not_reused_after_today_market_open(self):
        result, read = self._load(bar="2026-09-14 15:15", written="2026-09-14 15:31",
                                  now="2026-09-15 09:15")
        self.assertIsNone(result)
        read.assert_not_called()

    def test_partial_last_30minute_bucket_can_be_complete_at_session_close(self):
        result, _ = self._load(bar="2026-09-15 15:15", written="2026-09-15 15:31",
                               now="2026-09-15 18:00", interval="30m")
        self.assertIsNotNone(result)

    def test_daily_semantics_unchanged_no_file_ttl_check(self):
        path = Mock(spec=Path)
        path.exists.return_value = True
        frame = _frame()
        frame.index = pd.DatetimeIndex(["2026-09-14"])
        with patch("app.services.data_provider.pd.read_parquet", return_value=frame):
            result = self.service._load_from_parquet(path, interval="1d", now=_utc("2026-09-15 12:00").to_pydatetime())
        self.assertIsNotNone(result)
        path.stat.assert_not_called()

    def test_daily_cache_older_than_existing_businessday_limit_is_still_rejected(self):
        path = Mock(spec=Path)
        path.exists.return_value = True
        frame = _frame()
        frame.index = pd.DatetimeIndex(["2026-09-10"])
        with patch("app.services.data_provider.pd.read_parquet", return_value=frame):
            result = self.service._load_from_parquet(path, interval="1d", now=_utc("2026-09-15 12:00").to_pydatetime())
        self.assertIsNone(result)

    def test_single_fetch_passes_interval_and_uses_ram_after_disk_hit(self):
        path = Path("does-not-need-to-exist.parquet")
        with (patch.object(self.service, "_get_parquet_path", return_value=path),
              patch.object(self.service, "_load_from_parquet", return_value=_frame()) as load,
              patch("app.services.data_provider.yf.Ticker") as ticker):
            self.service.fetch_history("TEST", period="5d", interval="15m")
            self.service.fetch_history("TEST", period="5d", interval="15m")
        load.assert_called_once_with(path, interval="15m")
        ticker.assert_not_called()

    def test_batch_fetch_passes_interval_and_uses_ram_after_disk_hit(self):
        path = Path("does-not-need-to-exist.parquet")
        with (patch.object(self.service, "_get_parquet_path", return_value=path),
              patch.object(self.service, "_load_from_parquet", return_value=_frame()) as load,
              patch("app.services.data_provider.yf.download") as download):
            self.service.fetch_batch_history(["TEST"], period="5d", interval="15m")
            self.service.fetch_batch_history(["TEST"], period="5d", interval="15m")
        load.assert_called_once_with(path, interval="15m")
        download.assert_not_called()

    def test_expired_parquet_does_not_indefinitely_refill_ram(self):
        path = Mock(spec=Path)
        path.exists.return_value = True
        path.stat.return_value = SimpleNamespace(st_mtime=_utc("2026-09-15 12:00").timestamp())
        with (patch.object(self.service, "_get_parquet_path", return_value=path),
              patch.object(self.service, "_save_to_parquet"),
              patch("app.services.data_provider.pd.read_parquet", return_value=_frame()) as read,
              patch("app.services.data_provider.datetime") as clock,
              patch("app.services.data_provider.yf.Ticker") as ticker):
            clock.now.side_effect = [_utc("2026-09-15 12:00:30").to_pydatetime(),
                                     _utc("2026-09-15 12:01:31").to_pydatetime()]
            ticker.return_value.history.return_value = _frame("2026-09-15 12:00", aware=True)
            self.service.fetch_history("TEST", period="5d", interval="15m")
            self.service.history_cache.clear()
            self.service.fetch_history("TEST", period="5d", interval="15m")
            self.service.fetch_history("TEST", period="5d", interval="15m")
        read.assert_called_once()
        ticker.return_value.history.assert_called_once()


if __name__ == "__main__":
    unittest.main()
