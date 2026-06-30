"""
Phase 3.2: OI Change Tracking Service
Tracks Open Interest changes vs previous day to detect patterns:
- Short buildup (OI↑ + Price↓)
- Short covering (OI↓ + Price↑)
- Long buildup (OI↑ + Price↑)
- Long unwinding (OI↓ + Price↓)
"""

from __future__ import annotations

import logging
from typing import Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class OITrackerService:
    """Track and analyze OI changes to identify market patterns."""
    
    def __init__(self):
        self._oi_cache: Dict[str, Dict[str, Any]] = {}
        logger.info("OI Tracker Service initialized")

    def analyze_oi_change(
        self, 
        symbol: str,
        current_oi: int,
        previous_oi: int,
        current_price: float,
        previous_price: float
    ) -> Dict[str, Any]:
        """
        Analyze OI and price changes to detect trading patterns.
        
        Args:
            symbol: Stock symbol
            current_oi: Current Open Interest
            previous_oi: Previous day's Open Interest
            current_price: Current price
            previous_price: Previous day's price
        
        Returns:
            Dict with pattern analysis
        """
        try:
            # Calculate changes
            if previous_oi == 0:
                oi_change_pct = 0.0
            else:
                oi_change_pct = ((current_oi - previous_oi) / previous_oi) * 100
            
            if previous_price == 0:
                price_change_pct = 0.0
            else:
                price_change_pct = ((current_price - previous_price) / previous_price) * 100
            
            # Detect pattern
            pattern = self._detect_pattern(oi_change_pct, price_change_pct)
            
            # Calculate strength (0-100 scale)
            strength = self._calculate_strength(oi_change_pct, price_change_pct)
            
            return {
                "symbol": symbol,
                "pattern": pattern,
                "oi_change_pct": round(oi_change_pct, 2),
                "price_change_pct": round(price_change_pct, 2),
                "strength": strength,
                "interpretation": self._get_interpretation(pattern, strength),
                "current_oi": current_oi,
                "previous_oi": previous_oi,
                "timestamp": datetime.now().isoformat()
            }
        
        except Exception as e:
            logger.error(f"OI analysis failed for {symbol}: {e}", exc_info=True)
            return {
                "symbol": symbol,
                "pattern": "unknown",
                "oi_change_pct": 0.0,
                "price_change_pct": 0.0,
                "strength": 0,
                "interpretation": "Analysis unavailable",
                "error": str(e)
            }

    def _detect_pattern(self, oi_change_pct: float, price_change_pct: float) -> str:
        """
        Detect the OI/price pattern.
        
        Returns:
            Pattern name: short_buildup, short_covering, long_buildup, long_unwinding, or neutral
        """
        try:
            OI_THRESHOLD = 2.0  # Min 2% OI change to be significant
            PRICE_THRESHOLD = 0.5  # Min 0.5% price change
            
            oi_increased = oi_change_pct > OI_THRESHOLD
            oi_decreased = oi_change_pct < -OI_THRESHOLD
            price_increased = price_change_pct > PRICE_THRESHOLD
            price_decreased = price_change_pct < -PRICE_THRESHOLD
            
            if oi_increased and price_decreased:
                return "short_buildup"
            elif oi_decreased and price_increased:
                return "short_covering"
            elif oi_increased and price_increased:
                return "long_buildup"
            elif oi_decreased and price_decreased:
                return "long_unwinding"
            else:
                return "neutral"
        
        except Exception as e:
            logger.warning(f"Pattern detection failed: {e}")
            return "neutral"

    def _calculate_strength(self, oi_change_pct: float, price_change_pct: float) -> int:
        """
        Calculate pattern strength on a 0-100 scale.
        
        Args:
            oi_change_pct: OI change percentage
            price_change_pct: Price change percentage
        
        Returns:
            Strength score (0-100)
        """
        try:
            # Combined magnitude of changes
            oi_magnitude = abs(oi_change_pct)
            price_magnitude = abs(price_change_pct)
            
            # Weight OI changes more heavily (institutional activity indicator)
            strength = (oi_magnitude * 0.6 + price_magnitude * 0.4) * 5
            
            # Cap at 100
            return min(100, int(strength))
        
        except Exception:
            return 0

    def _get_interpretation(self, pattern: str, strength: int) -> str:
        """
        Provide human-readable interpretation of the pattern.
        
        Args:
            pattern: Detected pattern name
            strength: Pattern strength (0-100)
        
        Returns:
            Human-readable interpretation string
        """
        try:
            interpretations = {
                "short_buildup": f"Short Buildup (Bearish) - Strength: {strength}/100. Traders adding shorts as price falls.",
                "short_covering": f"Short Covering (Bullish) - Strength: {strength}/100. Shorts closing positions as price rises.",
                "long_buildup": f"Long Buildup (Bullish) - Strength: {strength}/100. Fresh longs entering as price rises.",
                "long_unwinding": f"Long Unwinding (Bearish) - Strength: {strength}/100. Longs exiting as price falls.",
                "neutral": f"Neutral - Strength: {strength}/100. No clear directional pattern.",
                "unknown": "Pattern analysis unavailable"
            }
            
            return interpretations.get(pattern, "Unknown pattern")
        
        except Exception:
            return "Interpretation unavailable"

    def update_cache(self, symbol: str, oi: int, price: float):
        """
        Update the OI cache for a symbol.
        
        Args:
            symbol: Stock symbol
            oi: Current Open Interest
            price: Current price
        """
        try:
            self._oi_cache[symbol] = {
                "oi": oi,
                "price": price,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            logger.warning(f"Failed to update OI cache for {symbol}: {e}")

    def get_cached_oi(self, symbol: str) -> Dict[str, Any] | None:
        """
        Get cached OI data for a symbol.
        
        Args:
            symbol: Stock symbol
        
        Returns:
            Cached OI data or None
        """
        try:
            return self._oi_cache.get(symbol)
        except Exception:
            return None


# Singleton instance
_oi_tracker_instance: OITrackerService | None = None


def get_oi_tracker() -> OITrackerService:
    """Get the singleton OI tracker instance."""
    global _oi_tracker_instance
    if _oi_tracker_instance is None:
        _oi_tracker_instance = OITrackerService()
    return _oi_tracker_instance
