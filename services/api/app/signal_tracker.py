"""Signal Tracker — monitors open signals, detects target/SL hits, saves to DB."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List, Optional

from app.live_data import LiveDataService


class SignalTracker:
    def __init__(self, db=None):
        self.db = db
        self.live_service = LiveDataService()

    def _signals_coll(self):
        return self.db["scanner_signals"] if self.db is not None else None

    def _perf_coll(self):
        return self.db["signal_performance"] if self.db is not None else None

    # ── Save new signal ──────────────────────────────────────────────
    def save_signal(self, signal: Dict) -> Optional[str]:
        coll = self._signals_coll()
        if coll is None:
            return None

        doc = {
            "symbol": signal["symbol"],
            "action": signal["action"],
            "setup": signal.get("setup", ""),
            "quality_grade": signal.get("quality_grade", ""),
            "quality_score": signal.get("quality_score", 0),
            "confidence": signal.get("confidence", 0),
            "direction": signal.get("direction", ""),
            "regime": signal.get("regime", ""),
            "entry_price": signal.get("entry_price", 0),
            "stop_loss": signal.get("stop_loss", 0),
            "target_1": signal.get("target_1", 0),
            "target_2": signal.get("target_2", 0),
            "rr": signal.get("rr", 0),
            "volume_ratio": signal.get("volume_ratio", 0),
            "mtf_sync": signal.get("mtf_sync", ""),
            "backtest_win_rate": signal.get("backtest_win_rate", 0),
            "backtest_pf": signal.get("backtest_pf", 0),
            "status": "OPEN",
            "peak_price": signal.get("entry_price", 0),
            "created_at": datetime.utcnow(),
            "closed_at": None,
            "close_reason": None,
            "pnl_pct": 0,
        }
        return str(coll.insert_one(doc).inserted_id)

    # ── Get open signals ─────────────────────────────────────────────
    def get_open_signals(self) -> List[Dict]:
        coll = self._signals_coll()
        if coll is None:
            return []
        signals = []
        for doc in coll.find({"status": "OPEN"}):
            doc["_id"] = str(doc["_id"])
            signals.append(doc)
        return signals

    # ── Check one signal for target/SL hit ───────────────────────────
    def _check_signal(self, signal: Dict) -> Optional[Dict]:
        try:
            symbol = signal["symbol"]
            quote = self.live_service.get_live_quote(symbol)
            price = quote.price

            entry = signal["entry_price"]
            sl = signal["stop_loss"]
            t1 = signal["target_1"]
            t2 = signal["target_2"]
            action = signal["action"]
            peak = signal.get("peak_price", entry)

            if action == "BUY" and price > peak:
                peak = price
            elif action == "SELL" and price < peak:
                peak = price

            event = None
            if action == "BUY":
                if price >= t2:
                    event = {"status": "T2_HIT", "reason": "Target 2 hit"}
                elif price >= t1 and signal["status"] == "OPEN":
                    event = {"status": "T1_HIT", "reason": "Target 1 hit"}
                elif price <= sl:
                    event = {"status": "SL_HIT", "reason": "Stop loss hit"}
            elif action == "SELL":
                if price <= t2:
                    event = {"status": "T2_HIT", "reason": "Target 2 hit"}
                elif price <= t1 and signal["status"] == "OPEN":
                    event = {"status": "T1_HIT", "reason": "Target 1 hit"}
                elif price >= sl:
                    event = {"status": "SL_HIT", "reason": "Stop loss hit"}

            if event:
                pnl_pct = ((price - entry) / entry) * 100
                if action == "SELL":
                    pnl_pct = -pnl_pct
                event.update({
                    "symbol": symbol, "action": action,
                    "entry_price": entry, "current_price": price,
                    "sl": sl, "t1": t1, "t2": t2,
                    "pnl_pct": round(pnl_pct, 2),
                    "peak_price": peak, "signal_id": signal["_id"],
                })
                return event

            # Update peak in DB
            coll = self._signals_coll()
            if coll is not None:
                from bson import ObjectId
                coll.update_one({"_id": ObjectId(signal["_id"])}, {"$set": {"peak_price": peak}})
            return None
        except Exception as exc:
            print(f"[TRACKER] Error checking {signal.get('symbol')}: {exc}")
            return None

    # ── Close signal in DB ───────────────────────────────────────────
    def _close_signal(self, event: Dict):
        coll = self._signals_coll()
        perf = self._perf_coll()
        if coll is None:
            return

        from bson import ObjectId
        is_final = event["status"] in ("T2_HIT", "SL_HIT")

        update = {"status": event["status"], "peak_price": event["peak_price"], "pnl_pct": event["pnl_pct"]}
        if is_final:
            update["closed_at"] = datetime.utcnow()
            update["close_reason"] = event["reason"]

        coll.update_one({"_id": ObjectId(event["signal_id"])}, {"$set": update})

        if is_final and perf is not None:
            perf.insert_one({
                "symbol": event["symbol"], "action": event["action"],
                "result": "WIN" if event["pnl_pct"] > 0 else "LOSS",
                "pnl_pct": event["pnl_pct"],
                "entry_price": event["entry_price"], "exit_price": event["current_price"],
                "close_reason": event["reason"], "closed_at": datetime.utcnow(),
            })

    # ── Performance stats ────────────────────────────────────────────
    def get_performance(self) -> Dict:
        perf = self._perf_coll()
        if perf is None:
            return {"available": False}

        all_trades = list(perf.find())
        if not all_trades:
            return {"total_trades": 0, "win_rate": 0, "avg_pnl": 0}

        wins = [t for t in all_trades if t.get("result") == "WIN"]
        losses = [t for t in all_trades if t.get("result") == "LOSS"]
        total_pnl = sum(t.get("pnl_pct", 0) for t in all_trades)

        return {
            "total_trades": len(all_trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(len(wins) / len(all_trades) * 100, 1),
            "avg_pnl_pct": round(total_pnl / len(all_trades), 2),
            "total_pnl_pct": round(total_pnl, 2),
            "avg_win_pct": round(sum(t.get("pnl_pct", 0) for t in wins) / max(len(wins), 1), 2),
            "avg_loss_pct": round(sum(t.get("pnl_pct", 0) for t in losses) / max(len(losses), 1), 2),
            "profit_factor": round(
                abs(sum(t.get("pnl_pct", 0) for t in wins))
                / max(abs(sum(t.get("pnl_pct", 0) for t in losses)), 0.01), 2,
            ),
        }


# ── Singleton ────────────────────────────────────────────────────────
_tracker: Optional[SignalTracker] = None


def get_tracker(db=None) -> SignalTracker:
    global _tracker
    if _tracker is None:
        _tracker = SignalTracker(db=db)
    return _tracker
