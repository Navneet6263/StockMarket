"""Router for AngelOne live services — live price, delivery, auto orders."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.services.angelone_live import get_angelone_live

router = APIRouter(prefix="/api/live", tags=["live"])
logger = logging.getLogger(__name__)


def _require_auto_order_authorization(provided_key: str | None) -> None:
    """Fail closed: order actions require a separate server-side secret."""

    expected_key = os.getenv("AUTO_ORDER_API_KEY", "").strip()
    if not expected_key:
        raise HTTPException(status_code=503, detail="Auto-order API authorization is not configured.")
    if not provided_key or not hmac.compare_digest(provided_key, expected_key):
        raise HTTPException(status_code=401, detail="Invalid auto-order authorization.")

# Real-time WebSocket connection registry
active_websockets: set[WebSocket] = set()
active_websockets_lock = asyncio.Lock()
global_loop = None

@router.websocket("/ws/live-entries")
async def websocket_endpoint(websocket: WebSocket):
    global global_loop
    global_loop = asyncio.get_running_loop()
    await websocket.accept()
    async with active_websockets_lock:
        active_websockets.add(websocket)
    try:
        from app.services.entry_monitor import get_entry_monitor
        monitor = get_entry_monitor()
        entries = monitor.get_live_entries()
        assessments = monitor.get_live_assessments()
        await websocket.send_json({
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "count": len(entries),
            "watchedCount": monitor.get_watched_count(),
            "entries": entries,
            "assessmentCounts": {
                status: len([item for item in assessments if item.get("status") == status])
                for status in ("CONFIRMED", "WAIT", "REJECT")
            },
            "assessments": assessments,
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        async with active_websockets_lock:
            active_websockets.remove(websocket)
    except Exception:
        async with active_websockets_lock:
            if websocket in active_websockets:
                active_websockets.remove(websocket)

@router.websocket("/ws/chart/{symbol}")
async def chart_websocket(websocket: WebSocket, symbol: str):
    await websocket.accept()
    from app.services.live_chart import get_ohlc_aggregator
    aggregator = get_ohlc_aggregator()
    queue = aggregator.subscribe(symbol.upper())
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        aggregator.unsubscribe(symbol.upper(), queue)
    except Exception:
        aggregator.unsubscribe(symbol.upper(), queue)

@router.get("/chart/history/{symbol}")
async def chart_history(symbol: str):
    """Fetch historical OHLC data for the chart initialization."""
    from app.services.data_provider import MarketDataService
    from app.core.settings import get_settings
    import pandas as pd
    
    try:
        data_service = MarketDataService(get_settings())
        df = await asyncio.to_thread(data_service.fetch_history, symbol, period="5d", interval="15m")
        if df.empty:
            return {"data": []}
        
        candles = []
        for idx, row in df.iterrows():
            candles.append({
                "time": int(idx.timestamp()),
                "open": float(row['Open']),
                "high": float(row['High']),
                "low": float(row['Low']),
                "close": float(row['Close']),
            })
        return {"data": candles[-100:]}
    except Exception as e:
        logger.warning(f"Failed to fetch chart history for {symbol}: {e}")
        return {"data": []}

active_options_websockets: set[WebSocket] = set()
active_options_websockets_lock = asyncio.Lock()

@router.websocket("/ws/options/{symbol}")
async def options_websocket(websocket: WebSocket, symbol: str):
    await websocket.accept()
    async with active_options_websockets_lock:
        active_options_websockets.add(websocket)
        
    from app.services.options_chain import get_live_options_chain
    chain_svc = get_live_options_chain()
    queue = asyncio.Queue()
    chain_svc.subscribe_ws(symbol.upper(), queue)
    
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        chain_svc.unsubscribe_ws(symbol.upper(), queue)
        async with active_options_websockets_lock:
            active_options_websockets.remove(websocket)
    except Exception:
        chain_svc.unsubscribe_ws(symbol.upper(), queue)
        async with active_options_websockets_lock:
            if websocket in active_options_websockets:
                active_options_websockets.remove(websocket)

async def _broadcast_payload(payload: dict):
    async with active_websockets_lock:
        websockets = list(active_websockets)
    for ws in websockets:
        try:
            await ws.send_json(payload)
        except Exception:
            pass

def on_live_entry_triggered(alert: dict):
    global global_loop
    if not global_loop:
        return
    try:
        from app.services.entry_monitor import get_entry_monitor
        monitor = get_entry_monitor()
        entries = monitor.get_live_entries()
        assessments = monitor.get_live_assessments()
        payload = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "count": len(entries),
            "watchedCount": monitor.get_watched_count(),
            "entries": entries,
            "assessmentCounts": {
                status: len([item for item in assessments if item.get("status") == status])
                for status in ("CONFIRMED", "WAIT", "REJECT")
            },
            "assessments": assessments,
            "new_alert": alert,
        }
        asyncio.run_coroutine_threadsafe(_broadcast_payload(payload), global_loop)
        
        # Save to performance tracker
        try:
            from app.services.performance_tracker import get_performance_tracker
            get_performance_tracker().save_new_entry(alert)
        except Exception as e:
            logger.error("Failed to save performance entry: %s", e)
            
    except Exception:
        pass

# Register callback with entry monitor
try:
    from app.services.entry_monitor import get_entry_monitor
    get_entry_monitor().subscribe(on_live_entry_triggered)
except Exception as e:
    logger.warning("Failed to register WS callback with entry monitor: %s", e)

# Register callback with OHLC Aggregator
try:
    from app.services.angelone_live import get_angelone_live
    from app.services.live_chart import get_ohlc_aggregator
    aggregator = get_ohlc_aggregator()
    live_svc = get_angelone_live()
    if live_svc and live_svc.feed:
        live_svc.feed.subscribe(aggregator.process_tick)
except Exception as e:
    logger.warning("Failed to register OHLCAggregator with live feed: %s", e)


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
        from app.services.entry_monitor import get_entry_monitor
        monitor = get_entry_monitor()
        symbols = monitor.get_watched_symbols()
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
    """Return AngelOne traded-volume context; broker candles do not supply delivery fields."""
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
    quantity: int = Query(1, ge=1, le=1_000_000),
    x_auto_order_key: str | None = Header(default=None, alias="X-Auto-Order-Key"),
):
    """Validate an order request; execution stays locked without native exits."""
    _require_auto_order_authorization(x_auto_order_key)
    svc = get_angelone_live()
    result = await asyncio.to_thread(
        svc.place_order, symbol, direction, entry_price, stop_loss, target, quantity
    )
    return result


@router.get("/orders/today")
async def today_orders(
    x_auto_order_key: str | None = Header(default=None, alias="X-Auto-Order-Key"),
):
    """Get all auto orders placed today."""
    _require_auto_order_authorization(x_auto_order_key)
    svc = get_angelone_live()
    return {"orders": svc.orders.get_today_orders(), "status": svc.get_status()}


@router.get("/status")
async def live_status():
    """Get status of all live services."""
    svc = get_angelone_live()
    return svc.get_status()
