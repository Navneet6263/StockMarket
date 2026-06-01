from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import logging
from typing import Dict, Any

from app.services.performance_tracker import get_performance_tracker
from app.services.angelone_live import get_angelone_live
from app.core.dependencies import get_market_hub

router = APIRouter(prefix="/api/performance", tags=["performance"])
logger = logging.getLogger(__name__)

# Real-time WebSocket connection registry
active_perf_websockets: set[WebSocket] = set()
active_perf_websockets_lock = asyncio.Lock()

@router.get("/open")
async def get_open_positions():
    tracker = get_performance_tracker()
    positions = list(tracker.collection.find({"status": "open"}, {"_id": 0}))
    
    # Attach live prices
    live_svc = get_angelone_live()
    live_prices = live_svc.get_all_live_prices()
    hub = get_market_hub()
    
    for pos in positions:
        symbol = pos["symbol"]
        live_data = live_prices.get(symbol)
        if not live_data:
            try:
                live_data = hub.data.fetch_live_snapshot(symbol)
            except Exception:
                pass
                
        if live_data:
            current = live_data.get("ltp", live_data.get("price"))
            if current:
                pos["current_price"] = current
                entry = pos["entry_price"]
                direction = pos["direction"]
                if direction == "bullish":
                    pos["unrealized_pnl"] = current - entry
                    pos["unrealized_pnl_pct"] = (current - entry) / entry * 100
                else:
                    pos["unrealized_pnl"] = entry - current
                    pos["unrealized_pnl_pct"] = (entry - current) / entry * 100
                    
    return {"positions": positions}

@router.get("/history")
async def get_history(filter: str = "all"):
    # filter logic: this_week, this_month, all
    tracker = get_performance_tracker()
    query = {"status": {"$in": ["closed", "target_hit", "stopped"]}}
    
    # We could parse date and filter by ISO strings if needed, 
    # but for simplicity return all and let frontend filter or implement basic date filtering.
    positions = list(tracker.collection.find(query, {"_id": 0}).sort("exit_date", -1))
    
    return {"positions": positions}

@router.post("/close/{symbol}")
async def close_position(symbol: str):
    tracker = get_performance_tracker()
    result = tracker.manual_close(symbol)
    return result

@router.websocket("/ws")
async def performance_websocket(websocket: WebSocket):
    await websocket.accept()
    async with active_perf_websockets_lock:
        active_perf_websockets.add(websocket)
        
    try:
        while True:
            # Send live payload every 5 seconds
            tracker = get_performance_tracker()
            positions = list(tracker.collection.find({"status": "open"}, {"_id": 0}))
            
            live_svc = get_angelone_live()
            live_prices = live_svc.get_all_live_prices()
            
            for pos in positions:
                symbol = pos["symbol"]
                live_data = live_prices.get(symbol)
                if live_data:
                    current = live_data.get("ltp", 0)
                    if current:
                        pos["current_price"] = current
                        entry = pos["entry_price"]
                        direction = pos["direction"]
                        if direction == "bullish":
                            pos["unrealized_pnl"] = current - entry
                            pos["unrealized_pnl_pct"] = (current - entry) / entry * 100
                        else:
                            pos["unrealized_pnl"] = entry - current
                            pos["unrealized_pnl_pct"] = (entry - current) / entry * 100
                            
            payload = {
                "type": "PERFORMANCE_SNAPSHOT",
                "open_positions": positions
            }
            await websocket.send_json(payload)
            await asyncio.sleep(5.0)
            
    except WebSocketDisconnect:
        async with active_perf_websockets_lock:
            active_perf_websockets.remove(websocket)
    except Exception:
        async with active_perf_websockets_lock:
            if websocket in active_perf_websockets:
                active_perf_websockets.remove(websocket)
