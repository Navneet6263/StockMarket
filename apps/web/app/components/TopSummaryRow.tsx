"use client";

import { MarketOverview, humanize } from "../lib/market";

type TopSummaryRowProps = {
  overview: MarketOverview | null;
  loading: boolean;
};

function SummaryCard({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="terminal-card soft">
      <div className="mono-label">{label}</div>
      <div className="summary-value">{value}</div>
      <div className="micro-copy">{note}</div>
    </div>
  );
}

export default function TopSummaryRow({ overview, loading }: TopSummaryRowProps) {
  return (
    <section className="stat-grid">
      <SummaryCard
        label="Market Mood"
        value={overview ? humanize(overview.summary.market_mood) : loading ? "Loading..." : "-"}
        note="Breadth, benchmark tone, and scanner balance"
      />
      <SummaryCard
        label="Opportunities"
        value={overview ? `${overview.summary.opportunities_count}` : "-"}
        note="Ranked setups with usable confirmation"
      />
      <SummaryCard
        label="Breakouts"
        value={overview ? `${overview.summary.breakout_count}` : "-"}
        note="Names testing or clearing recent structure"
      />
      <SummaryCard
        label="Bearish Risk"
        value={overview ? `${overview.summary.bearish_risk_count}` : "-"}
        note="Setups with weak confirmation or downside pressure"
      />
      <SummaryCard
        label="Breadth"
        value={
          overview
            ? `${overview.market_breadth.advancing}/${overview.market_breadth.declining}`
            : "-"
        }
        note={
          overview
            ? `${overview.market_breadth.bullish_setups} bullish vs ${overview.market_breadth.bearish_setups} bearish`
            : "Advancing versus declining names"
        }
      />
      <SummaryCard
        label="Benchmark"
        value={
          overview
            ? `${overview.market_breadth.benchmark_change_pct >= 0 ? "+" : ""}${overview.market_breadth.benchmark_change_pct.toFixed(2)}%`
            : "-"
        }
        note="Session change in the reference index"
      />
    </section>
  );
}
