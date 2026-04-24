# Prabhjot Ka Stock Predictor - Poora Guide

Tumne Colab pe LSTM model bana liya, MongoDB connect kar liya - bahut achha kiya.
Ab is guide me ye sikhoge:

1. Apne trained model ko API me kaise lagao (FastAPI)
2. Frontend (HTML page) kaise banao
3. Frontend se API kaise call karo
4. Sab kuch MongoDB se kaise jodo

---

## Abhi Tumhare Paas Kya Hai

Tumhara Colab notebook ye karta hai:

```
CSV file load karo
  --> Data clean karo
    --> MinMaxScaler se scale karo
      --> 60 din ka sequence banao
        --> LSTM model train karo
          --> Prediction karo
            --> Graph dikhao
              --> MongoDB me save karo
```

Ye sab sirf Colab ke andar chal raha hai. Koi bhi browser se access nahi kar sakta.

Ab hum ye banayenge:

```
Browser (HTML Page)
  --> FastAPI (Python Server)
    --> Tumhara LSTM Model
      --> MongoDB Atlas
```

---

## Part 1 - Model Ko Save Karo Sahi Tarike Se

Tumhara model pehle se save ho raha hai `stock_model.h5` me. Lekin scaler bhi save karna zaroori hai warna prediction galat aayegi.

Apne Colab notebook me ye code add karo MODULE 5 ke baad:

```python
import joblib

# Model save karo (ye pehle se ho raha hai tumhara)
model.save("stock_model.h5")

# Scaler bhi save karo - ye bahut zaroori hai
joblib.dump(scaler, "scaler.pkl")

# Train size bhi save karo
import json
with open("model_config.json", "w") as f:
    json.dump({
        "time_step": 60,
        "train_size": train_size,
        "total_rows": len(data)
    }, f)

print("Model, scaler aur config save ho gaye!")
```

Phir ye files download karo Colab se:

```python
from google.colab import files
files.download("stock_model.h5")
files.download("scaler.pkl")
files.download("model_config.json")
```

---

## Part 2 - FastAPI Backend Banana

FastAPI ek Python library hai jo tumhare model ko internet pe available karti hai. Jaise ek waiter jo browser ki request leke model tak jata hai aur result wapas lata hai.

### Step 1: Colab me naya cell banao aur ye install karo

```python
!pip install fastapi uvicorn nest-asyncio pyngrok joblib tensorflow pymongo
```

### Step 2: Apni files upload karo Colab me

```python
from google.colab import files

# Ye run karo aur stock_model.h5, scaler.pkl, model_config.json upload karo
uploaded = files.upload()
```

### Step 3: API ka code likho

Colab me naya cell banao aur ye poora code likho:

```python
# api.py ka code - isko ek cell me likho

api_code = '''
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pymongo import MongoClient
import numpy as np
import joblib
import json
import urllib.parse
from tensorflow.keras.models import load_model
from datetime import datetime

# App banao
app = FastAPI(title="Stock Predictor API")

# CORS lagao - ye zaroori hai warna browser block kar dega
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Model aur scaler load karo
model = load_model("stock_model.h5")
scaler = joblib.load("scaler.pkl")

with open("model_config.json") as f:
    config = json.load(f)

TIME_STEP = config["time_step"]  # 60

# MongoDB connect karo
password = urllib.parse.quote_plus("Prabh@19")
MONGO_URL = f"mongodb+srv://prabhjot_kaur19:{password}@cluster0.6u39qk5.mongodb.net/?appName=Cluster0"
client = MongoClient(MONGO_URL)
db = client["stock_price_module"]
predictions_col = db["predictions"]

# Request ka format
class PredictRequest(BaseModel):
    prices: list  # Last 60 din ke closing prices bhejo

# Health check
@app.get("/health")
def health():
    return {"status": "ok", "message": "API chal rahi hai!"}

# Prediction endpoint
@app.post("/predict")
def predict(req: PredictRequest):
    prices = req.prices
    
    # Validation
    if len(prices) < TIME_STEP:
        return {"error": f"Kam se kam {TIME_STEP} prices chahiye, tumne {len(prices)} bheje"}
    
    # Last 60 prices lo
    last_60 = np.array(prices[-TIME_STEP:]).reshape(-1, 1)
    
    # Scale karo
    scaled = scaler.transform(last_60)
    
    # Model ke liye shape banao: (1, 60, 1)
    X = scaled.reshape(1, TIME_STEP, 1)
    
    # Prediction karo
    pred_scaled = model.predict(X)
    
    # Wapas original scale me lao
    pred_price = float(scaler.inverse_transform(pred_scaled)[0][0])
    last_price = float(prices[-1])
    
    # Upar jayega ya neeche
    direction = "up" if pred_price > last_price else "down"
    change_pct = round(((pred_price - last_price) / last_price) * 100, 2)
    
    result = {
        "predicted_price": round(pred_price, 2),
        "last_price": round(last_price, 2),
        "direction": direction,
        "change_percent": change_pct,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    # MongoDB me save karo
    predictions_col.insert_one({**result, "prices_used": len(prices)})
    
    return result

# Purani predictions dekho
@app.get("/predictions")
def get_predictions():
    docs = list(predictions_col.find({}, {"_id": 0}).sort("timestamp", -1).limit(10))
    return {"predictions": docs, "count": len(docs)}
'''

# File me save karo
with open("main.py", "w") as f:
    f.write(api_code)

print("main.py ban gaya!")
```

### Step 4: API ko Colab me chalaao aur public URL lo

```python
import nest_asyncio
import uvicorn
from pyngrok import ngrok
import threading

nest_asyncio.apply()

# ngrok se public URL milega
# Pehle https://ngrok.com pe free account banao aur authtoken lo
ngrok.set_auth_token("APNA_NGROK_TOKEN_YAHAN_LIKHO")

# Tunnel banao
public_url = ngrok.connect(8000)
print("Tumhari API ka public URL hai:")
print(public_url)
print()
print("Ye URLs kaam karenge:")
print(f"{public_url}/health")
print(f"{public_url}/predict")
print(f"{public_url}/predictions")

# API chalaao background me
def run():
    uvicorn.run("main:app", host="0.0.0.0", port=8000)

thread = threading.Thread(target=run, daemon=True)
thread.start()

print("\nAPI chal rahi hai! Upar wala URL copy karo.")
```

### ngrok Account Kaise Banao (Free)

1. Jao https://ngrok.com
2. Sign up karo (Google se bhi ho jayega)
3. Dashboard me jao
4. "Your Authtoken" copy karo
5. Upar wale code me `APNA_NGROK_TOKEN_YAHAN_LIKHO` ki jagah woh token dalo

---

## Part 3 - API Test Karo

API chalne ke baad pehle test karo ki sahi kaam kar rahi hai ya nahi.

### Health check karo

Browser me ye URL kholo (apna ngrok URL use karo):

```
https://xxxx-xx-xx.ngrok.io/health
```

Ye dikhna chahiye:
```json
{"status": "ok", "message": "API chal rahi hai!"}
```

### Prediction test karo

Colab me naya cell me ye likho:

```python
import requests

# Apna ngrok URL yahan dalo
API_URL = "https://xxxx-xx-xx.ngrok.io"

# Test ke liye fake prices (real me CSV se lena)
test_prices = [100, 102, 101, 103, 105, 104, 106, 108, 107, 109,
               110, 112, 111, 113, 115, 114, 116, 118, 117, 119,
               120, 122, 121, 123, 125, 124, 126, 128, 127, 129,
               130, 132, 131, 133, 135, 134, 136, 138, 137, 139,
               140, 142, 141, 143, 145, 144, 146, 148, 147, 149,
               150, 152, 151, 153, 155, 154, 156, 158, 157, 159]

response = requests.post(f"{API_URL}/predict", json={"prices": test_prices})
print(response.json())
```

Ye aana chahiye:
```json
{
  "predicted_price": 161.23,
  "last_price": 159.0,
  "direction": "up",
  "change_percent": 1.40,
  "timestamp": "2024-01-15T10:30:00"
}
```

### Real CSV data se test karo

```python
import pandas as pd
import requests

# Apna CSV load karo
data = pd.read_csv("all_stocks_5yr.csv", on_bad_lines='skip')
data.columns = data.columns.str.strip().str.lower()
data['date'] = pd.to_datetime(data['date'], errors='coerce')
data = data.dropna(subset=['date']).sort_values('date').reset_index(drop=True)

# Kisi ek stock ke last 60 prices lo
# Pehle dekho kaun kaun se stocks hain
print("Available stocks:", data['name'].unique()[:10] if 'name' in data.columns else "name column nahi hai")

# Last 60 closing prices lo
last_60_prices = data['close'].tail(60).tolist()

# API call karo
response = requests.post(f"{API_URL}/predict", json={"prices": last_60_prices})
print("Prediction:", response.json())
```

---

## Part 4 - Frontend Banana (HTML Page)

Frontend matlab ek webpage jo browser me khulega. Usme ek form hoga jahan prices dalo aur prediction dekho.

Colab me naya cell banao:

```python
html_code = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stock Price Predictor</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: 'Segoe UI', sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            min-height: 100vh;
            padding: 40px 20px;
        }

        .container {
            max-width: 700px;
            margin: 0 auto;
        }

        h1 {
            font-size: 32px;
            margin-bottom: 8px;
            color: #38bdf8;
        }

        .subtitle {
            color: #94a3b8;
            margin-bottom: 32px;
            font-size: 15px;
        }

        .card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 16px;
            padding: 28px;
            margin-bottom: 24px;
        }

        .card h2 {
            font-size: 18px;
            margin-bottom: 16px;
            color: #cbd5e1;
        }

        label {
            display: block;
            font-size: 13px;
            color: #94a3b8;
            margin-bottom: 6px;
        }

        input, textarea {
            width: 100%;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 12px;
            color: #e2e8f0;
            font-size: 14px;
            margin-bottom: 16px;
        }

        textarea { height: 100px; resize: vertical; }

        button {
            background: linear-gradient(135deg, #22c55e, #06b6d4);
            border: none;
            color: #0f172a;
            padding: 14px 28px;
            border-radius: 12px;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            width: 100%;
        }

        button:disabled { opacity: 0.5; cursor: not-allowed; }

        .result-box {
            display: none;
            margin-top: 24px;
        }

        .result-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-top: 16px;
        }

        .result-item {
            background: #0f172a;
            border-radius: 12px;
            padding: 16px;
            border: 1px solid #334155;
        }

        .result-item .label {
            font-size: 12px;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .result-item .value {
            font-size: 24px;
            font-weight: 700;
            margin-top: 6px;
        }

        .up { color: #22c55e; }
        .down { color: #f97316; }
        .neutral { color: #38bdf8; }

        .error-box {
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid #ef4444;
            border-radius: 10px;
            padding: 14px;
            color: #fca5a5;
            margin-top: 16px;
            display: none;
        }

        .loading { display: none; color: #94a3b8; margin-top: 12px; text-align: center; }

        .api-url-note {
            background: rgba(56, 189, 248, 0.08);
            border: 1px solid rgba(56, 189, 248, 0.3);
            border-radius: 10px;
            padding: 14px;
            font-size: 13px;
            color: #7dd3fc;
            margin-bottom: 20px;
        }

        .history-item {
            background: #0f172a;
            border-radius: 10px;
            padding: 14px;
            margin-bottom: 10px;
            border: 1px solid #1e293b;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Stock Price Predictor</h1>
        <p class="subtitle">LSTM model se next day ka price predict karo</p>

        <div class="api-url-note">
            API URL: <span id="api-url-display">Neeche dalo</span>
        </div>

        <div class="card">
            <h2>Settings</h2>
            <label>Apni API URL yahan dalo (ngrok URL)</label>
            <input type="text" id="api-url" placeholder="https://xxxx-xx-xx.ngrok.io" />

            <button onclick="saveApiUrl()">URL Save Karo</button>
        </div>

        <div class="card">
            <h2>Prediction Karo</h2>
            <label>Last 60 din ke closing prices (comma se alag karo)</label>
            <textarea id="prices-input" placeholder="100.5, 102.3, 101.8, 103.2, ..."></textarea>

            <label>Ya CSV file se automatically lo</label>
            <input type="file" id="csv-file" accept=".csv" onchange="loadFromCSV()" />

            <button id="predict-btn" onclick="predict()">Predict Karo</button>
            <div class="loading" id="loading">Prediction ho rahi hai...</div>
            <div class="error-box" id="error-box"></div>

            <div class="result-box" id="result-box">
                <h2>Result</h2>
                <div class="result-grid">
                    <div class="result-item">
                        <div class="label">Aaj Ka Price</div>
                        <div class="value neutral" id="last-price">-</div>
                    </div>
                    <div class="result-item">
                        <div class="label">Predicted Price</div>
                        <div class="value" id="predicted-price">-</div>
                    </div>
                    <div class="result-item">
                        <div class="label">Direction</div>
                        <div class="value" id="direction">-</div>
                    </div>
                    <div class="result-item">
                        <div class="label">Change</div>
                        <div class="value" id="change-pct">-</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Purani Predictions (MongoDB se)</h2>
            <button onclick="loadHistory()">History Load Karo</button>
            <div id="history-list" style="margin-top: 16px;"></div>
        </div>
    </div>

    <script>
        let API_URL = localStorage.getItem("api_url") || "";

        if (API_URL) {
            document.getElementById("api-url").value = API_URL;
            document.getElementById("api-url-display").textContent = API_URL;
        }

        function saveApiUrl() {
            API_URL = document.getElementById("api-url").value.trim().replace(/\\/$/, "");
            localStorage.setItem("api_url", API_URL);
            document.getElementById("api-url-display").textContent = API_URL;
            alert("URL save ho gaya: " + API_URL);
        }

        function loadFromCSV() {
            const file = document.getElementById("csv-file").files[0];
            if (!file) return;

            const reader = new FileReader();
            reader.onload = function(e) {
                const lines = e.target.result.split("\\n");
                const header = lines[0].toLowerCase().split(",");
                const closeIdx = header.findIndex(h => h.trim() === "close");

                if (closeIdx === -1) {
                    alert("CSV me 'close' column nahi mila");
                    return;
                }

                const prices = [];
                for (let i = 1; i < lines.length; i++) {
                    const cols = lines[i].split(",");
                    if (cols[closeIdx] && !isNaN(parseFloat(cols[closeIdx]))) {
                        prices.push(parseFloat(cols[closeIdx]));
                    }
                }

                const last60 = prices.slice(-60);
                document.getElementById("prices-input").value = last60.join(", ");
                alert(`CSV se ${prices.length} prices mile, last 60 fill ho gaye!`);
            };
            reader.readAsText(file);
        }

        async function predict() {
            if (!API_URL) {
                alert("Pehle API URL dalo aur save karo!");
                return;
            }

            const input = document.getElementById("prices-input").value.trim();
            if (!input) {
                alert("Prices dalo pehle!");
                return;
            }

            const prices = input.split(",").map(p => parseFloat(p.trim())).filter(p => !isNaN(p));

            if (prices.length < 60) {
                alert(`Kam se kam 60 prices chahiye. Tumne ${prices.length} diye.`);
                return;
            }

            document.getElementById("predict-btn").disabled = true;
            document.getElementById("loading").style.display = "block";
            document.getElementById("result-box").style.display = "none";
            document.getElementById("error-box").style.display = "none";

            try {
                const response = await fetch(`${API_URL}/predict`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ prices: prices })
                });

                const data = await response.json();

                if (data.error) {
                    throw new Error(data.error);
                }

                document.getElementById("last-price").textContent = data.last_price;
                document.getElementById("predicted-price").textContent = data.predicted_price;
                document.getElementById("predicted-price").className = "value " + data.direction;

                const dirEl = document.getElementById("direction");
                dirEl.textContent = data.direction === "up" ? "Upar Jayega" : "Neeche Jayega";
                dirEl.className = "value " + data.direction;

                const chgEl = document.getElementById("change-pct");
                chgEl.textContent = (data.change_percent > 0 ? "+" : "") + data.change_percent + "%";
                chgEl.className = "value " + data.direction;

                document.getElementById("result-box").style.display = "block";

            } catch (err) {
                document.getElementById("error-box").textContent = "Error: " + err.message;
                document.getElementById("error-box").style.display = "block";
            } finally {
                document.getElementById("predict-btn").disabled = false;
                document.getElementById("loading").style.display = "none";
            }
        }

        async function loadHistory() {
            if (!API_URL) {
                alert("Pehle API URL dalo!");
                return;
            }

            try {
                const response = await fetch(`${API_URL}/predictions`);
                const data = await response.json();
                const list = document.getElementById("history-list");

                if (data.predictions.length === 0) {
                    list.innerHTML = "<p style='color:#64748b'>Abhi koi prediction nahi hai</p>";
                    return;
                }

                list.innerHTML = data.predictions.map(p => `
                    <div class="history-item">
                        <div>
                            <div style="font-size:13px;color:#64748b">${p.timestamp ? p.timestamp.split("T")[0] : "N/A"}</div>
                            <div style="font-size:15px;font-weight:600">Last: ${p.last_price} → Predicted: ${p.predicted_price}</div>
                        </div>
                        <div class="${p.direction}" style="font-size:18px;font-weight:700">
                            ${p.direction === "up" ? "Upar" : "Neeche"} ${p.change_percent > 0 ? "+" : ""}${p.change_percent}%
                        </div>
                    </div>
                `).join("");

            } catch (err) {
                document.getElementById("history-list").innerHTML = "<p style='color:#ef4444'>Error: " + err.message + "</p>";
            }
        }
    </script>
</body>
</html>"""

with open("index.html", "w", encoding="utf-8") as f:
    f.write(html_code)

print("index.html ban gaya!")

# Download karo
from google.colab import files
files.download("index.html")
```

---

## Part 5 - Sab Kuch Ek Saath Chalao

Ye order follow karo:

### Step 1: Colab me API chalaao

Upar wala Part 2 ka code run karo. Ek ngrok URL milega jaise:
```
https://abc123.ngrok.io
```

### Step 2: index.html browser me kholo

1. Download ki hui `index.html` file pe double click karo
2. Browser me khulegi
3. "Settings" section me apna ngrok URL dalo
4. "URL Save Karo" pe click karo

### Step 3: Prediction karo

Option A - Manual prices dalo:
- Textarea me 60 prices comma se alag karke dalo
- "Predict Karo" pe click karo

Option B - CSV file se:
- "Choose File" pe click karo
- Apni `all_stocks_5yr.csv` select karo
- Automatically last 60 prices fill ho jayenge
- "Predict Karo" pe click karo

### Step 4: History dekho

"History Load Karo" pe click karo - MongoDB se saari purani predictions aayengi.

---

## Poora Flow Ek Baar Aur Samjho

```
Tumhara Browser (index.html)
        |
        | POST /predict  {"prices": [100, 102, ...]}
        |
        v
FastAPI Server (Colab me chal raha hai)
        |
        | prices liye, scale kiye, model ko diye
        |
        v
LSTM Model (stock_model.h5)
        |
        | prediction di
        |
        v
FastAPI ne result MongoDB me save kiya
        |
        | result wapas bheja browser ko
        |
        v
Browser ne result dikhaya
```

---

## Common Problems Aur Solutions

### Problem 1: CORS Error browser me

```
Access to fetch at 'https://...' has been blocked by CORS policy
```

Solution: API code me ye line check karo, honi chahiye:
```python
app.add_middleware(CORSMiddleware, allow_origins=["*"], ...)
```

### Problem 2: ngrok URL kaam nahi kar raha

Har baar Colab restart hone pe ngrok URL badal jata hai. Naya URL copy karo aur HTML me update karo.

### Problem 3: Model load nahi ho raha

```
OSError: No file or directory: 'stock_model.h5'
```

Solution: Pehle files upload karo Colab me:
```python
from google.colab import files
uploaded = files.upload()  # stock_model.h5 aur scaler.pkl dono upload karo
```

### Problem 4: Prediction bahut galat aa rahi hai

Ye ho sakta hai agar scaler save nahi kiya tha. Dobara model train karo aur is baar scaler bhi save karo:
```python
joblib.dump(scaler, "scaler.pkl")
```

### Problem 5: MongoDB me data save nahi ho raha

Check karo ki password sahi hai aur Network Access me 0.0.0.0/0 allow hai.

---

## Aage Kya Kar Sakti Ho

### 1. Multiple Stocks Support

Abhi sirf prices bhejti ho. Stock ka naam bhi bhejo aur alag alag stocks ke liye predict karo:

```python
class PredictRequest(BaseModel):
    symbol: str  # "RELIANCE", "TCS" etc
    prices: list
```

### 2. Graph API me Add Karo

Prediction ke saath graph data bhi bhejo:

```python
@app.get("/chart/{symbol}")
def get_chart(symbol: str):
    # CSV se data lo, return karo
    pass
```

### 3. Streamlit se Better UI

HTML ki jagah Streamlit use karo - ye Python me hi likhte hain:

```python
!pip install streamlit

# app.py
import streamlit as st
import requests

st.title("Stock Predictor")
prices_input = st.text_area("60 prices dalo")
if st.button("Predict"):
    prices = [float(p) for p in prices_input.split(",")]
    result = requests.post(f"{API_URL}/predict", json={"prices": prices}).json()
    st.metric("Predicted Price", result["predicted_price"])
    st.metric("Direction", result["direction"])
```

### 4. Automatic Daily Prediction

Har din automatically predict karo aur MongoDB me save karo:

```python
import schedule
import time

def daily_job():
    # yfinance se latest prices lo
    # predict karo
    # MongoDB me save karo
    pass

schedule.every().day.at("09:30").do(daily_job)  # Market open hone pe
```

---

## Zaruri Links

- ngrok (free public URL): https://ngrok.com
- FastAPI docs: https://fastapi.tiangolo.com
- MongoDB Atlas: https://www.mongodb.com/cloud/atlas
- Streamlit (easy UI): https://streamlit.io
- yfinance (live stock data): https://pypi.org/project/yfinance
