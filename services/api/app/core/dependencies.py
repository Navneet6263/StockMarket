from __future__ import annotations

from functools import lru_cache

from app.core.settings import get_settings
from app.services.market_hub import MarketHubService


@lru_cache
def get_market_hub() -> MarketHubService:
    return MarketHubService(get_settings())
