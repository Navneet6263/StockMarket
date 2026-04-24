"use client";

import Link from "next/link";
import { FormEvent, startTransition, useCallback, useDeferredValue, useEffect, useRef, useState } from "react";

import {
  API_URL,
  MarketOverview,
  StockDetail,
  TrackerDashboard,
  TrackerSymbolHistory,
  humanize,
} from "../lib/market";
import OpportunityCard from "./OpportunityCard";
import ScannerPanel from "./ScannerPanel";
import SelectionModeBar from "./SelectionModeBar";
import StockDetailPanel from "./StockDetailPanel";
import TopSummaryRow from "./TopSummaryRow";
import TrackedBoard from "./TrackedBoard";

type Mode = "dashboard" | "scanner";

type MarketTerminalProps = {
  mode?: Mode;
};

type LoadDetailOptions = {
  background?: boolean;
  clearOnChange?: boolean;
};

const REFRESH_INTERVAL_MS = 60000;

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

export default function MarketTerminal({ mode = "dashboard" }: MarketTerminalProps) {
  const [overview, setOverview] = useState<MarketOverview | null>(null);
  const [trackerDashboard, setTrackerDashboard] = useState<TrackerDashboard | null>(null);
  const [scannerHighlightedSymbol, setScannerHighlightedSymbol] = useState("");
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [searchSymbol, setSearchSymbol] = useState("");
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

  const deferredSearch = useDeferredValue(searchSymbol.trim().toUpperCase());
  const activeDetailSymbol = selectedSymbol || scannerHighlightedSymbol;
  const activeDetailSymbolRef = useRef(activeDetailSymbol);

  useEffect(() => {
    activeDetailSymbolRef.current = activeDetailSymbol;
  }, [activeDetailSymbol]);

  useEffect(() => {
    currentDetailSymbolRef.current = detail?.symbol || "";
  }, [detail?.symbol]);

  const loadOverview = useCallback(async (forceRefresh = false) => {
    if (forceRefresh) setRefreshing(true);
    if (!hasLoadedOverviewRef.current) setOverviewLoading(true);
    try {
      setOverviewError("");
      const response = await fetch(`${API_URL}/api/market/overview?force_refresh=${forceRefresh ? "true" : "false"}`, {
        cache: "no-store",
      });
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
      const payload = (await response.json()) as TrackerDashboard;
      setTrackerDashboard(payload);
    } catch (err: unknown) {
      setTrackerError(err instanceof Error ? err.message : "tracker_error");
    }
  }, []);

  const loadDetail = useCallback(async (symbol: string, options: LoadDetailOptions = {}) => {
    if (!symbol) return;
    const requestId = ++detailRequestRef.current;
    if (options.clearOnChange) {
      setDetail(null);
    }
    if (!options.background) {
      setDetailLoading(true);
    }
    try {
      const response = await fetch(`${API_URL}/api/stocks/${encodeURIComponent(symbol)}?force_refresh=false`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`detail_${response.status}`);
      const payload = (await response.json()) as StockDetail;
      if (detailRequestRef.current !== requestId || activeDetailSymbolRef.current !== symbol) return;
      setDetailError("");
      setDetail(payload);
    } catch (err: unknown) {
      if (detailRequestRef.current !== requestId || activeDetailSymbolRef.current !== symbol) return;
      setDetailError(err instanceof Error ? err.message : "detail_error");
    } finally {
      if (detailRequestRef.current === requestId && !options.background) {
        setDetailLoading(false);
      }
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
      if (trackerSymbolRequestRef.current !== requestId || activeDetailSymbolRef.current !== symbol) return;
      setTrackedHistory(payload);
    } catch {}
  }, []);

  useEffect(() => {
    loadOverview(false);
    loadTrackerDashboard();
    const interval = setInterval(() => {
      loadOverview(false);
      loadTrackerDashboard();
      if (activeDetailSymbolRef.current) {
        loadDetail(activeDetailSymbolRef.current, { background: true });
        loadTrackedHistory(activeDetailSymbolRef.current);
      }
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadDetail, loadOverview, loadTrackedHistory, loadTrackerDashboard]);

  useEffect(() => {
    if (!activeDetailSymbol) {
      setDetail(null);
      setTrackedHistory(null);
      setDetailLoading(false);
      return;
    }
    loadDetail(activeDetailSymbol, {
      background: false,
      clearOnChange: currentDetailSymbolRef.current !== activeDetailSymbol,
    });
    loadTrackedHistory(activeDetailSymbol);
  }, [activeDetailSymbol, loadDetail, loadTrackedHistory]);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = searchSymbol.trim().toUpperCase();
    if (!next) return;
    startTransition(() => setSelectedSymbol(next));
  }

  function handleManualSelection(symbol: string) {
    startTransition(() => setSelectedSymbol(symbol));
  }

  function handleFollowScanner() {
    startTransition(() => setSelectedSymbol(null));
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
    await loadTrackedHistory(symbol);
  }

  async function handleUpdateSetup(setupId: string, values: Record<string, unknown>) {
    await fetch(`${API_URL}/api/tracker/setups/${encodeURIComponent(setupId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    await loadTrackerDashboard();
    if (activeDetailSymbolRef.current) {
      await loadTrackedHistory(activeDetailSymbolRef.current);
    }
  }

  async function handleArchiveSetup(setupId: string) {
    await fetch(`${API_URL}/api/tracker/setups/${encodeURIComponent(setupId)}/archive`, { method: "POST" });
    await loadTrackerDashboard();
    if (activeDetailSymbolRef.current) {
      await loadTrackedHistory(activeDetailSymbolRef.current);
    }
  }

  async function handleIgnoreSetup(setupId: string) {
    await fetch(`${API_URL}/api/tracker/setups/${encodeURIComponent(setupId)}/ignore`, { method: "POST" });
    await loadTrackerDashboard();
    if (activeDetailSymbolRef.current) {
      await loadTrackedHistory(activeDetailSymbolRef.current);
    }
  }

  const filterSignals = <T extends { symbol: string }>(items: T[]) => {
    if (!deferredSearch) return items;
    return items.filter((item) => item.symbol.includes(deferredSearch));
  };

  const title = mode === "dashboard" ? "Market Intelligence and Trade Journal" : "Scanner Explorer";
  const subtitle =
    mode === "dashboard"
      ? "The dashboard now scans the market, saves serious trade ideas, and keeps accountability on what passed, failed, or stayed active after the scanner moved on."
      : "This view emphasizes scanner buckets while the persistent watchlist keeps yesterday’s calls visible until target, invalidation, or expiry resolves them.";

  return (
    <main className="app-shell">
      <section className="hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Production Terminal</p>
          <h1 className="headline">{title}</h1>
          <p className="subline">{subtitle}</p>
        </div>

        <div className="terminal-card hero-panel">
          <div className="section-heading">
            <div>
              <div className="mono-label">Control Row</div>
              <h3>Scanner, watchlist, and trade-call control</h3>
            </div>
            <div className="badge-row">
              <span className="chip">{overview?.market_discovery?.source_mode || "auto"}</span>
              <span className="chip">{overview?.macro_context?.risk_mode ? humanize(overview.macro_context.risk_mode) : "loading"}</span>
              <span className="chip">Tracked {trackerDashboard?.summary.active_calls ?? 0}</span>
            </div>
          </div>

          <form className="search-form" onSubmit={handleSubmit}>
            <input
              className="search-input"
              value={searchSymbol}
              onChange={(event) => setSearchSymbol(event.target.value)}
              placeholder="Jump to symbol: RELIANCE, INFY, SBIN"
            />
            <button type="submit" className="terminal-button">Open Detail</button>
            <button type="button" className="ghost-button" onClick={() => loadOverview(true)} disabled={refreshing}>
              {refreshing ? "Refreshing..." : "Refresh Scan"}
            </button>
          </form>

          <SelectionModeBar
            activeDetailSymbol={activeDetailSymbol}
            scannerHighlightedSymbol={scannerHighlightedSymbol}
            selectedSymbol={selectedSymbol}
            onFollowScanner={handleFollowScanner}
          />

          <div className="action-row">
            <Link href={mode === "dashboard" ? "/opportunities" : "/"} className="chip ghost">
              {mode === "dashboard" ? "Open Scanner Explorer" : "Back To Dashboard"}
            </Link>
            <span className="micro-copy">API: {API_URL}</span>
          </div>
        </div>
      </section>

      {overviewError ? (
        <section className="terminal-card"><div className="empty-state">Error loading market intelligence: {overviewError}</div></section>
      ) : null}
      {trackerError ? (
        <section className="terminal-card"><div className="empty-state">Error loading tracked setups: {trackerError}</div></section>
      ) : null}

      <TopSummaryRow overview={overview} loading={overviewLoading} />

      <section className="terminal-card">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Scanner Feed</p>
            <h2>What the live scan wants you to review now</h2>
          </div>
          <div className="micro-copy">
            {overview?.generated_at ? `Updated ${new Date(overview.generated_at).toLocaleTimeString()}` : "Waiting for first scan"}
            {overview?.summary.scanner_leader ? ` | Scanner leader ${overview.summary.scanner_leader}` : ""}
          </div>
        </div>

        <div className="opportunity-grid">
          {filterSignals(overview?.top_opportunities || [])
            .slice(0, mode === "dashboard" ? 6 : 10)
            .map((item) => (
              <OpportunityCard
                key={item.symbol}
                item={item}
                active={activeDetailSymbol === item.symbol}
                onSelect={handleManualSelection}
              />
            ))}

          {!overviewLoading && !filterSignals(overview?.top_opportunities || []).length ? (
            <div className="empty-state-block">
              <strong>No confirmed priority setups right now.</strong>
              <span>Scanner is live; waiting for stronger multi-signal confirmation.</span>
            </div>
          ) : null}
        </div>
      </section>

      <section className="scanner-grid">
        <ScannerPanel
          title="Unusual Volume"
          subtitle="Big participation and acceleration"
          items={filterSignals(overview?.unusual_volume || []).slice(0, 8)}
          activeSymbol={activeDetailSymbol}
          emptyTitle="No unusual volume leaders right now."
          emptyMessage="Scanner is live; waiting for stronger participation and cleaner expansion."
          onSelect={handleManualSelection}
        />
        <ScannerPanel
          title="Breakout Candidates"
          subtitle="Structures trying to leave a range"
          items={filterSignals(overview?.breakout_candidates || []).slice(0, 8)}
          activeSymbol={activeDetailSymbol}
          emptyTitle="No confirmed breakouts right now."
          emptyMessage="Scanner is live; waiting for stronger range escape confirmation."
          onSelect={handleManualSelection}
        />
        <ScannerPanel
          title="Bearish Risk"
          subtitle="Weak tape and breakdown pressure"
          items={filterSignals(overview?.bearish_risks || []).slice(0, 8)}
          activeSymbol={activeDetailSymbol}
          emptyTitle="No concentrated bearish breakdowns right now."
          emptyMessage="Risk is present, but the scanner is not seeing enough downside confirmation to escalate this bucket."
          onSelect={handleManualSelection}
        />
        <section className="terminal-card scanner-panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Top Movers</p>
              <h3>Discovery feed from the live universe</h3>
            </div>
            <div className="micro-copy">Tape leaders, not always conviction-ranked</div>
          </div>
          <div className="scanner-table">
            {(overview?.top_movers || []).slice(0, 8).length ? (
              (overview?.top_movers || []).slice(0, 8).map((item) => (
                <button
                  key={`mover-${item.symbol}`}
                  type="button"
                  className={`scanner-row ${activeDetailSymbol === item.symbol ? "is-active" : ""}`}
                  onClick={() => handleManualSelection(item.symbol)}
                >
                  <div className="scanner-symbol">
                    <span className="mono-label">{item.symbol}</span>
                    <small>{(item.tags || []).slice(0, 2).map(humanize).join(" | ") || "live screener"}</small>
                  </div>
                  <div className="scanner-metrics">
                    <span>{item.price ? `INR ${item.price.toFixed(2)}` : "-"}</span>
                    <span style={{ color: (item.change_pct || 0) >= 0 ? "#34d399" : "#fb7185" }}>
                      {(item.change_pct || 0) >= 0 ? "+" : ""}
                      {(item.change_pct || 0).toFixed(2)}%
                    </span>
                  </div>
                </button>
              ))
            ) : (
              <div className="empty-state-block">
                <strong>No outsized tape leaders right now.</strong>
                <span>The discovery feed is live; waiting for clearer mover separation.</span>
              </div>
            )}
          </div>
        </section>
      </section>

      <TrackedBoard
        dashboard={trackerDashboard}
        activeSymbol={activeDetailSymbol}
        onSelectSymbol={handleManualSelection}
        onUpdateSetup={handleUpdateSetup}
        onArchiveSetup={handleArchiveSetup}
        onIgnoreSetup={handleIgnoreSetup}
      />

      <StockDetailPanel
        activeSymbol={activeDetailSymbol}
        detail={detail}
        trackedHistory={trackedHistory}
        onSaveManualWatch={handleSaveManualWatch}
        error={detailError}
        loading={detailLoading}
      />
    </main>
  );
}
