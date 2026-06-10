"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL, humanize, fmt, fmtPct, asNumber } from "./lib/market";
import TradingChart from "./components/TradingChart";

const TABS = [
  { id: "dashboard",   label: "Dashboard",       api: "/api/market/hot-picks" },
  { id: "live-action", label: "🔴 Live Action",  api: "/api/market/live-entries" },
  { id: "hot-picks",   label: "🌟 God-Tier Scans",  api: "/api/market/hot-picks" },
  { id: "volume-boomers", label: "📦 3x+ Volume Boomers", api: "/api/market/hot-picks" },
  { id: "watchlist",   label: "Watchlist",         api: "/api/market/hot-picks" },
  { id: "base-radar",  label: "Base Formation",    api: "/api/market/hot-picks" },
  { id: "momentum",    label: "Momentum",           api: "/api/market/hot-picks" },
  { id: "traps",       label: "Traps & Risks",     api: "/api/market/traps" },
  { id: "options",     label: "⚡ Options",         api: "/api/market/options-analysis?symbol=NIFTY" },
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

// ── Index Ticker Bar ────────────────────────────────────────────────────────
function IndexTickerBar() {
  const [indices, setIndices] = useState<any[]>([]);

  const fetchIndices = async () => {
    try {
      const r = await fetch(`${API_URL}/api/market/indices`, { cache: "no-store" });
      if (r.ok) {
        const data = await r.json();
        setIndices(data);
      }
    } catch {}
  };

  useEffect(() => {
    fetchIndices();
    const interval = setInterval(fetchIndices, 60000);
    return () => clearInterval(interval);
  }, []);

  if (!indices.length) return null;

  return (
    <div className="index-ticker-bar">
      {indices.map((idx: any) => {
        const isVix = idx.key === "VIX";
        const isUp = idx.is_up;
        const color = isVix
          ? idx.price > 20 ? "var(--red)" : idx.price > 15 ? "var(--orange)" : "var(--green)"
          : isUp ? "var(--green)" : "var(--red)";
        return (
          <div key={idx.key} className="index-ticker-item">
            <span className="index-ticker-name">{idx.name}</span>
            <span className="index-ticker-price" style={{ color }}>
              {idx.price != null ? (isVix ? idx.price.toFixed(2) : fmt(idx.price)) : "–"}
            </span>
            {!isVix && idx.change_pct != null && (
              <span className="index-ticker-change" style={{ color }}>
                {isUp ? "▲" : "▼"} {Math.abs(idx.change_pct).toFixed(2)}%
              </span>
            )}
            {isVix && idx.price != null && (
              <span className="index-ticker-change" style={{ color, fontSize: 11 }}>
                {idx.price > 20 ? "🔴 High" : idx.price > 15 ? "🟡 Elevated" : "🟢 Low"}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Stock Card ───────────────────────────────────────────────────────────────
function StockCard({ item, onSelect, tab }: { item: any; onSelect: (s: string) => void; tab?: string }) {
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
  const relVol = item.relative_volume ?? item.raw?.relative_volume ?? 0;

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
          {item.quality_grade && <span className="tag" style={{ 
            background: item.quality_grade === 'A' ? 'var(--green)' : item.quality_grade === 'B' ? 'var(--yellow)' : item.quality_grade === 'C' ? 'var(--orange)' : 'var(--red)',
            color: '#fff',
            fontWeight: 'bold'
          }}>Grade {item.quality_grade}</span>}
        </div>
        <span className={`badge ${isHighTrap ? 'bearish' : direction}`}>
          {isHighTrap ? '🛑 AVOID' : direction === 'bullish' ? '📈 Bullish' : direction === 'bearish' ? '📉 Bearish' : humanize(direction)}
        </span>
      </div>
      {chartPattern ? <div className="stock-reason" style={{ marginTop: 4 }}>{humanize(chartPattern)}</div> : null}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="stock-price">₹{fmt(price)}</span>
        <span className={`stock-change ${change >= 0 ? "up" : "down"}`}>{fmtPct(change)}</span>
      </div>
      <div className="stock-meta">
        <span className={`tag ${actionClass}`}>{action === 'BUY' ? '✅ BUY' : action === 'SELL' ? '🛑 SELL' : '👁️ WATCH'}</span>
        {(item.risk_reward || item.rr) ? <span className="tag">⚖️ RR 1:{fmt(item.risk_reward ?? item.rr, 1)}</span> : null}
        {item.confidence ? <span className="tag" style={{ 
          background: (item.confidence ?? 0) > 80 ? 'var(--green)' : (item.confidence ?? 0) > 60 ? 'var(--yellow)' : 'var(--orange)',
          color: '#fff',
          fontWeight: 'bold'
        }}>🎯 {fmt(item.confidence ?? item.score, 0)}%</span> : null}
        {relVol > 0 ? <span className="tag" style={{
          background: relVol >= 5 ? 'var(--orange)' : relVol >= 3 ? 'var(--green)' : 'transparent',
          color: relVol >= 3 ? '#fff' : 'inherit',
          fontWeight: relVol >= 3 ? 'bold' : 'normal',
          border: relVol >= 3 ? 'none' : '1px solid var(--border)'
        }}>{relVol >= 5 ? '🚀' : relVol >= 3 ? '🔥' : '📦'} {fmt(relVol, 1)}X VOL</span> : null}
        {item.entry_quality ? <span className="tag">{humanize(item.entry_quality)} entry</span> : null}
        {item.seller_pressure ? <span className="tag">{humanize(item.seller_pressure)} sellers</span> : null}
        {item.profit_booking_risk ? <span className="tag">{humanize(item.profit_booking_risk)} booking</span> : null}
        {timeHorizon ? <span className="tag" style={{ color: "var(--yellow)", fontWeight: "bold" }}>⏳ Runs in: {timeHorizon}</span> : null}
        {patternLabels.slice(0, 2).map((label: string) => <span key={label} className="tag">{humanize(label)}</span>)}
      </div>
      {tab === 'hot-picks' && (
        <div className="god-tier-panel" style={{ background: "rgba(139, 92, 246, 0.1)", border: "1px solid var(--accent)", borderRadius: 8, padding: 12, marginTop: 12, marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: "var(--accent)", textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 700, marginBottom: 8 }}>
            🛡️ God-Level Metrics
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {((item.raw?.tight_consolidation_pct ?? 100) < 4 || (item.raw?.bb_width_ratio ?? 1) < 0.15 || patternLabels.some((l: string) => l.toLowerCase().includes('squeeze') || l.toLowerCase().includes('base'))) && (
              <span className="tag" style={{ background: "rgba(234, 179, 8, 0.2)", color: "#facc15", border: "1px solid rgba(250, 204, 21, 0.4)" }}>🌪️ VCP & Vol Dry-Up</span>
            )}
            {((item.raw?.relative_strength_delta_5d ?? 0) > 3 || (item.raw?.rs_rating ?? 0) > 70 || (item.raw?.relative_strength ?? 0) > 1) && (
              <span className="tag" style={{ background: "rgba(52, 211, 153, 0.2)", color: "#34d399", border: "1px solid rgba(52, 211, 153, 0.4)" }}>💪 True Relative Strength</span>
            )}
            {(demandStatus === "strong_accumulation" || item.seller_pressure === "low" || smartMoneyRead?.toLowerCase().includes("accumulation")) && (
              <span className="tag" style={{ background: "rgba(96, 165, 250, 0.2)", color: "#60a5fa", border: "1px solid rgba(96, 165, 250, 0.4)" }}>🧊 Iceberg / Accumulation</span>
            )}
            {((item.raw?.sector_momentum ?? 0) > 0 || item.raw?.sector_strength === "strong") && (
              <span className="tag" style={{ background: "rgba(167, 139, 250, 0.2)", color: "#a78bfa", border: "1px solid rgba(167, 139, 250, 0.4)" }}>💸 Sector Rotation</span>
            )}
            {((item.raw?.iv_percentile ?? 0) > 80) ? (
              <span className="tag" style={{ background: "rgba(244, 63, 94, 0.2)", color: "#f43f5e", border: "1px solid rgba(244, 63, 94, 0.4)" }}>🚫 IV Crush Risk</span>
            ) : (item.raw?.options_activity === "bullish" || (item.raw?.put_oi_chg ?? 0) > (item.raw?.call_oi_chg ?? 0) * 1.5) ? (
              <span className="tag" style={{ background: "rgba(52, 211, 153, 0.2)", color: "#34d399", border: "1px solid rgba(52, 211, 153, 0.4)" }}>🏦 Smart Put Writing</span>
            ) : null}
            
            {/* Fallback if no specific rule matches heavily */}
            {!((item.raw?.tight_consolidation_pct ?? 100) < 4 || (item.raw?.bb_width_ratio ?? 1) < 0.15 || patternLabels.some((l: string) => l.toLowerCase().includes('squeeze') || l.toLowerCase().includes('base'))) && 
             !((item.raw?.relative_strength_delta_5d ?? 0) > 3 || (item.raw?.rs_rating ?? 0) > 70 || (item.raw?.relative_strength ?? 0) > 1) &&
             !(demandStatus === "strong_accumulation" || item.seller_pressure === "low" || smartMoneyRead?.toLowerCase().includes("accumulation")) &&
             !((item.raw?.sector_momentum ?? 0) > 0 || item.raw?.sector_strength === "strong") &&
             !((item.raw?.iv_percentile ?? 0) > 80) &&
             !(item.raw?.options_activity === "bullish" || (item.raw?.put_oi_chg ?? 0) > (item.raw?.call_oi_chg ?? 0) * 1.5) && (
               <span className="tag" style={{ color: "var(--muted)", background: "rgba(255,255,255,0.05)", border: "1px dashed var(--border)" }}>⚙️ Institutional Engine Active</span>
            )}
          </div>
        </div>
      )}
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

// ── Stock Detail Panel ───────────────────────────────────────────────────────
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
        setChartData(c || { data: [], active_zones: [] });
        setLoading(false); 
      })
      .catch(() => {
        if (isMounted) setLoading(false);
      });

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
      <div className="stock-targets" style={{ marginTop: 12 }}>
        <div><span>Entry</span><strong>₹{fmt(s.safe_entry_price ?? s.entry_price ?? s.entry_trigger ?? s.current_price)}</strong></div>
        <div><span>Target 1</span><strong>₹{fmt(s.new_target ?? s.target_1 ?? s.target_price)}</strong></div>
        <div><span>Stop Loss</span><strong>₹{fmt(s.invalidation_level ?? s.stop_loss ?? s.invalidation)}</strong></div>
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

// ── Options Dashboard — REBUILT ──────────────────────────────────────────────
function OptionsDashboard({ hotPicksData }: { hotPicksData: any }) {
  const [activeIndex, setActiveIndex] = useState("NIFTY");
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [lastFetch, setLastFetch] = useState<Date | null>(null);

  const INDEX_LIST = [
    { key: "NIFTY",      label: "Nifty 50" },
    { key: "BANKNIFTY",  label: "Bank Nifty" },
    { key: "FINNIFTY",   label: "Fin Nifty" },
    { key: "MIDCPNIFTY", label: "Midcap" },
  ];

  const fetchData = async (sym: string) => {
    setLoading(true);
    try {
      const r = await fetch(`${API_URL}/api/market/options-analysis?symbol=${sym}`, { cache: "no-store" });
      if (r.ok) {
        const json = await r.json();
        setData(json);
        setLastFetch(new Date());
      }
    } catch {}
    setLoading(false);
  };

  useEffect(() => {
    fetchData(activeIndex);
    const interval = setInterval(() => fetchData(activeIndex), 300000); // refresh every 5min
    return () => clearInterval(interval);
  }, [activeIndex]);

  const rec = data?.recommendation;
  const swing = data?.swing_prediction;
  const isFallback = data?.data_source === "fallback";
  const isNoTrade = !rec || rec.action === "NO_TRADE";
  const isCe = rec?.action === "BUY_CE";

  // Stock options from hot picks (confidence >= 75)
  const hotPicks = hotPicksData?.hotPicks || hotPicksData?.top_opportunities || [];
  const stockOptions = hotPicks.filter((s: any) => (s.confidence ?? s.score ?? 0) >= 75 && s.direction && s.direction !== "neutral");

  return (
    <div className="options-dashboard">
      {/* ── Index Selector ── */}
      <div className="options-index-tabs">
        {INDEX_LIST.map(idx => (
          <button
            key={idx.key}
            className={`options-index-tab ${activeIndex === idx.key ? "active" : ""}`}
            onClick={() => { setActiveIndex(idx.key); }}
            id={`options-tab-${idx.key.toLowerCase()}`}
          >
            {idx.label}
          </button>
        ))}
        <div style={{ marginLeft: "auto", fontSize: 12, color: "var(--muted)", alignSelf: "center" }}>
          {lastFetch ? `Updated: ${lastFetch.toLocaleTimeString()}` : ""}
          <button
            onClick={() => fetchData(activeIndex)}
            style={{ marginLeft: 8, background: "none", border: "1px solid var(--border)", color: "var(--muted)", padding: "4px 10px", borderRadius: 6, cursor: "pointer", fontSize: 12 }}
            disabled={loading}
          >
            {loading ? "⟳" : "↻ Refresh"}
          </button>
        </div>
      </div>

      {loading && !data ? (
        <div className="empty-msg">⟳ Loading option chain data…</div>
      ) : !data ? (
        <div className="empty-msg">Could not load options data. Try refreshing.</div>
      ) : (
        <>
          {/* ── Fallback Warning ── */}
          {isFallback && (
            <div className="options-warning-banner">
              ⚠️ <strong>Live NSE data unavailable.</strong> Showing estimated spot price only.
              Do NOT trade options based on this — wait for live data.
            </div>
          )}

          {/* ── 10-Day Swing Prediction + Recommendation ── */}
          <div className={`options-rec-box ${isNoTrade ? "no-trade" : isCe ? "bullish-rec" : "bearish-rec"}`}>
            <div className="options-rec-header">
              <span className="options-rec-label">
                {isNoTrade ? "⛔ NO TRADE — Market Unclear" : rec?.label}
              </span>
              <span className="options-rec-horizon">📅 10-Day Swing Prediction</span>
            </div>

            {isNoTrade ? (
              <p className="options-rec-reason">{rec?.reason || "Confidence too low or data unavailable. Stay out until clear signal."}</p>
            ) : (
              <>
                <div className="options-rec-main">
                  <div>
                    <div className="options-rec-strategy">{rec?.strategy}</div>
                    <div style={{ fontSize: 13, color: "var(--muted)", marginTop: 6 }}>
                      🎯 Confidence: <strong style={{ color: "#fff" }}>{rec?.confidence}%</strong>
                      &nbsp;&nbsp;⏳ Horizon: <strong style={{ color: "#fff" }}>{rec?.horizon}</strong>
                    </div>
                    {rec?.vix_note && (
                      <div style={{ fontSize: 12, color: "var(--orange)", marginTop: 4 }}>
                        📊 {rec.vix_note}
                      </div>
                    )}
                  </div>
                  <div className="options-rec-levels">
                    <div>
                      <span>Strike</span>
                      <strong>{rec?.strike}</strong>
                    </div>
                    <div>
                      <span>Stop (underlying)</span>
                      <strong style={{ color: "var(--red)" }}>₹{fmt(rec?.stop_loss_underlying)}</strong>
                    </div>
                    <div>
                      <span>Target (underlying)</span>
                      <strong style={{ color: "var(--green)" }}>₹{fmt(rec?.target_underlying)}</strong>
                    </div>
                  </div>
                </div>

                {rec?.reasons?.length > 0 && (
                  <div className="options-rec-reasons">
                    <div style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 8 }}>Why this trade:</div>
                    {rec.reasons.map((r: string, i: number) => (
                      <div key={i} className="options-signal-row">
                        <span className={isCe ? "signal-bull" : "signal-bear"}>
                          {isCe ? "▲" : "▼"}
                        </span>
                        <span>{r}</span>
                      </div>
                    ))}
                  </div>
                )}

                <div className="options-rec-warning">
                  ⚠️ {rec?.warning}
                </div>
              </>
            )}
          </div>

          {/* ── Key Metrics ── */}
          <div className="stats-row">
            <div className="stat-card">
              <span>Spot ({data.label})</span>
              <strong>₹{fmt(data.spot_price)}</strong>
            </div>
            <div className="stat-card">
              <span>ATM Strike</span>
              <strong>{data.atm_strike}</strong>
            </div>
            <div className="stat-card">
              <span>PCR</span>
              <strong style={{ color: data.pcr >= 1.2 ? "var(--green)" : data.pcr <= 0.8 ? "var(--red)" : "var(--orange)" }}>
                {data.pcr?.toFixed(2)}
                <span style={{ fontSize: 12, fontWeight: 400, marginLeft: 6 }}>
                  {data.pcr >= 1.2 ? "Bullish" : data.pcr <= 0.8 ? "Bearish" : "Neutral"}
                </span>
              </strong>
            </div>
            <div className="stat-card">
              <span>Max Pain</span>
              <strong>{data.max_pain}</strong>
            </div>
            <div className="stat-card">
              <span>IV Proxy</span>
              <strong>{data.iv_proxy > 0 ? `${(data.iv_proxy * 100).toFixed(1)}%` : "–"}</strong>
            </div>
            <div className="stat-card">
              <span>India VIX</span>
              <strong style={{ color: (data.india_vix ?? 15) > 20 ? "var(--red)" : (data.india_vix ?? 15) > 15 ? "var(--orange)" : "var(--green)" }}>
                {data.india_vix ? data.india_vix.toFixed(2) : "–"}
              </strong>
            </div>
          </div>

          {/* ── OI Signals Summary ── */}
          <div className="demand-panel">
            <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 24 }}>
              <div>
                <div style={{ fontSize: 11, color: "var(--green)", textTransform: "uppercase", letterSpacing: "0.1em" }}>SUPPORT (Max Put OI)</div>
                <div style={{ fontSize: 24, fontWeight: 800, color: "#fff", marginTop: 4 }}>₹{fmt(data.support)}</div>
                <div style={{ fontSize: 12, color: "var(--muted)" }}>OI: {fmt(data.oi_change?.put_change_oi)} contracts added</div>
              </div>
              <div>
                <div style={{ fontSize: 11, color: "var(--red)", textTransform: "uppercase", letterSpacing: "0.1em" }}>RESISTANCE (Max Call OI)</div>
                <div style={{ fontSize: 24, fontWeight: 800, color: "#fff", marginTop: 4 }}>₹{fmt(data.resistance)}</div>
                <div style={{ fontSize: 12, color: "var(--muted)" }}>OI: {fmt(data.oi_change?.call_change_oi)} contracts added</div>
              </div>
              <div>
                <div style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.1em" }}>OI Bias</div>
                <div style={{ fontSize: 18, fontWeight: 800, marginTop: 4, color: data.oi_change?.bias === "bullish_support" ? "var(--green)" : data.oi_change?.bias === "bearish_resistance" ? "var(--red)" : "var(--orange)" }}>
                  {data.oi_change?.bias === "bullish_support" ? "🟢 Bullish Support" :
                   data.oi_change?.bias === "bearish_resistance" ? "🔴 Bearish Resistance" : "⚪ Neutral"}
                </div>
              </div>
            </div>
          </div>

          {/* ── OI Chain Chart ── */}
          {data.records && data.records.length > 0 && (
            <div className="detail-panel">
              <h3 style={{ fontSize: 16, color: "#fff", marginBottom: 16 }}>
                Option Chain — Near ATM ({data.atm_strike})
              </h3>
              <div>
                <div className="options-strike-row" style={{ color: "var(--muted)", fontWeight: 700, borderBottom: "1px solid var(--border)" }}>
                  <div className="options-put">PUT OI → Support</div>
                  <div className="options-strike">Strike</div>
                  <div className="options-call">Call OI → Resistance</div>
                </div>
                {(() => {
                  const maxOi = Math.max(...data.records.map((r: any) => Math.max(r.pe_oi || 0, r.ce_oi || 0)), 1);
                  return data.records.map((r: any) => {
                    const peWidth = ((r.pe_oi || 0) / maxOi) * 100;
                    const ceWidth = ((r.ce_oi || 0) / maxOi) * 100;
                    const isAtm = r.strikePrice === data.atm_strike;
                    return (
                      <div key={r.strikePrice} className={`options-strike-row ${isAtm ? "atm" : ""}`}>
                        <div className="options-put">
                          <div style={{ display: "flex", alignItems: "center", gap: 8, justifyContent: "flex-end" }}>
                            <div className="oi-bar put" style={{ width: `${peWidth}%` }}></div>
                            <span style={{ minWidth: 60, textAlign: "right" }}>{fmt(r.pe_oi)}</span>
                          </div>
                          <div style={{ fontSize: 11, color: "var(--muted)", textAlign: "right" }}>
                            LTP: ₹{r.pe_ltp?.toFixed(1) || "–"}
                          </div>
                        </div>
                        <div className="options-strike">
                          {r.strikePrice}
                          {isAtm && <div style={{ fontSize: 10, color: "var(--accent)", fontWeight: 800 }}>ATM</div>}
                        </div>
                        <div className="options-call">
                          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                            <span style={{ minWidth: 60 }}>{fmt(r.ce_oi)}</span>
                            <div className="oi-bar call" style={{ width: `${ceWidth}%` }}></div>
                          </div>
                          <div style={{ fontSize: 11, color: "var(--muted)" }}>
                            LTP: ₹{r.ce_ltp?.toFixed(1) || "–"}
                          </div>
                        </div>
                      </div>
                    );
                  });
                })()}
              </div>
            </div>
          )}

          {/* ── All Technical Signals ── */}
          {swing?.signals?.length > 0 && (
            <div className="demand-panel">
              <div style={{ fontSize: 12, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 12 }}>
                📊 All Analysis Signals (10-day horizon)
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {swing.signals.map((sig: string, i: number) => (
                  <div key={i} className="options-signal-row">
                    <span style={{ color: "var(--accent)" }}>•</span>
                    <span style={{ fontSize: 14, color: "var(--muted)" }}>{sig}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── Stock Options from Hot Picks ── */}
          {stockOptions.length > 0 && (
            <div>
              <h2 className="section-title">📋 Stock Options Suggestions (from Hot Picks)</h2>
              <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 16 }}>
                These stocks have ≥75% confidence. Based on direction, here's the option play:
              </div>
              <div className="stock-grid">
                {stockOptions.slice(0, 6).map((stock: any) => {
                  const isBull = stock.direction === "bullish";
                  const rawPrice = stock.currentPrice ?? stock.current_price ?? stock.price ?? 0;
                  const price = Number(asNumber(rawPrice) || 0);
                  const step = price < 500 ? 10 : price < 2000 ? 50 : price < 5000 ? 100 : 200;
                  const atmStrike = Math.round(price / step) * step;
                  const conf = stock.confidence ?? stock.score ?? 0;
                  return (
                    <div key={stock.symbol} className={`stock-card ${isBull ? "options-ce-card" : "options-pe-card"}`}>
                      <div className="stock-card-top">
                        <h3>{stock.symbol}</h3>
                        <span className={`badge ${isBull ? "bullish" : "bearish"}`}>
                          {isBull ? "📈 BUY CE" : "📉 BUY PE"}
                        </span>
                      </div>
                      <div style={{ fontSize: 13, color: "var(--muted)" }}>
                        Stock at <strong style={{ color: "#fff" }}>₹{fmt(price)}</strong> — ATM Strike ~<strong style={{ color: "#fff" }}>{atmStrike}</strong>
                      </div>
                      <div className="stock-meta">
                        <span className={`tag ${isBull ? "buy" : "sell"}`}>
                          {isBull ? `Buy ${stock.symbol} ${atmStrike} CE` : `Buy ${stock.symbol} ${atmStrike} PE`}
                        </span>
                        <span className="tag">🎯 {fmt(conf, 0)}% conf</span>
                      </div>
                      <p className="stock-reason">
                        {stock.aiReason || stock.reason || stock.signal_summary || "Strong signal — direction confirmed by multiple indicators."}
                      </p>
                      <div className="stock-targets">
                        <div><span>Entry Zone</span><strong>₹{fmt(stock.entryZone ?? stock.entry_trigger ?? price)}</strong></div>
                        <div><span>Target</span><strong>₹{fmt(stock.target_1 ?? stock.target_price)}</strong></div>
                        <div><span>Stop</span><strong>₹{fmt(stock.stop_loss ?? stock.stoploss)}</strong></div>
                      </div>
                      <div style={{ fontSize: 11, color: "var(--orange)", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
                        ⚠️ Check liquidity before buying. Only trade options on stocks with high volume.
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ── Extract items from API data ──────────────────────────────────────────────
function extractItems(tab: string, data: any): any[] {
  if (!data) return [];
  if (tab === "dashboard") return data.hotPicks || data.top_opportunities || [];
  if (tab === "hot-picks") {
    const items = data.hotPicks || data.top_opportunities || [];
    return items.filter((item: any) => {
      const raw = item.raw || {};
      const patternLabels = item.patternLabels || [];
      const demandStatus = item.demandStatus || "";
      const smartMoneyRead = item.smartMoneyRead || "";
      
      const hasVCP = (raw.tight_consolidation_pct ?? 100) < 4 || (raw.bb_width_ratio ?? 1) < 0.15 || patternLabels.some((l: string) => l.toLowerCase().includes('squeeze') || l.toLowerCase().includes('base'));
      const hasRS = (raw.relative_strength_delta_5d ?? 0) > 3 || (raw.rs_rating ?? 0) > 70 || (raw.relative_strength ?? 0) > 1;
      const hasIceberg = demandStatus === "strong_accumulation" || item.seller_pressure === "low" || smartMoneyRead.toLowerCase().includes("accumulation");
      const hasSector = (raw.sector_momentum ?? 0) > 0 || raw.sector_strength === "strong";
      const hasOptions = (raw.options_activity === "bullish" || (raw.put_oi_chg ?? 0) > (raw.call_oi_chg ?? 0) * 1.5);
      
      // Keep only if it has at least one strong God-Level metric AND score > 70
      return (hasVCP || hasRS || hasIceberg || hasSector || hasOptions) && (item.confidence ?? item.score ?? 0) >= 70;
    });
  }
  if (tab === "volume-boomers") return (data.hotPicks || data.top_opportunities || []).filter((x: any) => (x.relative_volume ?? x.raw?.relative_volume ?? 1) >= 3);
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

// ── Main Page ────────────────────────────────────────────────────────────────
export default function Page() {
  const [tab, setTab] = useState("dashboard");
  const [dataByTab, setDataByTab] = useState<Record<string, any>>({});
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [search, setSearch] = useState("");
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [liveTicks, setLiveTicks] = useState<Record<string, any>>({});
  const [liveEntryCount, setLiveEntryCount] = useState(0);
  const [activeFilter, setActiveFilter] = useState("all");
  const activeTabRef = useRef(tab);

  // Real-time live entries subscription
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
          if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
        };
        ws.onmessage = (event) => {
          try {
            const json = JSON.parse(event.data);
            setLiveEntryCount(json.count || 0);
            setDataByTab(prev => ({ ...prev, "live-action": json }));
          } catch {}
        };
        ws.onclose = () => {
          if (!pollInterval) {
            pollLiveEntries();
            pollInterval = setInterval(pollLiveEntries, 15000);
          }
          setTimeout(connectWebSocket, 10000);
        };
        ws.onerror = () => { ws?.close(); };
      } catch {
        if (!pollInterval) {
          pollLiveEntries();
          pollInterval = setInterval(pollLiveEntries, 15000);
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
    const alreadyHasData = Boolean(dataByTab[tabId]);

    if (isCurrentTab && !alreadyHasData) {
      setLoading(true);
    } else if (isCurrentTab && !force) {
      setRefreshing(true);
    }

    // Options tab has its own internal fetch — skip here
    if (tabId === "options") {
      if (isCurrentTab) { setLoading(false); setRefreshing(false); }
      return;
    }

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
    if (activeTabRef.current === tabId) {
      setLoading(false);
      setRefreshing(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    load(tab);
    const interval = setInterval(() => load(tab), 90000);
    return () => clearInterval(interval);
  }, [tab, load]);

  const data = dataByTab[tab] || null;
  const items = extractItems(tab, data);
  const allLiveItems = items.map((item: any) => overlayLiveTick(item, liveTicks[item.symbol]));
  
  const liveItems = activeFilter === "all" ? allLiveItems
    : activeFilter === "bullish" ? allLiveItems.filter((x: any) => x.direction === "bullish")
    : activeFilter === "bearish" ? allLiveItems.filter((x: any) => x.direction === "bearish")
    : activeFilter === "breakout" ? allLiveItems.filter((x: any) => x.entry_label?.includes("BREAKOUT") || x.has_breakout)
    : activeFilter === "high-conf" ? allLiveItems.filter((x: any) => (x.confidence ?? x.score ?? 0) > 80)
    : activeFilter === "tomorrow" ? allLiveItems.filter((x: any) => (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase().includes("tomorrow"))
    : activeFilter === "1-2-days" ? allLiveItems.filter((x: any) => (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase().includes("1-2"))
    : activeFilter === "3-4-days" ? allLiveItems.filter((x: any) => { const s = (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase(); return s.includes("3-4") || s.includes("3 to 4") || s.includes("3 - 4"); })
    : activeFilter === "anytime" ? allLiveItems.filter((x: any) => { const s = (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase(); return s.includes("any time") || s.includes("anytime"); })
    : allLiveItems;

  const mood = summary?.marketMood || data?.marketMood || "loading";
  const stats = summary?.summary || data?.summary || {};

  useEffect(() => {
    if (selectedSymbol || !items.length) return;
    const symbols = Array.from(new Set(items.map((item: any) => item.symbol).filter(Boolean))).slice(0, 80);
    if (!symbols.length) return;
    const wsUrl = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:4001";
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => { symbols.forEach(symbol => ws.send(JSON.stringify({ type: "subscribe", symbol }))); };
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

  // Hot picks for stock options tab (always load dashboard data)
  const dashboardData = dataByTab["dashboard"] || null;

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-logo">BullAlways</div>
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
          <button key={t.id} className={`sidebar-item ${tab === t.id ? "active" : ""}`}
            onClick={() => { setTab(t.id); setSelectedSymbol(null); setActiveFilter("all"); }}
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
                animation: "pulse 2.5s infinite",
              }}>{liveEntryCount}</span>
            )}
          </button>
        ))}
        <div className="sidebar-section">Portfolio</div>
        <a href="/performance" className="sidebar-item" style={{ textDecoration: "none", display: "block" }}>
          Performance Tracker ↗
        </a>
      </aside>

      <div style={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
        {/* ── Index Ticker Bar ── */}
        <IndexTickerBar />

        <main className="main-content">
          <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
            <div>
              <h1>{selectedSymbol || TABS.find(t => t.id === tab)?.label}</h1>
              <p>Updated: {data?.generated_at || data?.lastUpdated ? new Date(data.generated_at || data.lastUpdated).toLocaleString() : "..."}</p>
            </div>
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              {refreshing && (
                <span style={{ fontSize: 11, color: "#888", animation: "pulse 1s infinite" }}>↻ updating…</span>
              )}
              <span className={`mood-badge ${mood}`}>{humanize(mood)}</span>
              <button className="refresh-btn" onClick={() => load(tab, true)} disabled={loading || refreshing}>
                {loading ? "..." : refreshing ? "↻" : "Refresh"}
              </button>
            </div>
          </div>

          {tab === "options" ? (
            <OptionsDashboard hotPicksData={dashboardData} />
          ) : selectedSymbol ? (
            <StockDetail symbol={selectedSymbol} onClose={() => setSelectedSymbol(null)} />
          ) : (
            <>
              <div className="stats-row">
                <div className="stat-card"><span>📊 Scanned</span><strong>{stats.totalScanned ?? stats.total_scanned_universe ?? "..."}</strong></div>
                <div className="stat-card"><span>🔥 Hot Picks</span><strong style={{ color: "var(--green)" }}>{stats.highConfidence ?? stats.high_priority ?? 0}</strong></div>
                <div className="stat-card"><span>⚡ Breakouts</span><strong style={{ color: "var(--yellow)" }}>{stats.breakouts ?? stats.breakout_count ?? 0}</strong></div>
                <div className="stat-card"><span>📈 Bullish</span><strong style={{ color: "var(--green)" }}>{stats.bullish ?? stats.bullish_setups ?? 0}</strong></div>
                <div className="stat-card"><span>📉 Bearish</span><strong style={{ color: "var(--red)" }}>{stats.bearish ?? stats.bearish_risk_count ?? 0}</strong></div>
                <div className="stat-card"><span>✅ This Tab</span><strong>{liveItems.length}</strong></div>
              </div>

              <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
                {([
                  { key: "all",       label: "All",             count: allLiveItems.length },
                  { key: "bullish",   label: "📈 Bullish",       count: allLiveItems.filter((x: any) => x.direction === "bullish").length },
                  { key: "bearish",   label: "📉 Bearish",       count: allLiveItems.filter((x: any) => x.direction === "bearish").length },
                  { key: "breakout",  label: "⚡ Breakout",      count: allLiveItems.filter((x: any) => x.entry_label?.includes("BREAKOUT") || x.has_breakout).length },
                  { key: "high-conf", label: "🎯 High Confidence", count: allLiveItems.filter((x: any) => (x.confidence ?? x.score ?? 0) > 80).length },
                  { key: "tomorrow",  label: "🌅 Tomorrow",      count: allLiveItems.filter((x: any) => (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase().includes("tomorrow")).length },
                  { key: "1-2-days",  label: "⏳ 1-2 Days",      count: allLiveItems.filter((x: any) => (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase().includes("1-2")).length },
                  { key: "3-4-days",  label: "📆 3-4 Days",      count: allLiveItems.filter((x: any) => { const s = (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase(); return s.includes("3-4") || s.includes("3 to 4") || s.includes("3 - 4"); }).length },
                  { key: "anytime",   label: "🔥 Anytime",       count: allLiveItems.filter((x: any) => { const s = (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase(); return s.includes("any time") || s.includes("anytime"); }).length },
                ] as { key: string; label: string; count: number }[]).map(f => (
                  <button
                    key={f.key}
                    className="refresh-btn"
                    onClick={() => setActiveFilter(f.key)}
                    style={{
                      fontSize: 13,
                      padding: "6px 12px",
                      background: activeFilter === f.key ? "var(--green, #22c55e)" : undefined,
                      color: activeFilter === f.key ? "#000" : undefined,
                      fontWeight: activeFilter === f.key ? 700 : undefined,
                    }}
                  >
                    {f.label} ({f.count})
                  </button>
                ))}
              </div>

              <h2 className="section-title">{TABS.find(t => t.id === tab)?.label} ({liveItems.length} stocks)</h2>
              {loading ? (
                <div className="stock-grid">
                  {[1,2,3,4,5,6].map(i => <div key={i} className="stock-card" style={{ animation: "pulse 2.5s infinite", background: "var(--bg, #1a1a1a)" }}>
                    <div style={{ height: 120, background: "var(--border, #333)" }}></div>
                  </div>)}
                </div>
              ) : liveItems.length ? (
                <div className="stock-grid">
                  {liveItems.map((item: any, i: number) => <StockCard key={item.symbol || i} item={item} onSelect={setSelectedSymbol} tab={tab} />)}
                </div>
              ) : (
                <p className="empty-msg">No stocks found in this category right now.</p>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  );
}
