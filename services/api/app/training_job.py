import os
import sys
import time
import joblib
import logging
from datetime import datetime

# Ensure package import works when run directly
CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

from app.services.data_provider import MarketDataService
from app.market_universe_config import get_full_nse_universe
from app.ml_predictor import StockPredictor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("training_job")

MODEL_DIR = os.path.join(CURRENT_DIR, "..", ".models")
os.makedirs(MODEL_DIR, exist_ok=True)

def train_for_symbol(sym: str, data_provider):
    try:
        # Fetch 5 years of daily data for robust training
        frames = data_provider.fetch_batch_history([sym], period="5y", interval="1d")
        df = frames.get(sym)
        if df is None or df.empty or len(df) < 250:
            logger.warning(f"{sym}: Insufficient data ({len(df) if df is not None else 0} rows)")
            return False

        model = StockPredictor()
        result = model.train_model(df)
        
        # Save model
        model_path = os.path.join(MODEL_DIR, f"{sym}_model.pkl")
        joblib.dump(model, model_path)
        
        logger.info(f"{sym}: SUCCESS | Train Acc: {result['train_accuracy']:.2f} | Test Acc: {result['test_accuracy']:.2f}")
        return True
    except Exception as e:
        logger.error(f"{sym}: FAILED | {e}")
        return False

from app.core.settings import get_settings

def run_batch_training():
    data_provider = MarketDataService(get_settings())
    universe = get_full_nse_universe()
    
    # Optional: prioritize training for the top 500 stocks to save time/space
    symbols = list(universe.keys())
    
    logger.info(f"Starting ML Training Job for {len(symbols)} symbols. Saving to {MODEL_DIR}")
    
    success_count = 0
    fail_count = 0
    
    for i, sym in enumerate(symbols):
        if i % 10 == 0:
            logger.info(f"Progress: {i}/{len(symbols)} ({(i/len(symbols))*100:.1f}%)")
            
        if train_for_symbol(sym, data_provider):
            success_count += 1
        else:
            fail_count += 1
            
        # Sleep slightly to prevent hammering Yahoo Finance API limits
        time.sleep(0.1)

    logger.info(f"Training Job Complete! Success: {success_count}, Failed: {fail_count}")

if __name__ == "__main__":
    run_batch_training()
