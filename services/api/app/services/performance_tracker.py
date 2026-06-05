"""
Performance Tracker
====================
- Auto saves every BUY alert as an open trade
- Every 5 min checks: target hit / SL hit / time stop / RSI reversal
- On exit: saves WHY it exited, what candle formed, how many days held
- Full history in MongoDB: entry date, exit date, price, pnl, reason, candle
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

from pymongo import MongoClient

logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME   = os.getenv("MONGO_DB_NAME", "stock_predictor_ml")


# ── Candle pattern detector (last candle) ──────────────────────────────────────
def _detect_candle_pattern(df) -> str:
    """Return a human-readable last candle pattern name."""
    if df is None or len(df) < 2:
        return "unknown"
    try:
        o, h, l, c = float(df["Open"].iloc[-1]), float(df["High"].iloc[-1]), float(df["Low"].iloc[-1]), float(df["Close"].iloc[-1])
        body   = abs(c - o)
        rng    = h - l if h != l else 0.001
        upper  = h - max(o, c)
        lower  = min(o, c) - l
        body_ratio = body / rng

        if body_ratio < 0.1:
            return "Doji"
        if body_ratio > 0.7:
            return "Bullish Marubozu" if c > o else "Bearish Marubozu"
        if lower > body * 2 and upper < body * 0.5 and c > o:
            return "Hammer"
        if upper > body * 2 and lower < body * 0.5 and c < o:
            return "Shooting Star"
        if lower > body * 2 and upper < body * 0.5 and c < o:
            return "Hanging Man"
        if upper > body * 2 and lower < body * 0.5 and c > o:
            return "Inverted Hammer"
        # Engulfing (compare with prev candle)
        po, pc = float(df["Open"].iloc[-2]), float(df["Close"].iloc[-2])
        if c > o and pc < po and c > po and o < pc:
            return "Bullish Engulfing"
        if c < o and pc > po and c < po and o > pc:
            return "Bearish Engulfing"
        return "Bullish Candle" if c > o else "Bearish Candle"
    except Exception:
        return "unknown"


def _calc_rsi(df, period: int = 14) -> float:
    try:
        if df is None or len(df) < period + 1:
            return 50.0
        delta = df["Close"].diff()
        gain  = delta.where(delta > 0, 0).rolling(period).mean()
        loss  = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs    = gain / loss
        return float((100 - (100 / (1 + rs))).iloc[-1])
    except Exception:
        return 50.0


class PerformanceTrackerService:
    def __init__(self):
        self.client     = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        self.db         = self.client[DB_NAME]
        self.collection = self.db["trade_positions"]
        # ensure indexes exist
        self.collection.create_index([("symbol", 1), ("status", 1)])
        self.collection.create_index([("entry_date", -1)])

    # ── Save new trade when BUY alert fires ────────────────────────────────────

    def save_new_entry(self, alert: dict):
        symbol = (alert.get("symbol") or "").upper()
        if not symbol:
            return

        if self.collection.find_one({"symbol": symbol, "status": "open"}):
            logger.info("[PERFORMANCE] %s already open — skipped", symbol)
            return

        entry_price = alert.get("entryLevel") or alert.get("livePrice")
        if not entry_price:
            return

        # ── Quality Gate: only save genuinely strong signals ──────────────────
        confidence  = float(alert.get("confidence") or 0)
        rr          = float(alert.get("rr") or 0)
        stop_loss   = alert.get("stopLoss")
        direction   = alert.get("direction", "bullish")
        label       = alert.get("label", "")
        distance    = float(alert.get("distancePct") or 0)

        # Must have: confidence >= 70, RR >= 1.3, stop loss defined, direction bullish
        if confidence < 70:
            logger.info("[PERFORMANCE] %s skipped — confidence %.1f < 70", symbol, confidence)
            return
        if rr and rr < 1.3:
            logger.info("[PERFORMANCE] %s skipped — RR %.2f < 1.3", symbol, rr)
            return
        if not stop_loss:
            logger.info("[PERFORMANCE] %s skipped — no stop loss defined", symbol)
            return
        if direction != "bullish":
            logger.info("[PERFORMANCE] %s skipped — direction is %s (only bullish tracked)", symbol, direction)
            return
        if "CHASE" in label.upper() or "FLYING" in label.upper():
            logger.info("[PERFORMANCE] %s skipped — label is CHASE/FLYING (too late to enter)", symbol)
            return
        # Stock must be within 3% of entry (not already far away)
        if distance > 3.0:
            logger.info("[PERFORMANCE] %s skipped — already %.1f%% away from entry", symbol, distance)
            return
        # ─────────────────────────────────────────────────────────────────────

        doc = {
            "symbol":       symbol,
            "entry_price":  round(float(entry_price), 2),
            "entry_date":   datetime.now(timezone.utc).isoformat(),
            "target1":      alert.get("target"),
            "stop_loss":    alert.get("stopLoss"),
            "direction":    alert.get("direction", "bullish"),
            "confidence":   alert.get("confidence", 0),
            "rr":           alert.get("rr"),
            "setup_type":   alert.get("setupType", ""),
            "signal_stage": alert.get("signalStage", ""),
            "status":       "open",
            # filled on close
            "exit_price":   None,
            "exit_date":    None,
            "exit_reason":  None,
            "exit_candle":  None,
            "days_held":    None,
            "pnl":          None,
            "pnl_pct":      None,
            "order_type":   "paper",   # upgraded to "live" by broker sync
        }

        self.collection.insert_one(doc)
        logger.info("[PERFORMANCE] Trade saved: %s @ ₹%.2f (%s)",
                    symbol, entry_price, doc["direction"])


    # ── Auto exit check — called every 5 min ──────────────────────────────────

    async def check_auto_exits(self):
        try:
            open_positions = list(self.collection.find(
                {"status": {"$in": ["open", "live_executed"]}}
            ))
            if not open_positions:
                return

            from app.services.angelone_live import get_angelone_live
            from app.core.dependencies import get_market_hub

            live_svc    = get_angelone_live()
            live_prices = live_svc.get_all_live_prices()
            hub         = get_market_hub()

            for pos in open_positions:
                symbol = pos["symbol"]

                # Get live price
                live_data = live_prices.get(symbol)
                if not live_data:
                    try:
                        live_data = hub.data.fetch_live_snapshot(symbol)
                    except Exception:
                        continue
                if not live_data:
                    continue

                current_price = live_data.get("ltp") or live_data.get("price")
                if not current_price:
                    continue

                current_price = float(current_price)
                entry_price   = float(pos.get("actual_entry_price") or pos["entry_price"])
                target1       = pos.get("target1")
                stop_loss     = pos.get("stop_loss")
                direction     = pos.get("direction", "bullish")

                exit_reason = None

                # Trailing Stop Loss (Risk-Free at 1:2 RR)
                if stop_loss and target1 and not exit_reason:
                    risk = abs(entry_price - float(stop_loss))
                    if risk > 0:
                        reward = (current_price - entry_price) if direction == "bullish" else (entry_price - current_price)
                        if reward >= 1.5 * risk:  # At 1:1.5 or 1:2 RR, move SL to entry
                            new_sl = entry_price
                            # Update SL only if it's tightening
                            if (direction == "bullish" and float(stop_loss) < new_sl) or \
                               (direction == "bearish" and float(stop_loss) > new_sl):
                                self.collection.update_one({"_id": pos["_id"]}, {"$set": {"stop_loss": new_sl}})
                                pos["stop_loss"] = new_sl
                                stop_loss = new_sl

                # 1. Hard Stop Loss / Target
                if direction == "bullish":
                    if stop_loss and current_price <= float(stop_loss):
                        exit_reason = "Stop Loss Hit"
                    elif target1 and current_price >= float(target1):
                        exit_reason = "Target 1 Hit"
                else:
                    if stop_loss and current_price >= float(stop_loss):
                        exit_reason = "Stop Loss Hit"
                    elif target1 and current_price <= float(target1):
                        exit_reason = "Target 1 Hit"

                # 2. Early profit booking: 60% to target + RSI reversal
                if not exit_reason and target1 and entry_price:
                    dist_total   = abs(float(target1) - entry_price)
                    dist_current = current_price - entry_price if direction == "bullish" else entry_price - current_price
                    if dist_total > 0 and dist_current / dist_total > 0.6:
                        rsi = await asyncio.to_thread(self._get_rsi, symbol, hub)
                        if (direction == "bullish" and rsi < 40) or (direction == "bearish" and rsi > 60):
                            exit_reason = "Early Profit Booking (RSI Reversal)"

                # 3. Time stop — 14 calendar days
                if not exit_reason:
                    entry_dt  = datetime.fromisoformat(pos["entry_date"].replace("Z", "+00:00"))
                    days_held = (datetime.now(timezone.utc) - entry_dt).days
                    if days_held >= 14:
                        exit_reason = "Time Stop (14 days)"

                if exit_reason:
                    # Fetch daily candle for exit candle pattern
                    candle = await asyncio.to_thread(self._get_last_candle, symbol, hub)
                    self._close_position(pos, current_price, exit_reason, candle)

        except Exception as e:
            logger.error("[PERFORMANCE] Auto-exit check failed: %s", e)

    # ── Close position ─────────────────────────────────────────────────────────

    def _close_position(self, pos: dict, exit_price: float, reason: str, exit_candle: str = "unknown"):
        entry_price = float(pos.get("actual_entry_price") or pos["entry_price"])
        direction   = pos.get("direction", "bullish")
        entry_dt    = datetime.fromisoformat(pos["entry_date"].replace("Z", "+00:00"))
        days_held   = (datetime.now(timezone.utc) - entry_dt).days

        pnl     = (exit_price - entry_price) if direction == "bullish" else (entry_price - exit_price)
        pnl_pct = (pnl / entry_price) * 100 if entry_price else 0

        status = "closed"
        if reason == "Target 1 Hit":
            status = "target_hit"
        elif reason == "Stop Loss Hit":
            status = "stopped"

        self.collection.update_one(
            {"_id": pos["_id"]},
            {"$set": {
                "status":      status,
                "exit_price":  round(exit_price, 2),
                "exit_date":   datetime.now(timezone.utc).isoformat(),
                "exit_reason": reason,
                "exit_candle": exit_candle,
                "days_held":   days_held,
                "pnl":         round(pnl, 2),
                "pnl_pct":     round(pnl_pct, 2),
            }}
        )
        logger.info("[PERFORMANCE] Closed %s @ ₹%.2f | reason: %s | candle: %s | PnL: ₹%.2f (%.1f%%) | %d days",
                    pos["symbol"], exit_price, reason, exit_candle, pnl, pnl_pct, days_held)

    def close_position(self, symbol: str, exit_price: float, reason: str, exit_candle: str = "unknown"):
        pos = self.collection.find_one({"symbol": symbol, "status": {"$in": ["open", "live_executed"]}})
        if pos:
            self._close_position(pos, exit_price, reason, exit_candle)

    # ── Manual close from UI ───────────────────────────────────────────────────

    def manual_close(self, symbol: str) -> dict:
        pos = self.collection.find_one({"symbol": symbol, "status": {"$in": ["open", "live_executed"]}})
        if not pos:
            return {"success": False, "reason": "No open position found"}

        from app.services.angelone_live import get_angelone_live
        from app.core.dependencies import get_market_hub
        live_svc  = get_angelone_live()
        live_data = live_svc.get_live_price(symbol)
        if not live_data:
            try:
                live_data = get_market_hub().data.fetch_live_snapshot(symbol)
            except Exception:
                live_data = {}

        price = live_data.get("ltp") or live_data.get("price") if live_data else None
        if not price:
            return {"success": False, "reason": "Could not fetch live price"}

        self.close_position(symbol, float(price), "Manual Close")
        return {"success": True, "exit_price": price}

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_rsi(self, symbol: str, hub) -> float:
        try:
            df = hub.data.fetch_history(symbol, period="5d", interval="15m")
            return _calc_rsi(df)
        except Exception:
            return 50.0

    def _get_last_candle(self, symbol: str, hub) -> str:
        try:
            df = hub.data.fetch_history(symbol, period="5d", interval="1d")
            return _detect_candle_pattern(df)
        except Exception:
            return "unknown"

    # ── Broker order sync ──────────────────────────────────────────────────────

    async def sync_broker_orders(self):
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

            orders  = resp.get("data") or []
            today   = datetime.now().date().isoformat()
            db_open = list(self.collection.find({"entry_date": {"$regex": f"^{today}"}}))

            for pos in db_open:
                symbol   = pos.get("symbol")
                matching = [o for o in orders if o.get("tradingsymbol", "").startswith(symbol)]
                if not matching:
                    if pos.get("order_type") != "paper":
                        self.collection.update_one({"_id": pos["_id"]}, {"$set": {"order_type": "paper"}})
                    continue

                order  = matching[0]
                status = order.get("status", "").lower()
                updates = {"order_type": "live"}

                if status in ("complete", "completed"):
                    actual = float(order.get("averageprice") or order.get("price") or 0)
                    if actual > 0 and pos.get("status") == "open":
                        signal_price = float(pos.get("entry_price", 0))
                        updates["actual_entry_price"] = actual
                        updates["status"]             = "live_executed"
                        updates["slippage"]           = actual - signal_price if pos.get("direction") == "bullish" else signal_price - actual

                elif status == "rejected":
                    if pos.get("status") == "open":
                        updates.update({
                            "status":     "live_rejected",
                            "exit_reason": "Order Rejected by Broker",
                            "exit_date":   datetime.now(timezone.utc).isoformat(),
                        })

                if updates:
                    self.collection.update_one({"_id": pos["_id"]}, {"$set": updates})

        except Exception as e:
            logger.error("[PERFORMANCE] Broker sync failed: %s", e)


_tracker: PerformanceTrackerService | None = None


def get_performance_tracker() -> PerformanceTrackerService:
    global _tracker
    if _tracker is None:
        _tracker = PerformanceTrackerService()
    return _tracker
