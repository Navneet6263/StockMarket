from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_market_hub
from app.options_analyzer import OptionsAnalyzer
from app.services.hot_picks import build_hot_picks_response
from app.services.market_hub import MarketHubService


router = APIRouter(prefix="/api", tags=["market"])
logger = logging.getLogger(__name__)


async def scan_market_with_timeout(hub: MarketHubService, force_refresh: bool):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(hub.scan_market, force_refresh),
            timeout=hub.settings.scan_refresh_timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.warning("market overview request timed out; returning fallback cache if available")
        return await asyncio.to_thread(hub.scan_market, False)


@router.get("/market/overview")
async def market_overview(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    return await scan_market_with_timeout(hub, force_refresh)


@router.get("/market/hot-picks")
async def market_hot_picks(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await scan_market_with_timeout(hub, force_refresh)
    try:
        tracker_dashboard = await asyncio.to_thread(hub.get_tracker_dashboard)
    except Exception:
        logger.exception("tracker dashboard failed while building hot picks")
        tracker_dashboard = None
    base = build_hot_picks_response(payload, tracker_dashboard)
    return {
        **base,
        "marketContext": payload.get("marketContext", {}),
        "smartHotPicks": payload.get("hotPicks", []),
        "smartBaseFormationRadar": payload.get("baseFormationRadar", []),
        "smartMomentumRadar": payload.get("momentumRadar", []),
        "blockedBuys": payload.get("blockedBuys", []),
        "smartDebug": payload.get("smartDebug", {}),
    }


@router.get("/market/opportunities")
async def market_opportunities(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await scan_market_with_timeout(hub, force_refresh)
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
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {"generated_at": payload["generated_at"], "results": payload["unusual_volume"]}


@router.get("/market/top-movers")
async def market_top_movers(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {"generated_at": payload["generated_at"], "results": payload["top_movers"]}


@router.get("/scanner/breakouts")
async def scanner_breakouts(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {"generated_at": payload["generated_at"], "results": payload["breakout_candidates"]}


@router.get("/scanner/bearish-risk")
async def scanner_bearish_risk(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {"generated_at": payload["generated_at"], "results": payload["bearish_risks"]}


@router.get("/market/context")
async def market_context(
    symbol: str = Query("NIFTY"),
    limit: int = Query(6, ge=1, le=10),
    hub: MarketHubService = Depends(get_market_hub),
):
    return await asyncio.to_thread(hub.get_market_context, symbol, limit)


@router.get("/market/candidates")
async def market_candidates(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    """Medium-conviction setups forming — Watchlist / Candidates bucket."""
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "candidates": payload.get("candidates", []),
        "count": len(payload.get("candidates", [])),
        "summary": {
            "candidates_count": payload.get("summary", {}).get("candidates_count", 0),
        },
    }


@router.get("/market/fast-movers")
async def market_fast_movers(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    """Fast movers and missed moves — big % change or volume shock."""
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "fast_movers": payload.get("fast_movers_missed_moves", []),
        "count": len(payload.get("fast_movers_missed_moves", [])),
        "summary": {
            "fast_movers_count": payload.get("summary", {}).get("fast_movers_count", 0),
        },
    }


@router.get("/market/pre-breakout")
async def market_pre_breakout(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    """Pre-breakout watch setups. These are alert-only, not buy calls."""
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "pre_breakout_setups": payload.get("pre_breakout_setups", []),
        "pattern_forming_setups": payload.get("pattern_forming_setups", payload.get("pre_breakout_setups", [])),
        "alert_above_setups": payload.get("alert_above_setups", []),
        "count": len(payload.get("pre_breakout_setups", [])),
        "summary": {
            "pre_breakout_count": payload.get("summary", {}).get("pre_breakout_count", 0),
            "alert_above_count": payload.get("summary", {}).get("alert_above_count", 0),
        },
    }


@router.get("/market/missed-moves")
async def market_missed_moves(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "missed_moves_analysis": payload.get("missed_moves_analysis", []),
        "count": len(payload.get("missed_moves_analysis", [])),
        "summary": {
            "missed_moves_count": payload.get("summary", {}).get("missed_moves_count", 0),
        },
    }


@router.get("/market/avoid-late-entry")
async def market_avoid_late_entry(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "avoid_late_entry": payload.get("avoid_late_entry", []),
        "count": len(payload.get("avoid_late_entry", [])),
        "summary": {
            "avoid_late_entry_count": payload.get("summary", {}).get("avoid_late_entry_count", 0),
        },
    }


@router.get("/market/re-entry")
async def market_re_entry(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "momentum_continuation": payload.get("momentum_continuation", []),
        "re_entry_setups": payload.get("re_entry_setups", payload.get("momentum_continuation", [])),
        "count": len(payload.get("momentum_continuation", [])),
        "summary": {
            "momentum_continuation_count": payload.get("summary", {}).get("momentum_continuation_count", 0),
            "re_entry_count": payload.get("summary", {}).get("re_entry_count", 0),
        },
    }


@router.get("/market/avoid")
async def market_avoid(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    """Avoid / Risky bucket — weak setups, high risk, no-trade signals."""
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "avoid_risky": payload.get("avoid_risky", []),
        "count": len(payload.get("avoid_risky", [])),
        "summary": {
            "avoid_count": payload.get("summary", {}).get("avoid_count", 0),
        },
    }


@router.get("/market/scan-stats")
async def market_scan_stats(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    """Full scan statistics — universe size, bucket counts, skipped symbols."""
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "summary": payload.get("summary", {}),
        "market_discovery": payload.get("market_discovery", {}),
    }


@router.get("/market/nifty-context")
async def market_nifty_context(
    hub: MarketHubService = Depends(get_market_hub),
):
    """
    Financial expert level Nifty analysis.
    Returns trend structure, key S/R levels, momentum quality,
    VIX regime, and plain-language narrative for stock selection.
    """
    return await asyncio.to_thread(hub._get_nifty_context)


@router.get("/market/breakout-radar")
async def market_breakout_radar(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    """
    '1-2 din mein fatne wale' stocks.
    Stocks building a base near resistance with accumulation signals,
    aligned with Nifty direction. These are the setups to watch closely.
    """
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "breakout_radar": payload.get("breakout_radar", []),
        "nifty_context": payload.get("nifty_context", {}),
        "count": len(payload.get("breakout_radar", [])),
        "summary": {
            "nifty_bias": payload.get("summary", {}).get("nifty_bias", "neutral"),
            "nifty_regime": payload.get("summary", {}).get("nifty_regime", "unknown"),
            "market_score": payload.get("summary", {}).get("market_score", 50),
            "breakout_radar_count": payload.get("summary", {}).get("breakout_radar_count", 0),
        },
    }


@router.get("/market/traps")
async def market_traps(
    force_refresh: bool = False,
    hub: MarketHubService = Depends(get_market_hub),
):
    """Institutional trap signals (bull traps, sell traps, etc.)"""
    payload = await scan_market_with_timeout(hub, force_refresh)
    return {
        "generated_at": payload["generated_at"],
        "trap_signals": payload.get("trap_signals", []),
        "count": len(payload.get("trap_signals", [])),
        "summary": {
            "trap_signals_count": payload.get("summary", {}).get("trap_signals_count", 0),
        },
    }


@router.get("/market/options-analysis")
async def market_options_analysis(
    symbol: str = Query("NIFTY"),
):
    """Option chain analysis for a given symbol (mostly used for indices like NIFTY/BANKNIFTY)"""
    try:
        analyzer = OptionsAnalyzer()
        data = await asyncio.to_thread(analyzer.get_nifty_options_chain, symbol)
        return analyzer.analyze_options_data(data)
    except Exception as e:
        logger.exception("Failed to analyze options for %s", symbol)
        return {"error": str(e), "symbol": symbol}
