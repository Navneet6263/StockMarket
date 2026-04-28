"use client";

import { FormEvent, startTransition, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";

import {
  API_URL,
  MarketMover,
  MarketOverview,
  MarketSignal,
  StockDetail,
  TrackerDashboard,
  TrackerSymbolHistory,
  TrackedSetup,
  humanize,
  toneForDirection,
  toneForRisk,
} from "../lib/market";
import StockDetailPanel from "./StockDetailPanel";
import TradeHistoryPanel from "./TradeHistoryPanel";
import TrackedSetupCard from "./TrackedSetupCard";

type Mode = "dashboard" | "scanner";
type MarketTerminalProps = { mode?: Mode };
type LoadDetailOptions = { background?: boolean; clearOnChange?: boolean };
type TabKey = "overview" | "bullish" | "bearish" | "breakout" | "watchlist" | "journal" | "performance";
type TimeframeFilter = "intraday" | "swing" | "positional";
type SegmentFilter = "all" | "bullish" | "bearish" | "breakout" | "high_volume" | "watchlist";

const REFRESH_INTERVAL_MS = 60000;

const tabs: Array<{ key: TabKey; label: string }> = [
  { key: "overview", label: "Overview" },
  { key: "bullish", label: "Bullish Stocks" },
  { key: "bearish", label: "Bearish Stocks" },
  { key: "breakout", label: "Breakout Stocks" },
  { key: "watchlist", label: "Watchlist" },
  { key: "journal", label: "Trade Journal" },
  { key: "performance", label: "Performance" },
];

function resolveScannerLeader(payload: MarketOverview) {
  return (
    payload.summary.scanner_leader ||
    payload.top_opportunities[0]?.symbol ||
    payload.breakout_candidates[0]?.symbol ||
    payload.unusual_volume[0]?.symbol ||
    payload.top_movers[0]?.symbol ||
    ""
  );
}

function formatInr(value?: number | null) {
  return value ? `INR ${value.toFixed(2)}` : "-";
}

function timeframeMatches(item: MarketSignal, timeframe: TimeframeFilter) {
  const days = item.timeframe_days || 0;
  const label = (item.timeframe_label || "").toLowerCase();
  if (timeframe === "intraday") return days <= 2 || label.includes("intraday");
  if (timeframe === "swing") return days > 2 && days <= 10;
  return days > 10 || label.includes("positional");
}

function uniqueSignals(items: MarketSignal[]) {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.symbol)) return false;
    seen.add(item.symbol);
    return true;
  });
}

function SummaryCard({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="dashboard-stat">
      <span>{label}</span>
      <strong style={{ color: tone }}>{value}</strong>
    </div>
  );
}

function StockRows({
  title,
  items,
  activeSymbol,
  empty,
  onSelect,
  onWatch,
}: {
  title: string;
  items: MarketSignal[];
  activeSymbol: string;
  empty: string;
  onSelect: (symbol: string) => void;
  onWatch: (symbol: string) => void;
}) {
  return (
    <section className="terminal-card compact-section">
      <div className="section-heading">
        <div>
          <p className="eyebrow">{title}</p>
          <h2>{items.length} scanner matches</h2>
        </div>
      </div>
      <div className="scanner-list-table">
        <div className="scanner-list-head">
          <span>Stock</span>
          <span>Price</span>
          <span>Confidence</span>
          <span>Plan</span>
          <span>Risk</span>
          <span>Actions</span>
        </div>
        {items.length ? (
          items.map((item) => (
            <div key={`${title}-${item.symbol}`} className={`scanner-list-row ${activeSymbol === item.symbol ? "is-active" : ""}`}>
              <div className="scanner-name">
                <strong>{item.symbol}</strong>
                <span>{item.company_name || item.setup_label}</span>
              </div>
              <div>
                <strong>{formatInr(item.current_price)}</strong>
                <span style={{ color: item.change_pct >= 0 ? "#34d399" : "#fb7185" }}>
                  {item.change_pct >= 0 ? "+" : ""}{item.change_pct.toFixed(2)}%
                </span>
              </div>
              <div>
                <strong style={{ color: toneForDirection(item.direction) }}>{item.confidence.toFixed(0)}%</strong>
                <span>{item.expected_move_pct >= 0 ? "+" : ""}{item.expected_move_pct.toFixed(2)}% move</span>
              </div>
              <div>
                <strong>{formatInr(item.target_price)}</strong>
                <span>Invalidation {formatInr(item.invalidation || item.stop_loss)}</span>
              </div>
              <div>
                <strong style={{ color: toneForRisk(item.risk_level) }}>{humanize(item.risk_level)}</strong>
                <span>{item.timeframe_label}</span>
              </div>
              <div className="row-actions">
                <button type="button" className="terminal-button compact" onClick={() => onSelect(item.symbol)}>View Detail</button>
                <button type="button" className="ghost-button compact" onClick={() => onWatch(item.symbol)}>Add to Watchlist</button>
              </div>
            </div>
          ))
        ) : (
          <div className="empty-state-block"><strong>{empty}</strong><span>Adjust filters or refresh the scan.</span></div>
        )}
      </div>
    </section>
  );
}

function MoverRows({ items, onSelect }: { items: MarketMover[]; onSelect: (symbol: string) => void }) {
  return (
    <div className="scanner-list-table compact-movers">
      {items.map((item) => (
        <button key={`mover-${item.symbol}`} type="button" className="scanner-row" onClick={() => onSelect(item.symbol)}>
          <div className="scanner-symbol">
            <span className="mono-label">{item.symbol}</span>
            <small>{item.company_name || (item.tags || []).slice(0, 2).map(humanize).join(" | ") || "live screener"}</small>
          </div>
          <div className="scanner-metrics">
            <span>{item.price ? `INR ${item.price.toFixed(2)}` : "-"}</span>
            <span style={{ color: (item.change_pct || 0) >= 0 ? "#34d399" : "#fb7185" }}>
              {(item.change_pct || 0) >= 0 ? "+" : ""}{(item.change_pct || 0).toFixed(2)}%
            </span>
          </div>
        </button>
      ))}
    </div>
  );
}

export default function MarketTerminal({ mode = "dashboard" }: MarketTerminalProps) {
  const [overview, setOverview] = useState<MarketOverview | null>(null);
  const [trackerDashboard, setTrackerDashboard] = useState<TrackerDashboard | null>(null);
  const [scannerHighlightedSymbol, setScannerHighlightedSymbol] = useState("");
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [searchSymbol, setSearchSymbol] = useState("");
  const [activeTab, setActiveTab] = useState<TabKey>(mode === "scanner" ? "bullish" : "overview");
  const [timeframeFilter, setTimeframeFilter] = useState<TimeframeFilter>("swing");
  const [segmentFilter, setSegmentFilter] = useState<SegmentFilter>("all");
  const [detail, setDetail] = useState<StockDetail | null>(null);
  const [trackedHistory, setTrackedHistory] = useState<TrackerSymbolHistory | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [overviewError, setOverviewError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [trackerError, setTrackerError] = useState("");
  const detailRequestRef = useRef(0);
  const trackerSymbolRequestRef = useRef(0);
  const hasLoadedOverviewRef = useRef(false);
  const currentDetailSymbolRef = useRef("");
  const selectedSymbolRef = useRef(selectedSymbol);
  const deferredSearch = useDeferredValue(searchSymbol.trim().toUpperCase());
  const activeDetailSymbol = selectedSymbol || "";

  useEffect(() => {
    selectedSymbolRef.current = selectedSymbol;
  }, [selectedSymbol]);

  useEffect(() => {
    currentDetailSymbolRef.current = detail?.symbol || "";
  }, [detail?.symbol]);

  const loadOverview = useCallback(async (forceRefresh = false) => {
    if (forceRefresh) setRefreshing(true);
    if (!hasLoadedOverviewRef.current) setOverviewLoading(true);
    try {
      setOverviewError("");
      const response = await fetch(`${API_URL}/api/market/overview?force_refresh=${forceRefresh ? "true" : "false"}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`overview_${response.status}`);
      const payload = (await response.json()) as MarketOverview;
      setOverview(payload);
      setScannerHighlightedSymbol(resolveScannerLeader(payload));
    } catch (err: unknown) {
      setOverviewError(err instanceof Error ? err.message : "overview_error");
    } finally {
      hasLoadedOverviewRef.current = true;
      setOverviewLoading(false);
      setRefreshing(false);
    }
  }, []);

  const loadTrackerDashboard = useCallback(async () => {
    try {
      setTrackerError("");
      const response = await fetch(`${API_URL}/api/tracker/dashboard`, { cache: "no-store" });
      if (!response.ok) throw new Error(`tracker_${response.status}`);
      setTrackerDashboard((await response.json()) as TrackerDashboard);
    } catch (err: unknown) {
      setTrackerError(err instanceof Error ? err.message : "tracker_error");
    }
  }, []);

  const loadDetail = useCallback(async (symbol: string, options: LoadDetailOptions = {}) => {
    if (!symbol) return;
    const requestId = ++detailRequestRef.current;
    if (options.clearOnChange) setDetail(null);
    if (!options.background) setDetailLoading(true);
    try {
      const response = await fetch(`${API_URL}/api/stocks/${encodeURIComponent(symbol)}?force_refresh=false`, { cache: "no-store" });
      if (!response.ok) throw new Error(`detail_${response.status}`);
      const payload = (await response.json()) as StockDetail;
      if (detailRequestRef.current !== requestId || selectedSymbolRef.current !== symbol) return;
      setDetailError("");
      setDetail(payload);
    } catch (err: unknown) {
      if (detailRequestRef.current !== requestId || selectedSymbolRef.current !== symbol) return;
      setDetailError(err instanceof Error ? err.message : "detail_error");
    } finally {
      if (detailRequestRef.current === requestId && !options.background) setDetailLoading(false);
    }
  }, []);

  const loadTrackedHistory = useCallback(async (symbol: string) => {
    if (!symbol) {
      setTrackedHistory(null);
      return;
    }
    const requestId = ++trackerSymbolRequestRef.current;
    try {
      const response = await fetch(`${API_URL}/api/tracker/stocks/${encodeURIComponent(symbol)}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`tracked_symbol_${response.status}`);
      const payload = (await response.json()) as TrackerSymbolHistory;
      if (trackerSymbolRequestRef.current !== requestId || selectedSymbolRef.current !== symbol) return;
      setTrackedHistory(payload);
    } catch {}
  }, []);

  useEffect(() => {
    loadOverview(false);
    loadTrackerDashboard();
    const interval = setInterval(() => {
      loadOverview(false);
      loadTrackerDashboard();
      if (selectedSymbolRef.current) {
        loadDetail(selectedSymbolRef.current, { background: true });
        loadTrackedHistory(selectedSymbolRef.current);
      }
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadDetail, loadOverview, loadTrackedHistory, loadTrackerDashboard]);

  useEffect(() => {
    if (!selectedSymbol) {
      setDetail(null);
      setTrackedHistory(null);
      setDetailLoading(false);
      return;
    }
    loadDetail(selectedSymbol, { background: false, clearOnChange: currentDetailSymbolRef.current !== selectedSymbol });
    loadTrackedHistory(selectedSymbol);
  }, [selectedSymbol, loadDetail, loadTrackedHistory]);

  function selectSymbol(symbol: string) {
    startTransition(() => setSelectedSymbol(symbol));
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = searchSymbol.trim().toUpperCase();
    if (next) selectSymbol(next);
  }

  async function handleSaveManualWatch(symbol: string) {
    const note = window.prompt("Optional note for the manual watchlist entry", "") || "";
    const timeframe = window.prompt("Optional timeframe override", detail?.prediction.timeframe_label || "3-5 days") || undefined;
    await fetch(`${API_URL}/api/tracker/manual-watch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, notes: note, timeframe_label: timeframe, pinned: false }),
    });
    await loadTrackerDashboard();
    if (selectedSymbolRef.current) await loadTrackedHistory(selectedSymbolRef.current);
  }

  async function handleUpdateSetup(setupId: string, values: Record<string, unknown>) {
    await fetch(`${API_URL}/api/tracker/setups/${encodeURIComponent(setupId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    await loadTrackerDashboard();
  }

  async function handleArchiveSetup(setupId: string) {
    await fetch(`${API_URL}/api/tracker/setups/${encodeURIComponent(setupId)}/archive`, { method: "POST" });
    await loadTrackerDashboard();
  }

  async function handleIgnoreSetup(setupId: string) {
    await fetch(`${API_URL}/api/tracker/setups/${encodeURIComponent(setupId)}/ignore`, { method: "POST" });
    await loadTrackerDashboard();
  }

  const allSignals = useMemo(() => uniqueSignals([
    ...(overview?.top_opportunities || []),
    ...(overview?.breakout_candidates || []),
    ...(overview?.bearish_risks || []),
    ...(overview?.unusual_volume || []),
  ]), [overview]);

  const filteredSignals = useMemo(() => {
    return allSignals.filter((item) => {
      if (deferredSearch && !`${item.symbol} ${item.company_name || ""}`.includes(deferredSearch)) return false;
      if (!timeframeMatches(item, timeframeFilter)) return false;
      if (segmentFilter === "bullish" && item.direction !== "bullish") return false;
      if (segmentFilter === "bearish" && item.direction !== "bearish") return false;
      if (segmentFilter === "breakout" && !item.tags.includes("breakout") && !item.tags.includes("breakdown")) return false;
      if (segmentFilter === "high_volume" && item.relative_volume < 1.6 && item.intraday_volume_ratio < 1.4) return false;
      return segmentFilter !== "watchlist";
    });
  }, [allSignals, deferredSearch, segmentFilter, timeframeFilter]);

  const bullishSignals = filteredSignals.filter((item) => item.direction === "bullish");
  const bearishSignals = filteredSignals.filter((item) => item.direction === "bearish");
  const breakoutSignals = filteredSignals.filter((item) => item.tags.includes("breakout") || item.tags.includes("breakdown"));
  const averageConfidence = allSignals.length ? allSignals.reduce((sum, item) => sum + item.confidence, 0) / allSignals.length : 0;

  const watchItems: TrackedSetup[] = trackerDashboard
    ? [
        ...trackerDashboard.watchlists.fresh_setups,
        ...trackerDashboard.watchlists.active_bullish,
        ...trackerDashboard.watchlists.active_bearish,
        ...trackerDashboard.watchlists.manual_watchlist,
      ]
    : [];

  return (
    <main className="app-shell dashboard-shell">
      <section className="dashboard-header terminal-card">
        <div className="dashboard-title">
          <p className="eyebrow">Production Terminal</p>
          <h1>Market Intelligence and Trade Journal</h1>
          <div className="micro-copy">
            {overview?.generated_at ? `Updated ${new Date(overview.generated_at).toLocaleTimeString()}` : "Waiting for first scan"}
            {scannerHighlightedSymbol ? ` | Scanner leader ${scannerHighlightedSymbol}` : ""}
          </div>
        </div>
        <form className="dashboard-controls" onSubmit={handleSubmit}>
          <input className="search-input" value={searchSymbol} onChange={(event) => setSearchSymbol(event.target.value)} placeholder="Search symbol or company" />
          <button type="submit" className="terminal-button">Open</button>
          <button type="button" className="ghost-button" onClick={() => loadOverview(true)} disabled={refreshing}>{refreshing ? "Refreshing..." : "Refresh Scan"}</button>
          <select className="filter-select" value={timeframeFilter} onChange={(event) => setTimeframeFilter(event.target.value as TimeframeFilter)}>
            <option value="intraday">Intraday</option>
            <option value="swing">Swing</option>
            <option value="positional">Positional</option>
          </select>
          <select className="filter-select" value={segmentFilter} onChange={(event) => {
            const next = event.target.value as SegmentFilter;
            setSegmentFilter(next);
            if (next === "bullish") setActiveTab("bullish");
            if (next === "bearish") setActiveTab("bearish");
            if (next === "breakout" || next === "high_volume") setActiveTab("breakout");
            if (next === "watchlist") setActiveTab("watchlist");
          }}>
            <option value="all">All</option>
            <option value="bullish">Bullish</option>
            <option value="bearish">Bearish</option>
            <option value="breakout">Breakout</option>
            <option value="high_volume">High Volume</option>
            <option value="watchlist">Watchlist</option>
          </select>
        </form>
      </section>

      {overviewError ? <section className="terminal-card"><div className="empty-state">Error loading market intelligence: {overviewError}</div></section> : null}
      {trackerError ? <section className="terminal-card"><div className="empty-state">Error loading tracked setups: {trackerError}</div></section> : null}

      <nav className="dashboard-tabs" aria-label="Dashboard sections">
        {tabs.map((tab) => (
          <button key={tab.key} type="button" className={activeTab === tab.key ? "is-active" : ""} onClick={() => setActiveTab(tab.key)}>
            {tab.label}
          </button>
        ))}
      </nav>

      {selectedSymbol ? (
        <StockDetailPanel
          activeSymbol={activeDetailSymbol}
          detail={detail}
          trackedHistory={trackedHistory}
          onSaveManualWatch={handleSaveManualWatch}
          error={detailError}
          loading={detailLoading}
        />
      ) : null}

      {activeTab === "overview" ? (
        <section className="tab-panel">
          <div className="summary-strip">
            <SummaryCard label="Market Mood" value={humanize(overview?.summary.market_mood)} tone={toneForDirection(overview?.summary.market_mood === "defensive" ? "bearish" : "bullish")} />
            <SummaryCard label="Total Stocks Scanned" value={overviewLoading ? "..." : overview?.universe_size ?? 0} />
            <SummaryCard label="Bullish Count" value={overview?.market_breadth.bullish_setups ?? 0} tone="#2dd4bf" />
            <SummaryCard label="Bearish Count" value={overview?.market_breadth.bearish_setups ?? 0} tone="#fb7185" />
            <SummaryCard label="Breakout Count" value={overview?.summary.breakout_count ?? 0} tone="#60a5fa" />
            <SummaryCard label="Average Confidence" value={`${averageConfidence.toFixed(0)}%`} tone="#fbbf24" />
          </div>
          <div className="overview-columns">
            <StockRows title="Top 5 Bullish Setups" items={bullishSignals.slice(0, 5)} activeSymbol={activeDetailSymbol} empty="No bullish setups match the current filters." onSelect={selectSymbol} onWatch={handleSaveManualWatch} />
            <StockRows title="Top 5 Bearish Risks" items={bearishSignals.slice(0, 5)} activeSymbol={activeDetailSymbol} empty="No bearish risks match the current filters." onSelect={selectSymbol} onWatch={handleSaveManualWatch} />
          </div>
        </section>
      ) : null}

      {activeTab === "bullish" ? <StockRows title="Bullish Stocks" items={bullishSignals} activeSymbol={activeDetailSymbol} empty="No bullish stocks match the current filters." onSelect={selectSymbol} onWatch={handleSaveManualWatch} /> : null}
      {activeTab === "bearish" ? <StockRows title="Bearish Stocks" items={bearishSignals} activeSymbol={activeDetailSymbol} empty="No bearish stocks match the current filters." onSelect={selectSymbol} onWatch={handleSaveManualWatch} /> : null}
      {activeTab === "breakout" ? (
        <section className="tab-panel">
          <StockRows title={segmentFilter === "high_volume" ? "High Volume Stocks" : "Breakout Stocks"} items={segmentFilter === "high_volume" ? filteredSignals : breakoutSignals} activeSymbol={activeDetailSymbol} empty="No breakout or high-volume stocks match the current filters." onSelect={selectSymbol} onWatch={handleSaveManualWatch} />
          <section className="terminal-card compact-section">
            <div className="section-heading"><div><p className="eyebrow">Top Movers</p><h3>Discovery feed from the live universe</h3></div></div>
            <MoverRows items={(overview?.top_movers || []).slice(0, 8)} onSelect={selectSymbol} />
          </section>
        </section>
      ) : null}

      {activeTab === "watchlist" ? (
        <section className="terminal-card compact-section">
          <div className="section-heading"><div><p className="eyebrow">Watchlist</p><h2>Saved and active trade ideas</h2></div></div>
          <div className="watch-grid">
            {watchItems.length ? watchItems.map((item) => (
              <TrackedSetupCard key={item.id} item={item} active={activeDetailSymbol === item.symbol} onSelect={selectSymbol} onUpdate={handleUpdateSetup} onArchive={handleArchiveSetup} onIgnore={handleIgnoreSetup} />
            )) : <div className="empty-state-block"><strong>No watchlist items yet.</strong><span>Add scanner names to keep tracking them after they leave the live feed.</span></div>}
          </div>
        </section>
      ) : null}

      {activeTab === "journal" ? <TradeHistoryPanel /> : null}

      {activeTab === "performance" ? (
        <section className="terminal-card compact-section">
          <div className="section-heading"><div><p className="eyebrow">Performance</p><h2>Scanner accountability</h2></div></div>
          {trackerDashboard ? (
            <>
              <div className="summary-strip">
                <SummaryCard label="Total Calls" value={trackerDashboard.summary.total_calls} />
                <SummaryCard label="Active" value={trackerDashboard.summary.active_calls} tone="#60a5fa" />
                <SummaryCard label="Passed" value={trackerDashboard.summary.passed_calls} tone="#34d399" />
                <SummaryCard label="Failed" value={trackerDashboard.summary.failed_calls} tone="#fb7185" />
                <SummaryCard label="Win Rate" value={`${trackerDashboard.summary.win_rate.toFixed(1)}%`} tone="#2dd4bf" />
                <SummaryCard label="Avg Return" value={`${trackerDashboard.summary.average_return > 0 ? "+" : ""}${trackerDashboard.summary.average_return.toFixed(2)}%`} tone={trackerDashboard.summary.average_return >= 0 ? "#34d399" : "#fb7185"} />
              </div>
              <div className="review-grid">
                <div className="terminal-card soft"><div className="mono-label">Worked</div><div className="bullet-list">{trackerDashboard.todays_review.what_worked.length ? trackerDashboard.todays_review.what_worked.map((item) => <div key={`${item.setup_id}-${item.id}`}>- {item.symbol}: {item.note}</div>) : <div className="empty-state">No target hits logged recently.</div>}</div></div>
                <div className="terminal-card soft"><div className="mono-label">Failed</div><div className="bullet-list">{trackerDashboard.todays_review.what_failed.length ? trackerDashboard.todays_review.what_failed.map((item) => <div key={`${item.setup_id}-${item.id}`}>- {item.symbol}: {item.note}</div>) : <div className="empty-state">No invalidations logged recently.</div>}</div></div>
                <div className="terminal-card soft"><div className="mono-label">Changed</div><div className="bullet-list">{trackerDashboard.todays_review.what_changed.length ? trackerDashboard.todays_review.what_changed.map((item) => <div key={`${item.setup_id}-${item.id}`}>- {item.symbol}: {humanize(item.label)} | {item.note}</div>) : <div className="empty-state">No material setup changes logged recently.</div>}</div></div>
              </div>
            </>
          ) : <div className="loading-text">Loading performance journal...</div>}
        </section>
      ) : null}
    </main>
  );
}
