"use client";
import { useCallback, useEffect, useState } from "react";
import { API_URL, humanize, fmt, fmtPct } from "./lib/market";

const TABS = [
  { id: "dashboard", label: "🏠 Dashboard", api: "/api/market/hot-picks" },
  { id: "hot-picks", label: "🔥 Hot Picks", api: "/api/market/hot-picks" },
  { id: "watchlist", label: "👁 Watchlist", api: "/api/market/candidates" },
  { id: "base-radar", label: "📊 Base Formation", api: "/api/market/pre-breakout" },
  { id: "momentum", label: "⚡ Momentum", api: "/api/market/fast-movers" },
  { id: "breakouts", label: "🚀 Breakouts", api: "/api/scanner/breakouts" },
  { id: "bearish", label: "🔻 Bearish Risk", api: "/api/scanner/bearish-risk" },
  { id: "fast-movers", label: "💨 Fast Movers", api: "/api/market/fast-movers" },
  { id: "avoid", label: "🚫 Avoid", api: "/api/market/avoid" },
];

function StockCard({ item }: { item: any }) {
  const change = item.change_pct ?? item.changePct ?? 0;
  const price = item.currentPrice ?? item.current_price ?? item.price ?? 0;
  const action = item.action ?? item.effectiveAction ?? item.display_action ?? item.recommended_action ?? "WATCH";
  const actionClass = action === "BUY" || action === "REENTRY_BUY" ? "buy" : action === "SELL" ? "sell" : "watch";
  const direction = item.direction || "neutral";
  return (
    <div className="stock-card">
      <div className="stock-card-top">
        <h3>{item.symbol}</h3>
        <span className={`badge ${direction}`}>{humanize(direction)}</span>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="stock-price">₹{fmt(price)}</span>
        <span className={`stock-change ${change >= 0 ? "up" : "down"}`}>{fmtPct(change)}</span>
      </div>
      <div className="stock-meta">
        <span className={`tag ${actionClass}`}>{action}</span>
        {(item.risk_reward || item.rr) ? <span className="tag">RR 1:{fmt(item.risk_reward ?? item.rr, 1)}</span> : null}
        {item.confidence ? <span className="tag">{fmt(item.confidence ?? item.score, 0)}%</span> : null}
        {item.relative_volume ? <span className="tag">{fmt(item.relative_volume, 1)}x vol</span> : null}
        {item.entry_label && <span className="tag">{humanize(item.entry_label)}</span>}
      </div>
      <p className="stock-reason">
        {item.reason || item.signal_summary || item.setup_label || item.whyInteresting || item.missed_reason || ""}
      </p>
      {item.chase_warnings?.length ? (
        <p className="stock-reason" style={{ color: "var(--orange)" }}>⚠️ {item.chase_warnings[0]}</p>
      ) : null}
      <div className="stock-targets">
        <div><span>Entry</span><strong>₹{fmt(item.entry_trigger ?? item.entryTrigger ?? item.breakoutTrigger ?? price)}</strong></div>
        <div><span>Target</span><strong>₹{fmt(item.target_1 ?? item.target_price ?? item.target ?? item.keyResistance)}</strong></div>
        <div><span>Stop</span><strong>₹{fmt(item.stop_loss ?? item.stoploss ?? item.invalidation ?? item.invalidationLevel)}</strong></div>
      </div>
    </div>
  );
}

function extractItems(tab: string, data: any): any[] {
  if (!data) return [];
  if (tab === "dashboard" || tab === "hot-picks") return data.hotPicks || data.top_opportunities || [];
  if (tab === "watchlist") return data.candidates || [];
  if (tab === "base-radar") return data.pre_breakout_setups || data.pattern_forming_setups || [];
  if (tab === "momentum" || tab === "fast-movers") return data.fast_movers || data.results || [];
  if (tab === "breakouts") return data.results || [];
  if (tab === "bearish") return data.results || [];
  if (tab === "avoid") return data.avoid_risky || [];
  return [];
}

export default function Page() {
  const [tab, setTab] = useState("dashboard");
  const [data, setData] = useState<any>(null);
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (tabId: string, force = false) => {
    setLoading(true);
    const tabConfig = TABS.find(t => t.id === tabId) || TABS[0];
    try {
      const r = await fetch(`${API_URL}${tabConfig.api}?force_refresh=${force}`, { cache: "no-store" });
      if (r.ok) {
        const json = await r.json();
        setData(json);
        if (tabId === "dashboard") setSummary(json);
      }
    } catch {}
    setLoading(false);
  }, []);

  useEffect(() => { load(tab); }, [tab, load]);
  useEffect(() => { load("dashboard"); const i = setInterval(() => load(tab), 60000); return () => clearInterval(i); }, []);

  const items = extractItems(tab, data);
  const mood = summary?.marketMood || data?.marketMood || "loading";
  const stats = summary?.summary || data?.summary || {};

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-logo">StockAI</div>
        <div className="sidebar-section">Scanner</div>
        {TABS.map(t => (
          <button key={t.id} className={`sidebar-item ${tab === t.id ? "active" : ""}`} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </aside>
      <main className="main-content">
        <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
          <div>
            <h1>{TABS.find(t => t.id === tab)?.label}</h1>
            <p>Updated: {data?.generated_at || data?.lastUpdated ? new Date(data.generated_at || data.lastUpdated).toLocaleString() : "..."}</p>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span className={`mood-badge ${mood}`}>{humanize(mood)}</span>
            <button className="refresh-btn" onClick={() => load(tab, true)} disabled={loading}>{loading ? "..." : "Refresh"}</button>
          </div>
        </div>

        <div className="stats-row">
          <div className="stat-card"><span>Scanned</span><strong>{stats.totalScanned ?? stats.total_scanned_universe ?? "..."}</strong></div>
          <div className="stat-card"><span>Hot Picks</span><strong>{stats.highConfidence ?? stats.high_priority ?? 0}</strong></div>
          <div className="stat-card"><span>Breakouts</span><strong>{stats.breakouts ?? stats.breakout_count ?? 0}</strong></div>
          <div className="stat-card"><span>Bullish</span><strong style={{ color: "var(--green)" }}>{stats.bullish ?? stats.bullish_setups ?? 0}</strong></div>
          <div className="stat-card"><span>Bearish</span><strong style={{ color: "var(--red)" }}>{stats.bearish ?? stats.bearish_risk_count ?? 0}</strong></div>
          <div className="stat-card"><span>This Tab</span><strong>{items.length}</strong></div>
        </div>

        <h2 className="section-title">{TABS.find(t => t.id === tab)?.label} ({items.length} stocks)</h2>
        {items.length ? (
          <div className="stock-grid">
            {items.map((item: any, i: number) => <StockCard key={item.symbol || i} item={item} />)}
          </div>
        ) : (
          <p className="empty-msg">{loading ? "Scanning market..." : "No stocks found in this category right now."}</p>
        )}
      </main>
    </div>
  );
}
