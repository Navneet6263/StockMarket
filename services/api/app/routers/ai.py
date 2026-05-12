"""Router for AI-powered stock intelligence."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends

from app.core.dependencies import get_market_hub
from app.services.ai_intelligence import get_ai_analysis
from app.services.finnhub_data import get_stock_intelligence
from app.services.gemini_ai import gemini_runtime_status
from app.services.market_hub import MarketHubService

router = APIRouter(prefix="/api/ai", tags=["ai"])
logger = logging.getLogger(__name__)


@router.get("/analyze/{symbol}")
async def ai_analyze(symbol: str, hub: MarketHubService = Depends(get_market_hub)):
    """Full AI analysis — Finnhub + Gemini + Technical combined."""
    try:
        detail = await asyncio.to_thread(hub.get_stock_detail, symbol)
        technical = detail.get("prediction", {})
        market_ctx = detail.get("macro_context", {})
        result = await asyncio.to_thread(get_ai_analysis, symbol, technical, market_ctx)
        return result
    except Exception as exc:
        logger.exception("AI analyze failed symbol=%s", symbol)
        return {"error": str(exc), "symbol": symbol}


@router.get("/status")
async def ai_status():
    """Safe AI runtime status for production diagnostics. Does not expose API keys."""
    return {"gemini": gemini_runtime_status()}


@router.get("/news/{symbol}")
async def ai_news(symbol: str):
    """Finnhub news + sentiment for a stock."""
    from app.services.finnhub_data import fetch_news_sentiment
    return await asyncio.to_thread(fetch_news_sentiment, symbol)


@router.get("/earnings/{symbol}")
async def ai_earnings(symbol: str):
    """Earnings calendar for a stock."""
    from app.services.finnhub_data import fetch_earnings
    return await asyncio.to_thread(fetch_earnings, symbol)


@router.get("/insider/{symbol}")
async def ai_insider(symbol: str):
    """Insider activity for a stock."""
    from app.services.finnhub_data import fetch_insider_activity
    return await asyncio.to_thread(fetch_insider_activity, symbol)


@router.get("/intelligence/{symbol}")
async def ai_full_intelligence(symbol: str):
    """All Finnhub data combined (news + earnings + insider + recommendations)."""
    return await asyncio.to_thread(get_stock_intelligence, symbol)


@router.get("/nifty-options")
async def nifty_options_signal(hub: MarketHubService = Depends(get_market_hub)):
    """Nifty CE/PE signal based on Nifty's own chart pattern — breakout/breakdown detection."""
    from app.services.nifty_options import analyze_nifty_for_options
    try:
        nifty_frame = await asyncio.to_thread(hub.data.fetch_history, "NIFTY", "6mo", "1d")
        result = analyze_nifty_for_options(nifty_frame)
        return result
    except Exception as exc:
        logger.exception("nifty options analysis failed")
        return {"signal": "NO_TRADE", "error": str(exc)}
