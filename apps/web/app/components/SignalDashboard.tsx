"use client";

import { useCallback, useEffect, useState, type CSSProperties, type ReactNode } from "react";

type Performance = {
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  avg_pnl_pct: number;
  total_pnl_pct: number;
  avg_win_pct: number;
  avg_loss_pct: number;
  profit_factor: number;
};

type Signal = {
  _id?: string;
  symbol: string;
  action: string;
  quality_grade: string;
  quality_score: number;
  entry_price: number;
  stop_loss: number;
  target_1: number;
  target_2: number;
  rr: number;
  status?: string;
  pnl_pct?: number;
  volume_ratio?: number;
  mtf_sync?: string;
  reasons?: string[];
  rejection_reasons?: string[];
  discovered_by?: string[];
};

type DashboardStats = {
  scanner: { last_scan: string | null; total_stocks: number; active_signals: number };
  open_signals: number;
  performance: Performance;
};

type TelegramStatus = {
  running: boolean;
  configured: boolean;
  last_error?: string;
  last_send_status?: { ok?: boolean; message?: string; sent_at?: string };
  last_scan_summary?: ScanSummary;
};

type ScannerProgress = {
  scanning: boolean;
  progress?: {
    total?: number;
    done?: number;
    batch?: number;
    stage?: string;
    message?: string;
    current_symbol?: string;
    signals_found?: number;
    rejected?: number;
    errors?: number;
    elapsed_sec?: number;
    candidate_pool?: number;
    selected?: number;
    partial_signals?: Signal[];
    partial_rejected?: Signal[];
    discovery?: {
      source_mode?: string;
      bucket_counts?: Record<string, number>;
      note?: string;
    };
  };
  result?: ScanSummary;
};

type ScanSummary = {
  scanned_at?: string;
  signals_sent?: number;
  signals?: Signal[];
  no_trade_message_sent?: boolean;
  top_rejected?: Signal[];
  discovery?: {
    source_mode?: string;
    bucket_counts?: Record<string, number>;
    candidate_pool?: number;
    selected_for_deep_scan?: number;
    deep_scan_limit?: number;
  };
  scanner_stats?: {
    total_scanned?: number;
    successful_scans?: number;
    signals?: number;
    rejected?: number;
    elapsed_sec?: number;
  };
  error?: string;
};

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function tone(action?: string) {
  return action === "BUY" ? "#22c55e" : action === "SELL" ? "#f97316" : "#e2e8f0";
}

function humanize(value?: string) {
  return value ? value.replace(/_/g, " ") : "-";
}

export default function SignalDashboard() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [openSignals, setOpenSignals] = useState<Signal[]>([]);
  const [botStatus, setBotStatus] = useState<TelegramStatus | null>(null);
  const [scanProgress, setScanProgress] = useState<ScannerProgress | null>(null);
  const [scanResult, setScanResult] = useState<ScannerProgress["result"] | null>(null);
  const [loading, setLoading] = useState("");
  const [actionMessage, setActionMessage] = useState("");

  const fetchDashboard = useCallback(async () => {
    try {
      const [dashRes, sigRes, tgRes, progressRes] = await Promise.all([
        fetch(`${API}/dashboard/stats`),
        fetch(`${API}/signals/open`),
        fetch(`${API}/telegram/status`),
        fetch(`${API}/scanner/progress`),
      ]);

      if (dashRes.ok) setStats(await dashRes.json());
      if (sigRes.ok) {
        const signalPayload = await sigRes.json();
        setOpenSignals(signalPayload.open_signals || []);
      }
      if (tgRes.ok) {
        const telegramPayload = (await tgRes.json()) as TelegramStatus;
        setBotStatus(telegramPayload);
        if (!scanResult && telegramPayload.last_scan_summary) {
          setScanResult(telegramPayload.last_scan_summary);
        }
      }
      if (progressRes.ok) {
        const progressPayload = (await progressRes.json()) as ScannerProgress;
        setScanProgress(progressPayload);
        if (progressPayload.result) {
          setScanResult(progressPayload.result);
        }
      }
    } catch (err) {
      console.error("Dashboard refresh error:", err);
    }
  }, [scanResult]);

  useEffect(() => {
    fetchDashboard();
    const interval = setInterval(fetchDashboard, 30000);
    return () => clearInterval(interval);
  }, [fetchDashboard]);

  useEffect(() => {
    if (!scanProgress?.scanning) return;

    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API}/scanner/progress`);
        if (!res.ok) return;
        const payload = (await res.json()) as ScannerProgress;
        setScanProgress(payload);
        if (!payload.scanning) {
          setLoading("");
          setScanResult(payload.result || null);
          fetchDashboard();
        }
      } catch {}
    }, 2500);

    return () => clearInterval(interval);
  }, [scanProgress?.scanning, fetchDashboard]);

  async function runScan() {
    setLoading("scan");
    setActionMessage("");
    setScanResult(null);
    try {
      const res = await fetch(`${API}/telegram/scan`, { method: "POST" });
      const data = await res.json();
      setActionMessage(data.message || data.status || "Scan started");
      setScanProgress((prev) => ({ ...(prev || {}), scanning: true }));
      fetchDashboard();
    } catch (err) {
      setLoading("");
      setActionMessage(String(err));
    }
  }

  async function startBot() {
    setLoading("start");
    setActionMessage("");
    try {
      const res = await fetch(`${API}/telegram/start`, { method: "POST" });
      const data = await res.json();
      setActionMessage(data.reason || data.status || "Bot started");
      await fetchDashboard();
    } catch (err) {
      setActionMessage(String(err));
    } finally {
      setLoading("");
    }
  }

  async function stopBot() {
    setLoading("stop");
    setActionMessage("");
    try {
      const res = await fetch(`${API}/telegram/stop`, { method: "POST" });
      const data = await res.json();
      setActionMessage(data.status || "Bot stopped");
      await fetchDashboard();
    } catch (err) {
      setActionMessage(String(err));
    } finally {
      setLoading("");
    }
  }

  async function testTelegram() {
    setLoading("test");
    setActionMessage("");
    try {
      const res = await fetch(`${API}/telegram/test`, { method: "POST" });
      const data = await res.json();
      setActionMessage(data.reason || data.status || "Test sent");
      await fetchDashboard();
    } catch (err) {
      setActionMessage(String(err));
    } finally {
      setLoading("");
    }
  }

  const perf = stats?.performance;
  const lastSummary = scanResult || botStatus?.last_scan_summary;
  const liveProgress = scanProgress?.progress;
  const liveSignals = scanProgress?.scanning ? liveProgress?.partial_signals ?? [] : lastSummary?.signals ?? [];
  const liveRejected = scanProgress?.scanning ? liveProgress?.partial_rejected ?? [] : lastSummary?.top_rejected ?? [];
  const summarySignals = scanProgress?.scanning ? lastSummary?.signals ?? [] : liveSignals;
  const summaryRejected = scanProgress?.scanning ? lastSummary?.top_rejected ?? [] : liveRejected;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 18, gap: 16, flexWrap: "wrap" }}>
        <div>
          <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1.1 }}>Automation Desk</div>
          <h3 style={{ margin: "6px 0 0" }}>Scanner and Telegram control room</h3>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <button onClick={runScan} disabled={loading === "scan" || scanProgress?.scanning}>
            {loading === "scan" || scanProgress?.scanning ? "Scanning..." : "Scan Now"}
          </button>
          {botStatus?.running ? (
            <button onClick={stopBot} disabled={loading === "stop"} style={{ background: "#ef4444", color: "#fff" }}>
              {loading === "stop" ? "Stopping..." : "Stop Bot"}
            </button>
          ) : (
            <button onClick={startBot} disabled={loading === "start"} style={{ background: "#22c55e", color: "#04111d" }}>
              {loading === "start" ? "Starting..." : "Start Bot"}
            </button>
          )}
          <button onClick={testTelegram} disabled={loading === "test"} style={{ background: "#0f172a", color: "#e2e8f0", border: "1px solid rgba(148,163,184,0.2)" }}>
            {loading === "test" ? "Testing..." : "Test Telegram"}
          </button>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14, marginBottom: 16 }}>
        <StatusCard
          label="Telegram"
          value={botStatus?.configured ? "Configured" : "Missing Config"}
          color={botStatus?.configured ? "#22c55e" : "#f97316"}
        />
        <StatusCard
          label="Bot State"
          value={botStatus?.running ? "Running" : "Stopped"}
          color={botStatus?.running ? "#38bdf8" : "#94a3b8"}
        />
        <StatusCard
          label="Last Send"
          value={botStatus?.last_send_status?.ok ? "Success" : botStatus?.last_send_status?.message || "Idle"}
          color={botStatus?.last_send_status?.ok ? "#22c55e" : "#fbbf24"}
        />
        <StatusCard
          label="Scan Progress"
          value={
            scanProgress?.scanning
              ? `${scanProgress.progress?.done ?? 0}/${scanProgress.progress?.total ?? 0}`
              : lastSummary?.scanner_stats?.total_scanned
                ? `${lastSummary.scanner_stats.total_scanned} scanned`
                : "Idle"
          }
          color={scanProgress?.scanning ? "#38bdf8" : "#e2e8f0"}
        />
        <StatusCard
          label="Stage"
          value={scanProgress?.scanning ? humanize(scanProgress.progress?.stage) : lastSummary ? "completed" : "idle"}
          color={scanProgress?.scanning ? "#fbbf24" : "#94a3b8"}
        />
        <StatusCard
          label="Universe"
          value={
            scanProgress?.scanning
              ? humanize(scanProgress.progress?.discovery?.source_mode || "yahoo_dynamic")
              : lastSummary?.discovery?.source_mode
                ? lastSummary.discovery.source_mode.replace(/_/g, " ")
                : "auto"
          }
          color="#cbd5e1"
        />
      </div>

      {(actionMessage || botStatus?.last_error) && (
        <div style={{ marginBottom: 16, padding: 14, borderRadius: 14, background: "rgba(15,23,42,0.75)", border: "1px solid rgba(148,163,184,0.14)", color: "#e2e8f0" }}>
          {actionMessage || botStatus?.last_error}
        </div>
      )}

      {scanProgress?.scanning && (
        <Panel title="Live Scan Feed">
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 14 }}>
            <Stat label="Candidate Pool" value={liveProgress?.candidate_pool ?? 0} />
            <Stat label="Deep Scan List" value={liveProgress?.selected ?? 0} color="#38bdf8" />
            <Stat label="Signals Found" value={liveProgress?.signals_found ?? 0} color="#22c55e" />
            <Stat label="Rejected" value={liveProgress?.rejected ?? 0} color="#f97316" />
            <Stat label="Errors" value={liveProgress?.errors ?? 0} color="#ef4444" />
            <Stat label="Elapsed" value={`${liveProgress?.elapsed_sec ?? 0}s`} />
          </div>
          <div style={{ marginTop: 14, color: "#cbd5e1", lineHeight: 1.7 }}>
            {liveProgress?.message || "Scan running..."}
            {liveProgress?.current_symbol ? ` Current symbol: ${liveProgress.current_symbol}` : ""}
          </div>
        </Panel>
      )}

      {scanProgress?.scanning && (!!liveSignals?.length || !!liveRejected?.length) && (
        <Panel title="Live Preview">
          {!!liveSignals?.length && (
            <div style={{ display: "grid", gap: 12 }}>
              {liveSignals.map((signal, index) => (
                <div key={`${signal.symbol}-${index}`} style={signalCardStyle}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                    <div>
                      <div style={{ color: tone(signal.action), fontWeight: 700, fontSize: 18 }}>
                        {signal.symbol} | {signal.action}
                      </div>
                      <div style={{ marginTop: 6, color: "#cbd5e1" }}>{(signal.reasons ?? []).slice(0, 2).join(" ")}</div>
                      {!!signal.discovered_by?.length && (
                        <div style={{ marginTop: 6, color: "#94a3b8" }}>
                          Picked by: {signal.discovered_by.map((item) => item.replace(/_/g, " ")).join(", ")}
                        </div>
                      )}
                    </div>
                    <div style={{ textAlign: "right", color: "#94a3b8" }}>
                      <div>Quality: {signal.quality_grade} ({signal.quality_score})</div>
                      <div>RR: 1:{signal.rr} | Volume: {signal.volume_ratio?.toFixed?.(2) ?? signal.volume_ratio}x</div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {!liveSignals?.length && !!liveRejected?.length && (
            <div style={{ display: "grid", gap: 12 }}>
              {liveRejected.map((signal, index) => (
                <div key={`${signal.symbol}-${index}`} style={signalCardStyle}>
                  <div style={{ color: "#f8fafc", fontWeight: 700 }}>{signal.symbol}</div>
                  <div style={{ marginTop: 8, color: "#cbd5e1", lineHeight: 1.6 }}>
                    {(signal.rejection_reasons ?? []).slice(0, 3).map((reason, reasonIndex) => (
                      <div key={reasonIndex}>- {reason}</div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>
      )}

      {perf && perf.total_trades > 0 && (
        <Panel title="Performance">
          <div style={gridStyle}>
            <Stat label="Total Trades" value={perf.total_trades} />
            <Stat label="Win Rate" value={`${perf.win_rate}%`} color={perf.win_rate >= 55 ? "#22c55e" : "#f97316"} />
            <Stat label="Profit Factor" value={perf.profit_factor} color={perf.profit_factor >= 1.5 ? "#22c55e" : "#f97316"} />
            <Stat label="Total P&L" value={`${perf.total_pnl_pct > 0 ? "+" : ""}${perf.total_pnl_pct}%`} color={perf.total_pnl_pct > 0 ? "#22c55e" : "#ef4444"} />
            <Stat label="Avg Win" value={`+${perf.avg_win_pct}%`} color="#22c55e" />
            <Stat label="Avg Loss" value={`${perf.avg_loss_pct}%`} color="#ef4444" />
          </div>
        </Panel>
      )}

      {stats && (
        <Panel title="Scanner Stats">
          <div style={gridStyle}>
            <Stat label="Stocks Scanned" value={stats.scanner.total_stocks} />
            <Stat label="Active Signals" value={stats.scanner.active_signals} color="#22c55e" />
            <Stat label="Open Trades" value={stats.open_signals} color="#38bdf8" />
            <Stat label="Last Scan" value={stats.scanner.last_scan ? new Date(stats.scanner.last_scan).toLocaleTimeString() : "-"} />
          </div>
        </Panel>
      )}

      {openSignals.length > 0 && (
        <Panel title="Open Signals">
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid rgba(148,163,184,0.18)" }}>
                  {["Symbol", "Action", "Quality", "Entry", "Stop", "T1", "T2", "RR", "Status", "P&L"].map((header) => (
                    <th key={header} style={{ padding: "10px 8px", textAlign: "left", color: "#94a3b8", fontSize: 11, textTransform: "uppercase", letterSpacing: 1 }}>
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {openSignals.map((signal) => (
                  <tr key={signal._id || `${signal.symbol}-${signal.entry_price}`} style={{ borderBottom: "1px solid rgba(148,163,184,0.08)" }}>
                    <td style={{ padding: 8, fontWeight: 700, color: "#e2e8f0" }}>{signal.symbol}</td>
                    <td style={{ padding: 8, color: tone(signal.action), fontWeight: 700 }}>{signal.action}</td>
                    <td style={{ padding: 8, color: "#fbbf24" }}>{signal.quality_grade} ({signal.quality_score})</td>
                    <td style={{ padding: 8, color: "#e2e8f0" }}>{signal.entry_price?.toFixed?.(2)}</td>
                    <td style={{ padding: 8, color: "#ef4444" }}>{signal.stop_loss?.toFixed?.(2)}</td>
                    <td style={{ padding: 8, color: "#22c55e" }}>{signal.target_1?.toFixed?.(2)}</td>
                    <td style={{ padding: 8, color: "#22c55e" }}>{signal.target_2?.toFixed?.(2)}</td>
                    <td style={{ padding: 8, color: "#e2e8f0" }}>1:{signal.rr}</td>
                    <td style={{ padding: 8, color: "#cbd5e1" }}>{signal.status}</td>
                    <td style={{ padding: 8, color: (signal.pnl_pct ?? 0) >= 0 ? "#22c55e" : "#ef4444", fontWeight: 700 }}>
                      {(signal.pnl_pct ?? 0) > 0 ? "+" : ""}
                      {signal.pnl_pct ?? 0}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {lastSummary && (
        <Panel title="Last Scan Brief">
          {lastSummary.error ? (
            <div style={{ color: "#ef4444" }}>{lastSummary.error}</div>
          ) : (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 14 }}>
                <Stat label="Signals Sent" value={lastSummary.signals_sent ?? 0} color="#22c55e" />
                <Stat label="Scanned" value={lastSummary.scanner_stats?.total_scanned ?? 0} />
                <Stat label="Deep Scans" value={lastSummary.scanner_stats?.successful_scans ?? 0} />
                <Stat label="Rejected" value={lastSummary.scanner_stats?.rejected ?? 0} />
                <Stat label="Elapsed" value={`${lastSummary.scanner_stats?.elapsed_sec ?? 0}s`} />
              </div>
              {!!lastSummary.discovery?.bucket_counts && (
                <div style={{ marginTop: 12, color: "#94a3b8", lineHeight: 1.7 }}>
                  Auto discovery: {Object.entries(lastSummary.discovery.bucket_counts).map(([key, value]) => `${key.replace(/_/g, " ")} ${value}`).join(" | ")}
                </div>
              )}
              {!!lastSummary.discovery?.candidate_pool && (
                <div style={{ marginTop: 8, color: "#94a3b8", lineHeight: 1.7 }}>
                  Shortlist flow: {lastSummary.discovery.candidate_pool} Yahoo candidates {"->"} {lastSummary.discovery.selected_for_deep_scan ?? 0} deep scans
                  {lastSummary.discovery.deep_scan_limit ? ` (limit ${lastSummary.discovery.deep_scan_limit})` : ""}
                </div>
              )}

              {!!summarySignals?.length && (
                <div style={{ marginTop: 16, display: "grid", gap: 12 }}>
                  {summarySignals.map((signal, index) => (
                    <div key={`${signal.symbol}-${index}`} style={signalCardStyle}>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                        <div>
                          <div style={{ color: tone(signal.action), fontWeight: 700, fontSize: 18 }}>
                            {signal.symbol} | {signal.action}
                          </div>
                          <div style={{ marginTop: 6, color: "#cbd5e1" }}>{(signal.reasons ?? []).slice(0, 2).join(" ")}</div>
                          {!!signal.discovered_by?.length && (
                            <div style={{ marginTop: 6, color: "#94a3b8" }}>
                              Picked by: {signal.discovered_by.map((item) => item.replace(/_/g, " ")).join(", ")}
                            </div>
                          )}
                        </div>
                        <div style={{ textAlign: "right", color: "#94a3b8" }}>
                          <div>Quality: {signal.quality_grade} ({signal.quality_score})</div>
                          <div>RR: 1:{signal.rr} | Volume: {signal.volume_ratio?.toFixed?.(2) ?? signal.volume_ratio}x</div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {!summarySignals?.length && !!summaryRejected?.length && (
                <div style={{ marginTop: 16, display: "grid", gap: 12 }}>
                  {summaryRejected.map((signal, index) => (
                    <div key={`${signal.symbol}-${index}`} style={signalCardStyle}>
                      <div style={{ color: "#f8fafc", fontWeight: 700 }}>{signal.symbol}</div>
                      <div style={{ marginTop: 8, color: "#cbd5e1", lineHeight: 1.6 }}>
                        {(signal.rejection_reasons ?? []).slice(0, 3).map((reason, reasonIndex) => (
                          <div key={reasonIndex}>- {reason}</div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </Panel>
      )}
    </div>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div style={{ background: "rgba(148,163,184,0.06)", padding: 18, borderRadius: 16, marginBottom: 16, border: "1px solid rgba(148,163,184,0.12)" }}>
      <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1, marginBottom: 12 }}>{title}</div>
      {children}
    </div>
  );
}

function StatusCard({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ background: "rgba(15,23,42,0.8)", padding: 16, borderRadius: 14, border: "1px solid rgba(148,163,184,0.12)" }}>
      <div style={{ fontSize: 11, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1 }}>{label}</div>
      <div style={{ marginTop: 8, fontSize: 18, fontWeight: 700, color: color || "#e2e8f0", lineHeight: 1.4 }}>{value}</div>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: any; color?: string }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1 }}>{label}</div>
      <div style={{ marginTop: 4, fontSize: 20, fontWeight: 700, color: color || "#e2e8f0" }}>{value}</div>
    </div>
  );
}

const gridStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
  gap: 14,
};

const signalCardStyle: CSSProperties = {
  background: "rgba(2,6,23,0.55)",
  padding: 14,
  borderRadius: 14,
  border: "1px solid rgba(148,163,184,0.12)",
};
