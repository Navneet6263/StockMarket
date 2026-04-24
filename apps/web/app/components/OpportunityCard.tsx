"use client";

import { MarketSignal, humanize, toneForDirection, toneForRisk } from "../lib/market";

type OpportunityCardProps = {
  item: MarketSignal;
  active: boolean;
  onSelect: (symbol: string) => void;
};

export default function OpportunityCard({ item, active, onSelect }: OpportunityCardProps) {
  return (
    <button
      type="button"
      className={`opportunity-card ${active ? "is-active" : ""}`}
      onClick={() => onSelect(item.symbol)}
    >
      <div className="card-topline">
        <div>
          <div className="mono-label">{item.symbol}</div>
          <h3>{item.setup_label}</h3>
        </div>
        <div style={{ color: toneForDirection(item.direction), fontWeight: 700 }}>
          {item.direction.toUpperCase()}
        </div>
      </div>

      <div className="card-row">
        <div className="price-stack">
          <div className="price-tag">INR {item.current_price.toFixed(2)}</div>
          <div style={{ color: item.change_pct >= 0 ? "#34d399" : "#fb7185" }}>
            {item.change_pct >= 0 ? "+" : ""}
            {item.change_pct.toFixed(2)}%
          </div>
        </div>
        <div className="badge-row">
          <span className="chip">{humanize(item.alert_level)}</span>
          <span className="chip" style={{ color: toneForRisk(item.risk_level) }}>
            {humanize(item.risk_level)} risk
          </span>
          {item.historical_evidence_status !== "validated" ? (
            <span className="chip ghost">
              {item.historical_evidence_status === "limited" ? "limited evidence" : "model-driven"}
            </span>
          ) : null}
        </div>
      </div>

      <div className="mini-grid">
        <div className="mini-stat">
          <span>Confidence</span>
          <strong>{item.confidence.toFixed(0)}%</strong>
        </div>
        <div className="mini-stat">
          <span>Move Quality</span>
          <strong>{item.move_quality}</strong>
        </div>
        <div className="mini-stat">
          <span>Rel Volume</span>
          <strong>{item.relative_volume.toFixed(2)}x</strong>
        </div>
        <div className="mini-stat">
          <span>Expected Move</span>
          <strong>{item.expected_move_pct > 0 ? "+" : ""}{item.expected_move_pct.toFixed(2)}%</strong>
        </div>
        <div className="mini-stat">
          <span>Timeframe</span>
          <strong>{item.timeframe_label}</strong>
        </div>
        <div className="mini-stat">
          <span>Target</span>
          <strong>INR {item.target_price.toFixed(2)}</strong>
        </div>
      </div>

      <p className="card-summary">{item.signal_summary}</p>

      <div className="score-bar">
        <div className="score-fill" style={{ width: `${item.confidence}%`, background: toneForDirection(item.direction) }} />
      </div>

      <div className="badge-row">
        {(item.tags || []).slice(0, 3).map((tag) => (
          <span key={`${item.symbol}-${tag}`} className="chip ghost">
            {humanize(tag)}
          </span>
        ))}
      </div>

      <div className="micro-copy">
        Invalidation: {item.invalidation ? `INR ${item.invalidation.toFixed(2)}` : "watch support and VWAP"}
      </div>
    </button>
  );
}
