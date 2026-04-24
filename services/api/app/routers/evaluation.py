from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from app.core.dependencies import get_market_hub
from app.services.market_hub import MarketHubService


router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])


@router.get("/backtest/{symbol}")
async def backtest(
    symbol: str,
    hub: MarketHubService = Depends(get_market_hub),
):
    return await asyncio.to_thread(hub.get_backtest, symbol)
