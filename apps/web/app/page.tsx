"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL, humanize, fmt, fmtPct, asNumber } from "./lib/market";
import { stockCardModel } from "./lib/stock-card-model";
import TradingChart from "./components/TradingChart";
import FreshZonesPanel from "./components/FreshZonesPanel";
import Icon from "./components/Icon";

const TABS = [
  { id: "dashboard",      group: "Overview",     icon: "home",     label: "Dashboard",          hint: "Top setups across the market right now",            api: "/api/market/hot-picks" },
  { id: "live-action",    group: "Overview",     icon: "live",     label: "Live Action",        hint: "Setups whose live price/volume conditions are being checked", api: "/api/market/live-entries" },
  { id: "hot-picks",      group: "Scans",        icon: "star",     label: "Pro Setup Scans",    hint: "Setups with multiple supporting signals",            api: "/api/market/hot-picks" },
  { id: "volume-boomers", group: "Scans",        icon: "volume",   label: "Volume Boomers",     hint: "Stocks trading at 3× or more of normal volume",       api: "/api/market/hot-picks" },
  { id: "base-radar",     group: "Scans",        icon: "base",     label: "Base Formation",     hint: "Stocks building a base before a possible move",       api: "/api/market/hot-picks" },
  { id: "momentum",       group: "Scans",        icon: "momentum", label: "Momentum",           hint: "Stocks already moving with volume behind them",       api: "/api/market/hot-picks" },
  { id: "fresh-zones",    group: "Scans",        icon: "zones",    label: "Fresh Zones",        hint: "Untested weekly/monthly/yearly demand & resistance zones near price", api: "/api/fresh-zones/opportunities" },
  { id: "watchlist",      group: "Watch & Risk", icon: "eye",      label: "Watchlist",          hint: "Setups to keep an eye on",                            api: "/api/market/hot-picks" },
  { id: "traps",          group: "Watch & Risk", icon: "alert",    label: "Traps & Risks",      hint: "Fake breakouts and setups to avoid",                  api: "/api/market/traps" },
  { id: "options",        group: "Derivatives",  icon: "options",  label: "Options",            hint: "Index options: OI, IV, max pain",                    api: "/api/market/options-analysis?symbol=NIFTY" },
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

// Internal reason codes (snake_case) and status chatter are not user-facing.
const INTERNAL_TEXT = /^[a-z0-9]+(_[a-z0-9]+)+$|gemini disabled|no key|invalidation level unavailable/i;

// Generic verdict phrases already shown by the card's label; not evidence.
const GENERIC_TEXT = /^(high alignment setup|watchlist setup|may move (lower|if)|needs confirmation)/i;

function cleanReasons(reasons: any): string[] {
  return (Array.isArray(reasons) ? reasons : []).map((r) => String(r || "").trim()).filter((r) => r && !INTERNAL_TEXT.test(r));
}

function reasonPoints(text: string): string[] {
  const seen = new Set<string>();
  return String(text || "")
    .split(/;\s*|\.\s+(?=[A-Z0-9])/)
    .map((part) => part.trim().replace(/\.$/, ""))
    .filter((part) => part.length > 3 && !INTERNAL_TEXT.test(part) && !GENERIC_TEXT.test(part))
    .filter((part) => (seen.has(part.toLowerCase()) ? false : (seen.add(part.toLowerCase()), true)));
}

const BULLISH_EVIDENCE = /higher lows|accumulation|relative strength vs nifty is positive|holding near .*support|demand/i;
const BEARISH_EVIDENCE = /lower highs|distribution|supply|relative strength vs nifty is negative|below .*ema|breakdown/i;

function mixedSignalNote(direction: string, points: string[], labels: string[], demandStatus?: string): string | null {
  const evidence = [...points, ...(labels || []), demandStatus || ""].join(" | ");
  if (direction === "bearish" && BULLISH_EVIDENCE.test(evidence)) {
    return "Mixed signals: bearish label, but some evidence below is bullish. Wait for price to confirm a direction.";
  }
  if (direction === "bullish" && BEARISH_EVIDENCE.test(evidence) && /supply pressure|distribution/i.test(evidence)) {
    return "Mixed signals: bullish label, but supply/distribution evidence is present. Wait for confirmation.";
  }
  return null;
}

// ── Stock Card ───────────────────────────────────────────────────────────────
function StockCard({ item, onSelect, tab }: { item: any; onSelect: (s: string) => void; tab?: string }) {
  if (item.entryRule && item.instrumentRule) {
    return (
      <div className="stock-card" onClick={() => onSelect(item.symbol)} style={{ cursor: "pointer", borderColor: item.status === "WATCH_CONTRACT" ? "var(--orange)" : "var(--border)" }}>
        <div className="stock-card-top">
          <h3>{item.symbol}</h3>
          <span className={`badge ${item.side === "PE" ? "bearish" : "bullish"}`}>{item.actionLabel}</span>
        </div>
        <div className="stock-meta">
          <span className="tag watch">{humanize(item.status)}</span>
          <span className="tag">Score {fmt(item.confidence, 0)}/100</span>
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

  const { change, price, sourceAction, action, direction, entryValue, targetValue, stopValue,
    liveStatus, livePlanValid, inferredReady, riskReward, riskPct, hasLiveRisk, candle, label: cardLabel } = stockCardModel(item, tab);
  const actionBucket = item.dashboardBucket || "";
  const actionClass = (tab === "dashboard" && actionBucket.endsWith("_READY")) || action === "BUY" || action === "REENTRY_BUY" || action === "ENTRY_READY"
    ? "buy"
    : action === "SELL" || actionBucket === "NO_TRADE" ? "sell" : "watch";
  const actionText = tab === "dashboard"
    ? action
    : action === "ENTRY_READY" ? "✅ ENTRY READY" : action === "BUY" || action === "REENTRY_BUY" ? `✅ ${humanize(action)}` : action === "SELL" ? "🛑 SELL" : action === "WATCH" ? "👁️ WATCH" : humanize(action);
  const chartPattern = item.chartPattern ?? item.setupType ?? item.setup_type ?? item.raw?.setup_type;
  const patternLabels = item.patternLabels ?? item.pattern_labels ?? item.raw?.pattern_labels ?? [];
  const entryLabel = item.entryLabel ?? item.entry_label ?? (item.entryMissed || item.entry_missed ? "Retest" : "Entry");
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
  const footprint = item.large_money_footprint || item.raw?.large_money_footprint || {};
  const footprintScore = footprint.score ?? item.large_money_footprint_score ?? item.raw?.large_money_footprint_score;
  const footprintQuality = footprint.data_quality?.grade ?? item.footprint_data_quality?.grade ?? item.raw?.footprint_data_quality?.grade;
  const liveScore = item.liveTriggerScore ?? item.liveConfirmation?.score;
  const liveScoreMeaning = item.liveTriggerScoreMeaning || item.liveConfirmation?.scoreMeaning;
  const liveEvidence = item.liveEvidence || item.liveConfirmation?.evidence || [];
  const fallbackReason = item.reason || item.signal_summary || item.setup_label || item.whyInteresting || item.missed_reason || "";
  const timeHorizon = item.timeHorizon || item.pre_breakout_timeframe || item.timeframe || item.raw?.pre_breakout_timeframe || "";
  const isHighTrap = trapRisk === "high" || item.trap_risk === "high" || item.bull_trap?.bull_trap_detected || (item.trap_warnings && item.trap_warnings.length > 0);
  const relVol = item.relative_volume ?? item.raw?.relative_volume ?? 0;
  const sb = item.score_breakdown || item.raw?.score_breakdown || {};
  const volStreak = sb.volume_streak || 0;
  const volBuilding = sb.vol_building || false;
  const stealthAccum = sb.stealth_accumulation || false;
  const volAccel = sb.volume_acceleration || 1;
  const volPriceDivergence = sb.vol_price_divergence || false;

  const whyPoints = reasonPoints(fallbackReason);
  const mixed = mixedSignalNote(direction, whyPoints, patternLabels, demandStatus);
  const planMissing = (targetValue == null || priceText(targetValue) === "-") && (stopValue == null || priceText(stopValue) === "-");
  const candleUseful = candle && String(candle.status || "UNAVAILABLE").toUpperCase() !== "UNAVAILABLE";
  const ds = Number(demandScore ?? 0), ss = Number(supplyScore ?? 0);
  const dsTotal = ds + ss;
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();

  return (
    <div className="stock-card" onClick={() => onSelect(item.symbol)} style={{ cursor: "pointer", borderColor: isHighTrap ? "var(--red)" : "var(--border)" }}>
      {isHighTrap ? (
        <div className="trap-alert">
          <div className="trap-kicker">Avoid — visible trap evidence</div>
          <div className="trap-desc">
            {item.bull_trap?.bull_trap_detected ? "Fake breakout: price broke resistance but failed to hold." :
             item.trap_warnings?.[0] || "Strong selling-pressure evidence; participant identity is unknown."}
          </div>
        </div>
      ) : null}

      {/* 1 · Identity + price */}
      <div className="stock-card-top">
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <h3>{item.symbol}</h3>
            {cardLabel ? <span className="tag sell">{cardLabel}</span> : null}
            {item.quality_grade ? <span className="tag">Grade {item.quality_grade}</span> : null}
          </div>
          {chartPattern ? <div className="card-subtitle">{humanize(chartPattern)}</div> : null}
        </div>
        <div style={{ textAlign: "right" }}>
          <div className="stock-price" style={{ fontSize: 22 }}>{priceText(price)}</div>
          <span className={`stock-change ${change == null ? "" : change >= 0 ? "up" : "down"}`}>{change == null ? "—" : fmtPct(change)}</span>
        </div>
      </div>

      {/* 2 · Verdict */}
      <div className="stock-meta" style={{ marginTop: 10 }}>
        <span className={`badge ${isHighTrap ? "bearish" : direction}`}>
          {isHighTrap ? "Avoid" : direction === "bullish" ? "▲ Bullish" : direction === "bearish" ? "▼ Bearish" : humanize(direction)}
        </span>
        <span className={`tag ${actionClass}`}>{actionText}</span>
        {timeHorizon ? <span className="tag">{timeHorizon}</span> : null}
      </div>
      {mixed ? <div className="card-warning">{mixed}</div> : null}
      {tab === "dashboard" && item.dashboardReason ? <p className="card-verdict">{item.dashboardReason}</p> : null}

      {/* 3 · Key numbers */}
      <div className="metric-strip">
        <div><span>Alignment</span><strong>{item.confidence != null || item.score != null ? `${fmt(item.confidence ?? item.score, 0)}/100` : "-"}</strong></div>
        <div><span>Volume</span><strong style={{ color: relVol >= 3 ? "var(--green)" : undefined }}>{relVol > 0 ? `${fmt(relVol, 1)}×` : "-"}</strong></div>
        <div>
          <span>Demand/Supply</span>
          {dsTotal > 0 ? (
            <div className="ds-bar" title={`Demand ${fmt(ds, 0)} · Supply ${fmt(ss, 0)}`}>
              <i style={{ width: `${(ds / dsTotal) * 100}%` }} />
            </div>
          ) : <strong>-</strong>}
          {dsTotal > 0 ? <small>{fmt(ds, 0)} / {fmt(ss, 0)}</small> : null}
        </div>
      </div>

      {/* 4 · Why */}
      {whyPoints.length ? (
        <ul className="card-why">
          {whyPoints.slice(0, 4).map((point, i) => <li key={i}>{point}</li>)}
        </ul>
      ) : null}
      {item.entry_plan_blocked ? (
        <div className="card-warning">Watch only — {item.entry_plan_blocked_reason || "the structural stop/target does not justify an entry yet."}</div>
      ) : null}

      {/* 5 · Plan */}
      <div className="stock-targets">
        <div><span>{entryLabel}</span><strong>{priceText(entryValue)}</strong></div>
        <div><span>Target</span><strong>{priceText(targetValue)}</strong></div>
        <div><span>Stop</span><strong>{priceText(stopValue)}</strong></div>
      </div>
      {planMissing ? <p className="card-note">No stop or target yet — watch only, don’t trade without a stop.</p> : null}
      {riskReward != null && riskReward > 0 ? (
        <div className="stock-meta" style={{ marginTop: 8 }}>
          <span className="tag">{hasLiveRisk ? "Live " : ""}Risk:reward 1:{fmt(riskReward, 1)}</span>
          {riskPct != null && riskPct > 0 ? <span className="tag">Stop risk {fmt(riskPct, 2)}%</span> : null}
        </div>
      ) : null}

      {/* 6 · Everything else, folded */}
      <details className="card-details" onClick={stop}>
        <summary>Details</summary>
        <div className="stock-meta" style={{ marginTop: 8 }}>
          {patternLabels.slice(0, 4).map((label: string) => <span key={label} className="tag">{humanize(label)}</span>)}
          {volBuilding ? <span className="tag buy">Volume building {volStreak}d</span> : null}
          {stealthAccum ? <span className="tag">Absorption proxy</span> : null}
          {volAccel >= 1.5 ? <span className="tag">Volume accelerating</span> : null}
          {volPriceDivergence ? <span className="tag">Price-volume divergence</span> : null}
          {footprintScore != null && Math.abs(footprintScore) >= 20 ? (
            <span className="tag">Large-money footprint {footprintScore > 0 ? "+" : ""}{fmt(footprintScore, 0)}/100{footprintQuality ? ` · ${humanize(footprintQuality)} data` : ""}</span>
          ) : null}
          {item.seller_pressure ? <span className="tag">{humanize(item.seller_pressure)} sellers</span> : null}
          {item.profit_booking_risk ? <span className="tag">{humanize(item.profit_booking_risk)} booking risk</span> : null}
          {item.mtfRiskFit ? <span className="tag buy">MTF risk fit · broker check</span> : null}
          {item.return60d != null && Math.abs(item.return60d) >= 20 ? <span className="tag">60-session move {fmtPct(item.return60d)}</span> : null}
          {item.maxPositionPctAt1PctAccountRisk != null ? <span className="tag">1% risk cap: max {fmt(item.maxPositionPctAt1PctAccountRisk, 1)}% capital</span> : null}
        </div>
        {(demandStatus || smartMoneyRead) ? (
          <div className="card-section">
            <div className="stock-meta">
              {demandStatus ? <span className={`tag ${demandTone(demandStatus, trapRisk)}`}>{humanize(demandStatus)}</span> : null}
              {trapRisk ? <span className="tag">Trap risk {humanize(trapRisk)}{trapRiskScore != null ? ` ${fmt(trapRiskScore, 0)}` : ""}</span> : null}
            </div>
            {smartMoneyRead ? <p className="stock-reason">{smartMoneyRead}</p> : null}
          </div>
        ) : null}
        {candleUseful ? (
          <div className="card-section">
            <div className="stock-meta">
              <span className={`tag ${candle.status === "READY" && candle.entry_ready ? "buy" : candle.status === "INVALID" ? "sell" : "watch"}`}>Candle setup {humanize(candle.status)}</span>
              {candle.higher_timeframe_trend ? <span className="tag">Higher TF: {humanize(candle.higher_timeframe_trend)}</span> : null}
              {candle.timeframe ? <span className="tag">{candle.timeframe}</span> : null}
            </div>
            {(candle.pattern || candle.phase) ? <p className="stock-reason">{[candle.pattern, candle.phase].filter(Boolean).map(humanize).join(" · ")}</p> : null}
            {cleanReasons(candle.reasons).slice(0, 2).map((reason: string, index: number) => <p className="stock-reason" key={index}>• {reason}</p>)}
            {(candle.trigger != null || candle.invalidation != null) ? (
              <div className="stock-targets">
                <div><span>Candle trigger</span><strong>{priceText(candle.trigger)}</strong></div>
                <div><span>Invalidation</span><strong>{priceText(candle.invalidation)}</strong></div>
                {candle.target != null ? <div><span>Setup target</span><strong>{priceText(candle.target)}</strong></div> : null}
              </div>
            ) : null}
          </div>
        ) : null}
        {liveStatus ? (
          <div className="card-section">
            <div className="stock-meta">
              <span className={`tag ${liveStatus === "CONFIRMED" ? "buy" : liveStatus === "REJECT" ? "sell" : "watch"}`}>Live gate {humanize(liveStatus)}</span>
              {liveScore != null ? <span className="tag">Evidence {fmt(liveScore, 0)}/100</span> : null}
            </div>
            {liveScoreMeaning ? <p className="stock-reason">{liveScoreMeaning}</p> : null}
            {liveStatus === "CONFIRMED" && !livePlanValid ? <p className="stock-reason" style={{ color: "var(--orange)" }}>Live conditions passed, but there is no valid entry/stop/target yet — not an entry signal on its own.</p> : null}
            {inferredReady ? <p className="stock-reason">Entry conditions and risk levels are available; verify the live quote before trading.</p> : null}
            {cleanReasons(liveEvidence).slice(0, 2).map((reason: string, index: number) => <p className="stock-reason" key={index}>• {reason}</p>)}
          </div>
        ) : null}
        {hasAi ? (
          <div className="card-section">
            <div className="stock-meta">
              <span className="tag">Gemini</span>
              {aiActionLabel ? <span className={`tag ${aiActionClass(aiActionLabel)}`}>{humanize(aiActionLabel)}</span> : null}
              {aiScore ? <span className="tag">AI {fmt(aiScore, 0)}%</span> : null}
            </div>
            {aiReason ? <p className="stock-reason">{aiReason}</p> : null}
            {aiTradePlan ? <p className="ai-plan">{aiTradePlan}</p> : null}
            {aiRisks.slice(0, 2).map((risk: string, index: number) => <p className="stock-reason" key={index}>⚠️ {risk}</p>)}
          </div>
        ) : null}
        {item.newsHeadlines?.length ? <p className="stock-reason">📰 {item.newsHeadlines[0]}</p> : null}
        {(item.aiWarnings?.[0] || item.chase_warnings?.[0]) ? <p className="stock-reason" style={{ color: "var(--orange)" }}>⚠️ {item.aiWarnings?.[0] || item.chase_warnings?.[0]}</p> : null}
        {item.newsSentiment && item.newsSentiment !== "unknown" ? (
          <div className="stock-meta">
            <span className={`tag ${item.newsSentiment === "bullish" ? "buy" : item.newsSentiment === "bearish" ? "sell" : "watch"}`}>News: {item.newsSentiment}</span>
            {item.insiderSignal && !["unknown", "neutral"].includes(item.insiderSignal) ? <span className="tag">{item.insiderSignal === "insider_buying" ? "Insider buy" : "Insider sell"}</span> : null}
            {item.analystConsensus && item.analystConsensus !== "unknown" ? <span className="tag">Analyst: {item.analystConsensus}</span> : null}
          </div>
        ) : null}
        <p className="card-note">Rule-based setup classification — not a success probability or a trade recommendation.</p>
      </details>
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
        <div className="stat-card"><span>Alignment</span><strong>{fmt(s.confidence, 0)}/100</strong></div>
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
// Smart Options Intelligence Panel
function SmartOptionsPanel() {
  const [activeSmartIndex, setActiveSmartIndex] = useState("NIFTY");
  const [smartData, setSmartData] = useState<Record<string, any>>({});
  const [fnoData, setFnoData] = useState<any>(null);
  const [loadingSmart, setLoadingSmart] = useState(false);
  const [loadingFno, setLoadingFno] = useState(false);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const INDEX_CONFIG: Record<string, { label: string; weekday: string }> = {
    NIFTY: { label: "Nifty 50", weekday: "Tue" },
    BANKNIFTY: { label: "Bank Nifty", weekday: "Monthly" },
    FINNIFTY: { label: "Fin Nifty", weekday: "Monthly" },
    MIDCPNIFTY: { label: "Midcap", weekday: "Monthly" },
  };
  const INDEX_SMART = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"];
  const fetchSmart = async (sym: string) => {
    setLoadingSmart(true);
    try {
      const r = await fetch(`${API_URL}/api/market/smart-options?symbol=${sym}`, { cache: "no-store" });
      if (r.ok) {
        const json = await r.json();
        setSmartData(prev => ({ ...prev, [sym]: json }));
        setLastUpdate(new Date());
      }
    } catch {}
    setLoadingSmart(false);
  };
  const fetchFno = async () => {
    setLoadingFno(true);
    try {
      const r = await fetch(`${API_URL}/api/market/fno-options-scan`, { cache: "no-store" });
      if (r.ok) setFnoData(await r.json());
    } catch {}
    setLoadingFno(false);
  };
  useEffect(() => {
    fetchSmart(activeSmartIndex);
    INDEX_SMART.forEach(sym => {
      if (sym !== activeSmartIndex) {
        fetch(`${API_URL}/api/market/smart-options?symbol=${sym}`, { cache: "no-store" })
          .then(r => (r.ok ? r.json() : null))
          .then(json => {
            if (json) setSmartData(prev => ({ ...prev, [sym]: json }));
          })
          .catch(() => {});
      }
    });
    const t = setInterval(() => fetchSmart(activeSmartIndex), 60000);
    return () => clearInterval(t);
  }, [activeSmartIndex]);
  useEffect(() => {
    fetchFno();
    const t = setInterval(fetchFno, 180000);
    return () => clearInterval(t);
  }, []);
  const sd = smartData[activeSmartIndex];
  const ss = sd?.smart_signals;
  const vc = (d: string) => d === "bullish" ? "var(--green)" : d === "bearish" ? "var(--red)" : "var(--orange)";
  const confBar = (c: number) => (
    <div style={{ height: 5, borderRadius: 3, background: "var(--border)", marginTop: 4, overflow: "hidden" }}>
      <div style={{ height: "100%", width: `${Math.min(c, 100)}%`, background: c >= 65 ? "var(--green)" : c >= 40 ? "var(--orange)" : "var(--red)", transition: "width 0.5s" }} />
    </div>
  );
  return (
    <div style={{ marginBottom: 28 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 8 }}>
        <h2 className="section-title" style={{ margin: 0 }}>Index options intelligence</h2>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {lastUpdate && <span style={{ fontSize: 11, color: "var(--muted)" }}>1-min refresh · {lastUpdate.toLocaleTimeString()}</span>}
          <button onClick={() => { fetchSmart(activeSmartIndex); fetchFno(); }} className="refresh-btn" disabled={loadingSmart} style={{ fontSize: 12, padding: "4px 10px" }}>{loadingSmart ? "⟳" : "↻ Now"}</button>
        </div>
      </div>
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 12 }}>
        📡 OI velocity · IV skew · gamma · max pain — <strong style={{ color: "var(--red)" }}>Delivery excluded</strong>, options/intraday only
      </div>
      <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
        {INDEX_SMART.map(sym => {
          const cfg = INDEX_CONFIG[sym] || { label: sym, weekday: "" };
          const itemData = smartData[sym];
          const isToday = itemData?.is_expiry_today;
          const isTmrw = itemData?.days_to_expiry === 1;
          const dte = itemData?.days_to_expiry;
          return (
            <button
              key={sym}
              className={`options-index-tab ${activeSmartIndex === sym ? "active" : ""}`}
              onClick={() => setActiveSmartIndex(sym)}
              style={{
                fontSize: 13,
                padding: "6px 14px",
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                borderColor: isToday ? "rgba(239,68,68,0.7)" : undefined,
              }}
            >
              <span>{cfg.label}</span>
              <span
                style={{
                  fontSize: 10,
                  padding: "1px 5px",
                  borderRadius: 4,
                  fontWeight: 700,
                  background: isToday
                    ? "rgba(239,68,68,0.25)"
                    : isTmrw
                    ? "rgba(245,158,11,0.25)"
                    : "rgba(255,255,255,0.08)",
                  color: isToday
                    ? "#ef4444"
                    : isTmrw
                    ? "#f59e0b"
                    : "var(--muted)",
                  border: isToday
                    ? "1px solid rgba(239,68,68,0.4)"
                    : undefined,
                }}
              >
                {isToday
                  ? "🔥 TODAY"
                  : isTmrw
                  ? "⚡ TMRW"
                  : dte !== undefined
                  ? `${cfg.weekday} (${dte}d)`
                  : cfg.weekday}
              </span>
            </button>
          );
        })}
      </div>
      {loadingSmart && !ss ? (
        <div className="empty-msg">⟳ Loading {activeSmartIndex} signals…</div>
      ) : ss ? (
        <>
          <div style={{ background: "rgba(20,20,30,0.95)", border: `2px solid ${vc(ss.verdict?.direction)}`, borderRadius: 10, padding: 16, marginBottom: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.1em" }}>Combined Verdict · {activeSmartIndex}</span>
                  {sd?.expiry_date && (
                    <span
                      style={{
                        fontSize: 11,
                        padding: "2px 8px",
                        borderRadius: 4,
                        fontWeight: 700,
                        background: sd.is_expiry_today
                          ? "rgba(239,68,68,0.2)"
                          : sd.days_to_expiry === 1
                          ? "rgba(245,158,11,0.2)"
                          : "rgba(255,255,255,0.08)",
                        color: sd.is_expiry_today
                          ? "var(--red)"
                          : sd.days_to_expiry === 1
                          ? "var(--orange)"
                          : "#93c5fd",
                        border: `1px solid ${
                          sd.is_expiry_today
                            ? "var(--red)"
                            : sd.days_to_expiry === 1
                            ? "var(--orange)"
                            : "rgba(147,197,253,0.3)"
                        }`,
                      }}
                    >
                      {sd.is_expiry_today
                        ? `🔥 EXPIRY TODAY (${sd.expiry_date})`
                        : sd.days_to_expiry === 1
                        ? `⚡ Expiry Tomorrow (${sd.expiry_date})`
                        : `📅 ${sd.expiry_day_name || ""} Expiry: ${sd.expiry_date} (${sd.days_to_expiry}d left)`}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 20, fontWeight: 800, color: vc(ss.verdict?.direction) }}>{ss.verdict?.action_label || ss.verdict?.action || "WAIT"}</div>
                <div style={{ fontSize: 13, color: "var(--muted)", marginTop: 4 }}>{ss.verdict?.reason}</div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: 11, color: "var(--muted)" }}>Confidence</div>
                <div style={{ fontSize: 28, fontWeight: 900, color: vc(ss.verdict?.direction) }}>{ss.verdict?.confidence ?? 0}%</div>
                {confBar(ss.verdict?.confidence ?? 0)}
              </div>
            </div>
            {ss.verdict?.reasons?.length > 0 && (
              <div style={{ marginTop: 10 }}>{ss.verdict.reasons.map((r: string, i: number) => <div key={i} style={{ fontSize: 12, color: "var(--muted)", marginTop: 3 }}>• {r}</div>)}</div>
            )}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 12, marginBottom: 16 }}>
            <div className="demand-panel" style={{ borderColor: vc(ss.oi_velocity?.direction || "neutral") }}>
              <div style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", marginBottom: 8 }}>⚡ OI Velocity (1-min)</div>
              {ss.oi_velocity?.available ? (
                <>
                  <div style={{ fontSize: 14, fontWeight: 700, color: vc(ss.oi_velocity.direction) }}>{ss.oi_velocity.signal}</div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 3 }}>{ss.oi_velocity.label}</div>
                  {confBar(ss.oi_velocity.confidence ?? 0)}
                  <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 5 }}>CE +{fmt(ss.oi_velocity.total_ce_oi_added)} | PE +{fmt(ss.oi_velocity.total_pe_oi_added)}</div>
                  {ss.oi_velocity.top_strikes?.slice(0, 3).map((s: any, i: number) => (
                    <div key={i} style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>Strike {s.strike}: {s.net_activity === "PE_BUILD" ? "🔴 PE" : "🟢 CE"} +{fmt(Math.abs(s.pe_oi_added || s.ce_oi_added))}</div>
                  ))}
                  <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 4 }}>{ss.oi_velocity.snapshots_used} snaps · {ss.oi_velocity.elapsed_minutes}min</div>
                </>
              ) : <div style={{ fontSize: 12, color: "var(--muted)" }}>Building 1-min buffer ({ss.oi_velocity?.snapshots_held ?? 0}/2)…</div>}
            </div>
            <div className="demand-panel" style={{ borderColor: vc(ss.iv_skew?.direction || "neutral") }}>
              <div style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", marginBottom: 8 }}>📐 IV Skew</div>
              {ss.iv_skew?.available ? (
                <>
                  <div style={{ fontSize: 14, fontWeight: 700, color: vc(ss.iv_skew.direction) }}>{ss.iv_skew.signal?.replace(/_/g, " ")}</div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 3 }}>{ss.iv_skew.label}</div>
                  {confBar(ss.iv_skew.confidence ?? 0)}
                  <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 5 }}>CE IV: {ss.iv_skew.avg_ce_iv_pct}% · PE IV: {ss.iv_skew.avg_pe_iv_pct}%</div>
                  <div style={{ fontSize: 11, color: "var(--muted)" }}>Ratio: <strong style={{ color: "#fff" }}>{ss.iv_skew.skew_ratio}</strong> · ATM: {ss.iv_skew.atm_strike}</div>
                </>
              ) : <div style={{ fontSize: 12, color: "var(--muted)" }}>No IV data: {ss.iv_skew?.reason}</div>}
            </div>
            <div className="demand-panel" style={{ borderColor: ss.gamma_exposure?.squeeze_imminent ? "var(--orange)" : "var(--border)" }}>
              <div style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", marginBottom: 8 }}>🌀 Gamma Exposure</div>
              {ss.gamma_exposure?.available ? (
                <>
                  {ss.gamma_exposure.squeeze_imminent && <div style={{ fontSize: 12, fontWeight: 700, color: "var(--orange)", marginBottom: 4 }}>🚨 SQUEEZE IMMINENT</div>}
                  <div style={{ fontSize: 12, color: "var(--muted)" }}>{ss.gamma_exposure.label}</div>
                  {ss.gamma_exposure.flip_point && <div style={{ fontSize: 12, color: "var(--yellow)", marginTop: 6 }}>🔀 Flip: <strong>{ss.gamma_exposure.flip_point}</strong></div>}
                  {ss.gamma_exposure.high_gamma_strikes?.slice(0, 3).map((s: any, i: number) => (
                    <div key={i} style={{ fontSize: 11, color: s.squeeze_probability === "high" ? "var(--orange)" : "var(--muted)", marginTop: 3 }}>
                      {s.squeeze_probability === "high" ? "🔥" : "🟡"} {s.strike} ({s.distance_pct}% away)
                    </div>
                  ))}
                </>
              ) : <div style={{ fontSize: 12, color: "var(--muted)" }}>No Greek data</div>}
            </div>
            <div className="demand-panel" style={{ borderColor: ss.max_pain_gravity?.pin_probability === "very_high" ? "var(--orange)" : ss.max_pain_gravity?.pin_probability === "high" ? "var(--yellow)" : "var(--border)" }}>
              <div style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", marginBottom: 8 }}>🧲 Max Pain</div>
              {ss.max_pain_gravity?.available ? (
                <>
                  <div style={{ fontSize: 24, fontWeight: 800, color: "#fff" }}>₹{fmt(ss.max_pain_gravity.max_pain)}</div>
                  <div style={{ fontSize: 11, color: "var(--muted)" }}>Band: ₹{fmt(ss.max_pain_gravity.gravity_band?.[0])} – ₹{fmt(ss.max_pain_gravity.gravity_band?.[1])}</div>
                  <div style={{ fontSize: 12, color: ss.max_pain_gravity.pin_probability === "very_high" ? "var(--orange)" : "var(--yellow)", marginTop: 5, fontWeight: 600 }}>{ss.max_pain_gravity.pin_label}</div>
                  <div style={{ fontSize: 11, color: vc(ss.max_pain_gravity.gravity_direction), marginTop: 3 }}>{ss.max_pain_gravity.gravity_label}</div>
                  <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 3 }}>
                    Dist: {ss.max_pain_gravity.distance_pct}% · {sd?.expiry_date ? `${sd.expiry_date} (${sd.days_to_expiry === 0 ? "🔥 Today" : sd.days_to_expiry === 1 ? "⚡ Tomorrow" : `${sd.days_to_expiry}d expiry`})` : `${ss.max_pain_gravity.days_to_expiry}d expiry`}
                  </div>
                </>
              ) : <div style={{ fontSize: 12, color: "var(--muted)" }}>Max pain unavailable</div>}
            </div>
          </div>
        </>
      ) : !loadingSmart && <div className="empty-msg">Smart signals loading…</div>}
      <div style={{ marginTop: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <h3 style={{ margin: 0, fontSize: 15, color: "#fff" }}>📋 F&O Stock Options Scan <span style={{ fontSize: 11, color: "var(--red)", fontWeight: 400 }}>(NO delivery)</span></h3>
          {loadingFno && <span style={{ fontSize: 11, color: "var(--muted)", animation: "pulse 1s infinite" }}>⟳</span>}
        </div>
        {fnoData ? (
          <>
            <div style={{ fontSize: 12, marginBottom: 10, display: "flex", gap: 8, flexWrap: "wrap" }}>
              <span className={`tag ${fnoData.nifty_gate?.side === "bullish" ? "buy" : fnoData.nifty_gate?.side === "bearish" ? "sell" : "watch"}`}>Gate: {fnoData.nifty_gate?.status?.replace(/_/g, " ")}</span>
              <span className="tag">Scanned: {fnoData.total_fno_scanned} F&O stocks</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
              <div>
                <div style={{ fontSize: 13, color: "var(--green)", fontWeight: 700, marginBottom: 8 }}>📈 CE Candidates</div>
                {fnoData.ce_candidates?.length ? fnoData.ce_candidates.slice(0, 8).map((c: any, i: number) => (
                  <div key={i} style={{ background: "rgba(34,197,94,0.07)", border: "1px solid rgba(34,197,94,0.2)", borderRadius: 8, padding: "8px 10px", marginBottom: 7 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <strong style={{ color: "#fff" }}>{c.symbol}</strong>
                      <div style={{ display: "flex", gap: 5, alignItems: "center" }}>
                        {c.expiry_date && <span style={{ fontSize: 10, color: "#93c5fd", background: "rgba(59,130,246,0.15)", padding: "1px 5px", borderRadius: 4 }}>Exp {c.expiry_date}</span>}
                        <span className="tag buy" style={{ fontSize: 10 }}>CE WATCH</span>
                      </div>
                    </div>
                    <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 3 }}>₹{fmt(c.underlying_price)} · Trigger: ₹{fmt(c.underlying_trigger)}</div>
                    <div style={{ fontSize: 11, color: "var(--muted)" }}>SL: ₹{fmt(c.underlying_invalidation)} · Tgt: ₹{fmt(c.underlying_structure_target)}</div>
                    {c.strategy && <div style={{ fontSize: 11, color: "var(--green)", marginTop: 2 }}>{c.strategy}</div>}
                  </div>
                )) : <div style={{ fontSize: 12, color: "var(--muted)" }}>No CE setups right now</div>}
              </div>
              <div>
                <div style={{ fontSize: 13, color: "var(--red)", fontWeight: 700, marginBottom: 8 }}>📉 PE Candidates</div>
                {fnoData.pe_candidates?.length ? fnoData.pe_candidates.slice(0, 8).map((c: any, i: number) => (
                  <div key={i} style={{ background: "rgba(239,68,68,0.07)", border: "1px solid rgba(239,68,68,0.2)", borderRadius: 8, padding: "8px 10px", marginBottom: 7 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <strong style={{ color: "#fff" }}>{c.symbol}</strong>
                      <div style={{ display: "flex", gap: 5, alignItems: "center" }}>
                        {c.expiry_date && <span style={{ fontSize: 10, color: "#93c5fd", background: "rgba(59,130,246,0.15)", padding: "1px 5px", borderRadius: 4 }}>Exp {c.expiry_date}</span>}
                        <span className="tag sell" style={{ fontSize: 10 }}>PE WATCH</span>
                      </div>
                    </div>
                    <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 3 }}>₹{fmt(c.underlying_price)} · Trigger: ₹{fmt(c.underlying_trigger)}</div>
                    <div style={{ fontSize: 11, color: "var(--muted)" }}>SL: ₹{fmt(c.underlying_invalidation)} · Tgt: ₹{fmt(c.underlying_structure_target)}</div>
                    {c.strategy && <div style={{ fontSize: 11, color: "var(--red)", marginTop: 2 }}>{c.strategy}</div>}
                  </div>
                )) : <div style={{ fontSize: 12, color: "var(--muted)" }}>No PE setups right now</div>}
              </div>
            </div>
            <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 8, padding: "6px 10px", background: "rgba(255,165,0,0.07)", borderRadius: 6 }}>⚠️ {fnoData.note}</div>
          </>
        ) : loadingFno ? <div className="empty-msg">⟳ Scanning…</div> : <div className="empty-msg">FNO scan unavailable</div>}
      </div>
    </div>
  );
}
function OptionsDashboard({ hotPicksData }: { hotPicksData: any }) {
  const [activeIndex, setActiveIndex] = useState("NIFTY");
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [lastFetch, setLastFetch] = useState<Date | null>(null);

  const INDEX_LIST = [
    { key: "NIFTY",      label: "Nifty 50 (Tue)" },
    { key: "BANKNIFTY",  label: "Bank Nifty (Monthly)" },
    { key: "FINNIFTY",   label: "Fin Nifty (Monthly)" },
    { key: "MIDCPNIFTY", label: "Midcap (Monthly)" },
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

  // Stock-option watch ideas from strongly aligned underlying setups.
  const hotPicks = hotPicksData?.hotPicks || hotPicksData?.top_opportunities || [];
  const stockOptions = hotPicks.filter((s: any) => (s.confidence ?? s.score ?? 0) >= 75 && s.direction && s.direction !== "neutral");

  return (
    <div className="options-dashboard">
      <SmartOptionsPanel />
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
          {data?.source_timestamps?.option_chain
            ? `Chain snapshot: ${new Date(data.source_timestamps.option_chain).toLocaleString()}`
            : lastFetch ? `Fetched: ${lastFetch.toLocaleTimeString()}` : ""}
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
              <p className="options-rec-reason">{rec?.reason || "Rule alignment is too low or data is unavailable. Stay out until the evidence is clear."}</p>
            ) : (
              <>
                <div className="options-rec-main">
                  <div>
                    <div className="options-rec-strategy">{rec?.strategy}</div>
                    <div style={{ fontSize: 13, color: "var(--muted)", marginTop: 6 }}>
                      🎯 Rule score: <strong style={{ color: "#fff" }}>{rec?.confidence}/100</strong>
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
                      <span>Invalidation (underlying)</span>
                      <strong style={{ color: "var(--red)" }}>₹{fmt(rec?.underlying_invalidation)}</strong>
                    </div>
                    <div>
                      <span>Price-structure target</span>
                      <strong style={{ color: "var(--green)" }}>₹{fmt(rec?.underlying_structure_target)}</strong>
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
              <h2 className="section-title">📋 Stock Options Watchlist (from Underlying Setups)</h2>
              <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 16 }}>
                These underlying setups have alignment ≥75/100. Option entry still needs live chain, spread, liquidity and premium confirmation:
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
                          {isBull ? "📈 CE WATCH" : "📉 PE WATCH"}
                        </span>
                      </div>
                      <div style={{ fontSize: 13, color: "var(--muted)" }}>
                        Stock at <strong style={{ color: "#fff" }}>₹{fmt(price)}</strong> — ATM Strike ~<strong style={{ color: "#fff" }}>{atmStrike}</strong>
                      </div>
                      <div className="stock-meta">
                        <span className={`tag ${isBull ? "buy" : "sell"}`}>
                          {isBull ? `Watch ${stock.symbol} ${atmStrike} CE` : `Watch ${stock.symbol} ${atmStrike} PE`}
                        </span>
                        <span className="tag">🎯 Alignment {fmt(conf, 0)}/100</span>
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
  if (tab === "dashboard") {
    const list = data.hotPicks || [];
    if (list.length >= 4) return list;
    const backfill = [
      ...list,
      ...(data.watchlist || []),
      ...(data.momentumRadar || []),
      ...(data.baseFormationRadar || []),
      ...(data.volumeBoomers || []),
      ...(data.top_opportunities || []),
    ];
    const seen = new Set();
    return backfill.filter((item: any) => {
      if (!item?.symbol || seen.has(item.symbol)) return false;
      seen.add(item.symbol);
      return true;
    }).slice(0, 16);
  }
  if (tab === "hot-picks") {
    const items = data.hotPicks || data.top_opportunities || [];
    const filtered = items.filter((item: any) => {
      const raw = item.raw || {};
      const patternLabels = item.patternLabels || [];
      const demandStatus = item.demandStatus || "";
      const smartMoneyRead = item.smartMoneyRead || "";
      const hasVCP = (raw.tight_consolidation_pct ?? 100) < 4 || (raw.bb_width_ratio ?? 1) < 0.15 || patternLabels.some((l: string) => l.toLowerCase().includes('squeeze') || l.toLowerCase().includes('base'));
      const hasRS = (raw.relative_strength_delta_5d ?? 0) > 3 || (raw.rs_rating ?? 0) > 70 || (raw.relative_strength ?? 0) > 1;
      const hasIceberg = demandStatus === "strong_accumulation" || item.seller_pressure === "low" || smartMoneyRead.toLowerCase().includes("accumulation");
      const hasSector = (raw.sector_momentum ?? 0) > 0 || raw.sector_strength === "strong";
      const hasOptions = (raw.options_activity === "bullish" || (raw.put_oi_chg ?? 0) > (raw.call_oi_chg ?? 0) * 1.5);
      return (hasVCP || hasRS || hasIceberg || hasSector || hasOptions) && (item.confidence ?? item.score ?? 0) >= 65;
    });
    return filtered.length > 0 ? filtered : items;
  }
  if (tab === "volume-boomers") return data.volumeBoomers || (data.hotPicks || data.top_opportunities || []).filter((x: any) => (x.relative_volume ?? x.raw?.relative_volume ?? 1) >= 3);
  if (tab === "live-action") {
    // Execution tab: render only fresh broker-tick confirmations. WAIT/REJECT
    // remain available in the diagnostic counters above the cards.
    return data.entries || data.liveAction || [];
  }
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

        // Keep lightweight polling active for WAIT/REJECT assessment changes;
        // WebSocket pushes remain useful for instant CONFIRMED alerts.
        ws.onopen = () => {};
        ws.onmessage = (event) => {
          try {
            const json = JSON.parse(event.data);
            setLiveEntryCount(json.count || 0);
            setDataByTab(prev => ({ ...prev, "live-action": json }));
          } catch {}
        };
        ws.onclose = () => {
          setTimeout(connectWebSocket, 10000);
        };
        ws.onerror = () => { ws?.close(); };
      } catch {
        // Polling is already active and safely covers WebSocket outages.
      }
    };

    pollLiveEntries();
    pollInterval = setInterval(pollLiveEntries, 15000);
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

    // Options and Fresh Zones tabs have their own internal fetch — skip here
    if (tabId === "options" || tabId === "fresh-zones") {
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
  const activeTab = TABS.find(t => t.id === tab) || TABS[0];
  const stats = summary?.summary || data?.summary || {};
  const assessmentCounts = data?.assessmentCounts || {};

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
        <div className="sidebar-logo"><span className="logo-mark">B</span>BullAlways</div>
        <form onSubmit={handleSearch}>
          <input
            className="search-box"
            placeholder="Search a stock (e.g. TCS)"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </form>
        {TABS.map((t, i) => (
          <div key={t.id}>
            {i === 0 || TABS[i - 1].group !== t.group ? <div className="sidebar-section">{t.group}</div> : null}
            <button className={`sidebar-item ${tab === t.id ? "active" : ""}`}
              onClick={() => { setTab(t.id); setSelectedSymbol(null); setActiveFilter("all"); }}
              title={t.hint}>
              <Icon name={t.icon} />
              {t.label}
              {t.id === "live-action" && liveEntryCount > 0 && (
                <span className="sidebar-count">{liveEntryCount}</span>
              )}
            </button>
          </div>
        ))}
        <div className="sidebar-section">Portfolio</div>
        <a href="/performance" className="sidebar-item" style={{ textDecoration: "none" }}>
          <Icon name="briefcase" />
          Performance Tracker
        </a>
      </aside>

      <div style={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
        {/* ── Index Ticker Bar ── */}
        <IndexTickerBar />

        <main className="main-content">
          <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", flexWrap: "wrap", gap: 12 }}>
            <div>
              <h1>{selectedSymbol || activeTab.label}</h1>
              <p>
                {selectedSymbol ? "Stock detail" : activeTab.hint}
                {!selectedSymbol && (data?.generated_at || data?.lastUpdated)
                  ? ` · Updated ${new Date(data.generated_at || data.lastUpdated).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
                  : ""}
              </p>
            </div>
            <div className="page-actions">
              {refreshing ? <span className="updating">Updating…</span> : null}
              {mood && mood !== "loading" ? <span className={`mood-badge ${mood}`} title="Overall market mood">Market: {humanize(mood)}</span> : null}
              {tab !== "fresh-zones" ? (
                <button className="refresh-btn btn-secondary" onClick={() => load(tab, true)} disabled={loading || refreshing}>
                  {loading ? "Loading…" : "Refresh"}
                </button>
              ) : null}
            </div>
          </div>

          {tab === "options" ? (
            <OptionsDashboard hotPicksData={dashboardData} />
          ) : selectedSymbol ? (
            <StockDetail symbol={selectedSymbol} onClose={() => setSelectedSymbol(null)} />
          ) : tab === "fresh-zones" ? (
            <FreshZonesPanel onSelect={setSelectedSymbol} />
          ) : (
            <>
              <div className="stats-row">
                <div className="stat-card"><span>Stocks scanned</span><strong>{stats.totalScanned ?? stats.total_scanned_universe ?? "..."}</strong></div>
                <div className="stat-card"><span>Top setups</span><strong style={{ color: "var(--green)" }}>{stats.highConfidence ?? stats.high_priority ?? 0}</strong></div>
                <div className="stat-card"><span>Breakouts</span><strong style={{ color: "var(--yellow)" }}>{stats.breakouts ?? stats.breakout_count ?? 0}</strong></div>
                <div className="stat-card"><span>Bullish</span><strong style={{ color: "var(--green)" }}>{stats.bullish ?? stats.bullish_setups ?? 0}</strong></div>
                <div className="stat-card"><span>Bearish</span><strong style={{ color: "var(--red)" }}>{stats.bearish ?? stats.bearish_risk_count ?? 0}</strong></div>
                <div className="stat-card"><span>In this list</span><strong>{liveItems.length}</strong></div>
              </div>

              {tab === "live-action" ? (
                <div className="stats-row">
                  <div className="stat-card"><span>Live pool</span><strong>{data?.watchedCount ?? 0}/100</strong></div>
                  <div className="stat-card"><span>Confirmed</span><strong style={{ color: "var(--green)" }}>{assessmentCounts.CONFIRMED ?? data?.count ?? 0}</strong></div>
                  <div className="stat-card"><span>Waiting for evidence</span><strong style={{ color: "var(--yellow)" }}>{assessmentCounts.WAIT ?? 0}</strong></div>
                  <div className="stat-card"><span>Rejected now</span><strong style={{ color: "var(--red)" }}>{assessmentCounts.REJECT ?? 0}</strong></div>
                </div>
              ) : null}

              <div className="chip-row">
                {([
                  { key: "all",       label: "All",             count: allLiveItems.length },
                  { key: "bullish",   label: "Bullish",       count: allLiveItems.filter((x: any) => x.direction === "bullish").length },
                  { key: "bearish",   label: "Bearish",       count: allLiveItems.filter((x: any) => x.direction === "bearish").length },
                  { key: "breakout",  label: "Breakout",      count: allLiveItems.filter((x: any) => x.entry_label?.includes("BREAKOUT") || x.has_breakout).length },
                  { key: "high-conf", label: "Strong alignment", count: allLiveItems.filter((x: any) => (x.confidence ?? x.score ?? 0) > 80).length },
                  { key: "tomorrow",  label: "Tomorrow",      count: allLiveItems.filter((x: any) => (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase().includes("tomorrow")).length },
                  { key: "1-2-days",  label: "1–2 days",      count: allLiveItems.filter((x: any) => (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase().includes("1-2")).length },
                  { key: "3-4-days",  label: "3–4 days",      count: allLiveItems.filter((x: any) => { const s = (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase(); return s.includes("3-4") || s.includes("3 to 4") || s.includes("3 - 4"); }).length },
                  { key: "anytime",   label: "Anytime",       count: allLiveItems.filter((x: any) => { const s = (x.timeHorizon || x.time_horizon || x.aiTimeframe || "").toLowerCase(); return s.includes("any time") || s.includes("anytime"); }).length },
                ] as { key: string; label: string; count: number }[]).filter(f => f.key === "all" || f.count > 0 || activeFilter === f.key).map(f => (
                  <button key={f.key} className={`chip ${activeFilter === f.key ? "active" : ""}`} onClick={() => setActiveFilter(f.key)}>
                    {f.label}<span className="count">{f.count}</span>
                  </button>
                ))}
              </div>

              <h2 className="section-title">{liveItems.length} {liveItems.length === 1 ? "stock" : "stocks"}</h2>
              {loading ? (
                <div className="stock-grid">
                  {[1,2,3,4,5,6].map(i => <div key={i} className="stock-card" style={{ opacity: 0.5 }}>
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
              {tab === "dashboard" ? <div style={{ marginTop: 36 }}><SmartOptionsPanel /></div> : null}
            </>
          )}
        </main>
      </div>
    </div>
  );
}
