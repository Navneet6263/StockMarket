"use client";

import {
  TrackedSetup,
  humanize,
  toneForAdvice,
  toneForDirection,
  toneForRisk,
  toneForStatus,
  fmt,
  fmtPct,
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
  const advice = item.hold_or_exit || "WAIT";
  const pnl = item.current_pnl_pct ?? item.result_pct ?? 0;
  const why = item.exit_reason ? humanize(item.exit_reason) : item.reason_for_exit_decision || item.last_update_note || "Lifecycle check pending.";

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
          <div className="micro-copy">
            {humanize(item.source_mode)}
            {item.suggested_at ? ` | ${new Date(item.suggested_at).toLocaleString()}` : ""}
          </div>
        </div>
        <div className="badge-row">
          <span className="chip" style={{ color: toneForDirection(item.direction) }}>{humanize(item.direction)}</span>
          <span className="chip" style={{ color: toneForAdvice(advice) }}>{humanize(advice)}</span>
          <span className="chip ghost" style={{ color: toneForStatus(item.status) }}>{humanize(item.scanner_call_status || item.status)}</span>
        </div>
      </div>

      <div className="mini-grid">
        <div className="mini-stat"><span>Entry</span><strong>INR {fmt(item.entry_price)}</strong></div>
        <div className="mini-stat"><span>Current</span><strong>INR {fmt(item.current_price ?? item.entry_price)}</strong></div>
        <div className="mini-stat"><span>Target</span><strong>INR {fmt(item.target_1 ?? item.target_price)}</strong></div>
        <div className="mini-stat"><span>Stop</span><strong>{item.stop_loss ? `INR ${fmt(item.stop_loss)}` : "-"}</strong></div>
        <div className="mini-stat"><span>Trail</span><strong>{item.trailing_stop ? `INR ${fmt(item.trailing_stop)}` : "-"}</strong></div>
        <div className="mini-stat"><span>P&L</span><strong style={{ color: pnl >= 0 ? "#34d399" : "#fb7185" }}>{fmtPct(pnl)}</strong></div>
        <div className="mini-stat"><span>Advice</span><strong style={{ color: toneForAdvice(advice) }}>{humanize(advice)}</strong></div>
      </div>

      <p className="card-summary">Why: {why}</p>

      <div className="badge-row">
        <span className="chip">{item.timeframe_label}</span>
        <span className="chip">{`${fmt(item.target_progress_pct ?? 0, 0)}% progress`}</span>
        <span className="chip" style={{ color: toneForRisk(item.risk_level) }}>{humanize(item.risk_level)} risk</span>
        {item.pinned ? <span className="chip ghost">Pinned</span> : null}
      </div>
      {item.notes ? <div className="micro-copy">Note: {item.notes}</div> : null}

      <div className="action-row compact">
        <button type="button" className="ghost-button compact" onClick={() => onSelect(item.symbol)}>
          View Detail
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
