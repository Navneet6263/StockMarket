from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _csv_env(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip().upper() for item in raw.split(",") if item.strip())


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_track_db_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "data" / "market_intelligence.db")


@dataclass(frozen=True)
class Settings:
    api_prefix: str = "/api"
    benchmark_symbol: str = os.getenv("BENCHMARK_SYMBOL", "NIFTY")
    scan_history_period: str = os.getenv("SCAN_HISTORY_PERIOD", "1y")
    detail_history_period: str = os.getenv("DETAIL_HISTORY_PERIOD", "6mo")
    universe_size: int = int(os.getenv("UNIVERSE_SIZE", "600"))
    scan_symbol_limit: int = int(os.getenv("SCAN_SYMBOL_LIMIT", "240"))
    intraday_symbol_limit: int = int(os.getenv("SCAN_INTRADAY_LIMIT", "36"))
    min_history_bars: int = int(os.getenv("MIN_HISTORY_BARS", "120"))
    min_price: float = float(os.getenv("MIN_PRICE", "10"))
    min_volume: int = int(os.getenv("MIN_VOLUME", "100000"))
    min_market_cap: float = float(os.getenv("MIN_MARKET_CAP", "0"))
    include_sme: bool = _bool_env("INCLUDE_SME", False)
    include_indices: bool = _bool_env("INCLUDE_INDICES", True)
    scan_interval_sec: int = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
    scan_cache_ttl_sec: int = int(os.getenv("SCAN_CACHE_TTL_SEC", "120"))
    detail_cache_ttl_sec: int = int(os.getenv("DETAIL_CACHE_TTL_SEC", "90"))
    backtest_cache_ttl_sec: int = int(os.getenv("BACKTEST_CACHE_TTL_SEC", "900"))
    history_cache_ttl_sec: int = int(os.getenv("HISTORY_CACHE_TTL_SEC", "180"))
    hold_days: int = int(os.getenv("BACKTEST_HOLD_DAYS", "5"))
    chart_limit: int = int(os.getenv("CHART_LIMIT", "220"))
    tracked_setup_db_path: str = os.getenv("TRACK_DB_PATH", _default_track_db_path())
    tracked_promotion_confidence_min: float = float(os.getenv("TRACK_PROMOTION_CONFIDENCE_MIN", "68"))
    tracked_promotion_move_quality_min: int = int(os.getenv("TRACK_PROMOTION_MOVE_QUALITY_MIN", "58"))
    tracked_review_limit: int = int(os.getenv("TRACK_REVIEW_LIMIT", "10"))
    custom_universe: tuple[str, ...] = _csv_env("UNIVERSE_SYMBOLS")
    universe_groups: tuple[str, ...] = _csv_env("UNIVERSE_GROUPS") or ("NIFTY_200", "FNO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
