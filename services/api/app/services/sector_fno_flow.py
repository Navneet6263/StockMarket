"""
Phase 3.3: Sector-wise F&O Money Flow Service
Groups stocks by sector and calculates net OI change per sector.
Returns top 3 bullish and top 3 bearish sectors.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class SectorFNOFlowService:
    """Analyze sector-wise F&O money flow based on OI changes."""
    
    def __init__(self):
        self._sector_data: Dict[str, Dict[str, Any]] = {}
        logger.info("Sector F&O Flow Service initialized")

    def analyze_sector_flow(
        self,
        stock_oi_data: List[Dict[str, Any]],
        symbol_to_sector: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Analyze sector-wise F&O flow.
        
        Args:
            stock_oi_data: List of dicts with symbol, oi_change_pct, pattern info
            symbol_to_sector: Mapping of symbol to sector name
        
        Returns:
            Dict with top bullish and bearish sectors
        """
        try:
            # Group by sector
            sector_aggregates = defaultdict(lambda: {
                "stocks": [],
                "total_oi_change": 0.0,
                "avg_oi_change": 0.0,
                "bullish_count": 0,
                "bearish_count": 0,
                "patterns": []
            })
            
            for stock_data in stock_oi_data:
                symbol = stock_data.get("symbol")
                sector = symbol_to_sector.get(symbol, "Unknown")
                oi_change = stock_data.get("oi_change_pct", 0.0)
                pattern = stock_data.get("pattern", "neutral")
                
                sector_agg = sector_aggregates[sector]
                sector_agg["stocks"].append(symbol)
                sector_agg["total_oi_change"] += oi_change
                sector_agg["patterns"].append(pattern)
                
                if pattern in ["long_buildup", "short_covering"]:
                    sector_agg["bullish_count"] += 1
                elif pattern in ["short_buildup", "long_unwinding"]:
                    sector_agg["bearish_count"] += 1
            
            # Calculate averages
            sector_scores = []
            for sector, data in sector_aggregates.items():
                stock_count = len(data["stocks"])
                if stock_count == 0:
                    continue
                
                avg_oi_change = data["total_oi_change"] / stock_count
                
                # Bias score: positive for bullish, negative for bearish
                bias_score = (data["bullish_count"] - data["bearish_count"]) / stock_count * 100
                
                # Net flow score combines OI change and bias
                net_flow_score = avg_oi_change + bias_score
                
                sector_scores.append({
                    "sector": sector,
                    "net_flow_score": round(net_flow_score, 2),
                    "avg_oi_change_pct": round(avg_oi_change, 2),
                    "bias_score": round(bias_score, 2),
                    "stock_count": stock_count,
                    "bullish_count": data["bullish_count"],
                    "bearish_count": data["bearish_count"],
                    "stocks": data["stocks"][:10]  # Limit to top 10 stocks
                })
            
            # Sort by net flow score
            sector_scores.sort(key=lambda x: x["net_flow_score"], reverse=True)
            
            # Top 3 bullish and bearish
            top_bullish = sector_scores[:3]
            top_bearish = sector_scores[-3:][::-1]  # Reverse to show most bearish first
            
            return {
                "top_bullish_sectors": top_bullish,
                "top_bearish_sectors": top_bearish,
                "total_sectors_analyzed": len(sector_scores),
                "timestamp": self._get_timestamp()
            }
        
        except Exception as e:
            logger.error(f"Sector flow analysis failed: {e}", exc_info=True)
            return {
                "top_bullish_sectors": [],
                "top_bearish_sectors": [],
                "total_sectors_analyzed": 0,
                "error": str(e)
            }

    def get_sector_summary(self, sector: str, stock_oi_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Get detailed summary for a specific sector.
        
        Args:
            sector: Sector name
            stock_oi_data: OI data for stocks in this sector
        
        Returns:
            Sector summary dict
        """
        try:
            if not stock_oi_data:
                return {
                    "sector": sector,
                    "status": "no_data",
                    "message": "No F&O data available for this sector"
                }
            
            total_oi_change = sum(s.get("oi_change_pct", 0) for s in stock_oi_data)
            avg_oi_change = total_oi_change / len(stock_oi_data)
            
            patterns = [s.get("pattern", "neutral") for s in stock_oi_data]
            bullish_patterns = sum(1 for p in patterns if p in ["long_buildup", "short_covering"])
            bearish_patterns = sum(1 for p in patterns if p in ["short_buildup", "long_unwinding"])
            
            sentiment = "neutral"
            if bullish_patterns > bearish_patterns:
                sentiment = "bullish"
            elif bearish_patterns > bullish_patterns:
                sentiment = "bearish"
            
            return {
                "sector": sector,
                "sentiment": sentiment,
                "avg_oi_change_pct": round(avg_oi_change, 2),
                "stock_count": len(stock_oi_data),
                "bullish_patterns": bullish_patterns,
                "bearish_patterns": bearish_patterns,
                "top_stocks": sorted(
                    stock_oi_data,
                    key=lambda x: abs(x.get("oi_change_pct", 0)),
                    reverse=True
                )[:5]
            }
        
        except Exception as e:
            logger.error(f"Sector summary failed for {sector}: {e}")
            return {
                "sector": sector,
                "status": "error",
                "message": str(e)
            }

    def _get_timestamp(self) -> str:
        """Get current timestamp."""
        try:
            from datetime import datetime
            return datetime.now().isoformat()
        except Exception:
            return ""


# Singleton instance
_sector_flow_instance: SectorFNOFlowService | None = None


def get_sector_fno_flow() -> SectorFNOFlowService:
    """Get the singleton sector F&O flow instance."""
    global _sector_flow_instance
    if _sector_flow_instance is None:
        _sector_flow_instance = SectorFNOFlowService()
    return _sector_flow_instance
