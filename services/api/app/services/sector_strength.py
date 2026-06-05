"""Sector Relative Strength — static NSE sector map, honest 'unknown' when data missing."""
from __future__ import annotations

import os
import logging
from typing import Dict, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ENABLE_SECTOR_RS: bool = os.getenv("ENABLE_SECTOR_RS", "true").lower() == "true"

# ── Static NSE sector map (extend via SECTOR_MAP_OVERRIDE env if needed) ─────
_SECTOR_MAP: Dict[str, str] = {
    # IT
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT",
    "LTIM": "IT", "MPHASIS": "IT", "PERSISTENT": "IT", "COFORGE": "IT", "KPITTECH": "IT",
    # Banking
    "HDFCBANK": "BANKING", "ICICIBANK": "BANKING", "SBIN": "BANKING", "AXISBANK": "BANKING",
    "KOTAKBANK": "BANKING", "INDUSINDBK": "BANKING", "BANKBARODA": "BANKING",
    "PNB": "BANKING", "CANBK": "BANKING", "FEDERALBNK": "BANKING", "IDFCFIRSTB": "BANKING",
    # NBFC/Finance
    "BAJFINANCE": "NBFC", "BAJAJFINSV": "NBFC", "CHOLAFIN": "NBFC", "SHRIRAMFIN": "NBFC",
    "LICHSGFIN": "NBFC", "MANAPPURAM": "NBFC", "MFSL": "NBFC", "JIOFIN": "NBFC",
    # Pharma
    "SUNPHARMA": "PHARMA", "DRREDDY": "PHARMA", "CIPLA": "PHARMA", "DIVISLAB": "PHARMA",
    "LUPIN": "PHARMA", "AUROPHARMA": "PHARMA", "ALKEM": "PHARMA", "TORNTPHARM": "PHARMA",
    "GLENMARK": "PHARMA", "BIOCON": "PHARMA", "ZYDUSLIFE": "PHARMA",
    # Auto
    "MARUTI": "AUTO", "TATAMOTORS": "AUTO", "M&M": "AUTO", "BAJAJ-AUTO": "AUTO",
    "HEROMOTOCO": "AUTO", "EICHERMOT": "AUTO", "TVSMOTOR": "AUTO", "ASHOKLEY": "AUTO",
    "MOTHERSON": "AUTO", "BALKRISIND": "AUTO", "UNOMINDA": "AUTO",
    # Energy/Oil
    "RELIANCE": "ENERGY", "ONGC": "ENERGY", "BPCL": "ENERGY", "IOC": "ENERGY",
    "GAIL": "ENERGY", "HINDPETRO": "ENERGY", "OIL": "ENERGY", "PETRONET": "ENERGY",
    # Power
    "NTPC": "POWER", "POWERGRID": "POWER", "TATAPOWER": "POWER", "ADANIPOWER": "POWER",
    "NHPC": "POWER", "TORNTPOWER": "POWER", "JSWENERGY": "POWER", "CESC": "POWER",
    # Metals
    "TATASTEEL": "METALS", "JSWSTEEL": "METALS", "HINDALCO": "METALS", "VEDL": "METALS",
    "SAIL": "METALS", "NMDC": "METALS", "HINDCOPPER": "METALS", "NATIONALUM": "METALS",
    # FMCG
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG", "BRITANNIA": "FMCG",
    "DABUR": "FMCG", "GODREJCP": "FMCG", "COLPAL": "FMCG", "MARICO": "FMCG",
    "TATACONSUM": "FMCG", "VBL": "FMCG", "UNITDSPR": "FMCG",
    # Infra/Capital Goods
    "LT": "INFRA", "ABB": "INFRA", "SIEMENS": "INFRA", "HAVELLS": "INFRA",
    "POLYCAB": "INFRA", "CGPOWER": "INFRA", "BHEL": "INFRA", "HAL": "INFRA",
    "BEL": "INFRA", "MAZDOCK": "INFRA", "GRSE": "INFRA", "TIINDIA": "INFRA",
    # Cement
    "ULTRACEMCO": "CEMENT", "SHREECEM": "CEMENT", "AMBUJACEM": "CEMENT",
    "ACC": "CEMENT", "DALBHARAT": "CEMENT",
    # Real Estate
    "DLF": "REALTY", "GODREJPROP": "REALTY", "LODHA": "REALTY", "OBEROIRLTY": "REALTY",
    "PRESTIGE": "REALTY", "PHOENIXLTD": "REALTY",
    # Telecom
    "BHARTIARTL": "TELECOM", "IDEA": "TELECOM",
    # Insurance
    "HDFCLIFE": "INSURANCE", "SBILIFE": "INSURANCE", "ICICIGI": "INSURANCE",
    "ICICIPRULI": "INSURANCE", "LICI": "INSURANCE",
    # Consumer Discretionary
    "TITAN": "CONSUMER", "TRENT": "CONSUMER", "DMART": "CONSUMER", "NYKAA": "CONSUMER",
    "KALYANKJIL": "CONSUMER", "PAGEIND": "CONSUMER",
}


def get_sector(symbol: str) -> str | None:
    return _SECTOR_MAP.get(symbol.upper().replace(".NS", ""))


def compute_sector_strength(
    symbol: str,
    signal_return_20d: float | None,
    all_signals: List[Dict],
) -> Dict:
    """
    Compute sector relative strength for a symbol.
    Uses return_20d from already-computed signals — no extra API calls.
    Returns sectorStrength, relativeStrengthScore, sectorRank, stockSectorRank.
    """
    if not ENABLE_SECTOR_RS:
        return _unknown("sector_rs_disabled")

    sector = get_sector(symbol)
    if not sector:
        return _unknown(f"sector_not_mapped_for_{symbol}")

    if signal_return_20d is None:
        return _unknown("return_20d_unavailable")

    try:
        # Collect all signals in same sector
        sector_peers: List[Dict] = [
            s for s in all_signals
            if get_sector(s.get("symbol", "")) == sector
            and s.get("return_20d") is not None
        ]

        if len(sector_peers) < 2:
            return _unknown(f"too_few_peers_in_{sector}")

        peer_returns = [float(s["return_20d"]) for s in sector_peers]
        sector_avg_return = round(float(np.mean(peer_returns)), 2)
        sector_std = float(np.std(peer_returns)) or 1.0

        # Stock rank inside sector (1 = best)
        sorted_peers = sorted(sector_peers, key=lambda s: float(s["return_20d"]), reverse=True)
        stock_rank = next(
            (i + 1 for i, s in enumerate(sorted_peers) if s.get("symbol", "").upper() == symbol.upper()),
            None,
        )

        rs_score = round((signal_return_20d - sector_avg_return) / sector_std, 2)

        if rs_score >= 1.0:
            strength = "sector_leader"
        elif rs_score >= 0.3:
            strength = "above_sector_avg"
        elif rs_score >= -0.3:
            strength = "in_line_with_sector"
        elif rs_score >= -1.0:
            strength = "below_sector_avg"
        else:
            strength = "sector_laggard"

        reason = (
            f"{symbol} 20d return {signal_return_20d:.1f}% vs sector {sector} avg {sector_avg_return:.1f}% "
            f"({len(sector_peers)} peers). RS score: {rs_score:.2f}."
        )

        logger.debug(
            "[SECTOR] symbol=%s sector=%s rs_score=%.2f rank=%s/%d strength=%s",
            symbol, sector, rs_score, stock_rank, len(sector_peers), strength,
        )

        return {
            "sectorStrength": strength,
            "sector": sector,
            "relativeStrengthScore": rs_score,
            "sectorAvgReturn20d": sector_avg_return,
            "stockReturn20d": round(signal_return_20d, 2),
            "sectorPeerCount": len(sector_peers),
            "stockSectorRank": stock_rank,
            "sectorRank": f"{stock_rank}/{len(sector_peers)}" if stock_rank else "unknown",
            "sectorReason": reason,
            "sectorAvailable": True,
        }

    except Exception as exc:
        logger.warning("[SECTOR] compute failed symbol=%s: %s", symbol, exc)
        return _unknown(f"compute_error: {exc}")


def rank_global_sectors(all_signals: List[Dict]) -> Dict[str, Dict]:
    """
    Ranks all mapped sectors based on their average 20-day return.
    Returns a dict mapping sector name to its global rank data (rank, category: top_3, bottom_3, neutral).
    """
    sector_returns = {}
    for s in all_signals:
        sec = get_sector(s.get("symbol", ""))
        ret = s.get("return_20d")
        if sec and ret is not None:
            if sec not in sector_returns:
                sector_returns[sec] = []
            sector_returns[sec].append(float(ret))
            
    # Calculate averages
    sector_avgs = []
    for sec, returns in sector_returns.items():
        if len(returns) >= 2: # At least 2 stocks
            sector_avgs.append({
                "sector": sec,
                "avg_return": sum(returns) / len(returns)
            })
            
    # Sort sectors descending by average return
    sector_avgs.sort(key=lambda x: x["avg_return"], reverse=True)
    
    ranked_map = {}
    total_sectors = len(sector_avgs)
    
    for i, data in enumerate(sector_avgs):
        rank = i + 1
        category = "neutral"
        if rank <= 3:
            category = "top_3"
        elif total_sectors >= 6 and rank > total_sectors - 3:
            category = "bottom_3"
            
        ranked_map[data["sector"]] = {
            "rank": rank,
            "total_sectors": total_sectors,
            "avg_return": round(data["avg_return"], 2),
            "category": category
        }
        
    return ranked_map


def _unknown(reason: str) -> Dict:
    return {
        "sectorStrength": "unknown",
        "sector": None,
        "relativeStrengthScore": None,
        "sectorAvgReturn20d": None,
        "stockReturn20d": None,
        "sectorPeerCount": 0,
        "stockSectorRank": None,
        "sectorRank": "unknown",
        "sectorReason": reason,
        "sectorAvailable": False,
        "globalSectorCategory": "neutral"
    }
