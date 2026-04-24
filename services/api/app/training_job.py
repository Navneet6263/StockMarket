import os
import sys
import joblib
from datetime import datetime

# Ensure package import works when run directly
CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

from app.live_data import LiveDataService
from app.ml_predictor import StockPredictor

SYMBOLS = [
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "NIFTY",
    "BANKNIFTY",
]

MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", ".models")
os.makedirs(MODEL_DIR, exist_ok=True)


def run():
    service = LiveDataService()
    for sym in SYMBOLS:
        try:
            df = service.get_historical_data(sym, "10y")
            if len(df) < 250:
                continue
            model = StockPredictor()
            result = model.train_model(df)
            joblib.dump(model, os.path.join(MODEL_DIR, f"{sym}_model.pkl"))
            print(f"{sym}: train {result['train_accuracy']:.2f} test {result['test_accuracy']:.2f}")
        except Exception as e:
            print(f"{sym}: {e}")


if __name__ == "__main__":
    run()
