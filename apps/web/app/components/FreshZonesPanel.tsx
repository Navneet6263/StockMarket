"use client";
import { useCallback, useEffect, useState } from "react";
import { API_URL, fmt, humanize } from "../lib/market";

type Filter = "all" | "demand" | "resistance" | "IMMINENT" | "NEAR";

const CATEGORY_TONE: Record<string, string> = { IMMINENT: "sell", NEAR: "watch", NOT_PRIORITY: "" };
const PATH_TONE: Record<string, string> = { CLEAR: "buy", PARTIALLY_OBSTRUCTED: "watch", HEAVILY_OBSTRUCTED: "sell" };
const TF_LABEL: Record<string, string> = { W: "Weekly", M: "Monthly", Y: "Yearly" };

function rupees(value: any) {
  const text = fmt(value);
  return text === "-" ? "-" : `₹${text}`;
}

async function getJson(path: string) {
  const r = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

function ZoneCard({ row, onSelect }: { row: any; onSelect: (s: string) => void }) {
  const zone = row.zone || {};
  const prox = row.proximity || {};
  const path = row.path || {};
  const isDemand = zone.zone_type === "demand";
  const breakdown = zone.score_breakdown || {};
  return (
    <div className="stock-card" onClick={() => onSelect(zone.symbol)} style={{ cursor: "pointer" }}>
      <div className="stock-card-top">
        <div>
          <h3>{zone.symbol}</h3>
          <span className={`badge ${isDemand ? "bullish" : "bearish"}`}>
            {isDemand ? "📈 Demand zone" : "📉 Resistance zone"}
          </span>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 20, fontWeight: 800 }}>{rupees(row.price)}</div>
          <div style={{ fontSize: 11, color: "var(--muted)" }}>{row.price_basis}</div>
        </div>
      </div>

      <div className="stock-meta" style={{ marginTop: 12 }}>
        <span className={`tag ${CATEGORY_TONE[prox.category] || ""}`}>{humanize(prox.category)}</span>
        <span className="tag">{TF_LABEL[zone.timeframe] || zone.timeframe}</span>
        <span className="tag">{zone.pattern}</span>
        <span className="tag buy">{humanize(zone.freshness)}</span>
        <span className="tag">Score {zone.score ?? "-"}/80</span>
        {zone.fvg ? <span className="tag">FVG {fmt(zone.fvg.size_atr_z, 2)}×ATR</span> : null}
      </div>

      <div className="demand-panel" style={{ marginTop: 12, borderColor: prox.category === "IMMINENT" ? "var(--red)" : "var(--yellow)" }}>
        <p className="stock-reason" style={{ margin: 0 }}>
          Price is {rupees(prox.distance_rupees)} ({fmt(prox.distance_pct, 2)}%, {fmt(prox.distance_atr_d, 2)}×ATR) {isDemand ? "above" : "below"} the
          zone, {prox.approach_direction}.
        </p>
      </div>

      <div className="demand-panel" style={{ marginTop: 10 }}>
        <div className="stock-meta">
          <span className={`tag ${PATH_TONE[path.classification] || ""}`}>Path {humanize(path.classification)}</span>
          <span className="tag">{path.barrier_count ?? 0} barrier(s)</span>
        </div>
        {(path.barriers || []).slice(0, 3).map((b: any, i: number) => (
          <p key={i} className="stock-reason" style={{ margin: "6px 0 0", fontSize: 13 }}>
            • {b.kind === "zone" ? `${TF_LABEL[b.timeframe] || b.timeframe} ${b.type} zone ${rupees(b.proximal)}–${rupees(b.distal)}` : `${humanize(b.kind)} ${rupees(b.price)} (${b.date})`} · {fmt(b.distance_pct, 2)}% away
          </p>
        ))}
      </div>

      <p className="stock-reason" style={{ fontSize: 12, marginTop: 10 }}>
        Base {breakdown.base_quality?.points ?? "-"}/25 · Departure {fmt(breakdown.departure_strength?.points, 1)}/30 ·
        TF & clarity {breakdown.timeframe_and_clarity?.points ?? "-"}/15 · FVG {breakdown.fvg?.points ?? "-"}/10 ·
        formed {zone.formation_date}
      </p>

      <div className="stock-targets">
        <div><span>Proximal</span><strong>{rupees(zone.proximal)}</strong></div>
        <div><span>Distal (invalidation)</span><strong>{rupees(zone.distal)}</strong></div>
        <div><span>Width</span><strong>{fmt(zone.width_pct, 2)}%</strong></div>
      </div>
    </div>
  );
}

export default function FreshZonesPanel({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [items, setItems] = useState<any[]>([]);
  const [alerts, setAlerts] = useState<any[]>([]);
  const [lastRun, setLastRun] = useState<any>(null);
  const [dataInfo, setDataInfo] = useState<any>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scanMsg, setScanMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [opp, al, runs] = await Promise.all([
        getJson("/api/fresh-zones/opportunities?limit=200"),
        getJson("/api/fresh-zones/alerts?limit=25"),
        getJson("/api/fresh-zones/runs?limit=1"),
      ]);
      setItems(opp.items || []);
      setDataInfo(opp.data || null);
      setAlerts(al.items || []);
      setLastRun((runs.items || [])[0] || null);
      setError(null);
    } catch {
      setError("Fresh-zone API is not reachable. Pull the latest backend and restart the API.");
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  const runScan = async () => {
    setScanMsg("Starting zone scan…");
    try {
      const r = await fetch(`${API_URL}/api/fresh-zones/nightly`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const json = await r.json().catch(() => ({}));
      setScanMsg(r.status === 409 ? "A zone scan is already running." :
        r.ok ? `Zone scan started for ${json.symbols} stocks. Results appear here when it finishes (can take several minutes).` :
        "Could not start the zone scan.");
    } catch {
      setScanMsg("Could not start the zone scan.");
    }
  };

  const nearby = items.filter((r) => r.proximity?.category !== "NOT_PRIORITY");
  const shown = nearby.filter((r) =>
    filter === "all" ? true :
    filter === "demand" || filter === "resistance" ? r.zone?.zone_type === filter :
    r.proximity?.category === filter);
  const count = (f: Filter) => nearby.filter((r) =>
    f === "all" ? true : f === "demand" || f === "resistance" ? r.zone?.zone_type === f : r.proximity?.category === f).length;

  return (
    <>
      <div className="stats-row">
        <div className="stat-card"><span>🧱 Fresh zones tracked</span><strong>{items.length}</strong></div>
        <div className="stat-card"><span>🎯 Imminent</span><strong style={{ color: "var(--red)" }}>{count("IMMINENT")}</strong></div>
        <div className="stat-card"><span>👀 Near</span><strong style={{ color: "var(--yellow)" }}>{count("NEAR")}</strong></div>
        <div className="stat-card"><span>📈 Demand</span><strong style={{ color: "var(--green)" }}>{count("demand")}</strong></div>
        <div className="stat-card"><span>📉 Resistance</span><strong style={{ color: "var(--red)" }}>{count("resistance")}</strong></div>
        <div className="stat-card"><span>🔔 Alerts today</span><strong>{alerts.filter((a) => a.ist_date === new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" })).length}</strong></div>
      </div>

      <div className="demand-panel" style={{ marginBottom: 16, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <p className="stock-reason" style={{ margin: 0, fontSize: 13 }}>
          Weekly / monthly / yearly fresh, untested zones near price.
          Last zone scan: {lastRun ? new Date(lastRun.finished_at || lastRun.started_at).toLocaleString() : "never"} ·
          Data: {dataInfo ? `${dataInfo.source} (${humanize(dataInfo.mode)})` : "-"}
        </p>
        <button className="refresh-btn" onClick={runScan}>Run zone scan</button>
      </div>
      {scanMsg ? <p className="stock-reason" style={{ marginTop: -6, marginBottom: 16, color: "var(--orange)" }}>{scanMsg}</p> : null}

      <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
        {([
          ["all", "All"], ["IMMINENT", "🎯 Imminent"], ["NEAR", "👀 Near"],
          ["demand", "📈 Demand"], ["resistance", "📉 Resistance"],
        ] as [Filter, string][]).map(([key, label]) => (
          <button key={key} className="refresh-btn" onClick={() => setFilter(key)}
            style={{ fontSize: 13, padding: "6px 12px",
              background: filter === key ? "var(--green, #22c55e)" : undefined,
              color: filter === key ? "#000" : undefined, fontWeight: filter === key ? 700 : undefined }}>
            {label} ({count(key)})
          </button>
        ))}
      </div>

      <h2 className="section-title">Fresh zones near price ({shown.length})</h2>
      {loading ? (
        <div className="stock-grid">
          {[1, 2, 3].map((i) => <div key={i} className="stock-card" style={{ animation: "pulse 2.5s infinite" }}><div style={{ height: 120 }} /></div>)}
        </div>
      ) : error ? (
        <p className="empty-msg">{error}</p>
      ) : shown.length ? (
        <div className="stock-grid">
          {shown.map((row) => <ZoneCard key={row.zone?.zone_id} row={row} onSelect={onSelect} />)}
        </div>
      ) : (
        <p className="empty-msg">
          {lastRun ? "No fresh zone is within 1.5×ATR of price right now." : "No zone scan has run yet — click “Run zone scan”."}
        </p>
      )}

      <h2 className="section-title" style={{ marginTop: 28 }}>Recent zone alerts</h2>
      {alerts.length ? (
        <div className="demand-panel">
          {alerts.map((a) => (
            <div key={a.alert_id} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "8px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
              <strong style={{ minWidth: 110, cursor: "pointer" }} onClick={() => onSelect(a.stock?.symbol)}>{a.stock?.symbol}</strong>
              <span className={`tag ${a.zone?.type === "demand" ? "buy" : "sell"}`}>{TF_LABEL[a.zone?.timeframe] || a.zone?.timeframe} {a.zone?.type}</span>
              <span className="tag watch">{humanize(a.states?.current)}{a.distance?.category ? ` · ${humanize(a.distance.category)}` : ""}</span>
              <span className="stock-reason" style={{ fontSize: 13, flex: 1 }}>{a.reason}</span>
              <span style={{ fontSize: 11, color: "var(--muted)" }}>
                {a.price?.timestamp ? new Date(a.price.timestamp).toLocaleString() : ""} · {humanize(a.data?.mode)}
                {a.delivery && !a.delivery.delivered ? ` · not sent (${a.delivery.suppressed_reason})` : ""}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <p className="empty-msg">No zone alerts yet.</p>
      )}

      <p className="stock-reason" style={{ fontSize: 12, marginTop: 16 }}>
        Technical zone classification from explicit rules — not a trade recommendation. The score (out of 80) describes
        structure quality only and is not a probability of success.
      </p>
    </>
  );
}
