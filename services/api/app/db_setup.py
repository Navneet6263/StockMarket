"""
MongoDB Setup Script for Stock Predictor ML
Run this to initialize MongoDB collections and indexes
"""

from pymongo import MongoClient, ASCENDING, DESCENDING
from datetime import datetime

# MongoDB connection
MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "stock_predictor_ml"

def setup_database():
    """Initialize MongoDB database with collections and indexes"""
    
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    
    print(f"Setting up database: {DB_NAME}")
    
    # 1. Predictions Collection - Store all predictions
    predictions = db["predictions"]
    predictions.create_index([("symbol", ASCENDING), ("created_at", DESCENDING)])
    predictions.create_index([("created_at", DESCENDING)])
    predictions.create_index([("direction", ASCENDING)])
    print("[OK] Created 'predictions' collection with indexes")
    
    # 2. Options Analysis Collection - Store options chain data
    options = db["options_analysis"]
    options.create_index([("symbol", ASCENDING), ("created_at", DESCENDING)])
    options.create_index([("created_at", DESCENDING)])
    print("[OK] Created 'options_analysis' collection with indexes")
    
    # 3. Model Performance Collection - Track accuracy
    performance = db["model_performance"]
    performance.create_index([("symbol", ASCENDING), ("date", DESCENDING)])
    performance.create_index([("accuracy", DESCENDING)])
    print("[OK] Created 'model_performance' collection with indexes")
    
    # 4. Pattern History Collection - Store pattern occurrences
    patterns = db["pattern_history"]
    patterns.create_index([("symbol", ASCENDING), ("pattern_name", ASCENDING)])
    patterns.create_index([("success_rate", DESCENDING)])
    print("[OK] Created 'pattern_history' collection with indexes")
    
    # 5. Training Logs Collection - Track model training
    training = db["training_logs"]
    training.create_index([("symbol", ASCENDING), ("trained_at", DESCENDING)])
    print("[OK] Created 'training_logs' collection with indexes")
    
    # Insert sample data
    sample_prediction = {
        "symbol": "NIFTY",
        "direction": "up",
        "confidence": 0.75,
        "probability_up": 0.75,
        "probability_down": 0.25,
        "support": 24500,
        "resistance": 24800,
        "created_at": datetime.utcnow(),
        "status": "pending"
    }
    predictions.insert_one(sample_prediction)
    print("[OK] Inserted sample prediction")
    
    print("\n[SUCCESS] Database setup complete!")
    print(f"[INFO] Database: {DB_NAME}")
    print(f"[INFO] Connection: {MONGO_URI}")
    print("\nCollections created:")
    print("  - predictions (for ML predictions)")
    print("  - options_analysis (for options chain data)")
    print("  - model_performance (for accuracy tracking)")
    print("  - pattern_history (for pattern learning)")
    print("  - training_logs (for model training history)")
    
    client.close()

if __name__ == "__main__":
    try:
        setup_database()
    except Exception as e:
        print(f"[ERROR] {e}")
        print("\nMake sure MongoDB is running:")
        print("  - Install MongoDB from: https://www.mongodb.com/try/download/community")
        print("  - Or use Docker: docker run -d -p 27017:27017 mongo:7.0")
