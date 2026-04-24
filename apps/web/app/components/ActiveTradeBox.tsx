"use client";

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

type ActiveTrade = {
  instrument: string;
  entry_price: number;
  current_price: number;
  lot_size: number;
  pnl: string;
  pnl_pct: string;
  spot_price: number;
  ai_feedback: {
    rating: string;
    status: string;
    logic: string;
    risk_meter: string;
    action: string;
    emoji: string;
    color: string;
  };
};

type TrailingAlert = {
  type: string;
  message: string;
  color: string;
};

export default function ActiveTradeBox() {
  const [strike, setStrike] = useState("");
  const [optionType, setOptionType] = useState("CALL");
  const [buyingPrice, setBuyingPrice] = useState("");
  const [lotSize, setLotSize] = useState("75");
  const [activeTrade, setActiveTrade] = useState<ActiveTrade | null>(null);
  const [isTracking, setIsTracking] = useState(false);
  const [tradeId, setTradeId] = useState("");
  const [sessionKey, setSessionKey] = useState(0);
  const [peakProfit, setPeakProfit] = useState(0);
  const [peakProfitPct, setPeakProfitPct] = useState(0);
  const [trailingAlert, setTrailingAlert] = useState<TrailingAlert | null>(null);
  const [blink, setBlink] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const tradeIdRef = useRef("");
  const wsUrl = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:4001";

  useEffect(() => {
    if (!isTracking || !sessionKey) return;

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          type: "track_my_trade",
          strike: parseFloat(strike),
          optionType,
          buy_price: parseFloat(buyingPrice),
          lot_size: parseInt(lotSize, 10),
        })
      );
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);

        if (message.type === "trade_tracked" && message.trade_id) {
          tradeIdRef.current = message.trade_id;
          setTradeId(message.trade_id);
          return;
        }

        if (message.type === "trade_update" && message.trade_id === tradeIdRef.current) {
          setActiveTrade(message.data);
          setPeakProfit(message.peak_profit || 0);
          setPeakProfitPct(message.peak_profit_pct || 0);

          if (message.trailing_alert) {
            setTrailingAlert(message.trailing_alert);
            setBlink(true);
            setTimeout(() => setBlink(false), 500);
          }
        }
      } catch (err) {
        console.error("Trade tracking message error:", err);
      }
    };

    ws.onerror = (err) => {
      console.error("Trade tracking socket error:", err);
    };

    return () => {
      if (tradeIdRef.current && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "stop_tracking", trade_id: tradeIdRef.current }));
      }
      ws.close();
    };
  }, [buyingPrice, isTracking, lotSize, optionType, sessionKey, strike, wsUrl]);

  function handleStartTracking() {
    if (!strike || !buyingPrice) {
      alert("Please fill strike and buying price.");
      return;
    }

    setTradeId("");
    tradeIdRef.current = "";
    setActiveTrade(null);
    setPeakProfit(0);
    setPeakProfitPct(0);
    setTrailingAlert(null);
    setIsTracking(true);
    setSessionKey(Date.now());
  }

  function handleStopTracking() {
    setIsTracking(false);
    setTradeId("");
    tradeIdRef.current = "";
    setActiveTrade(null);
    setTrailingAlert(null);
    if (wsRef.current) {
      wsRef.current.close();
    }
  }

  return (
    <div style={{ marginBottom: 20 }}>
      <div style={wrapperStyle}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center", flexWrap: "wrap", marginBottom: 16 }}>
          <div>
            <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1.1 }}>Options Tracker</div>
            <h3 style={{ margin: "6px 0 0" }}>Live option trade monitor</h3>
          </div>
          {tradeId && (
            <div style={{ color: "#94a3b8", fontSize: 13 }}>
              Trade ID: <span style={{ color: "#e2e8f0" }}>{tradeId}</span>
            </div>
          )}
        </div>

        {!isTracking ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
            <Field label="Strike Price">
              <input type="number" value={strike} onChange={(event) => setStrike(event.target.value)} placeholder="25500" />
            </Field>

            <Field label="Option Type">
              <select value={optionType} onChange={(event) => setOptionType(event.target.value)}>
                <option value="CALL">CALL</option>
                <option value="PUT">PUT</option>
              </select>
            </Field>

            <Field label="Buying Price">
              <input type="number" value={buyingPrice} onChange={(event) => setBuyingPrice(event.target.value)} placeholder="20" />
            </Field>

            <Field label="Lot Size">
              <input type="number" value={lotSize} onChange={(event) => setLotSize(event.target.value)} placeholder="75" />
            </Field>

            <div style={{ display: "flex", alignItems: "flex-end" }}>
              <button onClick={handleStartTracking} style={{ width: "100%" }}>
                Start Tracking
              </button>
            </div>
          </div>
        ) : (
          <div>
            {trailingAlert && (
              <div
                style={{
                  marginBottom: 16,
                  padding: 16,
                  background: `linear-gradient(135deg, ${trailingAlert.color}33, ${trailingAlert.color}12)`,
                  borderRadius: 16,
                  border: `2px solid ${trailingAlert.color}`,
                  boxShadow: blink ? `0 0 28px ${trailingAlert.color}` : "none",
                }}
              >
                <div style={{ color: trailingAlert.color, fontWeight: 700, textTransform: "uppercase", letterSpacing: 1 }}>
                  {trailingAlert.type.replace(/_/g, " ")}
                </div>
                <div style={{ marginTop: 8, color: "#f8fafc", fontSize: 15 }}>{trailingAlert.message}</div>
              </div>
            )}

            {activeTrade ? (
              <>
                <div
                  style={{
                    marginBottom: 16,
                    padding: 16,
                    background: `linear-gradient(135deg, ${activeTrade.ai_feedback.color}15, ${activeTrade.ai_feedback.color}08)`,
                    borderRadius: 16,
                    border: `1px solid ${activeTrade.ai_feedback.color}`,
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                    <div style={{ fontSize: 28 }}>{activeTrade.ai_feedback.emoji}</div>
                    <div>
                      <div style={{ color: activeTrade.ai_feedback.color, fontWeight: 700 }}>
                        {activeTrade.ai_feedback.rating.replace(/_/g, " ")}
                      </div>
                      <div style={{ marginTop: 4, color: "#94a3b8", fontSize: 13 }}>
                        Status: {activeTrade.ai_feedback.status} | Risk: {activeTrade.ai_feedback.risk_meter}
                      </div>
                    </div>
                  </div>
                  <div style={{ marginTop: 10, color: "#e2e8f0", lineHeight: 1.7 }}>{activeTrade.ai_feedback.logic}</div>
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12, marginBottom: 16 }}>
                  <Metric label="Instrument" value={activeTrade.instrument} />
                  <Metric label="Entry -> Current" value={`INR ${activeTrade.entry_price} -> INR ${activeTrade.current_price}`} />
                  <Metric label="P&L" value={`${activeTrade.pnl} (${activeTrade.pnl_pct})`} color={activeTrade.pnl.startsWith("+") ? "#22c55e" : "#ef4444"} />
                  <Metric label="Spot Price" value={`INR ${activeTrade.spot_price.toFixed(2)}`} />
                  {peakProfitPct > 0 && <Metric label="Peak Profit" value={`+INR ${peakProfit.toFixed(0)} (+${peakProfitPct.toFixed(1)}%)`} color="#22c55e" />}
                </div>

                <button onClick={handleStopTracking} style={{ background: "#ef4444", color: "#fff" }}>
                  Stop Tracking
                </button>
              </>
            ) : (
              <div style={{ color: "#94a3b8" }}>Waiting for first live trade update...</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <label style={{ fontSize: 12, color: "#94a3b8", display: "block", marginBottom: 6 }}>{label}</label>
      {children}
    </div>
  );
}

function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ background: "rgba(15,23,42,0.82)", padding: 12, borderRadius: 12, border: "1px solid rgba(148,163,184,0.14)" }}>
      <div style={{ fontSize: 11, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1 }}>{label}</div>
      <div style={{ marginTop: 8, color: color || "#e2e8f0", fontWeight: 700, lineHeight: 1.5 }}>{value}</div>
    </div>
  );
}

const wrapperStyle: CSSProperties = {
  padding: 18,
  background: "rgba(17,24,39,0.8)",
  borderRadius: 18,
  border: "1px solid rgba(148,163,184,0.18)",
};
