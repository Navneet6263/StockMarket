import os
import sys
import sqlite3
import pandas as pd
import numpy as np
import logging
from datetime import datetime
import joblib

CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

from app.services.data_provider import MarketDataService
from app.ml_predictor import StockPredictor
from app.core.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("global_training")

def fetch_historical_setups(db_path: str):
    logger.info(f"Connecting to DB: {db_path}")
    conn = sqlite3.connect(db_path)
    
    # We only care about setups that are closed with a conclusive outcome
    query = """
    SELECT symbol, direction, detected_at, status 
    FROM tracked_setups 
    WHERE status IN ('passed', 'failed')
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    logger.info(f"Fetched {len(df)} conclusive setups from DB.")
    return df

def build_global_dataset(setups_df, data_provider):
    if setups_df.empty:
        logger.error("No historical setups found!")
        return None
        
    setups_df['date'] = pd.to_datetime(setups_df['detected_at']).dt.tz_localize(None).dt.normalize()
    
    symbols = setups_df['symbol'].unique()
    logger.info(f"Fetching historical chart data for {len(symbols)} symbols...")
    
    # Batch fetch 5 years of daily data for all symbols involved
    frames = data_provider.fetch_batch_history(symbols.tolist(), period="5y", interval="1d")
    
    predictor = StockPredictor()
    
    all_features = []
    all_targets = []
    
    for symbol in symbols:
        df_chart = frames.get(symbol)
        if df_chart is None or df_chart.empty or len(df_chart) < 50:
            continue
            
        # Ensure df_chart index is tz-naive for matching
        df_chart.index = df_chart.index.tz_localize(None).normalize()
        
        try:
            # Calculate technical features for the entire 5-year chart of this symbol
            features = predictor.prepare_features(df_chart)
        except Exception as e:
            logger.warning(f"Feature engineering failed for {symbol}: {e}")
            continue
            
        # Get setups specifically for this symbol
        sym_setups = setups_df[setups_df['symbol'] == symbol]
        
        for _, row in sym_setups.iterrows():
            setup_date = row['date']
            direction = row['direction']
            status = row['status']
            
            # Find the chart data exactly on or right before the setup was detected
            # Because the setup might be detected intraday, we take the EOD features from the day of the setup
            if setup_date in features.index:
                feature_row = features.loc[setup_date]
            else:
                # Get the last available row before the setup
                past_dates = features.index[features.index <= setup_date]
                if len(past_dates) == 0:
                    continue
                feature_row = features.loc[past_dates[-1]]
                
            # Define Label (Target)
            # We want the AI to predict if a setup will hit its target.
            is_success = 1 if status == 'passed' else 0
            
            # Add context feature so the model knows if we are looking for a long or short
            feature_row = feature_row.copy()
            feature_row['is_bullish_setup'] = 1 if direction == 'bullish' else 0
            
            all_features.append(feature_row)
            all_targets.append(is_success)

    if not all_features:
        logger.error("No valid features could be aligned with setups.")
        return None, None
        
    X = pd.DataFrame(all_features)
    y = pd.Series(all_targets)
    
    logger.info(f"Built Master Dataset with {len(X)} rows.")
    return X, y

def train_global_model():
    settings = get_settings()
    data_provider = MarketDataService(settings)
    
    # 1. Fetch DB records
    setups_df = fetch_historical_setups(settings.tracked_setup_db_path)
    
    # 2. Build Dataset
    X, y = build_global_dataset(setups_df, data_provider)
    if X is None:
        return
        
    # 3. Train Model
    logger.info("Training Global Master Model...")
    
    # We modify StockPredictor temporarily to train on exact X and y instead of pure shifted df
    predictor = StockPredictor()
    
    # Train test split and fit manually since standard `train_model` expects df
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score
    
    # Handle NaN values before training
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0)
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    predictor.model.fit(X_train, y_train)
    
    train_acc = accuracy_score(y_train, predictor.model.predict(X_train))
    test_acc = accuracy_score(y_test, predictor.model.predict(X_test))
    
    logger.info(f"SUCCESS | Train Acc: {train_acc:.2f} | Test Acc: {test_acc:.2f}")
    
    # 4. Save Model
    predictor.is_trained = True
    predictor.feature_columns = list(X.columns)
    
    model_dir = os.path.join(CURRENT_DIR, "..", ".models")
    os.makedirs(model_dir, exist_ok=True)
    
    model_path = os.path.join(model_dir, "master_ai_model.pkl")
    joblib.dump(predictor, model_path)
    logger.info(f"Master AI Model saved to {model_path}")

if __name__ == "__main__":
    train_global_model()
