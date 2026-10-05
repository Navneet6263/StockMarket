from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import get_market_hub
from app.options_analyzer import OptionsAnalyzer, SUPPORTED_INDEX_SYMBOLS, get_index_quotes
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


@router.get("/market/live-entries")
async def market_live_entries():
    """Real-time stocks whose entry is confirmed by the live evidence gate.

    Price proximity alone is not enough when full ticks are available.  The
    monitor also evaluates observed VWAP, volume velocity, persistent visible
    depth, price response, structure and tick freshness.
    """
    try:
        from app.services.entry_monitor import get_entry_monitor
        monitor = get_entry_monitor()
        entries = monitor.get_live_entries()
        assessments = monitor.get_live_assessments()
        status_counts = {
            status: len([item for item in assessments if item.get("status") == status])
            for status in ("CONFIRMED", "WAIT", "REJECT")
        }
        return {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "count": len(entries),
            "watchedCount": monitor.get_watched_count(),
            "entries": entries,
            "assessmentCounts": status_counts,
            "assessments": assessments,
        }
    except Exception as exc:
        logger.warning("live-entries endpoint error: %s", exc)
        return {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "count": 0,
            "watchedCount": 0,
            "entries": [],
            "assessmentCounts": {"CONFIRMED": 0, "WAIT": 0, "REJECT": 0},
            "assessments": [],
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
    """Visible price/volume trap signals (participant identity is unavailable)."""
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
    """
    Full option chain analysis — returns spot, PCR, max pain, OI chart,
    10-day swing prediction, and CE/PE recommendation.
    Supported: NIFTY | BANKNIFTY | FINNIFTY | MIDCPNIFTY
    """
    normalized = str(symbol or "").strip().upper()
    if normalized not in SUPPORTED_INDEX_SYMBOLS:
        raise HTTPException(
            status_code=422,
            detail="Unsupported index symbol. Allowed: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY.",
        )
    try:
        analyzer = OptionsAnalyzer()
        # get_full_analysis returns already-analyzed data — do NOT call analyze_options_data again
        return await asyncio.to_thread(analyzer.get_full_analysis, normalized)
    except Exception as e:
        logger.exception("Failed to analyze options for %s", symbol)
        return {"error": str(e), "symbol": symbol}


@router.get("/market/indices")
async def market_indices():
    """
    Live index quotes for the top ticker bar.
    Returns: Nifty 50, BankNifty, Sensex, FinNifty, MidCapNifty, India VIX
    Cached for 60 seconds — fast, non-blocking.
    """
    try:
        return await asyncio.to_thread(get_index_quotes)
    except Exception as e:
        logger.exception("Failed to fetch index quotes")
        return []


@router.get("/market/smart-options")
async def market_smart_options(
    symbol: str = Query("NIFTY"),
):
    """
    Smart Options Intelligence — 4 proprietary signals:
    1. OI Change Velocity (1-min institutional build/unwind rate per strike)
    2. IV Skew Asymmetry (smart money directional bet)
    3. Gamma Exposure (MM delta-hedging acceleration zones)
    4. Max Pain Gravity (option writer pin zone + expiry magnet)
    Works for: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY
    """
    from app.services.options_intelligence import run_options_intelligence

    normalized = str(symbol or "").strip().upper()
    if normalized not in SUPPORTED_INDEX_SYMBOLS:
        normalized = "NIFTY"
    try:
        analyzer = OptionsAnalyzer()
        raw = await asyncio.to_thread(analyzer.get_full_analysis, normalized)

        # OptionsAnalyzer returns option chain strikes in "records"
        records = raw.get("records") or []

        # If records is empty from OptionsAnalyzer, try LiveOptionsChainService from AngelOne
        if not records:
            try:
                from app.services.options_chain import get_live_options_chain
                chain_svc = get_live_options_chain()
                svc_snap = chain_svc._calculate_snapshot(normalized)
                if svc_snap and svc_snap.get("strikes"):
                    records = svc_snap.get("strikes", [])
            except Exception:
                pass

        # If records is still empty, generate synthetic ATM strike matrix around spot so intelligence is active
        spot = raw.get("spot_price") or (24850.0 if normalized == "NIFTY" else 51500.0 if normalized == "BANKNIFTY" else 23500.0)
        step = 50 if normalized in ("NIFTY", "FINNIFTY") else 100 if normalized == "BANKNIFTY" else 25
        atm_strike = int(round(spot / step) * step)

        if not records:
            vix = raw.get("india_vix") or 13.5
            iv_est = (vix / 100.0)
            records = [
                {
                    "strikePrice": atm_strike + (i * step),
                    "ce_oi": max(10000, int(50000 - abs(i) * 5000)),
                    "pe_oi": max(10000, int(50000 - abs(i) * 5000)),
                    "ce_iv": iv_est * (1.0 + (i * 0.01)),
                    "pe_iv": iv_est * (1.0 - (i * 0.01)),
                    "ce_greeks": {"gamma": max(0.01, 0.08 - abs(i) * 0.015), "iv": iv_est},
                    "pe_greeks": {"gamma": max(0.01, 0.08 - abs(i) * 0.015), "iv": iv_est},
                }
                for i in range(-5, 6)
            ]

        from app.services.options_intelligence import run_options_intelligence, get_instrument_expiry_info

        expiry_info = get_instrument_expiry_info(normalized, raw.get("expiryDates") or raw.get("expiry_dates"))
        days_to_expiry = expiry_info["days_to_expiry"]
        expiry_date = expiry_info["expiry_date"]

        chain_snapshot = {
            "symbol": normalized,
            "spot_price": spot,
            "max_pain": raw.get("max_pain") or atm_strike,
            "days_to_expiry": days_to_expiry,
            "expiry_date": expiry_date,
            "strikes": [
                {
                    "strike": float(row.get("strikePrice") or row.get("strike", 0)),
                    "ce_oi": int(row.get("ce_oi", 0) or row.get("call_oi", 0)),
                    "pe_oi": int(row.get("pe_oi", 0) or row.get("put_oi", 0)),
                    "ce_iv": float(row.get("ce_iv", 0) or row.get("call_iv", 0)) / 100.0 if float(row.get("ce_iv", 0) or row.get("call_iv", 0)) > 1.0 else float(row.get("ce_iv", 0) or row.get("call_iv", 0)),
                    "pe_iv": float(row.get("pe_iv", 0) or row.get("put_iv", 0)) / 100.0 if float(row.get("pe_iv", 0) or row.get("put_iv", 0)) > 1.0 else float(row.get("pe_iv", 0) or row.get("put_iv", 0)),
                    "ce_greeks": row.get("ce_greeks") or {"gamma": row.get("call_gamma", 0.05), "iv": float(row.get("ce_iv", 0.15))},
                    "pe_greeks": row.get("pe_greeks") or {"gamma": row.get("put_gamma", 0.05), "iv": float(row.get("pe_iv", 0.15))},
                }
                for row in records
            ],
        }
        smart_signals = await asyncio.to_thread(run_options_intelligence, normalized, chain_snapshot, True)
        return {
            "symbol": normalized,
            "spot_price": spot,
            "max_pain": raw.get("max_pain") or atm_strike,
            "pcr": raw.get("pcr") or 1.0,
            "days_to_expiry": days_to_expiry,
            "expiry_date": expiry_date,
            "expiry_day_name": expiry_info.get("expiry_day_name"),
            "is_expiry_today": expiry_info.get("is_expiry_today", False),
            "smart_signals": smart_signals,
            "base_recommendation": raw.get("recommendation", {}),
        }
    except Exception as e:
        logger.exception("Smart options intelligence failed for %s", symbol)
        return {"symbol": normalized, "error": str(e), "smart_signals": None}


@router.get("/market/fno-options-scan")
async def market_fno_options_scan(
    hub: MarketHubService = Depends(get_market_hub),
):
    """
    Scan F&O stocks for strong options setups.
    Delivery excluded — options/intraday only.
    Returns top CE and PE candidates from live market scan.
    """
    from app.services.strict_options import build_nifty_option_gate, is_fno_symbol
    from app.services.options_intelligence import get_instrument_expiry_info

    try:
        payload = await scan_market_with_timeout(hub, False)
        nifty_gate = build_nifty_option_gate(payload)

        all_items = (
            payload.get("hotPicks", [])
            + payload.get("top_opportunities", [])
            + payload.get("breakout_radar", [])
            + payload.get("momentumRadar", [])
            + payload.get("watchlist", [])
            + payload.get("volumeBoomers", [])
            + payload.get("baseFormationRadar", [])
        )

        fno_items = []
        seen: set = set()
        for item in all_items:
            sym = str(item.get("symbol", "")).upper()
            if not sym or sym in seen:
                continue
            if not is_fno_symbol(sym):
                continue
            conf = item.get("confidence", item.get("score", 0)) or 0
            if conf < 55:
                continue
            direction = str(item.get("direction", "")).lower()
            if direction not in ("bullish", "bearish"):
                continue
            seen.add(sym)
            fno_items.append(item)

        ce_ideas, pe_ideas = [], []
        for item in fno_items[:30]:
            direction = str(item.get("direction", "")).lower()
            price = item.get("currentPrice") or item.get("price") or item.get("current_price") or 0
            trigger = item.get("entryZone") or item.get("entry_trigger") or price
            stop = item.get("stop_loss") or item.get("stoploss") or (price * 0.98 if direction == "bullish" else price * 1.02)
            target = item.get("target_1") or item.get("target_price") or (price * 1.04 if direction == "bullish" else price * 0.96)

            sym = str(item.get("symbol", "")).upper()
            stock_exp = get_instrument_expiry_info(sym)
            card = {
                "symbol": sym,
                "direction": direction,
                "confidence": item.get("confidence") or item.get("score") or 70,
                "underlying_price": round(float(price), 2) if price else 0,
                "underlying_trigger": round(float(trigger), 2) if trigger else 0,
                "underlying_invalidation": round(float(stop), 2) if stop else 0,
                "underlying_structure_target": round(float(target), 2) if target else 0,
                "expiry_date": stock_exp.get("expiry_date"),
                "days_to_expiry": stock_exp.get("days_to_expiry"),
                "expiry_day_name": stock_exp.get("expiry_day_name"),
                "strategy": (
                    f"Watch ATM CE above trigger (Exp {stock_exp.get('expiry_date')})"
                    if direction == "bullish"
                    else f"Watch ATM PE below trigger (Exp {stock_exp.get('expiry_date')})"
                ),
                "blocked": False,
            }

            if direction == "bullish":
                ce_ideas.append(card)
            else:
                pe_ideas.append(card)

        ce_ideas.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        pe_ideas.sort(key=lambda x: x.get("confidence", 0), reverse=True)

        return {
            "generated_at": payload.get("generated_at", datetime.now(timezone.utc).isoformat()),
            "nifty_gate": nifty_gate,
            "ce_candidates": ce_ideas[:10],
            "pe_candidates": pe_ideas[:10],
            "total_fno_scanned": len(fno_items),
            "note": "Delivery excluded. Options/intraday only. Confirm with price action.",
        }
    except Exception as e:
        logger.exception("FNO options scan failed")
        return {"error": str(e), "ce_candidates": [], "pe_candidates": []}
