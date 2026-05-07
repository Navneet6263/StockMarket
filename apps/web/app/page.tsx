"use client";
import { useCallback, useEffect, useState } from "react";
import { API_URL, humanize, fmt, fmtPct } from "./lib/market";

const TABS = [
  { id: "dashboard", label: "🏠 Dashboard", api: "/api/market/hot-picks" },
  { id: "hot-picks", label: "🔥 Hot Picks", api: "/api/market/hot-picks" },
  { id: "strict-options", label: "Strict Options", api: "/api/market/hot-picks" },
  { id: "watchlist", label: "👁 Watchlist", api: "/api/market/candidates" },
  { id: "base-radar", label: "📊 Base Formation", api: "/api/market/pre-breakout" },
  { id: "momentum", label: "⚡ Momentum", api: "/api/market/fast-movers" },
  { id: "breakouts", label: "🚀 Breakouts", api: "/api/scanner/breakouts" },
  { id: "bearish", label: "🔻 Bearish Risk", api: "/api/scanner/bearish-risk" },
  { id: "fast-movers", label: "💨 Fast Movers", api: "/api/market/fast-movers" },
  { id: "avoid", label: "🚫 Avoid", api: "/api/market/avoid" },
];

function StockCard({ item, onSelect }: { item: any; onSelect: (s: string) => void }) {
  if (item.entryRule && item.instrumentRule) {
    return (
      <div className="stock-card" onClick={() => onSelect(item.symbol)} style={{ cursor: "pointer", borderColor: item.status === "STRICT_READY" ? "var(--green)" : "var(--orange)" }}>
        <div className="stock-card-top">
          <h3>{item.symbol}</h3>
          <span className={`badge ${item.side === "PE" ? "bearish" : "bullish"}`}>{item.actionLabel}</span>
        </div>
        <div className="stock-meta">
          <span className="tag watch">{humanize(item.status)}</span>
          <span className="tag">{fmt(item.confidence, 0)}%</span>
          <span className="tag">{item.bucket}</span>
        </div>
        <p className="stock-reason">{item.entryRule}</p>
        <p className="stock-reason" style={{ color: "var(--orange)" }}>{item.expiryRule}</p>
        <div className="stock-targets">
          <div><span>Underlying</span><strong>{item.underlyingPrice}</strong></div>
          <div><span>Trigger</span><strong>{item.underlyingTrigger}</strong></div>
          <div><span>Invalid</span><strong>{item.underlyingInvalidation}</strong></div>
        </div>
        <p className="stock-reason">{item.riskRule}</p>
      </div>
    );
  }

  const change = item.change_pct ?? item.changePct ?? 0;
  const price = item.currentPrice ?? item.current_price ?? item.price ?? 0;
  const action = item.action ?? item.effectiveAction ?? item.display_action ?? item.recommended_action ?? "WATCH";
  const actionClass = action === "BUY" || action === "REENTRY_BUY" ? "buy" : action === "SELL" ? "sell" : "watch";
  const direction = item.direction || "neutral";
  return (
    <div className="stock-card" onClick={() => onSelect(item.symbol)} style={{ cursor: "pointer" }}>
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
        {item.entry_quality ? <span className="tag">{humanize(item.entry_quality)} entry</span> : null}
        {item.seller_pressure ? <span className="tag">{humanize(item.seller_pressure)} sellers</span> : null}
        {item.profit_booking_risk ? <span className="tag">{humanize(item.profit_booking_risk)} booking</span> : null}
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

function StockDetail({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const [detail, setDetail] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    setLoading(true);
    fetch(`${API_URL}/api/stocks/${symbol}`, { cache: "no-store" })
      .then(r => r.ok ? r.json() : null)
      .then(d => { setDetail(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, [symbol]);

  if (loading) return <div className="detail-panel">Loading {symbol}...</div>;
  if (!detail) return <div className="detail-panel">Could not load {symbol}</div>;

  const s = detail.prediction || {};
  const q = detail.quote || {};
  return (
    <div className="detail-panel">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2>{detail.company_context?.company_name || symbol}</h2>
        <button className="refresh-btn" onClick={onClose}>✕ Close</button>
      </div>
      <div className="stats-row" style={{ marginTop: 12 }}>
        <div className="stat-card"><span>Price</span><strong>₹{fmt(q.price)}</strong></div>
        <div className="stat-card"><span>Change</span><strong style={{ color: (q.change_percent ?? 0) >= 0 ? "var(--green)" : "var(--red)" }}>{fmtPct(q.change_percent)}</strong></div>
        <div className="stat-card"><span>Direction</span><strong>{humanize(s.direction)}</strong></div>
        <div className="stat-card"><span>Confidence</span><strong>{fmt(s.confidence, 0)}%</strong></div>
        <div className="stat-card"><span>Risk</span><strong>{humanize(s.risk_level)}</strong></div>
        <div className="stat-card"><span>RR</span><strong>1:{fmt(s.risk_reward, 1)}</strong></div>
        <div className="stat-card"><span>Entry Quality</span><strong>{humanize(s.entry_quality || "watch")}</strong></div>
        <div className="stat-card"><span>Seller Pressure</span><strong>{humanize(s.seller_pressure || "unknown")}</strong></div>
      </div>
      <div className="stock-targets" style={{ marginTop: 12 }}>
        <div><span>Entry</span><strong>₹{fmt(s.entry_trigger ?? s.current_price)}</strong></div>
        <div><span>Target 1</span><strong>₹{fmt(s.target_1 ?? s.target_price)}</strong></div>
        <div><span>Stop Loss</span><strong>₹{fmt(s.stop_loss ?? s.invalidation)}</strong></div>
      </div>
      {s.reasons?.length ? (
        <div style={{ marginTop: 12 }}>
          <strong>Why:</strong>
          <ul style={{ margin: "6px 0", paddingLeft: 18, color: "var(--muted)", fontSize: 13 }}>
            {s.reasons.slice(0, 4).map((r: string, i: number) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      ) : null}
      {s.risk_factors?.length ? (
        <div style={{ marginTop: 8 }}>
          <strong>Risks:</strong>
          <ul style={{ margin: "6px 0", paddingLeft: 18, color: "var(--red)", fontSize: 13 }}>
            {s.risk_factors.slice(0, 3).map((r: string, i: number) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      ) : null}
      {s.reentry_plan ? (
        <div style={{ marginTop: 8, color: "var(--muted)", fontSize: 13 }}>
          <strong>Best action:</strong> {humanize(s.best_action || s.recommended_action)}. {s.reentry_plan}
        </div>
      ) : null}
    </div>
  );
}

function extractItems(tab: string, data: any): any[] {
  if (!data) return [];
  if (tab === "dashboard" || tab === "hot-picks") return data.hotPicks || data.top_opportunities || [];
  if (tab === "strict-options") return data.strictOptions?.radar || [];
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
  const [search, setSearch] = useState("");
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);

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

  function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    const sym = search.trim().toUpperCase();
    if (sym) setSelectedSymbol(sym);
  }

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-logo">StockAI</div>
        <form onSubmit={handleSearch} style={{ padding: "0 4px", marginBottom: 12 }}>
          <input
            className="search-box"
            placeholder="Search stock..."
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </form>
        <div className="sidebar-section">Scanner</div>
        {TABS.map(t => (
          <button key={t.id} className={`sidebar-item ${tab === t.id ? "active" : ""}`} onClick={() => { setTab(t.id); setSelectedSymbol(null); }}>
            {t.label}
          </button>
        ))}
      </aside>
      <main className="main-content">
        <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
          <div>
            <h1>{selectedSymbol || TABS.find(t => t.id === tab)?.label}</h1>
            <p>Updated: {data?.generated_at || data?.lastUpdated ? new Date(data.generated_at || data.lastUpdated).toLocaleString() : "..."}</p>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span className={`mood-badge ${mood}`}>{humanize(mood)}</span>
            <button className="refresh-btn" onClick={() => load(tab, true)} disabled={loading}>{loading ? "..." : "Refresh"}</button>
          </div>
        </div>

        {selectedSymbol ? (
          <StockDetail symbol={selectedSymbol} onClose={() => setSelectedSymbol(null)} />
        ) : (
          <>
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
                {items.map((item: any, i: number) => <StockCard key={item.symbol || i} item={item} onSelect={setSelectedSymbol} />)}
              </div>
            ) : (
              <p className="empty-msg">{loading ? "Scanning market..." : "No stocks found in this category right now."}</p>
            )}
          </>
        )}
      </main>
    </div>
  );
}
