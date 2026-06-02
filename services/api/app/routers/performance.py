from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import logging

from app.services.performance_tracker import get_performance_tracker
from app.services.angelone_live import get_angelone_live
from app.core.dependencies import get_market_hub

router = APIRouter(prefix="/api/performance", tags=["performance"])
logger = logging.getLogger(__name__)

active_perf_websockets: set[WebSocket] = set()
active_perf_websockets_lock = asyncio.Lock()


def _attach_live_price(pos: dict) -> dict:
    """Attach current_price + unrealized PnL to an open position dict."""
    try:
        live_svc  = get_angelone_live()
        live_data = live_svc.get_live_price(pos["symbol"])
        if not live_data:
            live_data = get_market_hub().data.fetch_live_snapshot(pos["symbol"])
        if live_data:
            current = live_data.get("ltp") or live_data.get("price")
            if current:
                current      = float(current)
                entry        = float(pos.get("actual_entry_price") or pos["entry_price"])
                direction    = pos.get("direction", "bullish")
                pnl          = (current - entry) if direction == "bullish" else (entry - current)
                pnl_pct      = (pnl / entry) * 100 if entry else 0
                pos["current_price"]       = round(current, 2)
                pos["unrealized_pnl"]      = round(pnl, 2)
                pos["unrealized_pnl_pct"]  = round(pnl_pct, 2)
    except Exception:
        pass
    return pos


@router.get("/open")
async def get_open_positions():
    tracker   = get_performance_tracker()
    positions = list(tracker.collection.find(
        {"status": {"$in": ["open", "live_executed"]}}, {"_id": 0}
    ))
    positions = [_attach_live_price(p) for p in positions]
    return {"positions": positions}


@router.get("/history")
async def get_history():
    tracker   = get_performance_tracker()
    positions = list(tracker.collection.find(
        {"status": {"$in": ["closed", "target_hit", "stopped"]}},
        {"_id": 0}
    ).sort("exit_date", -1))
    return {"positions": positions}


@router.post("/close/{symbol}")
async def close_position(symbol: str):
    tracker = get_performance_tracker()
    return tracker.manual_close(symbol.upper())


@router.websocket("/ws")
async def performance_websocket(websocket: WebSocket):
    await websocket.accept()
    async with active_perf_websockets_lock:
        active_perf_websockets.add(websocket)
    try:
        while True:
            tracker   = get_performance_tracker()
            positions = list(tracker.collection.find(
                {"status": {"$in": ["open", "live_executed"]}}, {"_id": 0}
            ))
            positions = [_attach_live_price(p) for p in positions]
            await websocket.send_json({
                "type":           "PERFORMANCE_SNAPSHOT",
                "open_positions": positions,
            })
            await asyncio.sleep(5.0)
    except WebSocketDisconnect:
        async with active_perf_websockets_lock:
            active_perf_websockets.discard(websocket)
    except Exception:
        async with active_perf_websockets_lock:
            active_perf_websockets.discard(websocket)
