"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL, humanize, fmt, fmtPct } from "./lib/market";
import TradingChart from "./components/TradingChart";

const TABS = [
  { id: "dashboard", label: "Dashboard", api: "/api/market/hot-picks" },
  { id: "live-action", label: "🔴 Live Action", api: "/api/market/live-entries" },
  { id: "hot-picks", label: "Hot Picks", api: "/api/market/hot-picks" },
  { id: "pbs", label: "Profit Booking (PBS)", api: "/api/market/hot-picks" },
  { id: "watchlist", label: "Watchlist", api: "/api/market/hot-picks" },
  { id: "base-radar", label: "Base Formation", api: "/api/market/hot-picks" },
  { id: "momentum", label: "Momentum", api: "/api/market/hot-picks" },
  { id: "traps", label: "Traps & Risks", api: "/api/market/traps" },
  { id: "options", label: "Options Chain", api: "/api/market/options-analysis" },
];

function priceText(value: any) {
  if (typeof value === "string" && value.trim()) return value.replace(/INR/g, "₹");
  const formatted = fmt(value);
  return formatted === "-" ? "-" : `₹${formatted}`;
}

function livePriceFromTick(tick: any) {
  return tick?.price ?? tick?.ltp ?? tick?.data?.price ?? tick?.data?.ltp;
}

function overlayLiveTick(item: any, tick: any) {
  const livePrice = livePriceFromTick(tick);
  if (!livePrice) return item;
  const previousClose = tick?.previous_close ?? tick?.data?.previous_close;
  const changePct = previousClose ? ((Number(livePrice) - Number(previousClose)) / Number(previousClose)) * 100 : undefined;
  return {
    ...item,
    current_price: Number(livePrice),
    price: Number(livePrice),
    currentPrice: Number(livePrice),
    change_pct: changePct ?? item.change_pct,
    live_source: tick?.source || "angelone_ws",
    live_ts: tick?.server_received_at || tick?.ts,
  };
}

function aiActionClass(action: any) {
  const text = String(action || "").toUpperCase();
  if (text.includes("BUY")) return "buy";
  if (text.includes("AVOID") || text.includes("SELL")) return "sell";
  return "watch";
}

function demandTone(status: any, trapRisk: any) {
  const trap = String(trapRisk || "").toLowerCase();
  const text = String(status || "").toLowerCase();
  if (trap === "high" || trap === "very_high" || text.includes("trap") || text.includes("supply")) return "sell";
  if (text.includes("demand") || text.includes("accumulation")) return "buy";
  return "watch";
}

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

  const change = item.change_pct ?? item.changePct ?? item.raw?.change_pct ?? 0;
  const price = item.currentPrice ?? item.current_price ?? item.price ?? item.raw?.current_price ?? item.raw?.price ?? 0;
  const action = item.action ?? item.effectiveAction ?? item.display_action ?? item.recommended_action ?? "WATCH";
  const actionClass = action === "BUY" || action === "REENTRY_BUY" ? "buy" : action === "SELL" ? "sell" : "watch";
  const direction = item.direction || "neutral";
  const chartPattern = item.chartPattern ?? item.setupType ?? item.setup_type ?? item.raw?.setup_type;
  const patternLabels = item.patternLabels ?? item.pattern_labels ?? item.raw?.pattern_labels ?? [];
  const entryLabel = item.entryLabel ?? item.entry_label ?? (item.entryMissed || item.entry_missed ? "Retest" : "Entry");
  const entryValue = item.entryZone ?? item.entry_zone_text ?? item.entry_trigger ?? item.entryTrigger ?? item.breakoutTrigger ?? price;
  const aiReason = item.aiReason || "";
  const aiTradePlan = item.aiTradePlan || "";
  const aiRisks = item.aiRisks || [];
  const aiActionLabel = item.geminiAction || item.aiAction;
  const aiScore = item.aiScore ?? item.aiConviction;
  const hasAi = Boolean(item.aiAvailable && (aiReason || aiTradePlan || aiScore));
  const hasAiStatus = Boolean(item.aiStatus || item.aiAvailable === false);
  const demand = item.demandSupply || item.demand_supply || item.raw?.demand_supply || {};
  const demandStatus = item.demandStatus || item.demand_status || demand.status;
  const demandScore = item.demandScore ?? item.demand_score ?? demand.demandScore;
  const supplyScore = item.supplyScore ?? item.supply_score ?? demand.supplyScore;
  const trapRisk = item.trapRisk || item.trap_risk || demand.trapRisk;
  const trapRiskScore = item.trapRiskScore ?? item.trap_risk_score ?? demand.trapRiskScore;
  const smartMoneyRead = item.smartMoneyRead || demand.smartMoneyRead;
  const fallbackReason = item.reason || item.signal_summary || item.setup_label || item.whyInteresting || item.missed_reason || "";
  const timeHorizon = item.timeHorizon || item.pre_breakout_timeframe || item.timeframe || item.raw?.pre_breakout_timeframe || "";
  const isHighTrap = trapRisk === "high" || item.trap_risk === "high" || item.bull_trap?.bull_trap_detected || (item.trap_warnings && item.trap_warnings.length > 0);

  return (
    <div className="stock-card" onClick={() => onSelect(item.symbol)} style={{ cursor: "pointer", borderColor: isHighTrap ? "var(--red)" : "var(--border)" }}>
      {isHighTrap ? (
        <div className="trap-alert">
          <div className="trap-kicker">🚨 AVOID - INSTITUTIONAL TRAP</div>
          <div className="trap-desc">
            {item.bull_trap?.bull_trap_detected ? "Fake Breakout Detected. Price broke resistance but failed to sustain." : 
             item.trap_warnings?.[0] || "High institutional selling pressure detected."}
          </div>
          <div className="stock-meta" style={{ marginTop: 4 }}>
            <span className="tag sell" style={{ fontSize: 13, padding: '6px 12px' }}>🔴 DO NOT BUY / SELL TURANT</span>
          </div>
        </div>
      ) : null}
      <div className="stock-card-top">
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <h3>{item.symbol}</h3>
          {item.label && <span className="tag" style={{ background: "var(--red, #ef4444)", color: "#fff", fontWeight: "bold" }}>{item.label}</span>}
        </div>
        <span className={`badge ${isHighTrap ? 'bearish' : direction}`}>{isHighTrap ? 'AVOID' : humanize(direction)}</span>
      </div>
      {chartPattern ? <div className="stock-reason" style={{ marginTop: 4 }}>{humanize(chartPattern)}</div> : null}
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
        {timeHorizon ? <span className="tag" style={{ color: "var(--yellow)", fontWeight: "bold" }}>⏳ Runs in: {timeHorizon}</span> : null}
        {patternLabels.slice(0, 2).map((label: string) => <span key={label} className="tag">{humanize(label)}</span>)}
      </div>
      {hasAi ? (
        <div className="ai-insight">
          <div className="ai-insight-top">
            <span className="ai-kicker">Gemini analysis</span>
            <div className="stock-meta">
              {aiActionLabel ? <span className={`tag ${aiActionClass(aiActionLabel)}`}>{humanize(aiActionLabel)}</span> : null}
              {aiScore ? <span className="tag">AI {fmt(aiScore, 0)}%</span> : null}
              {item.aiTimeframe ? <span className="tag">{item.aiTimeframe}</span> : null}
            </div>
          </div>
          {aiReason ? <p className="stock-reason">{aiReason}</p> : null}
          {aiTradePlan ? <p className="ai-plan">{aiTradePlan}</p> : null}
          {aiRisks.length ? (
            <div className="ai-risk-list">
              {aiRisks.slice(0, 2).map((risk: string, index: number) => <span key={`${item.symbol}-ai-risk-${index}`}>{risk}</span>)}
            </div>
          ) : null}
        </div>
      ) : (
        <>
          <p className="stock-reason">{fallbackReason}</p>
          {hasAiStatus ? (
            <div className="stock-meta">
              <span className="tag watch">Gemini: {humanize(item.aiStatus || "unavailable")}</span>
            </div>
          ) : null}
        </>
      )}
      {(demandStatus || smartMoneyRead) ? (
        <div className="demand-panel">
          <div className="stock-meta">
            {demandStatus ? <span className={`tag ${demandTone(demandStatus, trapRisk)}`}>{humanize(demandStatus)}</span> : null}
            {demandScore != null ? <span className="tag">Demand {fmt(demandScore, 0)}</span> : null}
            {supplyScore != null ? <span className="tag">Supply {fmt(supplyScore, 0)}</span> : null}
            {trapRisk ? <span className={`tag ${demandTone(demandStatus, trapRisk)}`}>Trap {humanize(trapRisk)} {trapRiskScore != null ? fmt(trapRiskScore, 0) : ""}</span> : null}
          </div>
          {smartMoneyRead ? <p className="stock-reason">{smartMoneyRead}</p> : null}
        </div>
      ) : null}
      
      {item.bull_trap && item.bull_trap.bull_trap_detected && !isHighTrap ? (
        <p className="stock-reason" style={{ color: "var(--red)", fontWeight: "bold" }}>⚠️ Bull Trap Warning</p>
      ) : null}

      {item.newsHeadlines?.length ? (
        <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
          📰 {item.newsHeadlines[0]}
        </div>
      ) : null}
      {item.aiWarnings?.length ? (
        <p className="stock-reason" style={{ color: "var(--orange)" }}>⚠️ {item.aiWarnings[0]}</p>
      ) : item.chase_warnings?.length ? (
        <p className="stock-reason" style={{ color: "var(--orange)" }}>⚠️ {item.chase_warnings[0]}</p>
      ) : null}
      {item.newsSentiment && item.newsSentiment !== "unknown" ? (
        <div className="stock-meta" style={{ marginTop: 4 }}>
          <span className={`tag ${item.newsSentiment === "bullish" ? "buy" : item.newsSentiment === "bearish" ? "sell" : "watch"}`}>News: {item.newsSentiment}</span>
          {item.insiderSignal && item.insiderSignal !== "unknown" && item.insiderSignal !== "neutral" ? <span className="tag">{item.insiderSignal === "insider_buying" ? "🟢 Insider Buy" : "🔴 Insider Sell"}</span> : null}
          {item.analystConsensus && item.analystConsensus !== "unknown" ? <span className="tag">Analyst: {item.analystConsensus}</span> : null}
        </div>
      ) : null}
      <div className="stock-targets">
        <div><span>{entryLabel}</span><strong>{priceText(entryValue)}</strong></div>
        <div><span>Target</span><strong>{priceText(item.target_1 ?? item.target_price ?? item.target ?? item.keyResistance)}</strong></div>
        <div><span>Stop</span><strong>{priceText(item.stop_loss ?? item.stoploss ?? item.invalidation ?? item.invalidationLevel)}</strong></div>
      </div>
    </div>
  );
}

function StockDetail({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const [detail, setDetail] = useState<any>(null);
  const [chartData, setChartData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  
  useEffect(() => {
    setLoading(true);
    let isMounted = true;
    Promise.all([
      fetch(`${API_URL}/api/stocks/${symbol}`, { cache: "no-store" }).then(r => r.ok ? r.json() : null),
      fetch(`${API_URL}/api/stocks/${symbol}/history?period=2y`, { cache: "no-store" }).then(r => r.ok ? r.json() : null)
    ])
      .then(([d, c]) => { 
        if (!isMounted) return;
        setDetail(d); 
        if (c) setChartData(c);
        setLoading(false); 
      })
      .catch(() => {
        if (isMounted) setLoading(false);
      });

    // Auto-refresh live price every 15 seconds
    const interval = setInterval(() => {
      fetch(`${API_URL}/api/stocks/${symbol}/live`, { cache: "no-store" })
        .then(r => r.ok ? r.json() : null)
        .then(liveData => {
          if (!isMounted || !liveData) return;
          setDetail((prev: any) => {
            if (!prev) return prev;
            return {
              ...prev,
              quote: {
                ...prev.quote,
                price: liveData.price,
                change: liveData.change,
                change_percent: liveData.change_percent,
                timestamp: liveData.timestamp,
              },
            };
          });
        })
        .catch(() => {});
    }, 15000);

    return () => {
      isMounted = false;
      clearInterval(interval);
    };
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
      {/* ── Trade Levels ─────────────────────────────────────── */}
      {/* Priority: safe_entry_price (pullback zone) → entry_price → entry_trigger */}
      <div className="stock-targets" style={{ marginTop: 12 }}>
        <div><span>Entry</span><strong>₹{fmt(
          s.safe_entry_price ?? s.entry_price ?? s.entry_trigger ?? s.current_price
        )}</strong></div>
        <div><span>Target 1</span><strong>₹{fmt(
          s.new_target ?? s.target_1 ?? s.target_price
        )}</strong></div>
        <div><span>Stop Loss</span><strong>₹{fmt(
          s.invalidation_level ?? s.stop_loss ?? s.invalidation
        )}</strong></div>
      </div>
      
      {chartData ? (
        <TradingChart symbol={symbol} data={chartData} prediction={s} liveQuote={q} />
      ) : null}

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

function OptionsDashboard({ data }: { data: any }) {
  if (!data) return <div className="empty-msg">No options data available.</div>;
  if (data.error) return <div className="empty-msg">Error: {data.error}</div>;

  const { symbol, spot_price, pcr, max_pain, call_oi, put_oi, atm_strike, support, resistance, signal, records } = data;
  
  return (
    <div className="options-dashboard">
      <div className="stats-row">
        <div className="stat-card"><span>Spot Price ({symbol})</span><strong>₹{fmt(spot_price)}</strong></div>
        <div className="stat-card"><span>PCR</span><strong style={{ color: pcr >= 1 ? "var(--green)" : "var(--red)" }}>{pcr?.toFixed(2)}</strong></div>
        <div className="stat-card"><span>Max Pain</span><strong>{max_pain}</strong></div>
        <div className="stat-card"><span>Options Signal</span><strong style={{ color: signal?.direction === "bullish" ? "var(--green)" : signal?.direction === "bearish" ? "var(--red)" : "var(--orange)" }}>{humanize(signal?.direction)}</strong></div>
      </div>
      
      <div className="demand-panel">
        <h3 style={{ fontSize: 16, color: '#fff', marginBottom: 8 }}>Key Levels (OI Built-up)</h3>
        <div style={{ display: "flex", gap: 24 }}>
          <div>
            <span style={{ color: "var(--green)", fontSize: 12 }}>MAJOR SUPPORT (Put OI)</span>
            <strong style={{ display: 'block', fontSize: 20, color: '#fff' }}>{support?.level}</strong>
            <span style={{ fontSize: 12, color: 'var(--muted)' }}>{fmt(support?.oi)} contracts</span>
          </div>
          <div>
            <span style={{ color: "var(--red)", fontSize: 12 }}>MAJOR RESISTANCE (Call OI)</span>
            <strong style={{ display: 'block', fontSize: 20, color: '#fff' }}>{resistance?.level}</strong>
            <span style={{ fontSize: 12, color: 'var(--muted)' }}>{fmt(resistance?.oi)} contracts</span>
          </div>
        </div>
        {signal?.reason && <p className="stock-reason" style={{ marginTop: 8 }}>{signal.reason}</p>}
      </div>
      
      {records && (
        <div className="detail-panel" style={{ marginTop: 16 }}>
          <h3>Option Chain (Near ATM)</h3>
          <div style={{ marginTop: 16 }}>
            <div className="options-strike-row" style={{ color: "var(--muted)", borderBottom: "1px solid var(--border)", fontWeight: "bold" }}>
              <div className="options-put">PUT OI (Support)</div>
              <div className="options-strike">STRIKE</div>
              <div className="options-call">CALL OI (Resistance)</div>
            </div>
            {records.filter((r: any) => Math.abs(r.strikePrice - spot_price) < spot_price * 0.03).map((r: any) => {
              const maxOi = Math.max(...records.map((x: any) => Math.max(x.pe_oi || 0, x.ce_oi || 0)));
              const peWidth = ((r.pe_oi || 0) / maxOi) * 100;
              const ceWidth = ((r.ce_oi || 0) / maxOi) * 100;
              return (
                <div key={r.strikePrice} className={`options-strike-row ${r.strikePrice === atm_strike ? 'atm' : ''}`}>
                  <div className="options-put">
                    <div>{fmt(r.pe_oi)}</div>
                    <div className="oi-bar put" style={{ width: `${peWidth}%`, marginLeft: 'auto' }}></div>
                  </div>
                  <div className="options-strike">{r.strikePrice}</div>
                  <div className="options-call">
                    <div>{fmt(r.ce_oi)}</div>
                    <div className="oi-bar call" style={{ width: `${ceWidth}%` }}></div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function extractItems(tab: string, data: any): any[] {
  if (!data) return [];
  if (tab === "dashboard" || tab === "hot-picks") return data.hotPicks || data.top_opportunities || [];
  // live-action: data comes from /api/market/live-entries
  if (tab === "live-action") return data.entries || data.liveAction || [];
  if (tab === "traps") return data.trap_signals || [];
  if (tab === "pbs") return data.pbsRadar || [];
  if (tab === "watchlist") return data.watchlist || data.candidates || [];
  if (tab === "base-radar") return data.baseFormationRadar || data.pre_breakout_setups || data.pattern_forming_setups || [];
  if (tab === "momentum" || tab === "fast-movers") return data.momentumRadar || data.fast_movers || data.results || [];
  if (tab === "breakouts") return data.results || [];
  if (tab === "avoid") return data.avoid_risky || [];
  return [];
}

export default function Page() {
  const [tab, setTab] = useState("dashboard");
  const [dataByTab, setDataByTab] = useState<Record<string, any>>({});
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [liveTicks, setLiveTicks] = useState<Record<string, any>>({});
  const [liveEntryCount, setLiveEntryCount] = useState(0);
  const activeTabRef = useRef(tab);

  // Real-time live entries subscription (WebSocket with automatic polling fallback)
  useEffect(() => {
    let ws: WebSocket | null = null;
    let pollInterval: any = null;

    const pollLiveEntries = async () => {
      try {
        const r = await fetch(`${API_URL}/api/market/live-entries`, { cache: "no-store" });
        if (r.ok) {
          const json = await r.json();
          setLiveEntryCount(json.count || 0);
          setDataByTab(prev => ({ ...prev, "live-action": json }));
        }
      } catch {}
    };

    const connectWebSocket = () => {
      try {
        let wsUrl = "";
        if (!API_URL || API_URL.startsWith("/")) {
          const loc = window.location;
          const proto = loc.protocol === "https:" ? "wss:" : "ws:";
          wsUrl = `${proto}//${loc.host}${API_URL || ""}/api/live/ws/live-entries`;
        } else {
          wsUrl = API_URL.replace(/^http/, "ws") + "/api/live/ws/live-entries";
        }

        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
          console.log("[WS] Connected to live entries feed");
          if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
          }
        };

        ws.onmessage = (event) => {
          try {
            const json = JSON.parse(event.data);
            setLiveEntryCount(json.count || 0);
            setDataByTab(prev => ({ ...prev, "live-action": json }));
          } catch (e) {
            console.error("[WS] Failed to parse message", e);
          }
        };

        ws.onclose = () => {
          console.log("[WS] Connection closed, falling back to polling");
          if (!pollInterval) {
            pollLiveEntries();
            pollInterval = setInterval(pollLiveEntries, 3000);
          }
          setTimeout(connectWebSocket, 5000);
        };

        ws.onerror = (err) => {
          console.error("[WS] Error:", err);
          ws?.close();
        };
      } catch (e) {
        console.error("[WS] Connection failed:", e);
        if (!pollInterval) {
          pollLiveEntries();
          pollInterval = setInterval(pollLiveEntries, 3000);
        }
      }
    };

    connectWebSocket();

    return () => {
      if (ws) ws.close();
      if (pollInterval) clearInterval(pollInterval);
    };
  }, []);

  useEffect(() => {
    activeTabRef.current = tab;
  }, [tab]);

  const load = useCallback(async (tabId: string, force = false) => {
    const isCurrentTab = activeTabRef.current === tabId;
    if (isCurrentTab) setLoading(true);
    const tabConfig = TABS.find(t => t.id === tabId) || TABS[0];
    try {
      const r = await fetch(`${API_URL}${tabConfig.api}?force_refresh=${force}`, { cache: "no-store" });
      if (r.ok) {
        const json = await r.json();
        setDataByTab(prev => {
          const previous = prev[tabId];
          const previousItems = extractItems(tabId, previous);
          const incomingItems = extractItems(tabId, json);
          const transientEmpty =
            !force &&
            previousItems.length > 0 &&
            incomingItems.length === 0 &&
            (json?.isStale || json?.warning || ["empty_fallback", "refresh_in_progress", "tracked_fallback"].includes(json?.cacheStatus || json?.cache_status));
          if (transientEmpty) return prev;
          return { ...prev, [tabId]: json };
        });
        if (tabId === "dashboard") setSummary(json);
      }
    } catch {}
    if (activeTabRef.current === tabId) setLoading(false);
  }, []);

  useEffect(() => {
    load(tab);
    const interval = setInterval(() => load(tab), 60000);
    return () => clearInterval(interval);
  }, [tab, load]);

  const data = dataByTab[tab] || null;
  const items = extractItems(tab, data);
  const liveItems = items.map((item: any) => overlayLiveTick(item, liveTicks[item.symbol]));
  const mood = summary?.marketMood || data?.marketMood || "loading";
  const stats = summary?.summary || data?.summary || {};

  useEffect(() => {
    if (selectedSymbol || !items.length) return;
    const symbols = Array.from(new Set(items.map((item: any) => item.symbol).filter(Boolean))).slice(0, 80);
    if (!symbols.length) return;
    const wsUrl = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:4001";
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      symbols.forEach(symbol => ws.send(JSON.stringify({ type: "subscribe", symbol })));
    };
    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "live_update" && message.symbol) {
          setLiveTicks(prev => ({ ...prev, [message.symbol]: message.data || message }));
        }
      } catch {}
    };
    return () => {
      symbols.forEach(symbol => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "unsubscribe", symbol }));
      });
      ws.close();
    };
  }, [items, selectedSymbol]);

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
          <button key={t.id} className={`sidebar-item ${tab === t.id ? "active" : ""}`} onClick={() => { setTab(t.id); setSelectedSymbol(null); }}
            style={t.id === "live-action" && liveEntryCount > 0 ? { position: "relative" } : {}}>
            {t.label}
            {t.id === "live-action" && liveEntryCount > 0 && (
              <span style={{
                marginLeft: 6,
                background: "var(--red, #ef4444)",
                color: "#fff",
                borderRadius: 9,
                fontSize: 11,
                fontWeight: 700,
                padding: "1px 6px",
                animation: "pulse 1s infinite",
              }}>{liveEntryCount}</span>
            )}
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

        {tab === "options" ? (
          <OptionsDashboard data={data} />
        ) : selectedSymbol ? (
          <StockDetail symbol={selectedSymbol} onClose={() => setSelectedSymbol(null)} />
        ) : (
          <>
            <div className="stats-row">
              <div className="stat-card"><span>Scanned</span><strong>{stats.totalScanned ?? stats.total_scanned_universe ?? "..."}</strong></div>
              <div className="stat-card"><span>Hot Picks</span><strong>{stats.highConfidence ?? stats.high_priority ?? 0}</strong></div>
              <div className="stat-card"><span>Breakouts</span><strong>{stats.breakouts ?? stats.breakout_count ?? 0}</strong></div>
              <div className="stat-card"><span>Bullish</span><strong style={{ color: "var(--green)" }}>{stats.bullish ?? stats.bullish_setups ?? 0}</strong></div>
              <div className="stat-card"><span>Bearish</span><strong style={{ color: "var(--red)" }}>{stats.bearish ?? stats.bearish_risk_count ?? 0}</strong></div>
              <div className="stat-card"><span>This Tab</span><strong>{liveItems.length}</strong></div>
            </div>

            <h2 className="section-title">{TABS.find(t => t.id === tab)?.label} ({liveItems.length} stocks)</h2>
            {liveItems.length ? (
              <div className="stock-grid">
                {liveItems.map((item: any, i: number) => <StockCard key={item.symbol || i} item={item} onSelect={setSelectedSymbol} />)}
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
