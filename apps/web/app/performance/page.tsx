"use client";

import React, { useEffect, useState, useMemo } from "react";
import { API_URL, fmt, fmtPct, humanize } from "../lib/market";

interface Position {
  symbol: string;
  entry_price: number;
  entry_date: string;
  target1: number;
  stop_loss: number;
  direction: string;
  confidence: number;
  status: string;
  current_price?: number;
  unrealized_pnl?: number;
  unrealized_pnl_pct?: number;
  exit_price?: number;
  exit_date?: string;
  pnl?: number;
  exit_reason?: string;
  order_type?: string;
  actual_entry_price?: number;
  slippage?: number;
}

export default function PerformancePage() {
  const [openPositions, setOpenPositions] = useState<Position[]>([]);
  const [history, setHistory] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("all");
  const [closingSymbol, setClosingSymbol] = useState<string | null>(null);

  // Initial load
  useEffect(() => {
    async function load() {
      try {
        const [openRes, histRes] = await Promise.all([
          fetch(`${API_URL}/api/performance/open`),
          fetch(`${API_URL}/api/performance/history`)
        ]);
        if (openRes.ok) setOpenPositions((await openRes.json()).positions || []);
        if (histRes.ok) setHistory((await histRes.json()).positions || []);
      } catch (e) {
        console.error("Failed to load performance data", e);
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  // WebSocket for live updates
  useEffect(() => {
    const wsUrl = process.env.NEXT_PUBLIC_WS_URL ? process.env.NEXT_PUBLIC_WS_URL.replace(/:\\d+$/, ":8000") + "/api/performance/ws" : "ws://localhost:8000/api/performance/ws";
    const ws = new WebSocket(wsUrl);
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "PERFORMANCE_SNAPSHOT") {
          setOpenPositions(data.open_positions || []);
        }
      } catch (err) {}
    };
    return () => ws.close();
  }, []);

  // Filtering
  const filteredHistory = useMemo(() => {
    if (filter === "all") return history;
    const now = new Date();
    return history.filter(pos => {
      if (!pos.exit_date) return false;
      const d = new Date(pos.exit_date);
      if (filter === "week") {
        const diff = (now.getTime() - d.getTime()) / (1000 * 3600 * 24);
        return diff <= 7;
      }
      if (filter === "month") {
        const diff = (now.getTime() - d.getTime()) / (1000 * 3600 * 24);
        return diff <= 30;
      }
      return true;
    });
  }, [history, filter]);

  // Summary Metrics
  const summary = useMemo(() => {
    const closed = filteredHistory;
    const total = closed.length;
    const wins = closed.filter(p => (p.pnl || 0) > 0);
    const losses = closed.filter(p => (p.pnl || 0) <= 0);
    const winRate = total > 0 ? (wins.length / total) * 100 : 0;
    
    const totalPnl = closed.reduce((sum, p) => sum + (p.pnl || 0), 0);
    const avgProfit = wins.length > 0 ? wins.reduce((sum, p) => sum + (p.pnl || 0), 0) / wins.length : 0;
    const avgLoss = losses.length > 0 ? losses.reduce((sum, p) => sum + (p.pnl || 0), 0) / losses.length : 0;
    
    let best = 0;
    let worst = 0;
    closed.forEach(p => {
      if ((p.pnl || 0) > best) best = p.pnl!;
      if ((p.pnl || 0) < worst) worst = p.pnl!;
    });
    
    return { total, winRate, totalPnl, avgProfit, avgLoss, best, worst };
  }, [filteredHistory]);

  const handleManualClose = async (symbol: string) => {
    if (!confirm(`Are you sure you want to close position in ${symbol}?`)) return;
    setClosingSymbol(symbol);
    try {
      const res = await fetch(`${API_URL}/api/performance/close/${symbol}`, { method: 'POST' });
      const data = await res.json();
      if (data.success) {
        // Optimistically remove from open
        setOpenPositions(prev => prev.filter(p => p.symbol !== symbol));
        // Refresh history to see it
        const histRes = await fetch(`${API_URL}/api/performance/history`);
        if (histRes.ok) setHistory((await histRes.json()).positions || []);
      } else {
        alert(`Failed to close: ${data.reason}`);
      }
    } catch (e) {
      alert("Error closing position");
    } finally {
      setClosingSymbol(null);
    }
  };

  const exportCsv = () => {
    if (!filteredHistory.length) return;
    const headers = ["Symbol", "Entry Date", "Exit Date", "Direction", "Entry Price", "Exit Price", "PnL", "Exit Reason", "Days Held"];
    const rows = filteredHistory.map(p => {
      const entryD = new Date(p.entry_date);
      const exitD = p.exit_date ? new Date(p.exit_date) : new Date();
      const days = Math.round((exitD.getTime() - entryD.getTime()) / (1000 * 3600 * 24));
      return [
        p.symbol, p.entry_date, p.exit_date || "", p.direction, p.entry_price, p.exit_price || "", p.pnl || 0, p.exit_reason || "", days
      ].join(",");
    });
    
    const csvContent = "data:text/csv;charset=utf-8," + headers.join(",") + "\\n" + rows.join("\\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", "performance_history.csv");
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className="app-layout">
      {/* Copied Sidebar for standalone page */}
      <aside className="sidebar">
        <div className="sidebar-logo">StockAI</div>
        <div className="sidebar-section">Main</div>
        <a href="/" className="sidebar-item" style={{ textDecoration: "none", display: "block" }}>
          ← Back to Scanner
        </a>
        <div className="sidebar-section">Portfolio</div>
        <button className="sidebar-item active">
          Performance Tracker
        </button>
      </aside>
      
      <main className="main-content">
        <div className="page-header" style={{ marginBottom: 24 }}>
          <div>
            <h1>Performance Tracking</h1>
            <p>Live PnL & Automated Trade Logging</p>
          </div>
        </div>

        {/* SECTION 1: OPEN POSITIONS */}
        <section className="dashboard-band" style={{ marginBottom: 40 }}>
          <div className="section-heading">
            <div>
              <p className="eyebrow">Active Exposure</p>
              <h2>Open Positions ({openPositions.length})</h2>
            </div>
            <div className="micro-copy animate-pulse" style={{ color: "var(--green)" }}>● Live updating</div>
          </div>
          
          <div className="terminal-card" style={{ padding: 0, overflowX: "auto" }}>
            {loading && !openPositions.length ? (
              <div className="p-4 text-center">Loading open positions...</div>
            ) : openPositions.length === 0 ? (
              <div className="empty-state">No open positions right now.</div>
            ) : (
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50 border-b border-gray-200 text-gray-500 uppercase text-xs">
                  <tr>
                    <th className="p-3 font-medium">Symbol / Bias</th>
                    <th className="p-3 font-medium">Entry</th>
                    <th className="p-3 font-medium">Live Price</th>
                    <th className="p-3 font-medium text-right">Unrealized PnL</th>
                    <th className="p-3 font-medium">Target / SL</th>
                    <th className="p-3 font-medium">Days Held</th>
                    <th className="p-3 font-medium text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 font-mono">
                  {openPositions.map((pos) => {
                    const days = Math.round((new Date().getTime() - new Date(pos.entry_date).getTime()) / (1000 * 3600 * 24));
                    const pnl = pos.unrealized_pnl || 0;
                    const pnlPct = pos.unrealized_pnl_pct || 0;
                    const isProfit = pnl >= 0;
                    return (
                      <tr key={pos.symbol} className="hover:bg-gray-50">
                        <td className="p-3">
                          <div className="font-bold">
                            {pos.symbol}
                            {pos.order_type === "live" ? (
                              <span className="ml-2 text-[10px] bg-green-500 text-white px-1.5 py-0.5 rounded-full">🟢 Live</span>
                            ) : pos.order_type === "paper" ? (
                              <span className="ml-2 text-[10px] bg-gray-300 text-gray-800 px-1.5 py-0.5 rounded-full">📄 Paper</span>
                            ) : null}
                          </div>
                          <span className={`text-xs px-1.5 py-0.5 rounded-sm ${pos.direction === "bullish" ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>
                            {pos.direction.toUpperCase()}
                          </span>
                        </td>
                        <td className="p-3">
                          <div>₹{fmt(pos.actual_entry_price || pos.entry_price, 2)}</div>
                          <div className="text-xs text-gray-400">
                            {pos.actual_entry_price ? `(Signal: ₹${fmt(pos.entry_price, 2)})` : new Date(pos.entry_date).toLocaleDateString()}
                          </div>
                          {pos.slippage != null && (
                            <div className={`text-[10px] ${pos.slippage > 0 ? "text-red-500" : "text-green-500"}`}>
                              Slippage: {pos.slippage > 0 ? "+" : ""}₹{fmt(pos.slippage, 2)}
                            </div>
                          )}
                        </td>
                        <td className="p-3 font-semibold">{pos.current_price ? `₹${fmt(pos.current_price, 2)}` : "-"}</td>
                        <td className="p-3 text-right">
                          <div className={isProfit ? "text-green-600" : "text-red-600"}>
                            {isProfit ? "+" : ""}₹{fmt(pnl, 2)}
                          </div>
                          <div className={`text-xs ${isProfit ? "text-green-500" : "text-red-500"}`}>
                            {isProfit ? "+" : ""}{fmtPct(pnlPct, 2)}
                          </div>
                        </td>
                        <td className="p-3 text-xs">
                          <div>TGT: <span className="text-green-600">₹{pos.target1 || "-"}</span></div>
                          <div>SL: <span className="text-red-600">₹{pos.stop_loss || "-"}</span></div>
                        </td>
                        <td className="p-3">{days} days</td>
                        <td className="p-3 text-right">
                          <button 
                            className="text-xs bg-gray-200 hover:bg-gray-300 text-gray-800 px-3 py-1 rounded transition-colors"
                            onClick={() => handleManualClose(pos.symbol)}
                            disabled={closingSymbol === pos.symbol}
                          >
                            {closingSymbol === pos.symbol ? "Closing..." : "Close"}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </section>

        {/* SECTION 2: CLOSED POSITIONS HISTORY */}
        <section className="dashboard-band">
          <div className="section-heading" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
            <div>
              <p className="eyebrow">Realized PnL</p>
              <h2>Closed Positions History</h2>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <select className="search-box" style={{ width: "auto", padding: "6px 12px", border: "1px solid #ccc", borderRadius: 4 }} value={filter} onChange={e => setFilter(e.target.value)}>
                <option value="all">All Time</option>
                <option value="month">This Month</option>
                <option value="week">This Week</option>
              </select>
              <button className="terminal-button compact" onClick={exportCsv}>Export CSV</button>
            </div>
          </div>

          <div className="stats-row" style={{ marginBottom: 20 }}>
            <div className="stat-card"><span>Total Trades</span><strong>{summary.total}</strong></div>
            <div className="stat-card"><span>Win Rate</span><strong style={{ color: summary.winRate > 50 ? "var(--green)" : "var(--red)" }}>{fmt(summary.winRate, 1)}%</strong></div>
            <div className="stat-card">
              <span>Total PnL</span>
              <strong style={{ color: summary.totalPnl >= 0 ? "var(--green)" : "var(--red)" }}>
                {summary.totalPnl >= 0 ? "+" : ""}₹{fmt(summary.totalPnl, 2)}
              </strong>
            </div>
            <div className="stat-card"><span>Avg Profit</span><strong style={{ color: "var(--green)" }}>₹{fmt(summary.avgProfit, 2)}</strong></div>
            <div className="stat-card"><span>Avg Loss</span><strong style={{ color: "var(--red)" }}>₹{fmt(summary.avgLoss, 2)}</strong></div>
            <div className="stat-card"><span>Best / Worst</span><strong>₹{fmt(summary.best, 0)} / ₹{fmt(summary.worst, 0)}</strong></div>
          </div>

          <div className="terminal-card" style={{ padding: 0, overflowX: "auto" }}>
            {loading && !history.length ? (
              <div className="p-4 text-center">Loading history...</div>
            ) : filteredHistory.length === 0 ? (
              <div className="empty-state">No closed positions matching criteria.</div>
            ) : (
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50 border-b border-gray-200 text-gray-500 uppercase text-xs">
                  <tr>
                    <th className="p-3 font-medium">Symbol</th>
                    <th className="p-3 font-medium">Opened / Closed</th>
                    <th className="p-3 font-medium">Entry / Exit Price</th>
                    <th className="p-3 font-medium text-right">Realized PnL</th>
                    <th className="p-3 font-medium">Reason</th>
                    <th className="p-3 font-medium">Held</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 font-mono">
                  {filteredHistory.map((pos, idx) => {
                    const days = Math.round((new Date(pos.exit_date!).getTime() - new Date(pos.entry_date).getTime()) / (1000 * 3600 * 24));
                    const pnl = pos.pnl || 0;
                    const isProfit = pnl >= 0;
                    return (
                      <tr key={`${pos.symbol}-${idx}`} className="hover:bg-gray-50">
                        <td className="p-3">
                          <div className="font-bold">
                            {pos.symbol}
                            {pos.order_type === "live" ? (
                              <span className="ml-2 text-[10px] bg-green-500 text-white px-1.5 py-0.5 rounded-full" title="Live Executed">🟢 Live</span>
                            ) : pos.order_type === "paper" ? (
                              <span className="ml-2 text-[10px] bg-gray-300 text-gray-800 px-1.5 py-0.5 rounded-full" title="Paper Traded">📄 Paper</span>
                            ) : null}
                          </div>
                          <span className={`text-[10px] uppercase text-gray-500`}>{pos.direction}</span>
                        </td>
                        <td className="p-3 text-xs text-gray-600">
                          <div>{new Date(pos.entry_date).toLocaleDateString()}</div>
                          <div>{new Date(pos.exit_date!).toLocaleDateString()}</div>
                        </td>
                        <td className="p-3">
                          <div>IN: ₹{fmt(pos.actual_entry_price || pos.entry_price, 2)}</div>
                          <div>OUT: ₹{fmt(pos.exit_price, 2)}</div>
                          {pos.slippage != null && (
                            <div className={`text-[10px] ${pos.slippage > 0 ? "text-red-500" : "text-green-500"}`}>
                              Slip: {pos.slippage > 0 ? "+" : ""}₹{fmt(pos.slippage, 2)}
                            </div>
                          )}
                        </td>
                        <td className="p-3 text-right">
                          <div className={`font-bold ${isProfit ? "text-green-600" : "text-red-600"}`}>
                            {isProfit ? "+" : ""}₹{fmt(pnl, 2)}
                          </div>
                        </td>
                        <td className="p-3">
                          <span className={`text-xs px-2 py-1 rounded-full ${isProfit ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>
                            {pos.exit_reason || humanize(pos.status)}
                          </span>
                        </td>
                        <td className="p-3 text-gray-500">{days}d</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
