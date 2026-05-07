"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  API_URL,
  HotPick,
  HotPicksResponse,
  BaseFormationPick,
  StrictOptionIdea,
  StockDetail,
  TrackerDashboard,
  TrackerSymbolHistory,
  humanize,
  toneForDirection,
  toneForRisk,
  fmt,
  fmtPct,
} from "../lib/market";
import StockDetailPanel from "./StockDetailPanel";

type Mode = "dashboard" | "scanner";
type MarketTerminalProps = { mode?: Mode };

const REFRESH_INTERVAL_MS = 60000;
const STALE_AFTER_MS = 20 * 60 * 1000;

function formatUpdated(value?: string) {
  if (!value) return "Waiting for first scan";
  return new Date(value).toLocaleString();
}

function isClientStale(value?: string) {
  if (!value) return false;
  return Date.now() - new Date(value).getTime() > STALE_AFTER_MS;
}

function StatCard({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="dashboard-stat">
      <span>{label}</span>
      <strong style={{ color: tone }}>{value}</strong>
    </div>
  );
}

function SkeletonGrid() {
  return (
    <div className="hot-picks-grid">
      {Array.from({ length: 5 }).map((_, index) => (
        <div key={index} className="hot-pick-card skeleton-card">
          <div className="skeleton-line wide" />
          <div className="skeleton-line" />
          <div className="skeleton-line short" />
          <div className="skeleton-line wide" />
        </div>
      ))}
    </div>
  );
}

function EmptyState({ title, body, onRetry }: { title: string; body: string; onRetry?: () => void }) {
  return (
    <section className="empty-panel">
      <strong>{title}</strong>
      <span>{body}</span>
      {onRetry ? (
        <button type="button" className="terminal-button compact" onClick={onRetry}>
          Retry Scan
        </button>
      ) : null}
    </section>
  );
}

function ScoreBreakdown({ pick }: { pick: HotPick }) {
  const rows = pick.scoreBreakdown
    ? [
        ["Setup", pick.scoreBreakdown.chartSetup ?? pick.scoreBreakdown.breakoutSupportResistance ?? 0, 25],
        ["Volume", pick.scoreBreakdown.volumeLiquidity, 20],
        ["Trend", pick.scoreBreakdown.trendStrength, 20],
        ["Momentum", pick.scoreBreakdown.momentumIndicators, 15],
        ["Catalyst", pick.scoreBreakdown.newsSectorCatalyst ?? pick.scoreBreakdown.sectorMarketNews ?? 0, 20],
      ]
    : [];
  return (
    <div className="score-breakdown">
      {rows.map(([label, value, max]) => (
        <div key={String(label)} className="score-row">
          <span>{label}</span>
          <div className="score-track">
            <div style={{ width: `${(Number(value) / Number(max)) * 100}%` }} />
          </div>
          <strong>{Number(value).toFixed(0)}</strong>
        </div>
      ))}
    </div>
  );
}

function HotPickCard({ pick, compact = false, onSelect }: { pick: HotPick; compact?: boolean; onSelect: (symbol: string) => void }) {
  return (
    <article className={`hot-pick-card ${compact ? "is-compact" : ""}`}>
      <div className="hot-card-top">
        <div>
          <div className="symbol-line">
            <strong>{pick.symbol}</strong>
            <span className={`setup-badge ${pick.direction}`}>{humanize(pick.setupType)}</span>
          </div>
          <span className="micro-copy">Updated {formatUpdated(pick.lastUpdated)}</span>
        </div>
        <div className="confidence-ring" style={{ color: toneForDirection(pick.direction) }}>
          {fmt(pick.confidence, 0)}%
        </div>
      </div>

      <p className="pick-reason">{pick.reason}</p>
      {pick.tradeDecision ? <div className={`decision-strip ${pick.direction}`}>{pick.tradeDecision}</div> : null}

      <div className="trade-plan-grid">
        <div><span>Price</span><strong>{pick.currentPrice || "-"}</strong></div>
        <div><span>Score</span><strong>{pick.score != null ? fmt(pick.score, 0) : fmt(pick.confidence, 0)}</strong></div>
        <div><span>Bias</span><strong>{pick.biasLabel || humanize(pick.direction)}</strong></div>
        <div><span>Status</span><strong>{pick.entryStatus || "Watch"}</strong></div>
        {pick.entryQuality ? <div><span>Entry Quality</span><strong>{humanize(pick.entryQuality)}</strong></div> : null}
        {pick.sellerPressure ? <div><span>Seller Pressure</span><strong>{humanize(pick.sellerPressure)}</strong></div> : null}
        {pick.profitBookingRisk ? <div><span>Profit Booking</span><strong>{humanize(pick.profitBookingRisk)}</strong></div> : null}
        <div><span>Entry</span><strong>{pick.entryZone}</strong></div>
        <div><span>Trigger</span><strong>{pick.entryTrigger || "-"}</strong></div>
        <div><span>Target Zone</span><strong>{pick.targetZone || pick.target}</strong></div>
        <div><span>Stoploss</span><strong>{pick.stoploss}</strong></div>
        <div><span>Invalidation</span><strong>{pick.invalidation}</strong></div>
        <div><span>Catalyst</span><strong>{humanize(pick.catalystStatus || "unknown")}</strong></div>
      </div>

      {!compact ? <ScoreBreakdown pick={pick} /> : null}

      <div className="risk-line">
        <span style={{ color: toneForRisk(pick.riskLevel || "medium") }}>{humanize(pick.riskLevel || "medium")} risk</span>
        <span>{humanize(pick.timeHorizon)}</span>
      </div>
      <div className="risk-copy">{pick.risk}</div>
      {pick.reentryPlan ? <div className="risk-copy">{pick.reentryPlan}</div> : null}
      <div className="micro-copy">{pick.catalystReason || "No fresh catalyst found"}</div>
      <button type="button" className="ghost-button compact" onClick={() => onSelect(pick.symbol)}>
        Open Detail
      </button>
    </article>
  );
}

function BaseFormationCard({ pick }: { pick: BaseFormationPick }) {
  const direction = pick.direction || "neutral";
  return (
    <article className="hot-pick-card base-card">
      <div className="hot-card-top">
        <div>
          <div className="symbol-line">
            <strong>{pick.symbol}</strong>
            <span className={`setup-badge ${direction}`}>{pick.biasLabel || humanize(pick.stage)}</span>
            <span className="setup-badge">{pick.entryStatus || "Watch only"}</span>
          </div>
          <span className="micro-copy">{pick.tradeDecision || "Early accumulation - wait for confirmation"}</span>
        </div>
        <div className="confidence-ring" style={{ color: toneForDirection(direction) }}>{fmt(pick.accumulationScore, 0)}%</div>
      </div>
      <p className="pick-reason">{pick.whyInteresting}</p>
      {pick.confirmationText ? <div className={`decision-strip ${direction}`}>{pick.confirmationText}</div> : null}
      <div className="trade-plan-grid">
        <div><span>Price</span><strong>{pick.currentPrice || "-"}</strong></div>
        <div><span>Trigger</span><strong>{pick.triggerPrice || "-"}</strong></div>
        <div><span>Range</span><strong>{pick.range}</strong></div>
        <div><span>{direction === "bearish" ? "Resistance" : "Breakout Level"}</span><strong>{pick.keyResistance}</strong></div>
        <div><span>Support</span><strong>{pick.supportZone}</strong></div>
        <div><span>Invalidation</span><strong>{pick.invalidation}</strong></div>
        <div><span>Trigger Rule</span><strong>{pick.breakoutTrigger}</strong></div>
        <div><span>Target Zone</span><strong>{pick.targetZone || "-"}</strong></div>
      </div>
      <div className="risk-line">
        <span>{humanize(pick.pattern)}</span>
        <span>{humanize(pick.volumeBehavior)}</span>
      </div>
      <div className="micro-copy">
        Distance: {fmtPct(pick.distanceToTriggerPct, 2)} | Horizon: {humanize(pick.timeHorizon)}
      </div>
    </article>
  );
}

function toneForOptionStatus(status: string) {
  if (status === "STRICT_READY") return "#34d399";
  if (status === "WATCH_TRIGGER") return "#fbbf24";
  return "#94a3b8";
}

function StrictOptionCard({ idea }: { idea: StrictOptionIdea }) {
  return (
    <article className="hot-pick-card option-card">
      <div className="hot-card-top">
        <div>
          <div className="symbol-line">
            <strong>{idea.symbol}</strong>
            <span className={`setup-badge ${idea.side === "PE" ? "bearish" : "bullish"}`}>{idea.actionLabel}</span>
            <span className="setup-badge">{humanize(idea.status)}</span>
          </div>
          <span className="micro-copy">{idea.bucket} | Nifty gate: {humanize(idea.niftyGate)}</span>
        </div>
        <div className="confidence-ring" style={{ color: toneForOptionStatus(idea.status) }}>
          {fmt(idea.confidence, 0)}%
        </div>
      </div>

      <div className={`decision-strip ${idea.side === "PE" ? "bearish" : "bullish"}`}>{idea.entryRule}</div>

      <div className="trade-plan-grid">
        <div><span>Underlying</span><strong>{idea.underlyingPrice}</strong></div>
        <div><span>Trigger</span><strong>{idea.underlyingTrigger}</strong></div>
        <div><span>Invalidation</span><strong>{idea.underlyingInvalidation}</strong></div>
        <div><span>Distance</span><strong>{fmtPct(idea.distanceToTriggerPct, 2)}</strong></div>
        <div><span>Option Type</span><strong>{idea.instrumentRule}</strong></div>
        <div><span>Target</span><strong>{idea.targetRule}</strong></div>
      </div>

      <div className="risk-copy">{idea.expiryRule}</div>
      <div className="risk-copy">{idea.riskRule}</div>
      <div className="micro-copy">{idea.invalidationRule}</div>
    </article>
  );
}

function StrictOptionsPanel({ data }: { data?: HotPicksResponse["strictOptions"] }) {
  if (!data) return null;
  const gateTone = data.niftyGate.side === "bullish" ? "#2dd4bf" : data.niftyGate.side === "bearish" ? "#fb7185" : "#fbbf24";
  return (
    <section className="dashboard-band strict-options-panel">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Options Gate</p>
          <h2>Strict options radar</h2>
        </div>
        <span className="micro-copy">Research only. No far OTM weekly options; trigger + Nifty gate must confirm.</span>
      </div>

      <div className="summary-strip compact">
        <StatCard label="Nifty Gate" value={humanize(data.niftyGate.status)} tone={gateTone} />
        <StatCard label="Nifty Score" value={fmt(data.niftyGate.marketScore, 0)} />
        <StatCard label="PCR" value={data.niftyGate.pcr != null ? fmt(Number(data.niftyGate.pcr), 2) : "-"} />
        <StatCard label="Ready Options" value={data.summary.strictReady} tone="#34d399" />
        <StatCard label="Watch Options" value={data.summary.watchOnly} tone="#fbbf24" />
      </div>

      <div className="decision-strip">{data.niftyGate.message}</div>

      <div className="watch-pick-grid">
        {data.radar.map((idea) => <StrictOptionCard key={`${idea.symbol}-${idea.side}`} idea={idea} />)}
        {!data.radar.length ? (
          <div className="empty-state">
            No strict option entries right now. Wait for Nifty alignment, trigger confirmation, volume, and tight invalidation.
          </div>
        ) : null}
      </div>
      {data.warnings.map((warning) => <div key={warning} className="micro-copy">{warning}</div>)}
    </section>
  );
}

function DebugPanel({ data }: { data?: HotPicksResponse["debug"] }) {
  if (!data) return null;
  return (
    <details className="dashboard-band debug-panel">
      <summary>Debug / tuning visibility</summary>
      <div className="summary-strip compact">
        <StatCard label="Total Scanned" value={data.totalScanned} />
        <StatCard label="Source Candidates" value={data.sourceCandidates} />
        {Object.entries(data.candidatesByTier || {}).map(([tier, count]) => (
          <StatCard key={tier} label={humanize(tier)} value={count} />
        ))}
      </div>
      <div className="debug-grid">
        <div>
          <p className="eyebrow">Rejected Counts</p>
          <div className="sector-list">
            {Object.entries(data.rejectedCountsByReason || {}).map(([reason, count]) => (
              <span key={reason}>{humanize(reason)}: {count}</span>
            ))}
          </div>
        </div>
        <div>
          <p className="eyebrow">Top 10 Near Miss</p>
          <div className="journal-list">
            {(data.top10NearMissCandidates || []).map((item) => (
              <div key={item.symbol} className="journal-row">
                <strong>{item.symbol}</strong>
                <span>{fmt(item.score, 0)} | {humanize(item.setupType)}</span>
                <span>{[...(item.softReasons || []), ...(item.rejectionReasons || [])].map(humanize).join(", ") || "needs tuning"}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </details>
  );
}

function PerformancePanel({ data }: { data?: HotPicksResponse["performance"] }) {
  const enough = data && data.winRate !== null && data.winRate !== undefined;
  return (
    <section className="dashboard-band">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Paper Trade Journal</p>
          <h2>Past performance</h2>
        </div>
      </div>
      <div className="summary-strip compact">
        <StatCard label="Active Calls" value={data?.activeCalls ?? 0} tone="#60a5fa" />
        <StatCard label="Win Rate" value={enough ? `${fmt(Number(data?.winRate), 1)}%` : "Not enough data yet"} tone="#2dd4bf" />
        <StatCard label="Average Return" value={enough ? `${fmt(Number(data?.averageReturn ?? 0), 2)}%` : "Not enough data yet"} />
        <StatCard label="Max Loss" value={enough && data?.maxLoss != null ? `${fmt(Number(data.maxLoss), 2)}%` : "Not enough data yet"} tone="#fb7185" />
      </div>
      <div className="journal-list">
        {(data?.last30Calls || []).slice(0, 30).map((call) => (
          <div key={call.id} className="journal-row">
            <strong>{call.symbol}</strong>
            <span>{humanize(call.direction)} | {fmt(call.entry_price, 2)} to {fmt(call.target_price, 2)}</span>
            <span>{humanize(call.scanner_call_status || call.status)} | {call.result_pct != null ? fmtPct(call.result_pct) : "active"}</span>
          </div>
        ))}
        {!(data?.last30Calls || []).length ? <div className="empty-state">No paper trade journal entries yet.</div> : null}
      </div>
    </section>
  );
}

export default function MarketTerminal({ mode = "dashboard" }: MarketTerminalProps) {
  const [hotPicks, setHotPicks] = useState<HotPicksResponse | null>(null);
  const [trackerDashboard, setTrackerDashboard] = useState<TrackerDashboard | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [searchSymbol, setSearchSymbol] = useState("");
  const [detail, setDetail] = useState<StockDetail | null>(null);
  const [trackedHistory, setTrackedHistory] = useState<TrackerSymbolHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const detailRequestRef = useRef(0);
  const hasLoadedHotPicksRef = useRef(false);

  const stale = hotPicks?.isStale || isClientStale(hotPicks?.lastUpdated);

  const loadHotPicks = useCallback(async (forceRefresh = false) => {
    if (forceRefresh) setRefreshing(true);
    if (!hasLoadedHotPicksRef.current) setLoading(true);
    try {
      setError("");
      const response = await fetch(`${API_URL}/api/market/hot-picks?force_refresh=${forceRefresh ? "true" : "false"}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`hot_picks_${response.status}`);
      setHotPicks((await response.json()) as HotPicksResponse);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "hot_picks_error");
    } finally {
      hasLoadedHotPicksRef.current = true;
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  const loadTrackerDashboard = useCallback(async () => {
    try {
      const response = await fetch(`${API_URL}/api/tracker/dashboard`, { cache: "no-store" });
      if (response.ok) setTrackerDashboard((await response.json()) as TrackerDashboard);
    } catch {}
  }, []);

  const loadDetail = useCallback(async (symbol: string) => {
    const requestId = ++detailRequestRef.current;
    setDetailLoading(true);
    try {
      const response = await fetch(`${API_URL}/api/stocks/${encodeURIComponent(symbol)}?force_refresh=false`, { cache: "no-store" });
      if (!response.ok) throw new Error(`detail_${response.status}`);
      const payload = (await response.json()) as StockDetail;
      if (requestId !== detailRequestRef.current) return;
      setDetail(payload);
      setDetailError("");
    } catch (err: unknown) {
      if (requestId !== detailRequestRef.current) return;
      setDetailError(err instanceof Error ? err.message : "detail_error");
    } finally {
      if (requestId === detailRequestRef.current) setDetailLoading(false);
    }
  }, []);

  const loadTrackedHistory = useCallback(async (symbol: string) => {
    try {
      const response = await fetch(`${API_URL}/api/tracker/stocks/${encodeURIComponent(symbol)}`, { cache: "no-store" });
      if (response.ok) setTrackedHistory((await response.json()) as TrackerSymbolHistory);
    } catch {}
  }, []);

  useEffect(() => {
    loadHotPicks(false);
    loadTrackerDashboard();
    const interval = setInterval(() => {
      loadHotPicks(false);
      loadTrackerDashboard();
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadHotPicks, loadTrackerDashboard]);

  useEffect(() => {
    if (!selectedSymbol) return;
    loadDetail(selectedSymbol);
    loadTrackedHistory(selectedSymbol);
  }, [loadDetail, loadTrackedHistory, selectedSymbol]);

  const suggestions = useMemo(() => {
    const map = new Map<string, string>();
    [
      ...(hotPicks?.hotPicks || []),
      ...(hotPicks?.watchlist || []),
      ...(hotPicks?.momentumRadar || []),
      ...(hotPicks?.catalystRadar || []),
    ].forEach((pick) => map.set(pick.symbol, pick.setupType));
    (hotPicks?.baseFormationRadar || []).forEach((pick) => map.set(pick.symbol, pick.stage));
    trackerDashboard?.watchlists.active_scanner_calls?.forEach((item) => map.set(item.symbol, item.setup_label));
    return Array.from(map.entries()).slice(0, 100);
  }, [hotPicks, trackerDashboard]);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const symbol = searchSymbol.trim().toUpperCase();
    if (symbol) setSelectedSymbol(symbol);
  }

  async function handleSaveManualWatch(symbol: string) {
    await fetch(`${API_URL}/api/tracker/manual-watch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, notes: "Added from Hot Picks dashboard", pinned: false }),
    });
    await loadTrackerDashboard();
  }

  return (
    <main className="app-shell intelligence-shell">
      <section className="terminal-hero">
        <div>
          <p className="eyebrow">Stock Intelligence</p>
          <h1>Hot Picks Dashboard</h1>
          <p className="micro-copy">
            Last updated {formatUpdated(hotPicks?.lastUpdated)}
            {hotPicks?.cacheStatus ? ` | ${humanize(hotPicks.cacheStatus)}` : ""}
          </p>
        </div>
        <form className="dashboard-controls compact-controls" onSubmit={handleSubmit}>
          <input className="search-input" value={searchSymbol} onChange={(event) => setSearchSymbol(event.target.value)} placeholder="Search symbol" list="stock-search-suggestions" />
          <datalist id="stock-search-suggestions">
            {suggestions.map(([symbol, label]) => <option key={symbol} value={symbol} label={`${symbol} - ${label}`} />)}
          </datalist>
          <button type="submit" className="terminal-button">Open</button>
          <button type="button" className="ghost-button" onClick={() => loadHotPicks(true)} disabled={refreshing}>{refreshing ? "Refreshing..." : "Refresh Scan"}</button>
        </form>
      </section>

      {stale ? <div className="warning-strip">Stale data: showing the last successful scan while a fresh scan is attempted.</div> : null}
      {error && hotPicks ? <div className="warning-strip">Live refresh failed: {error}. Keeping the last valid dashboard on screen.</div> : null}
      {error && !hotPicks ? <EmptyState title="Market scan unavailable" body="No previous successful scan is available yet." onRetry={() => loadHotPicks(true)} /> : null}

      <section className="summary-strip hot-summary">
        <StatCard label="Market Mood" value={humanize(hotPicks?.marketMood || "sideways")} tone={toneForDirection(hotPicks?.marketMood === "bearish" ? "bearish" : "bullish")} />
        <StatCard label="Total Stocks Scanned" value={loading ? "..." : hotPicks?.summary.totalScanned ?? 0} />
        <StatCard label="Fresh Buy Picks" value={hotPicks?.summary.highConfidence ?? 0} tone="#34d399" />
        <StatCard label="Breakouts" value={hotPicks?.summary.breakouts ?? 0} tone="#60a5fa" />
        <StatCard label="Bullish" value={hotPicks?.summary.bullish ?? 0} tone="#2dd4bf" />
        <StatCard label="Bearish" value={hotPicks?.summary.bearish ?? 0} tone="#fb7185" />
        <StatCard label="Momentum Radar" value={hotPicks?.summary.momentumRadar ?? hotPicks?.momentumRadar?.length ?? 0} tone="#fbbf24" />
        <StatCard label="Base Formation" value={hotPicks?.summary.baseFormation ?? hotPicks?.baseFormationRadar?.length ?? 0} tone="#60a5fa" />
        <StatCard label="Strict Options" value={(hotPicks?.summary.strictOptionsReady ?? 0) + (hotPicks?.summary.strictOptionsWatch ?? 0)} tone="#fbbf24" />
      </section>

      {selectedSymbol ? (
        <StockDetailPanel
          activeSymbol={selectedSymbol}
          detail={detail}
          trackedHistory={trackedHistory}
          onSaveManualWatch={handleSaveManualWatch}
          error={detailError}
          loading={detailLoading}
        />
      ) : null}

      <StrictOptionsPanel data={hotPicks?.strictOptions} />

      <section className="dashboard-band">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Top 5</p>
            <h2>Hot Picks</h2>
          </div>
        </div>
        {loading && !hotPicks ? <SkeletonGrid /> : null}
        {hotPicks?.hotPicks.length ? (
          <div className="hot-picks-grid">
            {hotPicks.hotPicks.slice(0, 5).map((pick) => <HotPickCard key={pick.symbol} pick={pick} onSelect={setSelectedSymbol} />)}
          </div>
        ) : !loading ? (
          <EmptyState title="No fresh buy Hot Picks right now" body="High-score stocks can still appear below as watch-only, late-entry, or bearish-risk setups." onRetry={() => loadHotPicks(true)} />
        ) : null}
      </section>

      <section className="dashboard-band">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Base Formation / Early Movers</p>
            <h2>Silent Accumulation Detector</h2>
          </div>
          <span className="micro-copy">Watchlist only. Ready and near-trigger cards are sorted first; late-entry and bearish-risk cards are warnings.</span>
        </div>
        <div className="watch-pick-grid">
          {(hotPicks?.baseFormationRadar || []).map((pick) => <BaseFormationCard key={`base-${pick.symbol}`} pick={pick} />)}
          {hotPicks && !(hotPicks.baseFormationRadar || []).length ? <div className="empty-state">No clean base formation candidates from the current scan.</div> : null}
        </div>
      </section>

      <section className="dashboard-band">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Watchlist</p>
            <h2>Moderate setups</h2>
          </div>
        </div>
        <div className="watch-pick-grid">
          {(hotPicks?.watchlist || []).map((pick) => <HotPickCard key={`watch-${pick.symbol}`} pick={pick} compact onSelect={setSelectedSymbol} />)}
          {hotPicks && !hotPicks.watchlist.length ? <div className="empty-state">No moderate setups passed the score filter.</div> : null}
        </div>
      </section>

      <section className="dashboard-band">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Momentum Radar</p>
            <h2>Early movers and volume/momentum candidates</h2>
          </div>
          <span className="micro-copy">High score here does not mean fresh buy. Follow Status first, then trigger and invalidation.</span>
        </div>
        <div className="watch-pick-grid">
          {(hotPicks?.momentumRadar || []).map((pick) => <HotPickCard key={`momentum-${pick.symbol}`} pick={pick} compact onSelect={setSelectedSymbol} />)}
          {hotPicks && !(hotPicks.momentumRadar || []).length ? <div className="empty-state">No momentum radar candidates passed the score filter.</div> : null}
        </div>
      </section>

      <section className="dashboard-band">
        <div className="section-heading">
          <div>
            <p className="eyebrow">News / Catalyst Radar</p>
            <h2>Stocks with available catalyst context</h2>
          </div>
        </div>
        <div className="watch-pick-grid">
          {(hotPicks?.catalystRadar || []).map((pick) => <HotPickCard key={`catalyst-${pick.symbol}`} pick={pick} compact onSelect={setSelectedSymbol} />)}
          {hotPicks && !(hotPicks.catalystRadar || []).length ? <div className="empty-state">No fresh catalyst found in the current data source.</div> : null}
        </div>
      </section>

      <section className="dashboard-band market-notes">
        <div>
          <p className="eyebrow">Market Explanation</p>
          <p>{hotPicks?.marketExplanation || "Market context will appear after the first scan."}</p>
        </div>
        <div>
          <p className="eyebrow">Sector Strength</p>
          <div className="sector-list">
            {Object.entries(hotPicks?.sectorStrength || {}).slice(0, 8).map(([sector, count]) => (
              <span key={sector}>{humanize(sector)}: {count}</span>
            ))}
            {!Object.keys(hotPicks?.sectorStrength || {}).length ? <span>Sector data unavailable in current feed.</span> : null}
          </div>
        </div>
        <div className="disclaimer">
          {(hotPicks?.warnings || ["This is not financial advice. Use this only for research and paper trading."]).map((warning) => (
            <span key={warning}>{warning}</span>
          ))}
        </div>
      </section>

      <PerformancePanel data={hotPicks?.performance} />
      <DebugPanel data={hotPicks?.debug} />
    </main>
  );
}
