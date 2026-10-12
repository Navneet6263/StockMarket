from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.fresh_zones.service import get_engine, universe_symbols


router = APIRouter(prefix="/api/fresh-zones", tags=["fresh-zones"])

_nightly_lock = threading.Lock()


class NightlyRequest(BaseModel):
    symbols: list[str] | None = None
    enforce_universe: bool = True


@router.get("/params")
def params() -> dict[str, Any]:
    engine = get_engine()
    return {"version": engine.params.version, "params": engine.params.to_dict(),
            "data": engine.provider.quality().to_dict()}


@router.get("/opportunities")
def opportunities(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """Fresh, eligible zones in IMMINENT/NEAR proximity, ranked."""
    engine = get_engine()
    return {"param_version": engine.params.version, "data": engine.provider.quality().to_dict(),
            "items": engine.ranked_opportunities(limit=limit)}


@router.get("/zones")
def zones(symbol: str | None = None, status: list[str] | None = Query(None), limit: int = Query(200, ge=1, le=2000)):
    engine = get_engine()
    found = engine.store.list_zones(symbol=symbol.upper() if symbol else None, statuses=status, limit=limit)
    return {"count": len(found), "items": [z.to_dict() for z in found]}


@router.get("/zones/{zone_id}")
def zone(zone_id: str) -> dict[str, Any]:
    engine = get_engine()
    found = engine.store.get_zone(zone_id)
    if found is None:
        raise HTTPException(status_code=404, detail="zone not found")
    watch = engine.store.get_watch(zone_id)
    return {"zone": found.to_dict(), "watch": watch.to_dict() if watch else None,
            "events": engine.store.list_events(zone_id=zone_id)}


@router.get("/alerts")
def alerts(date: str | None = None, delivered: bool | None = None, limit: int = Query(100, ge=1, le=1000)):
    return {"items": get_engine().store.list_alerts(ist_date=date, delivered=delivered, limit=limit)}


@router.get("/events")
def events(symbol: str | None = None, zone_id: str | None = None, limit: int = Query(200, ge=1, le=2000)):
    return {"items": get_engine().store.list_events(symbol=symbol.upper() if symbol else None,
                                                    zone_id=zone_id, limit=limit)}


@router.get("/runs")
def runs(limit: int = Query(20, ge=1, le=200)):
    return {"items": get_engine().store.list_runs(limit)}


@router.post("/nightly", status_code=202)
def nightly(request: NightlyRequest) -> dict[str, Any]:
    if not _nightly_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="a nightly run is already in progress")
    symbols = [s.upper() for s in request.symbols] if request.symbols else universe_symbols()

    def _run() -> None:
        try:
            get_engine().run_nightly(symbols, enforce_universe=request.enforce_universe)
        finally:
            _nightly_lock.release()

    threading.Thread(target=_run, name="fresh-zone-nightly", daemon=True).start()
    return {"accepted": True, "symbols": len(symbols)}


@router.post("/monitor")
def monitor() -> dict[str, Any]:
    return get_engine().monitor_cycle()
