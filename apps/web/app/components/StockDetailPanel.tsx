"use client";

import LiveChart from "./LiveChart";
import {
  StockDetail,
  TrackerSymbolHistory,
  humanize,
  toneForDirection,
  toneForEvidence,
  toneForRisk,
  toneForStatus,
  toneForTrackingLabel,
} from "../lib/market";

type StockDetailPanelProps = {
  activeSymbol: string;
  detail: StockDetail | null;
  trackedHistory?: TrackerSymbolHistory | null;
  onSaveManualWatch?: (symbol: string) => Promise<void> | void;
  error?: string;
  loading: boolean;
};

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="mini-stat">
      <span>{label}</span>
      <strong style={{ color: tone }}>{value}</strong>
    </div>
  );
}

function BulletPanel({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="terminal-card soft">
      <div className="mono-label">{title}</div>
      <div className="bullet-list">
        {items.length ? items.map((item, index) => <div key={`${title}-${index}`}>- {item}</div>) : <div className="empty-state">No extra context available.</div>}
      </div>
    </div>
  );
}

export default function StockDetailPanel({
  activeSymbol,
  detail,
  trackedHistory,
  onSaveManualWatch,
  error,
  loading,
}: StockDetailPanelProps) {
  if (loading && !detail) {
    return <section className="terminal-card detail-panel"><div className="loading-text">Loading selected stock intelligence...</div></section>;
  }

  if (error && !detail) {
    return (
      <section className="terminal-card detail-panel">
        <div className="empty-state">Unable to load detail for {activeSymbol || "the selected symbol"} right now. {error}</div>
      </section>
    );
  }

  if (!detail) {
    return <section className="terminal-card detail-panel"><div className="empty-state">Select a stock from the dashboard to open its explanation panel.</div></section>;
  }

  const signal = detail.prediction;
  const business = detail.company_context.business_model;
  const backtestSide = signal.direction === "bearish" ? detail.backtest.bearish : detail.backtest.bullish;
  const evidenceCount = backtestSide?.signal_count ?? 0;
  const evidenceUnavailable = signal.direction !== "neutral" && evidenceCount === 0;
  const evidenceLimited = signal.direction !== "neutral" && evidenceCount > 0 && evidenceCount < 8;
  const latestTracked = trackedHistory?.setups?.[0];

  return (
    <section className="terminal-card detail-panel">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Detail Panel</p>
          <h2>{detail.company_context.company_name || detail.symbol}</h2>
          <p className="section-note">
            {[detail.company_context.sector, detail.company_context.industry, detail.company_context.market_cap_label].filter(Boolean).join(" | ")}
          </p>
        </div>
        <div className="badge-row">
          <span className="chip">{signal.setup_label}</span>
          <span className="chip" style={{ color: toneForDirection(signal.direction) }}>{humanize(signal.direction)}</span>
          <span className="chip" style={{ color: toneForRisk(signal.risk_level) }}>{humanize(signal.risk_level)} risk</span>
          <span className="chip" style={{ color: toneForEvidence(signal.historical_evidence_status) }}>{humanize(signal.historical_evidence_status)} evidence</span>
        </div>
      </div>

      <div className="stat-grid">
        <Stat label="Bias" value={humanize(signal.direction)} tone={toneForDirection(signal.direction)} />
        <Stat label="Confidence" value={`${signal.confidence.toFixed(0)}%`} />
        <Stat label="Risk" value={humanize(signal.risk_level)} tone={toneForRisk(signal.risk_level)} />
        <Stat label="Expected Move" value={`${signal.expected_move_pct > 0 ? "+" : ""}${signal.expected_move_pct.toFixed(2)}%`} tone={toneForDirection(signal.direction)} />
        <Stat label="Timeframe" value={signal.timeframe_label} />
        <Stat label="Invalidation" value={signal.invalidation ? `INR ${signal.invalidation.toFixed(2)}` : "Monitor support"} tone={toneForRisk(signal.risk_level)} />
      </div>

      <div className="detail-grid detail-priority">
        <div className="detail-main">
          <LiveChart symbol={detail.symbol} candles={detail.chart} prediction={signal} quote={detail.quote} />
        </div>
        <div className="detail-aside">
          <BulletPanel title="Why It Is Flagged" items={detail.explanation.why_it_is_flagged || detail.signals.reasons} />
          <BulletPanel title="What Is Strong" items={detail.explanation.what_is_strong || []} />
          <BulletPanel title="What Is Weak" items={detail.explanation.what_is_weak || detail.signals.weaknesses} />
          <BulletPanel title="Watch Next" items={detail.explanation.watch_next || []} />
        </div>
      </div>

      <div className="terminal-card soft">
        <div className="mono-label">Confidence Read</div>
        <p className="detail-summary">{signal.confidence_note}</p>
        <div className="micro-copy">
          Model confidence {signal.model_confidence.toFixed(0)}%
          {signal.evidence_confidence !== null && signal.evidence_confidence !== undefined
            ? ` | Historical hit-rate proxy ${signal.evidence_confidence.toFixed(0)}%`
            : " | Historical hit-rate proxy unavailable"}
        </div>
        {error ? <div className="micro-copy">Latest refresh note: {error}</div> : null}
      </div>

      <div className="terminal-card soft">
        <div className="section-heading">
          <div>
            <div className="mono-label">Watchlist Control</div>
            <h3>Persistent tracking beyond the live scanner</h3>
          </div>
          {onSaveManualWatch ? (
            <button type="button" className="terminal-button" onClick={() => onSaveManualWatch(detail.symbol)}>
              Save To Manual Watchlist
            </button>
          ) : null}
        </div>
        {latestTracked ? (
          <>
            <div className="mini-grid">
              <div className="mini-stat"><span>Status</span><strong style={{ color: toneForStatus(latestTracked.status) }}>{humanize(latestTracked.status)}</strong></div>
              <div className="mini-stat"><span>Tracking Label</span><strong style={{ color: toneForTrackingLabel(latestTracked.tracking_label) }}>{latestTracked.tracking_label}</strong></div>
              <div className="mini-stat"><span>Target</span><strong>INR {latestTracked.target_price.toFixed(2)}</strong></div>
              <div className="mini-stat"><span>Stop</span><strong>{latestTracked.stop_loss ? `INR ${latestTracked.stop_loss.toFixed(2)}` : "-"}</strong></div>
              <div className="mini-stat"><span>Result</span><strong>{`${(latestTracked.result_pct || 0) > 0 ? "+" : ""}${(latestTracked.result_pct || 0).toFixed(2)}%`}</strong></div>
              <div className="mini-stat"><span>Latest Update</span><strong>{latestTracked.last_update_label || "Fresh setup"}</strong></div>
            </div>
            <div className="micro-copy">{latestTracked.last_update_note || "Tracked setup is active."}</div>
          </>
        ) : (
          <div className="empty-state-block">
            <strong>No persistent tracked setup yet.</strong>
            <span>Save the current symbol to manual watchlist if you want it tracked even after it leaves the live scanner.</span>
          </div>
        )}
      </div>

      <div className="detail-grid">
        <div className="detail-main">
          <div className="terminal-card soft">
            <div className="mono-label">Trade Explanation</div>
            <p className="detail-summary">{detail.explanation.summary}</p>
            <div className="micro-copy">{detail.explanation.macro_take}</div>
            <div className="micro-copy">{detail.explanation.global_take}</div>
          </div>
        </div>

        <div className="detail-aside">
          <BulletPanel title="Risk Factors" items={detail.explanation.risk_factors || detail.signals.risk_factors} />
        </div>
      </div>

      <div className="detail-grid">
        <div className="terminal-card soft">
          <div className="mono-label">Backtest Evidence</div>
          {evidenceUnavailable ? (
            <div className="empty-state-block">
              <strong>Historical evidence unavailable.</strong>
              <span>No comparable signals were found in the current backtest window. Treat the confidence score as model-driven live alignment, not validated edge.</span>
            </div>
          ) : (
            <>
              <div className="mini-grid">
                <div className="mini-stat"><span>Signals</span><strong>{backtestSide?.signal_count ?? 0}</strong></div>
                <div className="mini-stat"><span>Win Rate</span><strong>{((backtestSide?.win_rate ?? 0) * 100).toFixed(1)}%</strong></div>
                <div className="mini-stat"><span>Profit Factor</span><strong>{backtestSide?.profit_factor ?? 0}</strong></div>
                <div className="mini-stat"><span>Expectancy</span><strong>{backtestSide?.expectancy_pct ?? 0}%</strong></div>
              </div>
              {evidenceLimited ? <div className="micro-copy">Historical sample size is still limited. Use the win rate as context, not as robust validation.</div> : null}
              <div className="bullet-list">
                {(detail.backtest.calibration || []).slice(0, 4).length ? (
                  (detail.backtest.calibration || []).slice(0, 4).map((item) => (
                    <div key={item.bucket}>- Confidence {item.bucket}: {item.signal_count} signals, {(item.win_rate * 100).toFixed(1)}% win rate</div>
                  ))
                ) : (
                  <div className="empty-state">Confidence bucket calibration is not populated yet.</div>
                )}
              </div>
            </>
          )}
        </div>

        <div className="terminal-card soft">
          <div className="mono-label">Business Quality</div>
          <p className="detail-summary">{detail.explanation.business_take}</p>
          <div className="mini-grid">
            <div className="mini-stat"><span>Rating</span><strong>{humanize(business?.rating)}</strong></div>
            <div className="mini-stat"><span>Score</span><strong>{business?.score ?? "-"}</strong></div>
          </div>
          <div className="bullet-list">
            {(business?.strengths || []).slice(0, 3).map((item, index) => <div key={`strength-${index}`}>- {item}</div>)}
            {(business?.risks || []).slice(0, 2).map((item, index) => <div key={`risk-${index}`}>- Risk: {item}</div>)}
          </div>
        </div>

        <div className="terminal-card soft">
          <div className="mono-label">Tracked Setup History</div>
          <div className="bullet-list">
            {trackedHistory?.setups?.length ? (
              trackedHistory.setups.slice(0, 5).map((item) => (
                <div key={item.id}>
                  - {new Date(item.detected_at).toLocaleDateString()}: {item.tracking_label}, {humanize(item.status)}, target {item.target_price.toFixed(2)}, stop {item.stop_loss ? item.stop_loss.toFixed(2) : "-"}
                </div>
              ))
            ) : (
              <div className="empty-state">No historical tracked calls recorded yet for this symbol.</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
