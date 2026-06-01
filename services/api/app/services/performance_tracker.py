import asyncio
import logging
from datetime import datetime, timezone
import pandas as pd
from pymongo import MongoClient

from app.services.angelone_live import get_angelone_live
from app.core.dependencies import get_market_hub

logger = logging.getLogger(__name__)

import os
from dotenv import load_dotenv

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = "stock_predictor_ml"

class PerformanceTrackerService:
    def __init__(self):
        self.client = MongoClient(MONGO_URI)
        self.db = self.client[DB_NAME]
        self.collection = self.db["trade_positions"]

    def save_new_entry(self, alert: dict):
        """Called when entry_monitor fires a BUY/SELL signal."""
        symbol = alert.get("symbol")
        if not symbol:
            return

        # Check if already open
        existing = self.collection.find_one({"symbol": symbol, "status": "open"})
        if existing:
            return

        entry = {
            "symbol": symbol,
            "entry_price": alert.get("entryLevel", alert.get("livePrice")),
            "entry_date": datetime.now(timezone.utc).isoformat(),
            "target1": alert.get("target"),
            "stop_loss": alert.get("stopLoss"),
            "direction": alert.get("direction", "bullish"),
            "confidence": alert.get("confidence", 0),
            "status": "open",
            "exit_price": None,
            "exit_date": None,
            "pnl": None,
            "exit_reason": None,
        }
        
        try:
            self.collection.insert_one(entry)
            logger.info(f"[PERFORMANCE] Saved new open position for {symbol}")
        except Exception as e:
            logger.error(f"[PERFORMANCE] Failed to save entry for {symbol}: {e}")

    def _calculate_rsi(self, symbol: str) -> float:
        """Calculate recent RSI to detect reversals for early exit."""
        try:
            hub = get_market_hub()
            df = hub.data.fetch_history(symbol, period="5d", interval="15m")
            if df.empty or len(df) < 14:
                return 50.0
                
            delta = df['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            return float(rsi.iloc[-1])
        except Exception as e:
            logger.warning(f"[PERFORMANCE] Failed to calculate RSI for {symbol}: {e}")
            return 50.0

    async def check_auto_exits(self):
        """Background loop executed every 5 mins."""
        try:
            open_positions = list(self.collection.find({"status": {"$in": ["open", "live_executed"]}}))
            if not open_positions:
                return

            live_svc = get_angelone_live()
            live_prices = live_svc.get_all_live_prices()
            
            for pos in open_positions:
                symbol = pos["symbol"]
                live_data = live_prices.get(symbol)
                
                # Try quote if live_data not in WS
                if not live_data:
                    try:
                        hub = get_market_hub()
                        live_data = hub.data.fetch_live_snapshot(symbol)
                    except Exception:
                        continue
                        
                if not live_data:
                    continue
                    
                current_price = live_data.get("ltp", live_data.get("price"))
                if not current_price:
                    continue
                    
                entry_price = pos.get("actual_entry_price", pos["entry_price"])
                target1 = pos["target1"]
                stop_loss = pos["stop_loss"]
                direction = pos["direction"]
                
                exit_reason = None
                
                # Check Target and Stop Loss
                if direction == "bullish":
                    if stop_loss and current_price <= stop_loss:
                        exit_reason = "Stop Loss Hit"
                    elif target1 and current_price >= target1:
                        exit_reason = "Target 1 Hit"
                else: # bearish
                    if stop_loss and current_price >= stop_loss:
                        exit_reason = "Stop Loss Hit"
                    elif target1 and current_price <= target1:
                        exit_reason = "Target 1 Hit"
                        
                # Early Profit Booking Logic
                if not exit_reason and target1 and entry_price:
                    distance_to_target = abs(target1 - entry_price)
                    current_distance = current_price - entry_price if direction == "bullish" else entry_price - current_price
                    
                    if distance_to_target > 0:
                        progress = current_distance / distance_to_target
                        if progress > 0.6: # 60% of the way there
                            rsi = self._calculate_rsi(symbol)
                            if (direction == "bullish" and rsi < 40) or (direction == "bearish" and rsi > 60):
                                exit_reason = "Early Profit Booking"
                                
                # Time Stop Logic
                if not exit_reason:
                    entry_date = datetime.fromisoformat(pos["entry_date"].replace('Z', '+00:00'))
                    days_held = (datetime.now(timezone.utc) - entry_date).days
                    if days_held >= 14: # roughly 10 trading days
                        exit_reason = "Time Stop"

                if exit_reason:
                    self.close_position(symbol, current_price, exit_reason)
                    
        except Exception as e:
            logger.error(f"[PERFORMANCE] Auto-exit check failed: {e}")

    def close_position(self, symbol: str, exit_price: float, reason: str):
        pos = self.collection.find_one({"symbol": symbol, "status": {"$in": ["open", "live_executed"]}})
        if not pos:
            return
            
        entry_price = pos.get("actual_entry_price", pos["entry_price"])
        direction = pos["direction"]
        
        if direction == "bullish":
            pnl = exit_price - entry_price
        else:
            pnl = entry_price - exit_price
            
        status = "closed"
        if reason == "Target 1 Hit":
            status = "target_hit"
        elif reason == "Stop Loss Hit":
            status = "stopped"
            
        self.collection.update_one(
            {"_id": pos["_id"]},
            {"$set": {
                "status": status,
                "exit_price": round(exit_price, 2),
                "exit_date": datetime.now(timezone.utc).isoformat(),
                "pnl": round(pnl, 2),
                "exit_reason": reason
            }}
        )
        logger.info(f"[PERFORMANCE] Closed position {symbol} @ {exit_price} - {reason}")
        
    def manual_close(self, symbol: str) -> dict:
        pos = self.collection.find_one({"symbol": symbol, "status": {"$in": ["open", "live_executed"]}})
        if not pos:
            return {"success": False, "reason": "No open position found"}
            
        # Get live price
        live_svc = get_angelone_live()
        live_data = live_svc.get_live_price(symbol)
        if not live_data:
            try:
                hub = get_market_hub()
                live_data = hub.data.fetch_live_snapshot(symbol)
            except Exception:
                live_data = {}
                
        price = live_data.get("ltp", live_data.get("price"))
        if not price:
            return {"success": False, "reason": "Could not fetch live price"}
            
        self.close_position(symbol, price, "Manual Close")
        return {"success": True, "exit_price": price}

    async def sync_broker_orders(self):
        """Fetch AngelOne order book and sync with our DB."""
        try:
            from app.services.broker_adapter import get_broker_adapter, AngelOneAdapter
            adapter = get_broker_adapter()
            if not isinstance(adapter, AngelOneAdapter) or not adapter.is_available():
                return
                
            api = adapter._api
            if not api:
                return
                
            resp = api.orderBook()
            if not resp or not resp.get("status"):
                return
                
            orders = resp.get("data") or []
            if not orders:
                return
                
            today = datetime.now().date().isoformat()
            
            db_positions = list(self.collection.find({
                "entry_date": {"$regex": f"^{today}"}
            }))
            
            for pos in db_positions:
                symbol = pos.get("symbol")
                matching_orders = [o for o in orders if o.get("tradingsymbol", "").startswith(symbol)]
                if not matching_orders:
                    if pos.get("order_type") != "paper":
                        self.collection.update_one({"_id": pos["_id"]}, {"$set": {"order_type": "paper"}})
                    continue
                    
                order = matching_orders[0]
                status = order.get("status", "").lower()
                
                updates = {"order_type": "live"}
                
                if status == "complete" or status == "completed":
                    actual_price = float(order.get("averageprice", 0) or order.get("price", 0))
                    if actual_price > 0 and pos.get("status") == "open":
                        updates["actual_entry_price"] = actual_price
                        updates["status"] = "live_executed"
                        
                        signal_price = pos.get("entry_price")
                        direction = pos.get("direction", "bullish")
                        if direction == "bullish":
                            updates["slippage"] = actual_price - signal_price
                        else:
                            updates["slippage"] = signal_price - actual_price
                            
                elif status == "rejected":
                    if pos.get("status") == "open":
                        updates["status"] = "live_rejected"
                        updates["exit_reason"] = "Order Rejected by Broker"
                        updates["exit_date"] = datetime.now(timezone.utc).isoformat()
                        
                if updates:
                    self.collection.update_one({"_id": pos["_id"]}, {"$set": updates})
                    
        except Exception as e:
            logger.error(f"[PERFORMANCE] Broker order sync failed: {e}")

_tracker = None
def get_performance_tracker():
    global _tracker
    if _tracker is None:
        _tracker = PerformanceTrackerService()
    return _tracker
