"use client";

import { TrackerDashboard, TrackedSetup, humanize } from "../lib/market";
import TrackedSetupCard from "./TrackedSetupCard";

type TrackedBoardProps = {
  dashboard: TrackerDashboard | null;
  activeSymbol: string;
  onSelectSymbol: (symbol: string) => void;
  onUpdateSetup: (setupId: string, values: Record<string, unknown>) => Promise<void> | void;
  onArchiveSetup: (setupId: string) => Promise<void> | void;
  onIgnoreSetup: (setupId: string) => Promise<void> | void;
};

function Bucket({
  title,
  subtitle,
  items,
  activeSymbol,
  onSelectSymbol,
  onUpdateSetup,
  onArchiveSetup,
  onIgnoreSetup,
}: {
  title: string;
  subtitle: string;
  items: TrackedSetup[];
  activeSymbol: string;
  onSelectSymbol: (symbol: string) => void;
  onUpdateSetup: (setupId: string, values: Record<string, unknown>) => Promise<void> | void;
  onArchiveSetup: (setupId: string) => Promise<void> | void;
  onIgnoreSetup: (setupId: string) => Promise<void> | void;
}) {
  return (
    <section className="terminal-card">
      <div className="section-heading">
        <div>
          <p className="eyebrow">{title}</p>
          <h3>{subtitle}</h3>
        </div>
        <div className="micro-copy">{items.length ? `${items.length} tracked` : "No tracked items"}</div>
      </div>
      <div className="watch-grid">
        {items.length ? (
          items.map((item) => (
            <TrackedSetupCard
              key={item.id}
              item={item}
              active={activeSymbol === item.symbol}
              onSelect={onSelectSymbol}
              onUpdate={onUpdateSetup}
              onArchive={onArchiveSetup}
              onIgnore={onIgnoreSetup}
            />
          ))
        ) : (
          <div className="empty-state-block">
            <strong>No tracked ideas here right now.</strong>
            <span>Scanner-generated calls stay visible once promoted; this section is waiting for the next qualified setup.</span>
          </div>
        )}
      </div>
    </section>
  );
}

export default function TrackedBoard({
  dashboard,
  activeSymbol,
  onSelectSymbol,
  onUpdateSetup,
  onArchiveSetup,
  onIgnoreSetup,
}: TrackedBoardProps) {
  if (!dashboard) {
    return (
      <section className="terminal-card">
        <div className="loading-text">Loading watchlists, trade ideas, and performance journal...</div>
      </section>
    );
  }

  return (
    <>
      <section className="terminal-card">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Performance Dashboard</p>
            <h2>Scanner accountability and trade-call journal</h2>
          </div>
          <div className="micro-copy">Updated {new Date(dashboard.generated_at).toLocaleTimeString()}</div>
        </div>
        <div className="stat-grid">
          <div className="terminal-card soft"><div className="mono-label">Total Calls</div><div className="summary-value">{dashboard.summary.total_calls}</div></div>
          <div className="terminal-card soft"><div className="mono-label">Active</div><div className="summary-value">{dashboard.summary.active_calls}</div></div>
          <div className="terminal-card soft"><div className="mono-label">Passed</div><div className="summary-value">{dashboard.summary.passed_calls}</div></div>
          <div className="terminal-card soft"><div className="mono-label">Failed</div><div className="summary-value">{dashboard.summary.failed_calls}</div></div>
          <div className="terminal-card soft"><div className="mono-label">Win Rate</div><div className="summary-value">{dashboard.summary.win_rate.toFixed(1)}%</div></div>
          <div className="terminal-card soft"><div className="mono-label">Avg Return</div><div className="summary-value">{`${dashboard.summary.average_return > 0 ? "+" : ""}${dashboard.summary.average_return.toFixed(2)}%`}</div></div>
        </div>
        <div className="mini-grid" style={{ marginTop: 12 }}>
          <div className="mini-stat">
            <span>Best Call</span>
            <strong>{dashboard.summary.best_call ? `${dashboard.summary.best_call.symbol} ${dashboard.summary.best_call.result_pct?.toFixed(2)}%` : "No closed calls yet"}</strong>
          </div>
          <div className="mini-stat">
            <span>Worst Call</span>
            <strong>{dashboard.summary.worst_call ? `${dashboard.summary.worst_call.symbol} ${dashboard.summary.worst_call.result_pct?.toFixed(2)}%` : "No closed calls yet"}</strong>
          </div>
        </div>
      </section>

      <Bucket
        title="Saved Trade Ideas"
        subtitle="Today’s fresh setups promoted from the scanner"
        items={dashboard.watchlists.fresh_setups}
        activeSymbol={activeSymbol}
        onSelectSymbol={onSelectSymbol}
        onUpdateSetup={onUpdateSetup}
        onArchiveSetup={onArchiveSetup}
        onIgnoreSetup={onIgnoreSetup}
      />

      <section className="watch-columns">
        <Bucket
          title="Active Bullish Setups"
          subtitle="Long-side ideas still alive"
          items={dashboard.watchlists.active_bullish}
          activeSymbol={activeSymbol}
          onSelectSymbol={onSelectSymbol}
          onUpdateSetup={onUpdateSetup}
          onArchiveSetup={onArchiveSetup}
          onIgnoreSetup={onIgnoreSetup}
        />
        <Bucket
          title="Active Bearish Setups"
          subtitle="Downside ideas still being tracked"
          items={dashboard.watchlists.active_bearish}
          activeSymbol={activeSymbol}
          onSelectSymbol={onSelectSymbol}
          onUpdateSetup={onUpdateSetup}
          onArchiveSetup={onArchiveSetup}
          onIgnoreSetup={onIgnoreSetup}
        />
      </section>

      <section className="watch-columns">
        <Bucket
          title="Passed Calls"
          subtitle="Targets reached before invalidation"
          items={dashboard.watchlists.passed_calls}
          activeSymbol={activeSymbol}
          onSelectSymbol={onSelectSymbol}
          onUpdateSetup={onUpdateSetup}
          onArchiveSetup={onArchiveSetup}
          onIgnoreSetup={onIgnoreSetup}
        />
        <Bucket
          title="Failed Calls"
          subtitle="Ideas invalidated before target"
          items={dashboard.watchlists.failed_calls}
          activeSymbol={activeSymbol}
          onSelectSymbol={onSelectSymbol}
          onUpdateSetup={onUpdateSetup}
          onArchiveSetup={onArchiveSetup}
          onIgnoreSetup={onIgnoreSetup}
        />
      </section>

      <section className="watch-columns">
        <Bucket
          title="Expired Calls"
          subtitle="No follow-through inside the stated timeframe"
          items={dashboard.watchlists.expired_calls}
          activeSymbol={activeSymbol}
          onSelectSymbol={onSelectSymbol}
          onUpdateSetup={onUpdateSetup}
          onArchiveSetup={onArchiveSetup}
          onIgnoreSetup={onIgnoreSetup}
        />
        <Bucket
          title="Manual Watchlist"
          subtitle="User-controlled ideas and pinned names"
          items={dashboard.watchlists.manual_watchlist}
          activeSymbol={activeSymbol}
          onSelectSymbol={onSelectSymbol}
          onUpdateSetup={onUpdateSetup}
          onArchiveSetup={onArchiveSetup}
          onIgnoreSetup={onIgnoreSetup}
        />
      </section>

      <section className="terminal-card">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Today’s Review</p>
            <h3>What worked, what failed, and what changed since yesterday</h3>
          </div>
        </div>
        <div className="review-grid">
          <div className="terminal-card soft">
            <div className="mono-label">Worked</div>
            <div className="bullet-list">
              {dashboard.todays_review.what_worked.length ? (
                dashboard.todays_review.what_worked.map((item) => (
                  <div key={`${item.setup_id}-${item.id}`}>- {item.symbol}: {item.note}</div>
                ))
              ) : (
                <div className="empty-state">No target hits logged in the latest review window.</div>
              )}
            </div>
          </div>
          <div className="terminal-card soft">
            <div className="mono-label">Failed</div>
            <div className="bullet-list">
              {dashboard.todays_review.what_failed.length ? (
                dashboard.todays_review.what_failed.map((item) => (
                  <div key={`${item.setup_id}-${item.id}`}>- {item.symbol}: {item.note}</div>
                ))
              ) : (
                <div className="empty-state">No invalidations logged in the latest review window.</div>
              )}
            </div>
          </div>
          <div className="terminal-card soft">
            <div className="mono-label">Changed</div>
            <div className="bullet-list">
              {dashboard.todays_review.what_changed.length ? (
                dashboard.todays_review.what_changed.map((item) => (
                  <div key={`${item.setup_id}-${item.id}`}>- {item.symbol}: {humanize(item.label)} | {item.note}</div>
                ))
              ) : (
                <div className="empty-state">No material setup-strength changes logged recently.</div>
              )}
            </div>
          </div>
        </div>
      </section>
    </>
  );
}
