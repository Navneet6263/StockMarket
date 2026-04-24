"use client";

import {
  TrackedSetup,
  humanize,
  toneForDirection,
  toneForRisk,
  toneForStatus,
  toneForTrackingLabel,
} from "../lib/market";

type TrackedSetupCardProps = {
  item: TrackedSetup;
  active?: boolean;
  onSelect: (symbol: string) => void;
  onUpdate?: (setupId: string, values: Record<string, unknown>) => Promise<void> | void;
  onArchive?: (setupId: string) => Promise<void> | void;
  onIgnore?: (setupId: string) => Promise<void> | void;
};

export default function TrackedSetupCard({
  item,
  active = false,
  onSelect,
  onUpdate,
  onArchive,
  onIgnore,
}: TrackedSetupCardProps) {
  function handleNote() {
    if (!onUpdate) return;
    const next = window.prompt("Add or update note", item.notes || "");
    if (next === null) return;
    onUpdate(item.id, { notes: next });
  }

  function handleTimeframe() {
    if (!onUpdate) return;
    const next = window.prompt("Update timeframe label", item.timeframe_label || "3-5 days");
    if (!next) return;
    onUpdate(item.id, { timeframe_label: next });
  }

  return (
    <div className={`terminal-card soft tracked-card ${active ? "is-active" : ""}`}>
      <div className="card-topline">
        <div>
          <div className="mono-label">{item.symbol}</div>
          <h3>{item.company_name || item.symbol}</h3>
          <div className="micro-copy">{item.sector || "Sector pending"} | {humanize(item.source_mode)}</div>
        </div>
        <div className="badge-row">
          <span className="chip" style={{ color: toneForDirection(item.direction) }}>{humanize(item.direction)}</span>
          <span className="chip" style={{ color: toneForStatus(item.status) }}>{humanize(item.status)}</span>
          <span className="chip ghost" style={{ color: toneForTrackingLabel(item.tracking_label) }}>
            {item.tracking_label}
          </span>
        </div>
      </div>

      <div className="mini-grid">
        <div className="mini-stat"><span>Entry</span><strong>INR {item.entry_price.toFixed(2)}</strong></div>
        <div className="mini-stat"><span>Target</span><strong>INR {item.target_price.toFixed(2)}</strong></div>
        <div className="mini-stat"><span>Stop</span><strong>{item.stop_loss ? `INR ${item.stop_loss.toFixed(2)}` : "-"}</strong></div>
        <div className="mini-stat"><span>Timeframe</span><strong>{item.timeframe_label}</strong></div>
        <div className="mini-stat"><span>Confidence</span><strong>{item.confidence.toFixed(0)}%</strong></div>
        <div className="mini-stat"><span>Result</span><strong style={{ color: toneForDirection(item.direction) }}>{`${(item.result_pct || 0) > 0 ? "+" : ""}${(item.result_pct || 0).toFixed(2)}%`}</strong></div>
      </div>

      <p className="card-summary">{item.reason_summary}</p>

      <div className="badge-row">
        <span className="chip">{humanize(item.scanner_bucket)}</span>
        <span className="chip" style={{ color: toneForRisk(item.risk_level) }}>{humanize(item.risk_level)} risk</span>
        <span className="chip">RR {item.risk_reward.toFixed(2)}</span>
        {item.pinned ? <span className="chip ghost">Pinned</span> : null}
      </div>

      <div className="micro-copy">
        {item.last_update_label || "Fresh setup"}: {item.last_update_note || "Tracked idea is active."}
      </div>
      {item.notes ? <div className="micro-copy">Note: {item.notes}</div> : null}

      <div className="action-row compact">
        <button type="button" className="ghost-button compact" onClick={() => onSelect(item.symbol)}>
          Open
        </button>
        {onUpdate ? (
          <button
            type="button"
            className="ghost-button compact"
            onClick={() => onUpdate(item.id, { pinned: !item.pinned })}
          >
            {item.pinned ? "Unpin" : "Pin"}
          </button>
        ) : null}
        {onUpdate ? <button type="button" className="ghost-button compact" onClick={handleNote}>Note</button> : null}
        {onUpdate ? <button type="button" className="ghost-button compact" onClick={handleTimeframe}>Timeframe</button> : null}
        {onArchive ? <button type="button" className="ghost-button compact" onClick={() => onArchive(item.id)}>Archive</button> : null}
        {onIgnore ? <button type="button" className="ghost-button compact" onClick={() => onIgnore(item.id)}>Ignore</button> : null}
      </div>
    </div>
  );
}
