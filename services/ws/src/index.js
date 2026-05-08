require("dotenv").config();

const path = require("path");
require("dotenv").config({ path: path.resolve(__dirname, "../../../.env") });
require("dotenv").config({ path: path.resolve(__dirname, "../../api/.env"), override: true });
require("dotenv").config({ path: path.resolve(__dirname, "../.env"), override: true });

const http = require("http");
const { WebSocketServer } = require("ws");
const axios = require("axios");
const { AngelOneStream } = require("./angeloneStream");

function positiveNumber(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

const port = Number(process.env.WS_PORT || 4001);
const apiUrl = process.env.API_URL || "http://localhost:8000";
const fallbackPollIntervalMs = positiveNumber(process.env.FALLBACK_POLL_INTERVAL_MS, 500);
const fallbackPollConcurrency = Math.max(1, Math.floor(positiveNumber(process.env.FALLBACK_POLL_CONCURRENCY, 10)));

const server = http.createServer();
const wss = new WebSocketServer({ server });
const angelStream = new AngelOneStream();

const subscriptions = new Map();
const activeTrades = new Map();
let fallbackPollRunning = false;

function broadcast(payload) {
  const message = JSON.stringify(payload);
  wss.clients.forEach((client) => {
    if (client.readyState === 1) {
      client.send(message);
    }
  });
}

async function fetchLiveData(symbol) {
  const tick = angelStream.getLatest(symbol);
  if (tick && !angelStream.shouldUseFallback(symbol)) {
    return tick;
  }
  try {
    const response = await axios.get(`${apiUrl}/live/${symbol}`);
    return response.data;
  } catch (error) {
    console.error(`Error fetching ${symbol}:`, error.message);
    return null;
  }
}

wss.on("connection", (ws) => {
  ws.send(JSON.stringify({ type: "welcome", ts: new Date().toISOString(), message: "Live stock data feed" }));

  ws.on("message", (data) => {
    try {
      const message = JSON.parse(data);
      
      if (message.type === "subscribe" && message.symbol) {
        const symbol = message.symbol.toUpperCase();
        if (!subscriptions.has(symbol)) {
          subscriptions.set(symbol, new Set());
        }
        subscriptions.get(symbol).add(ws);
        angelStream.subscribe(symbol).catch((error) => {
          console.error(`AngelOne subscribe failed ${symbol}:`, error.message);
        });
        ws.send(JSON.stringify({
          type: "subscribed",
          symbol: symbol,
          ts: new Date().toISOString(),
          source: angelStream.isConfigured() ? "angelone_ws" : "http_fallback",
          angelone: angelStream.status()
        }));
      }
      
      if (message.type === "unsubscribe" && message.symbol) {
        const symbol = message.symbol.toUpperCase();
        if (subscriptions.has(symbol)) {
          subscriptions.get(symbol).delete(ws);
          if (subscriptions.get(symbol).size === 0) {
            angelStream.unsubscribe(symbol).catch(() => {});
          }
        }
      }
      
      if (message.type === "track_my_trade") {
        const tradeId = `TRADE_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
        activeTrades.set(tradeId, {
          ws: ws,
          strike: message.strike,
          optionType: message.optionType,
          buyPrice: message.buy_price,
          lotSize: message.lot_size || 75,
          entryTime: Date.now(),
          peakProfit: 0,
          peakProfitPct: 0
        });
        ws.send(JSON.stringify({ type: "trade_tracked", trade_id: tradeId, ts: new Date().toISOString() }));
      }
      
      if (message.type === "stop_tracking" && message.trade_id) {
        activeTrades.delete(message.trade_id);
        ws.send(JSON.stringify({ type: "tracking_stopped", trade_id: message.trade_id, ts: new Date().toISOString() }));
      }
    } catch (error) {
      console.error("Error processing message:", error);
    }
  });

  ws.on("close", () => {
    subscriptions.forEach((clients) => {
      clients.delete(ws);
    });
    for (const [tradeId, trade] of activeTrades.entries()) {
      if (trade.ws === ws) {
        activeTrades.delete(tradeId);
      }
    }
  });
});

angelStream.onTick((symbol, tick) => {
  const clients = subscriptions.get(symbol);
  if (!clients || clients.size === 0) return;
  const message = JSON.stringify({
    type: "live_update",
    symbol,
    data: tick,
    ts: new Date().toISOString(),
    source: "angelone_ws"
  });
  clients.forEach((client) => {
    if (client.readyState === 1) {
      client.send(message);
    }
  });
});

async function sendFallbackUpdate(symbol, clients) {
  const liveData = await fetchLiveData(symbol);
  if (!liveData) return;
  const message = JSON.stringify({
    type: "live_update",
    symbol: symbol,
    data: liveData,
    ts: new Date().toISOString(),
    source: liveData.source || liveData.data_source || "http_fallback"
  });
  clients.forEach((client) => {
    if (client.readyState === 1) {
      client.send(message);
    }
  });
}

async function sendLiveUpdates() {
  if (fallbackPollRunning) return;
  fallbackPollRunning = true;
  try {
    const jobs = [];
    for (const [symbol, clients] of subscriptions.entries()) {
      if (clients.size > 0 && angelStream.shouldUseFallback(symbol)) {
        jobs.push([symbol, clients]);
      }
    }
    for (let i = 0; i < jobs.length; i += fallbackPollConcurrency) {
      const batch = jobs.slice(i, i + fallbackPollConcurrency);
      await Promise.allSettled(batch.map(([symbol, clients]) => sendFallbackUpdate(symbol, clients)));
    }
  } finally {
    fallbackPollRunning = false;
  }
}

async function sendTradeUpdates() {
  for (const [tradeId, trade] of activeTrades.entries()) {
    if (trade.ws.readyState === 1) {
      try {
        const res = await axios.post(`${apiUrl}/track-trade`, {
          trade_id: tradeId,
          instrument: "NIFTY",
          option_type: trade.optionType,
          strike: trade.strike,
          buying_price: trade.buyPrice,
          lot_size: trade.lotSize
        });
        
        if (res.data && res.data.active_trade) {
          const tradeData = res.data.active_trade;
          const pnlPct = parseFloat(tradeData.pnl_pct.replace('%', '').replace('+', ''));
          
          if (pnlPct > trade.peakProfitPct) {
            trade.peakProfit = parseFloat(tradeData.pnl.replace('+', ''));
            trade.peakProfitPct = pnlPct;
          }
          
          const profitDrop = trade.peakProfitPct - pnlPct;
          let trailingAlert = null;
          
          if (trade.peakProfitPct > 15 && profitDrop > 20) {
            trailingAlert = {
              type: 'TRAILING_SL_HIT',
              message: `🚨 Trailing SL hit! Peak profit ${trade.peakProfitPct.toFixed(1)}% se ${profitDrop.toFixed(1)}% gir gaya. Ghar le jao munafa!`,
              color: '#ef4444',
              blink: true
            };
          }
          
          trade.ws.send(JSON.stringify({
            type: "trade_update",
            trade_id: tradeId,
            data: tradeData,
            peak_profit: trade.peakProfit,
            peak_profit_pct: trade.peakProfitPct,
            trailing_alert: trailingAlert,
            ts: new Date().toISOString()
          }));
        }
      } catch (error) {
        console.error(`Error tracking trade ${tradeId}:`, error.message);
      }
    }
  }
}

setInterval(sendLiveUpdates, fallbackPollIntervalMs);
setInterval(sendTradeUpdates, 1000);

setInterval(() => {
  broadcast({
    type: "heartbeat",
    ts: new Date().toISOString(),
    active_subscriptions: Array.from(subscriptions.keys()),
    angelone: angelStream.status()
  });
}, 30000);

server.listen(port, () => {
  console.log(`Enhanced WebSocket server on ${port}`);
  console.log("Features: AngelOne tick streaming with HTTP fallback, trade tracking");
});
