"""
Phase 4.1: Missed Opportunities Tracking
Tracks 10%+ movers that the scanner missed and analyzes why.
Stores results in MongoDB for ML training improvement.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Any
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)


class MissedOpportunitiesService:
    """Track and analyze missed trading opportunities for ML improvement."""
    
    def __init__(self, settings=None):
        self.settings = settings
        self._mongo_client = None
        self._db = None
        logger.info("Missed Opportunities Service initialized")

    def _get_mongo_db(self):
        """Get MongoDB database connection (lazy initialization)."""
        if self._db is not None:
            return self._db
        
        try:
            from pymongo import MongoClient
            
            mongo_uri = "mongodb://localhost:27017/"
            if self.settings and hasattr(self.settings, 'mongodb_uri'):
                mongo_uri = self.settings.mongodb_uri
            
            self._mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
            self._db = self._mongo_client["trading_system"]
            
            # Create indexes
            self._db.missed_trades.create_index([("date", -1), ("symbol", 1)])
            self._db.missed_trades.create_index([("why_missed", 1)])
            
            logger.info("MongoDB connection established for missed opportunities")
            return self._db
        
        except Exception as e:
            logger.warning(f"MongoDB connection failed: {e}. Using in-memory fallback.")
            return None

    def track_missed_opportunity(
        self,
        symbol: str,
        move_pct: float,
        scan_date: date,
        why_missed: str,
        pre_filter_reason: str | None = None,
        actual_score: int = 0,
        scanned_signals: List[Dict] = None
    ) -> Dict[str, Any]:
        """
        Record a missed trading opportunity.
        
        Args:
            symbol: Stock symbol
            move_pct: Percentage move that was missed
            scan_date: Date of the scan
            why_missed: Reason code (pre_filter_rejected, score_too_low, etc.)
            pre_filter_reason: Details if pre-filter rejected
            actual_score: Score the stock would have gotten
            scanned_signals: All signals from that day's scan
        
        Returns:
            Dict with tracking result
        """
        try:
            db = self._get_mongo_db()
            
            record = {
                "symbol": symbol,
                "date": scan_date.isoformat(),
                "move_pct": round(move_pct, 2),
                "why_missed": why_missed,
                "pre_filter_reason": pre_filter_reason,
                "actual_score": actual_score,
                "should_have_been": self._classify_opportunity(move_pct, actual_score),
                "created_at": datetime.now().isoformat()
            }
            
            if db is not None:
                # Store in MongoDB
                result = db.missed_trades.insert_one(record)
                record["_id"] = str(result.inserted_id)
                logger.info(f"Tracked missed opportunity: {symbol} ({move_pct:.1f}%) - {why_missed}")
            else:
                # In-memory fallback (no persistence)
                logger.warning(f"Missed opportunity tracked in-memory only: {symbol}")
            
            return {
                "status": "tracked",
                "record": record
            }
        
        except Exception as e:
            logger.error(f"Failed to track missed opportunity for {symbol}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e)
            }

    def find_missed_movers(
        self,
        scan_date: date,
        scanned_symbols: List[str],
        market_data: Dict[str, Dict[str, Any]],
        min_move_pct: float = 10.0
    ) -> List[Dict[str, Any]]:
        """
        Find stocks that moved 10%+ but weren't in top opportunities.
        
        Args:
            scan_date: Date to analyze
            scanned_symbols: Symbols that were scanned
            market_data: Dict of symbol -> data with price changes
            min_move_pct: Minimum move to consider (default 10%)
        
        Returns:
            List of missed movers
        """
        try:
            missed_movers = []
            
            for symbol, data in market_data.items():
                move_pct = abs(data.get("change_pct", 0))
                
                if move_pct < min_move_pct:
                    continue
                
                # Determine why it was missed
                if symbol not in scanned_symbols:
                    why_missed = "not_in_scanned_universe"
                    pre_filter_reason = "Symbol was not included in scanner universe"
                elif data.get("in_top_opportunities"):
                    continue  # Not missed - was in top opportunities
                else:
                    why_missed = "in_universe_but_not_surfaced"
                    pre_filter_reason = self._analyze_why_not_surfaced(data)
                
                missed_movers.append({
                    "symbol": symbol,
                    "move_pct": move_pct,
                    "scan_date": scan_date,
                    "why_missed": why_missed,
                    "pre_filter_reason": pre_filter_reason,
                    "actual_score": data.get("score", 0),
                    "data": data
                })
            
            return missed_movers
        
        except Exception as e:
            logger.error(f"Failed to find missed movers: {e}", exc_info=True)
            return []

    def _analyze_why_not_surfaced(self, data: Dict[str, Any]) -> str:
        """
        Analyze why a stock wasn't surfaced in scanner results.
        
        Args:
            data: Stock data dict
        
        Returns:
            Reason string
        """
        try:
            reasons = []
            
            # Check common pre-filters
            if data.get("return_5d", 0) > 14:
                reasons.append("return_5d exceeded threshold")
            
            if data.get("volume", 0) < 100000:
                reasons.append("volume too low")
            
            if data.get("rsi", 50) > 75:
                reasons.append("RSI overbought")
            elif data.get("rsi", 50) < 25:
                reasons.append("RSI oversold")
            
            if data.get("score", 0) < 50:
                reasons.append("score below threshold")
            
            if not reasons:
                return "Unknown - passed pre-filters but not surfaced"
            
            return "; ".join(reasons)
        
        except Exception:
            return "Analysis failed"

    def _classify_opportunity(self, move_pct: float, score: int) -> str:
        """
        Classify how good the missed opportunity was.
        
        Args:
            move_pct: Percentage move
            score: Opportunity score
        
        Returns:
            Classification string
        """
        try:
            abs_move = abs(move_pct)
            
            if abs_move >= 15 and score >= 70:
                return "top_opportunity"
            elif abs_move >= 10 and score >= 60:
                return "strong_opportunity"
            elif abs_move >= 10:
                return "moderate_opportunity"
            else:
                return "minor_opportunity"
        
        except Exception:
            return "unclassified"

    def get_missed_opportunities_summary(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 50
    ) -> Dict[str, Any]:
        """
        Get summary of missed opportunities over a date range.
        
        Args:
            start_date: Start date (default: 30 days ago)
            end_date: End date (default: today)
            limit: Max records to return
        
        Returns:
            Summary dict
        """
        try:
            db = self._get_mongo_db()
            
            if db is None:
                return {
                    "status": "unavailable",
                    "message": "MongoDB not available"
                }
            
            # Default date range
            if end_date is None:
                end_date = date.today()
            if start_date is None:
                start_date = end_date - timedelta(days=30)
            
            # Query missed trades
            query = {
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lte": end_date.isoformat()
                }
            }
            
            cursor = db.missed_trades.find(query).sort("date", -1).limit(limit)
            records = list(cursor)
            
            # Convert ObjectId to string
            for record in records:
                if "_id" in record:
                    record["_id"] = str(record["_id"])
            
            # Aggregate statistics
            total_missed = len(records)
            avg_move = sum(r.get("move_pct", 0) for r in records) / max(total_missed, 1)
            
            # Group by reason
            reasons_count = {}
            for record in records:
                reason = record.get("why_missed", "unknown")
                reasons_count[reason] = reasons_count.get(reason, 0) + 1
            
            return {
                "status": "success",
                "total_missed": total_missed,
                "avg_move_pct": round(avg_move, 2),
                "date_range": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat()
                },
                "reasons_breakdown": reasons_count,
                "records": records[:20]  # Return top 20
            }
        
        except Exception as e:
            logger.error(f"Failed to get missed opportunities summary: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e)
            }


# Singleton instance
_missed_opp_instance: MissedOpportunitiesService | None = None


def get_missed_opportunities_service(settings=None) -> MissedOpportunitiesService:
    """Get the singleton missed opportunities service instance."""
    global _missed_opp_instance
    if _missed_opp_instance is None:
        _missed_opp_instance = MissedOpportunitiesService(settings)
    return _missed_opp_instance
