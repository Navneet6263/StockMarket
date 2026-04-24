# MongoDB Setup for Stock Predictor ML

## Option 1: Install MongoDB Locally (Recommended)

### Windows:
1. Download MongoDB Community Server: https://www.mongodb.com/try/download/community
2. Run installer (keep default settings)
3. MongoDB will start automatically on port 27017

### Verify Installation:
```bash
# Open Command Prompt
mongosh
# Should connect to MongoDB
```

---

## Option 2: Use Docker (If you have Docker Desktop)

```bash
# Start MongoDB container
docker run -d -p 27017:27017 --name mongodb mongo:7.0

# Check if running
docker ps
```

---

## Setup Database

After MongoDB is running, initialize the database:

```bash
# Navigate to project directory
cd "c:\Users\Naveent Kumar\Downloads\crm photo"

# Run setup script
python services/api/app/db_setup.py
```

You should see:
```
✓ Created 'predictions' collection with indexes
✓ Created 'options_analysis' collection with indexes
✓ Created 'model_performance' collection with indexes
✓ Created 'pattern_history' collection with indexes
✓ Created 'training_logs' collection with indexes
✓ Inserted sample prediction

✅ Database setup complete!
```

---

## What MongoDB Does for ML:

### 1. **Predictions Collection**
- Stores every prediction made
- Tracks: symbol, direction, confidence, support/resistance
- Used for: Learning from past predictions

### 2. **Options Analysis Collection**
- Stores options chain data (PCR, Max Pain, etc.)
- Used for: Options strategy backtesting

### 3. **Model Performance Collection**
- Tracks model accuracy over time
- Used for: Model improvement

### 4. **Pattern History Collection**
- Stores candlestick pattern occurrences
- Used for: Pattern reliability learning

### 5. **Training Logs Collection**
- Records when models are trained
- Tracks: accuracy, data points used
- Used for: Model version tracking

---

## Benefits:

✅ **Learn from History** - Model improves by analyzing past predictions
✅ **Track Accuracy** - See which predictions were correct
✅ **Pattern Learning** - Identify which patterns work best
✅ **Performance Monitoring** - Track model improvements over time
✅ **Data Persistence** - Never lose prediction history

---

## Check if MongoDB is Working:

After starting backend:
```bash
npm run dev:api
```

Look for this line in terminal:
```
✓ MongoDB connected: stock_predictor_ml
```

---

## View Data (Optional):

### Using MongoDB Compass (GUI):
1. Download: https://www.mongodb.com/try/download/compass
2. Connect to: `mongodb://localhost:27017`
3. Open database: `stock_predictor_ml`
4. View collections and data

### Using Command Line:
```bash
mongosh
use stock_predictor_ml
db.predictions.find().pretty()
```

---

## Without MongoDB:

If you don't want to use MongoDB, the app will still work!
- Predictions won't be saved
- No historical learning
- But all features will work normally

---

## Troubleshooting:

**MongoDB not starting?**
- Windows: Check Services → MongoDB Server should be running
- Docker: Run `docker ps` to check container status

**Connection error?**
- Make sure port 27017 is not blocked by firewall
- Check if another MongoDB instance is running

**Setup script fails?**
- Make sure MongoDB is running first
- Check if pymongo is installed: `pip install pymongo`
