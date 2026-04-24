from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_market_hub
from app.services.market_hub import MarketHubService


router = APIRouter(prefix="/api/tracker", tags=["tracker"])


class ManualWatchRequest(BaseModel):
    symbol: str
    notes: str | None = None
    pinned: bool = False
    timeframe_label: str | None = None


class SetupUpdateRequest(BaseModel):
    notes: str | None = None
    pinned: bool | None = None
    timeframe_label: str | None = None
    timeframe_days: int | None = None
    status: str | None = None


@router.get("/dashboard")
async def tracker_dashboard(hub: MarketHubService = Depends(get_market_hub)):
    return await asyncio.to_thread(hub.get_tracker_dashboard)


@router.get("/stocks/{symbol}")
async def tracker_symbol(symbol: str, hub: MarketHubService = Depends(get_market_hub)):
    return await asyncio.to_thread(hub.get_tracker_symbol, symbol)


@router.post("/evaluate")
async def evaluate_tracked_setups(hub: MarketHubService = Depends(get_market_hub)):
    return await asyncio.to_thread(hub.evaluate_tracked_setups)


@router.post("/manual-watch")
async def create_manual_watch(payload: ManualWatchRequest, hub: MarketHubService = Depends(get_market_hub)):
    return await asyncio.to_thread(
        hub.create_manual_watch,
        payload.symbol,
        notes=payload.notes,
        pinned=payload.pinned,
        timeframe_label=payload.timeframe_label,
    )


@router.patch("/setups/{setup_id}")
async def update_setup(setup_id: str, payload: SetupUpdateRequest, hub: MarketHubService = Depends(get_market_hub)):
    values = {key: value for key, value in payload.model_dump().items() if value is not None}
    updated = await asyncio.to_thread(hub.update_tracked_setup, setup_id, values)
    if not updated:
        raise HTTPException(status_code=404, detail="Tracked setup not found")
    return updated


@router.post("/setups/{setup_id}/archive")
async def archive_setup(setup_id: str, hub: MarketHubService = Depends(get_market_hub)):
    updated = await asyncio.to_thread(hub.archive_tracked_setup, setup_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Tracked setup not found")
    return updated


@router.post("/setups/{setup_id}/ignore")
async def ignore_setup(setup_id: str, hub: MarketHubService = Depends(get_market_hub)):
    updated = await asyncio.to_thread(hub.ignore_tracked_setup, setup_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Tracked setup not found")
    return updated
