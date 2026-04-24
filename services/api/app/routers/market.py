from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_market_hub
from app.services.market_hub import MarketHubService


router = APIRouter(prefix="/api", tags=["market"])


@router.get("/market/overview")
async def market_overview(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    return await asyncio.to_thread(hub.scan_market, force_refresh)


@router.get("/market/opportunities")
async def market_opportunities(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await asyncio.to_thread(hub.scan_market, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "top_opportunities": payload["top_opportunities"],
        "summary": payload["summary"],
    }


@router.get("/market/top-volume")
async def market_top_volume(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await asyncio.to_thread(hub.scan_market, force_refresh)
    return {"generated_at": payload["generated_at"], "results": payload["unusual_volume"]}


@router.get("/market/top-movers")
async def market_top_movers(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await asyncio.to_thread(hub.scan_market, force_refresh)
    return {"generated_at": payload["generated_at"], "results": payload["top_movers"]}


@router.get("/scanner/breakouts")
async def scanner_breakouts(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await asyncio.to_thread(hub.scan_market, force_refresh)
    return {"generated_at": payload["generated_at"], "results": payload["breakout_candidates"]}


@router.get("/scanner/bearish-risk")
async def scanner_bearish_risk(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await asyncio.to_thread(hub.scan_market, force_refresh)
    return {"generated_at": payload["generated_at"], "results": payload["bearish_risks"]}


@router.get("/market/context")
async def market_context(
    symbol: str = Query("NIFTY"),
    limit: int = Query(6, ge=1, le=10),
    hub: MarketHubService = Depends(get_market_hub),
):
    return await asyncio.to_thread(hub.get_market_context, symbol, limit)
