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

export type DemandSupply = {
  status?: string;
  demandScore?: number;
  supplyScore?: number;
  trapRiskScore?: number;
  trapRisk?: string;
  smartMoneyRead?: string;
  evidence?: string[];
  trapReasons?: string[];
  blockFreshEntry?: boolean;
  confirmationRule?: string;
};

export type LargeMoneyFootprint = {
  bias: string;
  score: number;
  strength?: number;
  evidence?: string[];
  warnings?: string[];
  summary?: string;
  buying_pressure?: boolean;
  selling_pressure?: boolean;
  identity_inference_supported?: false;
  score_is_calibrated_probability?: false;
  data_quality?: {
    grade?: "high" | "medium" | "low" | string;
    score?: number;
    delivery_verified?: boolean;
    available?: Record<string, boolean>;
  };
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
  probability?: number | null;
  probability_available?: boolean;
  model_alignment_score?: number;
  move_quality: number;
  expected_move_pct: number;
  risk_level: "low" | "medium" | "high" | string;
  current_price: number;
  entry_zone?: { low: number; high: number };
  target_1?: number;
  target_2?: number;
  extended_target?: number;
  target_price: number;
  extended_target_price?: number | null;
  stop_loss?: number | null;
  trailing_stop?: number | null;
  exit_signal?: boolean;
  hold_or_exit?: "HOLD" | "PARTIAL_BOOK" | "EXIT" | "WAIT" | string;
  reason_for_exit_decision?: string;
  setup_stage?: string;
  trade_labels?: string[];
  recommended_action?: string;
  entry_quality?: "good" | "watch" | "poor" | "avoid" | string;
  entry_timing?: string;
  seller_pressure?: "low" | "medium" | "high" | "very_high" | string;
  seller_pressure_score?: number;
  profit_booking_risk?: "low" | "medium" | "high" | "very_high" | string;
  demand_supply?: DemandSupply;
  large_money_footprint?: LargeMoneyFootprint;
  large_money_footprint_bias?: string;
  large_money_footprint_score?: number;
  footprint_data_quality?: LargeMoneyFootprint["data_quality"];
  live_pool_reason?: string;
  live_pool_rank?: number;
  demand_status?: string;
  demand_score?: number;
  supply_score?: number;
  trap_risk?: string;
  trap_risk_score?: number;
  best_action?: string;
  reentry_plan?: string;
  entry_timing_reasons?: string[];
  attention_only?: boolean;
  allow_buy_call?: boolean;
  chase_risk?: boolean;
  overextended_fresh_entry?: boolean;
  next_day_profit_booking_risk?: boolean;
  chase_risk_reason?: string;
  return_5d?: number;
  return_20d?: number;
  ema20_distance_pct?: number;
  vwap_distance_pct?: number;
  is_pre_breakout?: boolean;
  setup_type?: string;
  signal_stage?: string;
  entry_trigger?: number;
  pattern_labels?: string[];
  pattern_score?: number;
  pattern_reason?: string;
  pre_breakout_labels?: string[];
  pre_breakout_score?: number;
  pre_breakout_confidence?: number;
  pre_breakout_action?: "WATCH" | "ALERT_ABOVE_LEVEL" | "WAIT_FOR_CONFIRMATION" | string;
  breakout_level?: number | null;
  alert_price?: number | null;
  alert_above_price?: number | null;
  invalidation_level?: number | null;
  expected_breakout_move?: number | null;
  expected_move?: number | null;
  pre_breakout_reason?: string;
  pre_breakout_timeframe?: string;
  action?: string;
  is_momentum_continuation?: boolean;
  continuation_type?: "pullback" | "consolidation" | "trend" | string;
  continuation_labels?: string[];
  re_entry_zone?: { low: number; high: number };
  safe_entry_price?: number;
  new_target?: number;
  continuation_risk_reward?: number;
  continuation_action?: "WAIT_FOR_REENTRY" | "ALERT" | "REENTRY_BUY" | string;
  continuation_reason?: string;
  riskPct?: number | null;
  riskPerShare?: number | null;
  maxPositionPctAt1PctAccountRisk?: number | null;
  targetMethod?: string;
  entry_plan_status?: string;
  entry_plan_blocked?: boolean;
  entry_plan_blocked_reason?: string | null;
  proposed_entry?: number | null;
  structural_invalidation?: number | null;
  opposing_structure_target?: number | null;
  historical_evidence?: {
    sample_count: number;
    win_rate: number;
    avg_return: number;
    max_drawdown: number;
    false_positive_rate: number;
  };
  validation_status?: string;
  catalyst_summary?: {
    sentiment?: string;
    summary?: string;
    catalysts?: string[];
    headlines?: string[];
  };
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
  move_bucket?: string;
  missed_reason?: string;
  attention_only?: boolean;
  recommended_action?: string;
  trade_labels?: string[];
  chase_risk_reason?: string;
  entry_label?: string;
  display_action?: string;
  is_chase_risk?: boolean;
  is_profit_booking_risk?: boolean;
  chase_warnings?: string[];
  next_day_risk?: string[];
  ui_guidance?: string[];
  was_in_prior_pre_breakout_scan?: boolean;
  was_pre_breakout_setup_yesterday?: boolean;
  pre_breakout_score_before_move?: number;
  missed_move_analysis?: string;
};

export type MarketOverview = {
  generated_at: string;
  universe_size: number;
  valid_signal_count?: number;
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
    total_discovered?: number;
    total_scanned?: number;
    valid_signal_count?: number;
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
  top_validated_calls?: MarketSignal[];
  unusual_volume: MarketSignal[];
  breakout_candidates: MarketSignal[];
  bearish_risks: MarketSignal[];
  top_movers: MarketMover[];
  fast_movers_missed_moves?: MarketMover[];
  pre_breakout_setups?: MarketSignal[];
  pattern_forming_setups?: MarketSignal[];
  alert_above_setups?: MarketSignal[];
  retest_entry?: MarketSignal[];
  momentum_continuation?: MarketSignal[];
  re_entry_setups?: MarketSignal[];
  missed_moves_analysis?: MarketMover[];
  avoid_late_entry?: MarketSignal[];
  live_candidate_symbols?: string[];
  live_candidate_pool?: Array<{
    symbol: string;
    rank: number;
    reason: string;
    direction: MarketSignal["direction"];
    confidence?: number | null;
    large_money_footprint_score?: number | null;
  }>;
  all_entry_levels?: Array<{
    symbol: string;
    entry_trigger: number;
    stop_loss?: number | null;
    target_1?: number | null;
    direction?: MarketSignal["direction"];
  }>;
  data_health?: {
    institution_identity_available?: false;
    large_money_label?: string;
    delivery_archive?: Record<string, unknown>;
  };
  summary: {
    high_priority: number;
    watchlist: number;
    avoid: number;
    opportunities_count: number;
    top_validated_count?: number;
    breakout_count: number;
    bearish_risk_count: number;
    unusual_volume_count: number;
    market_mood: string;
    scanner_leader?: string | null;
    total_scanned_universe?: number;
    valid_signal_count?: number;
    fast_movers_count?: number;
    pre_breakout_count?: number;
    pattern_forming_count?: number;
    alert_above_count?: number;
    retest_entry_count?: number;
    momentum_continuation_count?: number;
    re_entry_count?: number;
    missed_moves_count?: number;
    avoid_late_entry_count?: number;
    live_candidate_count?: number;
  };
};

export type HotPick = {
  symbol: string;
  currentPrice?: string;
  setupType: "breakout" | "pullback" | "reversal" | "momentum continuation" | "breakdown" | string;
  chartPattern?: string;
  patternLabels?: string[];
  patternReason?: string;
  direction: "bullish" | "bearish" | "neutral";
  confidence: number;
  score?: number;
  scoreBreakdown?: {
    breakoutSupportResistance?: number;
    chartSetup?: number;
    volumeLiquidity: number;
    trendStrength: number;
    momentumIndicators: number;
    sectorMarketNews?: number;
    newsSectorCatalyst?: number;
  };
  chart?: {
    nearBreakout?: boolean;
    breakoutConfirmed?: boolean;
    pullbackToSupport?: boolean;
    volumeSpike?: boolean;
    rsiMomentum?: number;
    movingAverageTrend?: string;
    riskReward?: number;
    distanceFromStopPct?: number | null;
    overextended?: boolean;
  };
  entryZone: string;
  entryTrigger?: string;
  entryLabel?: string;
  entryDistancePct?: number | null;
  entryMissed?: boolean;
  retestZone?: string;
  target: string;
  targetZone?: string;
  stoploss: string;
  invalidation: string;
  reason: string;
  whyItCanMove?: string;
  risk: string;
  riskLevel?: "low" | "medium" | "high" | string;
  timeHorizon: string;
  lastUpdated: string;
  catalystStatus?: "positive" | "negative" | "neutral" | "unknown";
  catalystReason?: string;
  catalystSource?: string | null;
  catalystTime?: string | null;
  biasLabel?: string;
  entryStatus?: string;
  tradeDecision?: string;
  confirmationText?: string;
  entryQuality?: string;
  entryTiming?: string;
  sellerPressure?: string;
  sellerPressureScore?: number;
  profitBookingRisk?: string;
  bestAction?: string;
  reentryPlan?: string;
  cleanRiskSetup?: boolean;
  aiAvailable?: boolean;
  aiStatus?: string;
  aiScore?: number;
  aiAction?: string;
  geminiAction?: "BUY" | "WATCH" | "AVOID" | "WAIT_FOR_PULLBACK" | string;
  aiConviction?: number | null;
  aiReason?: string;
  aiTradePlan?: string;
  aiRisks?: string[];
  aiTimeframe?: string;
  aiNewsImpact?: "positive" | "negative" | "neutral" | "unknown" | string;
  aiModel?: string;
  aiFinishReason?: string;
  aiTokenUsage?: Record<string, number>;
  aiStale?: boolean;
  aiStaleAgeSec?: number;
  newsHeadlines?: string[];
  newsSentiment?: "bullish" | "bearish" | "neutral" | "unknown" | string;
  insiderSignal?: string;
  analystConsensus?: string;
  aiWarnings?: string[];
  demandSupply?: DemandSupply;
  demandStatus?: string;
  demandScore?: number;
  supplyScore?: number;
  trapRisk?: string;
  trapRiskScore?: number;
  smartMoneyRead?: string;
  rejectionReasons?: string[];
  softReasons?: string[];
  raw?: MarketSignal;
};

export type BaseFormationPick = {
  symbol: string;
  direction?: "bullish" | "bearish" | "neutral" | string;
  currentPrice?: string;
  stage: "strong base" | "building base" | "loose consolidation" | string;
  biasLabel?: string;
  entryStatus?: string;
  tradeDecision?: string;
  confirmationText?: string;
  accumulationScore: number;
  range: string;
  keyResistance: string;
  supportZone: string;
  breakoutTrigger: string;
  triggerPrice?: string;
  invalidation: string;
  targetZone?: string;
  distanceToTriggerPct?: number | null;
  volumeBehavior: string;
  pattern: string;
  whyInteresting: string;
  risk: string;
  timeHorizon: string;
  lastUpdated: string;
  debug?: {
    avgRangePct?: number;
    resistanceTouches?: number;
    supportTouches?: number;
    volatilityCompressionPct?: number;
    tightConsolidationPct?: number | null;
  };
};

export type StrictOptionIdea = {
  symbol: string;
  bucket: string;
  side: "CE" | "PE" | "NO_TRADE" | string;
  status: "WATCH_CONTRACT" | "WATCH_TRIGGER" | "NO_TRADE" | string;
  actionLabel: string;
  confidence: number;
  underlyingPrice: string;
  underlyingTrigger: string;
  underlyingInvalidation: string;
  distanceToTriggerPct?: number | null;
  instrumentRule: string;
  entryRule: string;
  targetRule: string;
  invalidationRule: string;
  expiryRule: string;
  riskRule: string;
  niftyGate: string;
  blockers: string[];
};

export type StrictOptionsResponse = {
  niftyGate: {
    status: string;
    side: "bullish" | "bearish" | "neutral" | string;
    message: string;
    niftyBias: string;
    niftyRegime: string;
    marketScore: number;
    benchmarkChangePct: number;
    pcr?: number | null;
    pcrSignal?: string;
    dataAvailable?: boolean;
  };
  radar: StrictOptionIdea[];
  blocked: StrictOptionIdea[];
  summary: {
    strictReady: number;
    watchOnly: number;
    blockedHighScore: number;
  };
  warnings: string[];
};

export type HotPicksDebug = {
  totalScanned: number;
  sourceCandidates: number;
  candidatesByTier: Record<string, number>;
  rejectedCountsByReason: Record<string, number>;
  top10NearMissCandidates: Array<{
    symbol: string;
    score: number;
    setupType: string;
    softReasons?: string[];
    rejectionReasons?: string[];
    why?: string;
  }>;
};

export type HotPicksResponse = {
  marketMood: "bullish" | "bearish" | "sideways" | string;
  lastUpdated: string;
  cacheStatus?: string;
  isStale?: boolean;
  summary: {
    totalScanned: number;
    highConfidence: number;
    bullish: number;
    bearish: number;
    breakouts: number;
    watchlist?: number;
    momentumRadar?: number;
    baseFormation?: number;
    strictOptionsReady?: number;
    strictOptionsWatch?: number;
  };
  hotPicks: HotPick[];
  watchlist: HotPick[];
  momentumRadar?: HotPick[];
  catalystRadar?: HotPick[];
  baseFormationRadar?: BaseFormationPick[];
  strictOptions?: StrictOptionsResponse;
  rejectionSummary?: {
    totalRejected: number;
    countsByReason: Record<string, number>;
    nearMissCandidates: HotPicksDebug["top10NearMissCandidates"];
  };
  debug?: HotPicksDebug;
  marketExplanation?: string;
  sectorStrength?: Record<string, number>;
  performance?: {
    last30Calls?: TrackedSetup[];
    activeCalls?: number;
    winRate?: number | null;
    averageReturn?: number | null;
    maxLoss?: number | null;
    message?: string | null;
  };
  aiDiagnostics?: {
    enabled?: boolean;
    apiKeyConfigured?: boolean;
    model?: string;
    timeoutSec?: number;
    cacheTtlSec?: number;
    maxOutputTokens?: number;
    temperature?: number;
    thinkingBudget?: number;
    cacheSize?: number;
    lastSuccessCount?: number;
    reason?: string;
  };
  warnings: string[];
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
  target_1?: number | null;
  target_2?: number | null;
  extended_target_price?: number | null;
  stop_loss?: number | null;
  trailing_stop?: number | null;
  scanner_call_status?: string | null;
  hold_or_exit?: string | null;
  reason_for_exit_decision?: string | null;
  current_pnl_pct?: number | null;
  target_progress_pct?: number | null;
  suggested_at?: string | null;
  target_hit_at?: string | null;
  partial_book_at?: string | null;
  exit_suggested_at?: string | null;
  stop_loss_hit_at?: string | null;
  expired_at?: string | null;
  last_checked_at?: string | null;
  closed_at?: string | null;
  exit_reason?: string | null;
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
    target_hit_count?: number;
    stop_loss_count?: number;
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
    todays_top_10_scanner_calls?: TrackedSetup[];
    active_scanner_calls?: TrackedSetup[];
    past_target_hit?: TrackedSetup[];
    past_stop_loss_failed?: TrackedSetup[];
    expired_no_followthrough?: TrackedSetup[];
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

export function toneForAdvice(advice?: string | null) {
  if (advice === "HOLD") return "#34d399";
  if (advice === "PARTIAL_BOOK") return "#60a5fa";
  if (advice === "EXIT") return "#fb7185";
  if (advice === "EXPIRED") return "#f59e0b";
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

/** Safe toFixed — returns fallback string when value is null/undefined/NaN */
export function asNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const match = value.replace(/,/g, "").match(/-?\d+(?:\.\d+)?/);
  if (!match) return null;
  const parsed = Number(match[0]);
  return Number.isFinite(parsed) ? parsed : null;
}

export function fmt(value: number | string | null | undefined, decimals = 2, fallback = "-"): string {
  const parsed = asNumber(value);
  if (parsed === null) return fallback;
  return parsed.toFixed(decimals);
}

/** Safe percentage formatter with sign */
export function fmtPct(value: number | string | null | undefined, decimals = 2, fallback = "-"): string {
  const n = asNumber(value);
  if (n === null) return fallback;
  return `${n >= 0 ? "+" : ""}${n.toFixed(decimals)}%`;
}
