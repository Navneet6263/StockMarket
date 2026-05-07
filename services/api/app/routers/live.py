"""Router for AngelOne live services — live price, delivery, auto orders."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query

from app.services.angelone_live import get_angelone_live

router = APIRouter(prefix="/api/live", tags=["live"])
logger = logging.getLogger(__name__)


@router.get("/prices")
async def live_prices():
    """Get all real-time prices from WebSocket feed."""
    svc = get_angelone_live()
    return {"prices": svc.get_all_live_prices(), "status": svc.get_status()}


@router.get("/price/{symbol}")
async def live_price(symbol: str):
    """Get real-time price for a single symbol."""
    svc = get_angelone_live()
    price = svc.get_live_price(symbol)
    return {"symbol": symbol.upper(), "data": price, "source": "angelone_websocket" if price else "not_streaming"}


@router.post("/feed/start")
async def start_feed(symbols: list[str] | None = None):
    """Start WebSocket live feed for given symbols (or top 50 from universe)."""
    svc = get_angelone_live()
    if not symbols:
        from app.services.market_hub import MarketHubService
        from app.core.dependencies import get_market_hub
        hub = get_market_hub()
        discovery = await asyncio.to_thread(hub.universe.discover_market)
        symbols = (discovery.get("scan_symbols") or discovery.get("symbols") or [])[:50]
    svc.start_feed(symbols)
    return {"started": True, "symbols_count": len(symbols), "status": svc.get_status()}


@router.get("/delivery/{symbol}")
async def delivery_data(symbol: str):
    """Fetch delivery volume data from AngelOne."""
    svc = get_angelone_live()
    data = await asyncio.to_thread(svc.get_delivery, symbol)
    return {"symbol": symbol.upper(), **data}


@router.post("/order")
async def place_order(
    symbol: str,
    direction: str = Query(..., pattern="^(bullish|bearish)$"),
    entry_price: float = Query(..., gt=0),
    stop_loss: float = Query(..., gt=0),
    target: float = Query(..., gt=0),
    quantity: int = Query(1, ge=1),
):
    """Place GTT auto order via AngelOne. Requires ENABLE_AUTO_ORDER=true."""
    svc = get_angelone_live()
    result = await asyncio.to_thread(
        svc.place_order, symbol, direction, entry_price, stop_loss, target, quantity
    )
    return result


@router.get("/orders/today")
async def today_orders():
    """Get all auto orders placed today."""
    svc = get_angelone_live()
    return {"orders": svc.orders.get_today_orders(), "status": svc.get_status()}


@router.get("/status")
async def live_status():
    """Get status of all live services."""
    svc = get_angelone_live()
    return svc.get_status()
