from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.dependencies import get_market_hub
from app.services.market_hub import MarketHubService


router = APIRouter(tags=["legacy"])


class PredictRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    horizon: str = Field("5d")
    lookback_years: Optional[float] = Field(None, ge=0.5, le=20)


def _legacy_direction(direction: str) -> str:
    return {"bullish": "up", "bearish": "down"}.get(direction, "neutral")


@router.get("/historical/{symbol}")
async def historical(symbol: str, period: str = Query("6mo"), hub: MarketHubService = Depends(get_market_hub)):
    try:
        return await asyncio.to_thread(hub.get_historical_chart, symbol, period)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/live/{symbol}")
async def live(symbol: str, hub: MarketHubService = Depends(get_market_hub)):
    try:
        payload = await asyncio.to_thread(hub.get_live_payload, symbol)
        if payload.get("prediction"):
            payload["prediction"] = {
                **payload["prediction"],
                "direction": _legacy_direction(payload["prediction"].get("direction", "neutral")),
            }
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/predict")
async def predict(req: PredictRequest, hub: MarketHubService = Depends(get_market_hub)):
    try:
        detail = await asyncio.to_thread(hub.get_stock_detail, req.symbol, False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    prediction = detail["prediction"]
    explanation = detail["explanation"]
    backtest = detail["backtest"]
    return {
        "symbol": detail["symbol"],
        "horizon": req.horizon,
        "direction": _legacy_direction(prediction["direction"]),
        "confidence": round(prediction["confidence"] / 100, 4),
        "probability_up": round(prediction["probability"], 4) if prediction["direction"] == "bullish" else round(1 - prediction["probability"], 4) if prediction["direction"] == "bearish" else 0.5,
        "probability_down": round(1 - prediction["probability"], 4) if prediction["direction"] == "bullish" else round(prediction["probability"], 4) if prediction["direction"] == "bearish" else 0.5,
        "predicted_return": round(prediction["expected_move_pct"] / 100, 4),
        "patterns": [],
        "volume_analysis": {
            "volume_ratio": prediction["relative_volume"],
            "intraday_volume_ratio": prediction["intraday_volume_ratio"],
        },
        "market_regime": {
            "macro_risk": detail["macro_context"].get("risk_mode"),
            "global_state": detail["global_context"].get("state"),
        },
        "market_structure": prediction["score_breakdown"],
        "strategy": {
            "action": "BUY" if prediction["direction"] == "bullish" else "SELL" if prediction["direction"] == "bearish" else "WATCH",
            "setup": prediction["setup_label"],
            "is_no_trade": prediction["direction"] == "neutral",
            "no_trade_reason": "Signal confirmation is weak." if prediction["direction"] == "neutral" else "",
            "quality_score": prediction["move_quality"],
            "quality_grade": "A" if prediction["confidence"] >= 80 else "B" if prediction["confidence"] >= 65 else "C" if prediction["confidence"] >= 55 else "D",
        },
        "trade_plan": {
            "entry_price": prediction["current_price"],
            "stop_loss": prediction["invalidation"],
            "target_1": round(prediction["current_price"] * (1 + (prediction["expected_move_pct"] / 100)), 2) if prediction["direction"] == "bullish" else round(prediction["current_price"] * (1 - abs(prediction["expected_move_pct"]) / 100), 2) if prediction["direction"] == "bearish" else prediction["current_price"],
            "target_2": round(prediction["current_price"] * (1 + (prediction["expected_move_pct"] / 100) * 1.6), 2) if prediction["direction"] == "bullish" else round(prediction["current_price"] * (1 - abs(prediction["expected_move_pct"]) / 100 * 1.6), 2) if prediction["direction"] == "bearish" else prediction["current_price"],
            "expected_move_pct": prediction["expected_move_pct"],
            "risk_reward_ratio": round(abs(prediction["expected_move_pct"]) / max(abs((prediction["current_price"] - (prediction["invalidation"] or prediction["current_price"])) / prediction["current_price"] * 100), 0.5), 2),
        },
        "backtest": backtest,
        "reasons": prediction["reasons"],
        "risk_management": prediction["risk_factors"],
        "support": prediction["support"],
        "resistance": prediction["resistance"],
        "risk_reward_ratio": round(abs(prediction["expected_move_pct"]) / max(abs((prediction["current_price"] - (prediction["invalidation"] or prediction["current_price"])) / prediction["current_price"] * 100), 0.5), 2),
        "macro_context": detail["macro_context"],
        "news_context": detail["news_context"],
        "global_context": detail["global_context"],
        "company_context": detail["company_context"],
        "explanation": explanation,
        "quality_score": prediction["move_quality"],
        "quality_grade": "A" if prediction["confidence"] >= 80 else "B" if prediction["confidence"] >= 65 else "C" if prediction["confidence"] >= 55 else "D",
        "model_type": "explainable_factor_engine",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "prediction_id": None,
    }


@router.get("/backtest/{symbol}")
async def legacy_backtest(symbol: str, hub: MarketHubService = Depends(get_market_hub)):
    try:
        return await asyncio.to_thread(hub.get_backtest, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/market/discovery")
async def market_discovery(force_refresh: bool = False, hub: MarketHubService = Depends(get_market_hub)):
    payload = await asyncio.to_thread(hub.scan_market, force_refresh)
    return payload["market_discovery"]


@router.get("/opportunities/scan")
async def opportunities_scan(force_refresh: bool = False, hub: MarketHubService = Depends(get_market_hub)):
    return await asyncio.to_thread(hub.scan_market, force_refresh)


@router.get("/company/{symbol}/research")
async def company_research(symbol: str, hub: MarketHubService = Depends(get_market_hub)):
    return (await asyncio.to_thread(hub.get_stock_detail, symbol))["company_context"]


@router.get("/dashboard/stats")
async def dashboard_stats(force_refresh: bool = False, hub: MarketHubService = Depends(get_market_hub)):
    payload = await asyncio.to_thread(hub.scan_market, force_refresh)
    breadth = payload["market_breadth"]
    return {
        "scanner": {
            "last_scan": payload["generated_at"],
            "total_stocks": payload["universe_size"],
            "active_signals": payload["summary"]["high_priority"],
        },
        "open_signals": payload["summary"]["watchlist"],
        "performance": {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": round((payload["summary"]["high_priority"] / max(payload["universe_size"], 1)) * 100, 1),
            "avg_pnl_pct": 0,
            "total_pnl_pct": 0,
            "avg_win_pct": 0,
            "avg_loss_pct": 0,
            "profit_factor": 0,
        },
        "breadth": breadth,
    }
