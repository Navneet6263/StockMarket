"""
Auto-score pending predictions using yfinance closes.
Run daily after market close:
    python -m app.score_pending
"""
import os
import sys
from datetime import datetime, timezone
import pandas as pd
from pymongo import MongoClient

CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

from app.live_data import LiveDataService


def pick_next_closes(df: pd.DataFrame, created_at: datetime):
    """Return next 1d and 5d closes after created_at timestamp."""
    df = df.copy()
    df["ts"] = df.index.tz_localize(None)
    after = df[df["ts"] > created_at]
    if after.empty:
        return None, None, None, None
    c1 = after.iloc[0]["Close"]
    d1 = after.iloc[0]["ts"].date().isoformat()
    if len(after) >= 5:
        c5 = after.iloc[4]["Close"]
        d5 = after.iloc[4]["ts"].date().isoformat()
    else:
        c5 = None
        d5 = None
    return c1, d1, c5, d5


def main():
    mongo_uri = os.getenv("MONGO_URL") or os.getenv("MONGO_URI") or "mongodb://localhost:27017"
    db_name = os.getenv("MONGO_DB_NAME", "stock_predictor_ml")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=1000)
    db = client[db_name]
    predictions = db["predictions"]

    service = LiveDataService()

    pending = list(predictions.find({"status": "pending"}, {"symbol": 1, "created_at": 1, "entry_price": 1, "direction": 1}))
    print(f"Pending predictions: {len(pending)}")

    for doc in pending:
        symbol = doc["symbol"]
        created_at = doc.get("created_at")
        if not created_at:
            continue
        try:
            df = service.get_historical_data(symbol, "1mo")
        except Exception as e:
            print(f"{symbol}: fetch error {e}")
            continue

        c1, d1, c5, d5 = pick_next_closes(df, created_at)
        if c1 is None:
            continue

        entry = doc.get("entry_price")
        outcomes = {"actual_close_1d": float(c1), "actual_date_1d": d1, "was_scored": True}
        if entry:
            dir1 = "up" if c1 > entry else "down"
            outcomes["actual_direction_1d"] = dir1
            outcomes["was_correct_1d"] = (dir1 == doc.get("direction"))

        if c5 is not None:
            outcomes["actual_close_5d"] = float(c5)
            outcomes["actual_date_5d"] = d5
            if entry:
                dir5 = "up" if c5 > entry else "down"
                outcomes["actual_direction_5d"] = dir5
                outcomes["was_correct_5d"] = (dir5 == doc.get("direction"))

        predictions.update_one({"_id": doc["_id"]}, {"$set": {"outcomes": outcomes, "status": "scored"}})
        print(f"{symbol}: scored")


if __name__ == "__main__":
    main()
