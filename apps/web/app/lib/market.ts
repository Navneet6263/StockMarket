export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Candle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type BacktestSide = {
  signal_count: number;
  win_rate: number;
  false_positive_rate: number;
  avg_win_pct: number;
  avg_loss_pct: number;
  profit_factor: number;
  expectancy_pct: number;
};

export type BacktestResult = {
  window_days?: number;
  bullish?: BacktestSide;
  bearish?: BacktestSide;
  calibration?: Array<{
    bucket: string;
    signal_count: number;
    win_rate: number;
  }>;
};

export type MarketSignal = {
  symbol: string;
  company_name?: string;
  direction: "bullish" | "bearish" | "neutral";
  setup_label: string;
  alert_level: "high_priority" | "watchlist" | "low_priority" | "avoid" | string;
  confidence: number;
  model_confidence: number;
  evidence_confidence?: number | null;
  historical_evidence_status: "validated" | "limited" | "unavailable" | "not_loaded" | string;
  confidence_note: string;
  probability: number;
  move_quality: number;
  expected_move_pct: number;
  risk_level: "low" | "medium" | "high" | string;
  current_price: number;
  target_price: number;
  extended_target_price?: number | null;
  stop_loss?: number | null;
  risk_reward: number;
  timeframe_label: string;
  timeframe_days: number;
  change_pct: number;
  volume: number;
  relative_volume: number;
  gap_pct: number;
  intraday_volume_ratio: number;
  benchmark_relative_strength: number;
  rsi: number;
  atr_pct: number;
  support?: number | null;
  resistance?: number | null;
  rolling_vwap?: number | null;
  invalidation?: number | null;
  reasons: string[];
  weaknesses: string[];
  risk_factors: string[];
  tags: string[];
  signal_summary: string;
  score_breakdown: Record<string, number>;
  discovered_by?: string[];
  backtest?: BacktestResult;
  historical_context?: BacktestSide;
};

export type MarketMover = {
  symbol: string;
  company_name?: string;
  price?: number;
  change_pct?: number;
  volume?: number;
  tags?: string[];
};

export type MarketOverview = {
  generated_at: string;
  universe_size: number;
  market_breadth: {
    advancing: number;
    declining: number;
    advance_decline_ratio: number;
    bullish_setups: number;
    bearish_setups: number;
    bullish_ratio: number;
    benchmark_change_pct: number;
  };
  market_discovery: {
    source_mode?: string;
    note?: string;
    bucket_counts?: Record<string, number>;
  };
  macro_context: {
    risk_mode?: string;
    summary?: string;
  };
  global_context: {
    state?: string;
    summary?: string;
  };
  top_opportunities: MarketSignal[];
  unusual_volume: MarketSignal[];
  breakout_candidates: MarketSignal[];
  bearish_risks: MarketSignal[];
  top_movers: MarketMover[];
  summary: {
    high_priority: number;
    watchlist: number;
    avoid: number;
    opportunities_count: number;
    breakout_count: number;
    bearish_risk_count: number;
    unusual_volume_count: number;
    market_mood: string;
    scanner_leader?: string | null;
  };
};

export type StockDetail = {
  symbol: string;
  quote: {
    symbol: string;
    price: number;
    previous_close: number;
    change: number;
    change_percent: number;
    volume: number;
    timestamp: string;
  };
  prediction: MarketSignal;
  signals: {
    reasons: string[];
    weaknesses: string[];
    risk_factors: string[];
    tags: string[];
    score_breakdown: Record<string, number>;
  };
  backtest: BacktestResult;
  macro_context: {
    risk_mode?: string;
    summary?: string;
  };
  news_context: {
    items?: Array<{
      title?: string;
      publisher?: string;
      published_at?: string;
    }>;
  };
  global_context: {
    state?: string;
    summary?: string;
  };
  company_context: {
    company_name?: string;
    sector?: string;
    industry?: string;
    market_cap_label?: string;
    business_summary?: string;
    business_model?: {
      score?: number;
      rating?: string;
      note?: string;
      strengths?: string[];
      risks?: string[];
    };
  };
  explanation: {
    summary?: string;
    why_it_is_flagged?: string[];
    what_is_strong?: string[];
    what_is_weak?: string[];
    risk_factors?: string[];
    watch_next?: string[];
    macro_take?: string;
    global_take?: string;
    business_take?: string;
    news_headlines?: string[];
  };
  chart: Candle[];
  generated_at: string;
};

export type TrackedSetupUpdate = {
  id: number;
  setup_id: string;
  symbol?: string;
  direction?: string;
  status?: string;
  result_pct?: number | null;
  created_at: string;
  label: string;
  note: string;
  meta: Record<string, unknown>;
};

export type TrackedSetup = {
  id: string;
  symbol: string;
  company_name?: string | null;
  sector?: string | null;
  direction: "bullish" | "bearish" | "neutral" | string;
  setup_label: string;
  tracking_label: string;
  scanner_bucket: string;
  source_mode: string;
  detected_at: string;
  last_seen_at: string;
  last_evaluated_at?: string | null;
  expires_at?: string | null;
  timeframe_label: string;
  timeframe_days: number;
  entry_price: number;
  current_price: number;
  target_price: number;
  extended_target_price?: number | null;
  stop_loss?: number | null;
  invalidation?: number | null;
  confidence: number;
  model_confidence?: number | null;
  evidence_confidence?: number | null;
  evidence_status?: string | null;
  expected_move_pct: number;
  risk_level: string;
  risk_reward: number;
  move_quality: number;
  relative_volume: number;
  intraday_volume_ratio: number;
  change_pct: number;
  reason_summary: string;
  reasons: string[];
  risk_factors: string[];
  tags: string[];
  status: "active" | "watch_only" | "passed" | "failed" | "expired" | string;
  result_pct?: number | null;
  max_favorable_move?: number | null;
  max_adverse_move?: number | null;
  last_update_label?: string | null;
  last_update_note?: string | null;
  notes?: string | null;
  pinned: boolean;
  ignored: boolean;
  archived: boolean;
  updates?: TrackedSetupUpdate[];
};

export type TrackerDashboard = {
  generated_at: string;
  summary: {
    total_calls: number;
    active_calls: number;
    passed_calls: number;
    failed_calls: number;
    expired_calls: number;
    win_rate: number;
    average_return: number;
    best_call?: TrackedSetup | null;
    worst_call?: TrackedSetup | null;
  };
  watchlists: {
    fresh_setups: TrackedSetup[];
    active_bullish: TrackedSetup[];
    active_bearish: TrackedSetup[];
    passed_calls: TrackedSetup[];
    failed_calls: TrackedSetup[];
    expired_calls: TrackedSetup[];
    manual_watchlist: TrackedSetup[];
  };
  todays_review: {
    updates: TrackedSetupUpdate[];
    what_worked: TrackedSetupUpdate[];
    what_failed: TrackedSetupUpdate[];
    what_changed: TrackedSetupUpdate[];
  };
};

export type TrackerSymbolHistory = {
  symbol: string;
  setups: TrackedSetup[];
  generated_at: string;
};

export function toneForDirection(direction: string) {
  if (direction === "bullish") return "#2dd4bf";
  if (direction === "bearish") return "#fb7185";
  return "#94a3b8";
}

export function toneForRisk(risk: string) {
  if (risk === "low") return "#34d399";
  if (risk === "high") return "#fb7185";
  return "#fbbf24";
}

export function humanize(value?: string) {
  return (value || "unknown").replace(/_/g, " ");
}

export function toneForEvidence(status?: string) {
  if (status === "validated") return "#34d399";
  if (status === "limited") return "#fbbf24";
  if (status === "unavailable") return "#fb7185";
  return "#94a3b8";
}

export function toneForStatus(status?: string) {
  if (status === "passed") return "#34d399";
  if (status === "failed") return "#fb7185";
  if (status === "expired") return "#f59e0b";
  if (status === "watch_only") return "#60a5fa";
  return "#cbd5e1";
}

export function toneForTrackingLabel(label?: string) {
  if (label === "Confirmed Setup") return "#34d399";
  if (label === "Early Watch") return "#60a5fa";
  if (label === "Needs Volume Confirmation") return "#fbbf24";
  if (label === "Overextended") return "#f59e0b";
  if (label === "Low Liquidity" || label === "Avoid / High Risk") return "#fb7185";
  return "#cbd5e1";
}
