"use client";

import React, { useEffect, useState, useMemo } from "react";
import { API_URL, fmt, fmtPct, humanize } from "../lib/market";

interface Trade {
  symbol: string;
  direction: string;
  entry_price: number;
  actual_entry_price?: number;
  entry_date: string;
  target1?: number;
  stop_loss?: number;
  confidence?: number;
  rr?: number;
  setup_type?: string;
  signal_stage?: string;
  status: string;
  order_type?: string;
  slippage?: number;
  // open position live fields
  current_price?: number;
  unrealized_pnl?: number;
  unrealized_pnl_pct?: number;
  // closed fields
  exit_price?: number;
  exit_date?: string;
  exit_reason?: string;
  exit_candle?: string;
  days_held?: number;
  pnl?: number;
  pnl_pct?: number;
}

function Badge({ text, color }: { text: string; color: string }) {
  return (
    <span style={{
      fontSize: 11, fontWeight: 700, padding: "2px 8px",
      borderRadius: 12, background: color, color: "#fff", whiteSpace: "nowrap"
    }}>{text}</span>
  );
}

function dirColor(d: string) { return d === "bullish" ? "#22c55e" : "#ef4444"; }
function pnlColor(v: number)  { return v >= 0 ? "#22c55e" : "#ef4444"; }

function DaysBar({ days, max = 14 }: { days: number; max?: number }) {
  const pct = Math.min(100, (days / max) * 100);
  const color = pct > 75 ? "#ef4444" : pct > 50 ? "#f59e0b" : "#22c55e";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ flex: 1, height: 6, background: "#333", borderRadius: 4, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: color, borderRadius: 4 }} />
      </div>
      <span style={{ fontSize: 12, color, fontWeight: 700, minWidth: 30 }}>{days}d</span>
    </div>
  );
}

function OpenCard({ pos, onClose }: { pos: Trade; onClose: (s: string) => void }) {
  const entry     = pos.actual_entry_price || pos.entry_price;
  const pnl       = pos.unrealized_pnl ?? 0;
  const pnlPct    = pos.unrealized_pnl_pct ?? 0;
  const days      = Math.round((Date.now() - new Date(pos.entry_date).getTime()) / 86400000);
  const toPct     = pos.target1 && entry ? ((pos.target1 - entry) / entry * 100) : null;
  const slPct     = pos.stop_loss && entry ? ((entry - pos.stop_loss) / entry * 100) : null;

  return (
    <div style={{
      background: "#1a1a1a", border: `1px solid ${pnl >= 0 ? "#22c55e44" : "#ef444444"}`,
      borderRadius: 12, padding: 16, display: "flex", flexDirection: "column", gap: 10
    }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontWeight: 800, fontSize: 18 }}>{pos.symbol}</span>
          <Badge text={pos.direction.toUpperCase()} color={dirColor(pos.direction)} />
          {pos.order_type === "live"  && <Badge text="🟢 Live Order"  color="#16a34a" />}
          {pos.order_type === "paper" && <Badge text="📄 Paper Trade" color="#6b7280" />}
          {pos.confidence ? <Badge text={`${fmt(pos.confidence, 0)}/100 alignment`} color="#7c3aed" /> : null}
          {pos.setup_type ? <Badge text={humanize(pos.setup_type)} color="#0369a1" /> : null}
        </div>
        <button
          onClick={() => onClose(pos.symbol)}
          style={{ background: "#ef4444", color: "#fff", border: "none", borderRadius: 8,
            padding: "6px 14px", cursor: "pointer", fontWeight: 700, fontSize: 12 }}
        >✕ Close</button>
      </div>

      {/* Prices row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
        <div style={{ background: "#111", borderRadius: 8, padding: "8px 12px" }}>
          <div style={{ fontSize: 11, color: "#888" }}>Entry Price</div>
          <div style={{ fontWeight: 700 }}>₹{fmt(entry, 2)}</div>
          {pos.slippage != null && (
            <div style={{ fontSize: 11, color: pos.slippage > 0 ? "#ef4444" : "#22c55e" }}>
              Slip {pos.slippage > 0 ? "+" : ""}₹{fmt(pos.slippage, 2)}
            </div>
          )}
        </div>
        <div style={{ background: "#111", borderRadius: 8, padding: "8px 12px" }}>
          <div style={{ fontSize: 11, color: "#888" }}>Live Price</div>
          <div style={{ fontWeight: 700 }}>{pos.current_price ? `₹${fmt(pos.current_price, 2)}` : "—"}</div>
        </div>
        <div style={{ background: "#111", borderRadius: 8, padding: "8px 12px" }}>
          <div style={{ fontSize: 11, color: "#888" }}>Unrealized PnL</div>
          <div style={{ fontWeight: 800, color: pnlColor(pnl) }}>
            {pnl >= 0 ? "+" : ""}₹{fmt(pnl, 2)}
          </div>
          <div style={{ fontSize: 11, color: pnlColor(pnlPct) }}>
            {pnlPct >= 0 ? "+" : ""}{fmt(pnlPct, 2)}%
          </div>
        </div>
      </div>

      {/* Target / SL */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div style={{ background: "#0f2b0f", borderRadius: 8, padding: "8px 12px" }}>
          <div style={{ fontSize: 11, color: "#888" }}>🎯 Target</div>
          <div style={{ color: "#22c55e", fontWeight: 700 }}>
            ₹{fmt(pos.target1, 2)}
            {toPct ? <span style={{ fontSize: 11, marginLeft: 6 }}>(+{fmt(toPct, 1)}%)</span> : null}
          </div>
        </div>
        <div style={{ background: "#2b0f0f", borderRadius: 8, padding: "8px 12px" }}>
          <div style={{ fontSize: 11, color: "#888" }}>🛑 Stop Loss</div>
          <div style={{ color: "#ef4444", fontWeight: 700 }}>
            ₹{fmt(pos.stop_loss, 2)}
            {slPct ? <span style={{ fontSize: 11, marginLeft: 6 }}>(-{fmt(slPct, 1)}%)</span> : null}
          </div>
        </div>
      </div>

      {/* Days held progress bar */}
      <div>
        <div style={{ fontSize: 11, color: "#888", marginBottom: 4 }}>
          Holding — {new Date(pos.entry_date).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}
        </div>
        <DaysBar days={days} />
      </div>
    </div>
  );
}

function HistoryRow({ pos }: { pos: Trade }) {
  const pnl    = pos.pnl ?? 0;
  const pnlPct = pos.pnl_pct ?? 0;
  const isWin  = pnl >= 0;

  const statusColor: Record<string, string> = {
    target_hit: "#22c55e",
    stopped:    "#ef4444",
    closed:     "#f59e0b",
  };

  const exitIcon: Record<string, string> = {
    "Target 1 Hit":                 "🏆",
    "Stop Loss Hit":                "🛑",
    "Early Profit Booking (RSI Reversal)": "📊",
    "Time Stop (14 days)":          "⏰",
    "Manual Close":                 "👆",
  };

  return (
    <tr style={{ borderBottom: "1px solid #222" }}>
      <td style={{ padding: "10px 12px" }}>
        <div style={{ fontWeight: 700 }}>{pos.symbol}</div>
        <Badge text={pos.direction.toUpperCase()} color={dirColor(pos.direction)} />
        {pos.setup_type ? <div style={{ fontSize: 11, color: "#888", marginTop: 2 }}>{humanize(pos.setup_type)}</div> : null}
      </td>
      <td style={{ padding: "10px 12px", fontSize: 12, color: "#aaa" }}>
        <div>📅 {new Date(pos.entry_date).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}</div>
        <div>🚪 {pos.exit_date ? new Date(pos.exit_date).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "—"}</div>
        <div style={{ color: "#666", marginTop: 2 }}>🕐 {pos.days_held ?? "?"} days</div>
      </td>
      <td style={{ padding: "10px 12px", fontSize: 13 }}>
        <div>IN: ₹{fmt(pos.actual_entry_price || pos.entry_price, 2)}</div>
        <div>OUT: ₹{fmt(pos.exit_price, 2)}</div>
        {pos.slippage != null && (
          <div style={{ fontSize: 11, color: pos.slippage > 0 ? "#ef4444" : "#22c55e" }}>
            Slip: {pos.slippage > 0 ? "+" : ""}₹{fmt(pos.slippage, 2)}
          </div>
        )}
      </td>
      <td style={{ padding: "10px 12px", textAlign: "right" }}>
        <div style={{ fontWeight: 800, fontSize: 16, color: pnlColor(pnl) }}>
          {isWin ? "+" : ""}₹{fmt(pnl, 2)}
        </div>
        <div style={{ fontSize: 12, color: pnlColor(pnlPct) }}>
          {pnlPct >= 0 ? "+" : ""}{fmt(pnlPct, 2)}%
        </div>
      </td>
      <td style={{ padding: "10px 12px" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{
            fontSize: 12, padding: "3px 8px", borderRadius: 8, fontWeight: 700,
            background: statusColor[pos.status] + "22",
            color: statusColor[pos.status] || "#aaa",
            border: `1px solid ${statusColor[pos.status] || "#444"}44`
          }}>
            {exitIcon[pos.exit_reason ?? ""] || "📌"} {pos.exit_reason || humanize(pos.status)}
          </span>
          {pos.exit_candle && pos.exit_candle !== "unknown" && (
            <span style={{ fontSize: 11, color: "#f59e0b" }}>
              🕯️ {pos.exit_candle}
            </span>
          )}
        </div>
      </td>
      <td style={{ padding: "10px 12px" }}>
        {pos.rr ? <div style={{ fontSize: 12 }}>⚖️ 1:{fmt(pos.rr, 1)}</div> : null}
        {pos.confidence ? <div style={{ fontSize: 12, color: "#888" }}>🎯 {fmt(pos.confidence, 0)}/100 alignment</div> : null}
      </td>
    </tr>
  );
}

export default function PerformancePage() {
  const [openPositions, setOpenPositions] = useState<Trade[]>([]);
  const [history,       setHistory]       = useState<Trade[]>([]);
  const [loading,       setLoading]       = useState(true);
  const [filter,        setFilter]        = useState("all");
  const [closingSymbol, setClosingSymbol] = useState<string | null>(null);

  async function loadAll() {
    try {
      const [o, h] = await Promise.all([
        fetch(`${API_URL}/api/performance/open`),
        fetch(`${API_URL}/api/performance/history`),
      ]);
      if (o.ok) setOpenPositions((await o.json()).positions || []);
      if (h.ok) setHistory((await h.json()).positions || []);
    } catch {}
    setLoading(false);
  }

  useEffect(() => { loadAll(); }, []);

  // WebSocket live updates
  useEffect(() => {
    const base  = API_URL.startsWith("/") ? `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}${API_URL}` : API_URL.replace(/^http/, "ws");
    const ws    = new WebSocket(`${base}/api/performance/ws`);
    ws.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.type === "PERFORMANCE_SNAPSHOT") setOpenPositions(d.open_positions || []);
      } catch {}
    };
    return () => ws.close();
  }, []);

  // Filter history by time
  const filteredHistory = useMemo(() => {
    if (filter === "all") return history;
    const cutoff = Date.now() - (filter === "week" ? 7 : 30) * 86400000;
    return history.filter(p => p.exit_date && new Date(p.exit_date).getTime() >= cutoff);
  }, [history, filter]);

  // Summary stats
  const stats = useMemo(() => {
    const t    = filteredHistory.length;
    const wins = filteredHistory.filter(p => (p.pnl ?? 0) > 0);
    const loss = filteredHistory.filter(p => (p.pnl ?? 0) <= 0);
    const totalPnl = filteredHistory.reduce((s, p) => s + (p.pnl ?? 0), 0);
    const avgWin   = wins.length ? wins.reduce((s, p) => s + (p.pnl ?? 0), 0) / wins.length : 0;
    const avgLoss  = loss.length ? loss.reduce((s, p) => s + (p.pnl ?? 0), 0) / loss.length : 0;
    const best     = filteredHistory.reduce((m, p) => Math.max(m, p.pnl ?? 0), 0);
    const worst    = filteredHistory.reduce((m, p) => Math.min(m, p.pnl ?? 0), 0);
    const winRate  = t ? (wins.length / t) * 100 : 0;
    return { t, winRate, totalPnl, avgWin, avgLoss, best, worst };
  }, [filteredHistory]);

  async function handleClose(symbol: string) {
    if (!confirm(`Close ${symbol} position?`)) return;
    setClosingSymbol(symbol);
    try {
      const r = await fetch(`${API_URL}/api/performance/close/${symbol}`, { method: "POST" });
      if ((await r.json()).success) {
        setOpenPositions(p => p.filter(x => x.symbol !== symbol));
        loadAll();
      }
    } finally {
      setClosingSymbol(null);
    }
  }

  function exportCsv() {
    const rows = filteredHistory.map(p => [
      p.symbol, p.direction, p.entry_date, p.exit_date ?? "",
      p.actual_entry_price ?? p.entry_price, p.exit_price ?? "",
      p.pnl ?? "", p.pnl_pct ?? "", p.days_held ?? "",
      p.exit_reason ?? "", p.exit_candle ?? "", p.setup_type ?? ""
    ].join(","));
    const csv = "data:text/csv;charset=utf-8,Symbol,Direction,Entry Date,Exit Date,Entry Price,Exit Price,PnL,PnL%,Days Held,Exit Reason,Exit Candle,Setup\n" + rows.join("\n");
    const a   = document.createElement("a");
    a.href    = encodeURI(csv);
    a.download = "trade_history.csv";
    a.click();
  }

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-logo">StockAI</div>
        <div className="sidebar-section">Main</div>
        <a href="/" className="sidebar-item" style={{ textDecoration: "none", display: "block" }}>← Back to Scanner</a>
        <div className="sidebar-section">Portfolio</div>
        <button className="sidebar-item active">Performance Tracker</button>
      </aside>

      <main className="main-content">
        <div className="page-header" style={{ marginBottom: 24 }}>
          <h1>📊 Performance Tracker</h1>
          <p>Auto trade logging — every BUY signal is tracked from entry to exit</p>
        </div>

        {/* ── OPEN POSITIONS ─────────────────────────────────────── */}
        <section style={{ marginBottom: 40 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
            <h2 style={{ margin: 0 }}>Open Positions ({openPositions.length})</h2>
            <span style={{ fontSize: 12, color: "#22c55e", animation: "pulse 1s infinite" }}>● Live</span>
          </div>

          {loading ? (
            <p style={{ color: "#888" }}>Loading...</p>
          ) : openPositions.length === 0 ? (
            <p style={{ color: "#888" }}>No open positions. Every BUY alert will auto-appear here.</p>
          ) : (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16 }}>
              {openPositions.map(pos => (
                <OpenCard key={pos.symbol} pos={pos} onClose={handleClose} />
              ))}
            </div>
          )}
        </section>

        {/* ── HISTORY ────────────────────────────────────────────── */}
        <section>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12, marginBottom: 16 }}>
            <h2 style={{ margin: 0 }}>Closed Trades ({filteredHistory.length})</h2>
            <div style={{ display: "flex", gap: 8 }}>
              {["all", "month", "week"].map(f => (
                <button key={f} className="refresh-btn"
                  onClick={() => setFilter(f)}
                  style={{ fontWeight: filter === f ? 700 : 400, background: filter === f ? "var(--green)" : undefined, color: filter === f ? "#000" : undefined }}>
                  {f === "all" ? "All Time" : f === "month" ? "This Month" : "This Week"}
                </button>
              ))}
              <button className="refresh-btn" onClick={exportCsv}>⬇ CSV</button>
            </div>
          </div>

          {/* Summary stats */}
          <div className="stats-row" style={{ marginBottom: 20 }}>
            <div className="stat-card"><span>Total Trades</span><strong>{stats.t}</strong></div>
            <div className="stat-card"><span>Win Rate</span><strong style={{ color: stats.winRate >= 50 ? "#22c55e" : "#ef4444" }}>{fmt(stats.winRate, 1)}%</strong></div>
            <div className="stat-card"><span>Total PnL</span><strong style={{ color: pnlColor(stats.totalPnl) }}>{stats.totalPnl >= 0 ? "+" : ""}₹{fmt(stats.totalPnl, 2)}</strong></div>
            <div className="stat-card"><span>Avg Win</span><strong style={{ color: "#22c55e" }}>₹{fmt(stats.avgWin, 2)}</strong></div>
            <div className="stat-card"><span>Avg Loss</span><strong style={{ color: "#ef4444" }}>₹{fmt(stats.avgLoss, 2)}</strong></div>
            <div className="stat-card"><span>Best / Worst</span><strong>₹{fmt(stats.best, 0)} / ₹{fmt(stats.worst, 0)}</strong></div>
          </div>

          {filteredHistory.length === 0 ? (
            <p style={{ color: "#888" }}>No closed trades yet.</p>
          ) : (
            <div style={{ overflowX: "auto", background: "#111", borderRadius: 12, border: "1px solid #222" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid #333", color: "#888", fontSize: 12 }}>
                    <th style={{ padding: "10px 12px", textAlign: "left" }}>Symbol</th>
                    <th style={{ padding: "10px 12px", textAlign: "left" }}>Entry / Exit</th>
                    <th style={{ padding: "10px 12px", textAlign: "left" }}>Prices</th>
                    <th style={{ padding: "10px 12px", textAlign: "right" }}>PnL</th>
                    <th style={{ padding: "10px 12px", textAlign: "left" }}>Exit Reason + Candle</th>
                    <th style={{ padding: "10px 12px", textAlign: "left" }}>RR / Conf</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredHistory.map((pos, i) => <HistoryRow key={`${pos.symbol}-${i}`} pos={pos} />)}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
