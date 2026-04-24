"use client";

import { useState } from "react";

type Trade = {
  trade_id: string;
  instrument: string;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
};

type Stats = {
  total_trades: number;
  win_rate: number;
  total_pnl: number;
  ai_accuracy: {
    ai_accuracy_pct: number;
    message: string;
  };
};

export default function TradeHistoryPanel() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

  async function loadHistory() {
    setLoading(true);
    try {
      const res = await fetch(`${apiUrl}/trade-history?limit=20`);
      const data = await res.json();
      setTrades(data.trades || []);
      setStats(data.stats || null);
      setShowHistory(true);
    } catch (err) {
      console.error("Trade history error:", err);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{ padding: 18, background: "rgba(17,24,39,0.8)", borderRadius: 18, border: "1px solid rgba(148,163,184,0.18)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16, gap: 16, flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1.1 }}>Review</div>
            <h3 style={{ margin: "6px 0 0" }}>Trade history and AI accuracy</h3>
          </div>
          <button onClick={loadHistory} disabled={loading}>
            {loading ? "Loading..." : showHistory ? "Refresh" : "Load History"}
          </button>
        </div>

        {showHistory && stats && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12, marginBottom: 16 }}>
              <Metric label="Total Trades" value={stats.total_trades} />
              <Metric label="Win Rate" value={`${stats.win_rate.toFixed(1)}%`} color="#22c55e" />
              <Metric label="Total P&L" value={`INR ${stats.total_pnl.toFixed(0)}`} color={stats.total_pnl >= 0 ? "#22c55e" : "#ef4444"} />
              <Metric label="AI Accuracy" value={`${stats.ai_accuracy.ai_accuracy_pct.toFixed(1)}%`} color="#38bdf8" />
            </div>

            <div style={{ marginBottom: 16, padding: 12, background: "rgba(59,130,246,0.1)", borderRadius: 12, border: "1px solid rgba(59,130,246,0.22)", color: "#e2e8f0" }}>
              {stats.ai_accuracy.message}
            </div>

            {trades.length > 0 && (
              <div style={{ display: "grid", gap: 10 }}>
                {trades.map((trade) => (
                  <div
                    key={trade.trade_id}
                    style={{
                      padding: 14,
                      background: "rgba(15,23,42,0.8)",
                      borderRadius: 14,
                      border: `1px solid ${trade.pnl >= 0 ? "rgba(34,197,94,0.25)" : "rgba(239,68,68,0.25)"}`,
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                      <div>
                        <div style={{ color: "#e2e8f0", fontWeight: 700 }}>{trade.instrument}</div>
                        <div style={{ marginTop: 6, color: "#94a3b8", fontSize: 13 }}>
                          Entry {trade.entry_price} {"->"} Exit {trade.exit_price}
                        </div>
                      </div>
                      <div style={{ textAlign: "right" }}>
                        <div style={{ color: trade.pnl >= 0 ? "#22c55e" : "#ef4444", fontWeight: 700 }}>
                          {trade.pnl >= 0 ? "+" : ""}INR {trade.pnl.toFixed(0)}
                        </div>
                        <div style={{ marginTop: 6, color: "#94a3b8", fontSize: 13 }}>
                          ({trade.pnl_pct >= 0 ? "+" : ""}
                          {trade.pnl_pct.toFixed(1)}%)
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Metric({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <div style={{ background: "rgba(15,23,42,0.82)", padding: 12, borderRadius: 12, border: "1px solid rgba(148,163,184,0.14)" }}>
      <div style={{ fontSize: 11, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1 }}>{label}</div>
      <div style={{ marginTop: 8, fontSize: 20, fontWeight: 700, color: color || "#e2e8f0" }}>{value}</div>
    </div>
  );
}
