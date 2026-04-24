from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_market_hub
from app.services.market_hub import MarketHubService


router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("/{symbol}")
async def stock_detail(
    symbol: str,
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    return await asyncio.to_thread(hub.get_stock_detail, symbol, force_refresh)


@router.get("/{symbol}/prediction")
async def stock_prediction(
    symbol: str,
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    return await asyncio.to_thread(hub.get_stock_prediction, symbol, force_refresh)


@router.get("/{symbol}/signals")
async def stock_signals(
    symbol: str,
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    return await asyncio.to_thread(hub.get_stock_signals, symbol, force_refresh)


@router.get("/{symbol}/history")
async def stock_history(
    symbol: str,
    period: str = Query("6mo"),
    hub: MarketHubService = Depends(get_market_hub),
):
    return await asyncio.to_thread(hub.get_historical_chart, symbol, period)


@router.get("/{symbol}/live")
async def stock_live(
    symbol: str,
    hub: MarketHubService = Depends(get_market_hub),
):
    return await asyncio.to_thread(hub.get_live_payload, symbol)
