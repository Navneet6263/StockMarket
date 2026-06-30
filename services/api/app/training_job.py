import os
import sys
import time
import joblib
import logging
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
import numpy as np

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
METRICS_DIR = os.path.join(CURRENT_DIR, "..", ".metrics")
os.makedirs(MODEL_DIR, exist_ok=True)
os.makedirs(METRICS_DIR, exist_ok=True)

def train_for_symbol(sym: str, data_provider, use_cross_validation: bool = True):
    """
    Phase 4: Enhanced training with cross-validation and proper confidence.
    
    Args:
        sym: Stock symbol
        data_provider: Market data service
        use_cross_validation: Use TimeSeriesSplit for validation (Phase 4.2)
    
    Returns:
        bool: Success status
    """
    try:
        # Fetch 5 years of daily data for robust training
        frames = data_provider.fetch_batch_history([sym], period="5y", interval="1d")
        df = frames.get(sym)
        if df is None or df.empty or len(df) < 250:
            logger.warning(f"{sym}: Insufficient data ({len(df) if df is not None else 0} rows)")
            return False

        model = StockPredictor()
        
        # Phase 4.2: Cross-validation with TimeSeriesSplit
        if use_cross_validation:
            result = train_with_cross_validation(model, df, sym)
        else:
            result = model.train_model(df)
        
        # Save model
        model_path = os.path.join(MODEL_DIR, f"{sym}_model.pkl")
        joblib.dump(model, model_path)
        
        # Phase 4.4: Track accuracy metrics over time
        save_training_metrics(sym, result)
        
        logger.info(f"{sym}: SUCCESS | Train Acc: {result['train_accuracy']:.2f} | Test Acc: {result['test_accuracy']:.2f}")
        return True
    except Exception as e:
        logger.error(f"{sym}: FAILED | {e}")
        return False


def train_with_cross_validation(model: StockPredictor, df, symbol: str):
    """
    Phase 4.2: Train with TimeSeriesSplit cross-validation (5 folds).
    
    Args:
        model: StockPredictor instance
        df: Training DataFrame
        symbol: Stock symbol
    
    Returns:
        dict: Training results with CV scores
    """
    try:
        # Prepare features
        features = model.prepare_features(df)
        target = (df["Close"].shift(-1) > df["Close"]).astype(int)
        
        # Pattern performance learning
        model._learn_pattern_performance(df, features)
        
        # Valid indices (drop last row with no target)
        valid_idx = features.index[:-1]
        X = features.loc[valid_idx]
        y = target.loc[valid_idx]
        
        # Phase 4.2: TimeSeriesSplit for time series data
        tscv = TimeSeriesSplit(n_splits=5)
        
        cv_scores = []
        fold_num = 0
        
        for train_idx, val_idx in tscv.split(X):
            fold_num += 1
            X_train_fold = X.iloc[train_idx]
            y_train_fold = y.iloc[train_idx]
            X_val_fold = X.iloc[val_idx]
            y_val_fold = y.iloc[val_idx]
            
            # Train on this fold
            model.model.fit(X_train_fold, y_train_fold)
            
            # Validate
            val_score = float(model.model.score(X_val_fold, y_val_fold))
            cv_scores.append(val_score)
            
            logger.debug(f"{symbol} Fold {fold_num}: Val Acc = {val_score:.4f}")
        
        # Train final model on all data
        model.model.fit(X, y)
        train_score = float(model.model.score(X, y))
        
        # Use mean CV score as test accuracy
        mean_cv_score = float(np.mean(cv_scores))
        std_cv_score = float(np.std(cv_scores))
        
        # Backtest on out-of-sample data
        split_idx = int(len(X) * 0.8)
        model.backtest_metrics = model._run_backtest(df.loc[valid_idx], X, split_idx)
        model.feature_importances = model._get_feature_importances()
        
        model.training_summary = {
            "train_accuracy": round(train_score, 4),
            "test_accuracy": round(mean_cv_score, 4),
            "cv_scores": [round(s, 4) for s in cv_scores],
            "cv_std": round(std_cv_score, 4),
            "model_type": model.model_name,
            "feature_importance": model.feature_importances[:10],
            "backtest": model.backtest_metrics,
            "cross_validation": True
        }
        
        model.is_trained = True
        
        return {
            "train_accuracy": train_score,
            "test_accuracy": mean_cv_score,
            "cv_scores": cv_scores,
            "cv_std": std_cv_score,
            "pattern_stats": model._get_pattern_stats(),
            "feature_importance": model.feature_importances[:10],
            "backtest_metrics": model.backtest_metrics,
            "model_type": model.model_name,
        }
    
    except Exception as e:
        logger.error(f"Cross-validation training failed: {e}", exc_info=True)
        # Fallback to standard training
        return model.train_model(df)


def save_training_metrics(symbol: str, result: dict):
    """
    Phase 4.4: Save training metrics for tracking over time.
    
    Args:
        symbol: Stock symbol
        result: Training results dict
    """
    try:
        metrics_file = os.path.join(METRICS_DIR, f"{symbol}_metrics.txt")
        
        with open(metrics_file, "a") as f:
            timestamp = datetime.now().isoformat()
            train_acc = result.get("train_accuracy", 0)
            test_acc = result.get("test_accuracy", 0)
            cv_std = result.get("cv_std", 0)
            
            f.write(f"{timestamp},{train_acc:.4f},{test_acc:.4f},{cv_std:.4f}\n")
        
        logger.debug(f"Saved training metrics for {symbol}")
    
    except Exception as e:
        logger.warning(f"Failed to save metrics for {symbol}: {e}")

from app.core.settings import get_settings

def run_batch_training(use_cross_validation: bool = True):
    """
    Run batch training for all symbols.
    
    Args:
        use_cross_validation: Enable Phase 4.2 cross-validation (default: True)
    """
    data_provider = MarketDataService(get_settings())
    universe = get_full_nse_universe()
    
    # Optional: prioritize training for the top 500 stocks to save time/space
    symbols = list(universe.keys())
    
    logger.info(f"Starting ML Training Job for {len(symbols)} symbols. Saving to {MODEL_DIR}")
    logger.info(f"Cross-validation: {'ENABLED' if use_cross_validation else 'DISABLED'}")
    
    success_count = 0
    fail_count = 0
    
    for i, sym in enumerate(symbols):
        if i % 10 == 0:
            logger.info(f"Progress: {i}/{len(symbols)} ({(i/len(symbols))*100:.1f}%)")
            
        if train_for_symbol(sym, data_provider, use_cross_validation):
            success_count += 1
        else:
            fail_count += 1
            
        # Sleep slightly to prevent hammering Yahoo Finance API limits
        time.sleep(0.1)

    logger.info(f"Training Job Complete! Success: {success_count}, Failed: {fail_count}")


def run_incremental_training(symbols: list[str], use_cross_validation: bool = True):
    """
    Phase 4.4: Incremental training for specific symbols (e.g., weekly retrain).
    
    Args:
        symbols: List of symbols to retrain
        use_cross_validation: Enable cross-validation
    """
    data_provider = MarketDataService(get_settings())
    
    logger.info(f"Starting incremental training for {len(symbols)} symbols")
    
    success_count = 0
    fail_count = 0
    
    for sym in symbols:
        if train_for_symbol(sym, data_provider, use_cross_validation):
            success_count += 1
        else:
            fail_count += 1
        
        time.sleep(0.1)
    
    logger.info(f"Incremental training complete! Success: {success_count}, Failed: {fail_count}")
    return {"success": success_count, "failed": fail_count}

if __name__ == "__main__":
    run_batch_training()
