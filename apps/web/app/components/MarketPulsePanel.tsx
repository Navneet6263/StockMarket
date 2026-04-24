"use client";

import { useEffect, useState, type CSSProperties } from "react";

type MarketPulseProps = {
  symbol: string;
};

type MacroItem = {
  key: string;
  label: string;
  value: number;
  change_pct?: number;
  change_bps?: number;
  bias: string;
  signal: string;
  implication: string;
};

type NewsItem = {
  title: string;
  publisher: string;
  published_at?: string;
  bias?: string;
  link?: string;
};

type ContextPayload = {
  symbol: string;
  macro: {
    risk_mode: string;
    summary: string;
    routine: string[];
    checklist: MacroItem[];
  };
  news: {
    sentiment: string;
    note: string;
    items: NewsItem[];
  };
};

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function toneColor(bias?: string) {
  if (bias === "bullish") return "#22c55e";
  if (bias === "bearish") return "#f97316";
  return "#94a3b8";
}

export default function MarketPulsePanel({ symbol }: MarketPulseProps) {
  const [context, setContext] = useState<ContextPayload | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        setError("");
        const res = await fetch(`${API}/market/context?symbol=${encodeURIComponent(symbol)}&limit=5`);
        if (!res.ok) {
          throw new Error(`market_context_${res.status}`);
        }
        const data = (await res.json()) as ContextPayload;
        if (active) setContext(data);
      } catch (err: unknown) {
        if (active) {
          setError(err instanceof Error ? err.message : "market_context_error");
        }
      }
    }

    load();
    const interval = setInterval(load, 120000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [symbol]);

  if (error && !context) {
    return (
      <div style={panelStyle}>
        <div style={eyebrowStyle}>Market Pulse</div>
        <div style={{ color: "#f97316", marginTop: 8 }}>Unable to load macro context: {error}</div>
      </div>
    );
  }

  const macro = context?.macro;
  const news = context?.news;

  return (
    <section style={panelStyle}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div>
          <div style={eyebrowStyle}>Morning Filter</div>
          <h3 style={{ margin: "8px 0 6px", fontSize: 28 }}>Crude, Bond Yield, VIX</h3>
          <p style={{ margin: 0, color: "#cbd5e1", maxWidth: 720 }}>
            Ignore noisy headlines first. Check these three gauges every morning, then look at price and volume.
          </p>
        </div>
        {macro && (
          <div
            style={{
              padding: "10px 14px",
              borderRadius: 999,
              background: macro.risk_mode === "risk_on" ? "rgba(34,197,94,0.12)" : macro.risk_mode === "risk_off" ? "rgba(249,115,22,0.12)" : "rgba(148,163,184,0.12)",
              color: macro.risk_mode === "risk_on" ? "#4ade80" : macro.risk_mode === "risk_off" ? "#fdba74" : "#cbd5e1",
              fontSize: 12,
              fontWeight: 700,
              letterSpacing: 1,
              textTransform: "uppercase",
              border: "1px solid rgba(148,163,184,0.15)",
            }}
          >
            {macro.risk_mode.replace("_", " ")}
          </div>
        )}
      </div>

      {macro && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14, marginTop: 18 }}>
            {macro.checklist.map((item) => (
              <div key={item.key} style={macroCardStyle}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                  <div>
                    <div style={{ fontSize: 11, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1 }}>{item.label}</div>
                    <div style={{ marginTop: 8, fontSize: 26, fontWeight: 700, color: "#f8fafc" }}>{item.value}</div>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <div style={{ color: toneColor(item.bias), fontWeight: 700 }}>{item.signal}</div>
                    <div style={{ marginTop: 6, fontSize: 13, color: toneColor(item.bias) }}>
                      {item.change_pct !== undefined ? `${item.change_pct >= 0 ? "+" : ""}${item.change_pct.toFixed(2)}%` : `${item.change_bps?.toFixed(1)} bps`}
                    </div>
                  </div>
                </div>
                <p style={{ margin: "12px 0 0", color: "#cbd5e1", fontSize: 13, lineHeight: 1.6 }}>{item.implication}</p>
              </div>
            ))}
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: 16, marginTop: 18 }}>
            <div style={subPanelStyle}>
              <div style={subLabelStyle}>How To Read It</div>
              <p style={{ margin: "8px 0 0", color: "#e2e8f0", lineHeight: 1.7 }}>{macro.summary}</p>
              <div style={{ marginTop: 14, display: "grid", gap: 10 }}>
                {macro.routine.map((item, index) => (
                  <div key={index} style={{ color: "#cbd5e1", fontSize: 14, lineHeight: 1.6 }}>
                    {index + 1}. {item}
                  </div>
                ))}
              </div>
            </div>

            <div style={subPanelStyle}>
              <div style={subLabelStyle}>Headline Watch</div>
              <div style={{ marginTop: 10, display: "grid", gap: 10 }}>
                {news?.items?.length ? (
                  news.items.slice(0, 3).map((item, index) => (
                    <div key={`${item.title}-${index}`} style={{ padding: "10px 12px", borderRadius: 12, background: "rgba(2,6,23,0.55)", border: "1px solid rgba(148,163,184,0.12)" }}>
                      <div style={{ color: "#f8fafc", fontSize: 14, lineHeight: 1.5 }}>{item.title}</div>
                      <div style={{ marginTop: 6, color: "#94a3b8", fontSize: 12 }}>
                        {item.publisher}
                        {item.published_at ? ` | ${new Date(item.published_at).toLocaleString()}` : ""}
                      </div>
                    </div>
                  ))
                ) : (
                  <div style={{ color: "#94a3b8" }}>No recent headlines available.</div>
                )}
              </div>
              {news?.note && <p style={{ margin: "12px 0 0", color: "#94a3b8", fontSize: 12, lineHeight: 1.6 }}>{news.note}</p>}
            </div>
          </div>
        </>
      )}
    </section>
  );
}

const panelStyle: CSSProperties = {
  padding: 24,
  borderRadius: 24,
  border: "1px solid rgba(148,163,184,0.14)",
  background: "linear-gradient(145deg, rgba(13,19,34,0.96), rgba(8,14,27,0.9))",
  boxShadow: "0 24px 80px rgba(2,6,23,0.45)",
};

const eyebrowStyle: CSSProperties = {
  fontSize: 12,
  letterSpacing: 1.8,
  textTransform: "uppercase",
  color: "#67e8f9",
};

const macroCardStyle: CSSProperties = {
  padding: 18,
  borderRadius: 18,
  background: "linear-gradient(180deg, rgba(15,23,42,0.85), rgba(2,6,23,0.78))",
  border: "1px solid rgba(148,163,184,0.14)",
};

const subPanelStyle: CSSProperties = {
  padding: 18,
  borderRadius: 18,
  background: "rgba(2,6,23,0.58)",
  border: "1px solid rgba(148,163,184,0.12)",
};

const subLabelStyle: CSSProperties = {
  fontSize: 11,
  color: "#94a3b8",
  textTransform: "uppercase",
  letterSpacing: 1.1,
};
