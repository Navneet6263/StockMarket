"use client";

import { MarketSignal, humanize, toneForDirection } from "../lib/market";

type ScannerPanelProps = {
  title: string;
  subtitle: string;
  items: MarketSignal[];
  activeSymbol?: string;
  emptyTitle?: string;
  emptyMessage?: string;
  onSelect: (symbol: string) => void;
};

export default function ScannerPanel({
  title,
  subtitle,
  items,
  activeSymbol,
  emptyTitle,
  emptyMessage,
  onSelect,
}: ScannerPanelProps) {
  return (
    <section className="terminal-card scanner-panel">
      <div className="section-heading">
        <div>
          <p className="eyebrow">{title}</p>
          <h3>{subtitle}</h3>
        </div>
        <div className="micro-copy">{items.length ? `${items.length} live names` : "Scanner is live"}</div>
      </div>

      <div className="scanner-table">
        {items.length ? (
          items.map((item) => (
            <button
              key={`${title}-${item.symbol}`}
              type="button"
              className={`scanner-row ${activeSymbol === item.symbol ? "is-active" : ""}`}
              onClick={() => onSelect(item.symbol)}
            >
              <div className="scanner-symbol">
                <span className="mono-label">{item.symbol}</span>
                <small>{item.setup_label}</small>
              </div>
              <div className="scanner-metrics">
                <span style={{ color: toneForDirection(item.direction) }}>{item.confidence.toFixed(0)}%</span>
                <span>{item.relative_volume.toFixed(2)}x</span>
                <span>{item.expected_move_pct > 0 ? "+" : ""}{item.expected_move_pct.toFixed(2)}%</span>
              </div>
            </button>
          ))
        ) : (
          <div className="empty-state-block">
            <strong>{emptyTitle || "No confirmed setups right now."}</strong>
            <span>{emptyMessage || "Scanner is live; waiting for stronger confirmation."}</span>
          </div>
        )}
      </div>
    </section>
  );
}
