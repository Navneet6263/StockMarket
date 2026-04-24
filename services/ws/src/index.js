require("dotenv").config();

const http = require("http");
const { WebSocketServer } = require("ws");
const axios = require("axios");

const port = Number(process.env.WS_PORT || 4001);
const apiUrl = process.env.API_URL || "http://localhost:8000";

const server = http.createServer();
const wss = new WebSocketServer({ server });

const subscriptions = new Map();
const activeTrades = new Map();

function broadcast(payload) {
  const message = JSON.stringify(payload);
  wss.clients.forEach((client) => {
    if (client.readyState === 1) {
      client.send(message);
    }
  });
}

async function fetchLiveData(symbol) {
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
        ws.send(JSON.stringify({ type: "subscribed", symbol: symbol, ts: new Date().toISOString() }));
      }
      
      if (message.type === "unsubscribe" && message.symbol) {
        const symbol = message.symbol.toUpperCase();
        if (subscriptions.has(symbol)) {
          subscriptions.get(symbol).delete(ws);
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

async function sendLiveUpdates() {
  for (const [symbol, clients] of subscriptions.entries()) {
    if (clients.size > 0) {
      const liveData = await fetchLiveData(symbol);
      if (liveData) {
        const message = JSON.stringify({ type: "live_update", symbol: symbol, data: liveData, ts: new Date().toISOString() });
        clients.forEach((client) => {
          if (client.readyState === 1) {
            client.send(message);
          }
        });
      }
    }
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

setInterval(sendLiveUpdates, 2000);
setInterval(sendTradeUpdates, 1000);

setInterval(() => {
  broadcast({ type: "heartbeat", ts: new Date().toISOString(), active_subscriptions: Array.from(subscriptions.keys()) });
}, 30000);

server.listen(port, () => {
  console.log(`Enhanced WebSocket server on ${port}`);
  console.log("Features: Live data, Patterns, ML predictions");
});
