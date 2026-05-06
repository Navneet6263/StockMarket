"""Stock Scanner - 4-bucket output, background ML training, cached models, safe concurrency."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.live_data import LiveDataService
from app.market_universe import MarketUniverseService
from app.ml_predictor import StockPredictor
from app.multi_timeframe import MultiTimeframeAnalyzer

INDICES = ["NIFTY", "BANKNIFTY"]

BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "50"))
MAX_WORKERS = int(os.getenv("SCAN_WORKERS", "10"))
BATCH_COOLDOWN = float(os.getenv("BATCH_COOLDOWN_SEC", "1.0"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY_SEC", "0.05"))
RETRY_COUNT = int(os.getenv("SCAN_RETRY_COUNT", "2"))
RETRY_DELAY = float(os.getenv("SCAN_RETRY_DELAY", "0.5"))
CACHE_TTL_MIN = int(os.getenv("SCAN_CACHE_TTL_MIN", "10"))
DEEP_SCAN_LIMIT = int(os.getenv("SCAN_DEEP_LIMIT", "200"))
PREVIEW_LIMIT = int(os.getenv("SCAN_PREVIEW_LIMIT", "10"))
MODEL_CACHE_TTL_MIN = int(os.getenv("MODEL_CACHE_TTL_MIN", "60"))

# ── Strict filters for Top Calls (HIGH CONVICTION only) ──────────────────────
TOP_CALL_MIN_RR = float(os.getenv("TOP_CALL_MIN_RR", "2.0"))
TOP_CALL_MIN_QUALITY = os.getenv("TOP_CALL_MIN_QUALITY", "A")
TOP_CALL_REQUIRE_MTF = os.getenv("TOP_CALL_REQUIRE_MTF", "true").lower() == "true"
TOP_CALL_REQUIRE_TRENDING = os.getenv("TOP_CALL_REQUIRE_TRENDING", "true").lower() == "true"
TOP_CALL_MIN_VOLUME_RATIO = float(os.getenv("TOP_CALL_MIN_VOLUME_RATIO", "1.2"))
TOP_CALL_REQUIRE_BREAKOUT = os.getenv("TOP_CALL_REQUIRE_BREAKOUT", "true").lower() == "true"

# ── Medium filters for Candidates / Watchlist ────────────────────────────────
CANDIDATE_MIN_RR = float(os.getenv("CANDIDATE_MIN_RR", "1.5"))
CANDIDATE_MIN_QUALITY = os.getenv("CANDIDATE_MIN_QUALITY", "B")
CANDIDATE_MIN_VOLUME_RATIO = float(os.getenv("CANDIDATE_MIN_VOLUME_RATIO", "0.8"))

# ── Loose filters for Fast Movers / Missed Moves ─────────────────────────────
FAST_MOVER_MIN_CHANGE_PCT = float(os.getenv("FAST_MOVER_MIN_CHANGE_PCT", "2.0"))
FAST_MOVER_MIN_VOLUME_RATIO = float(os.getenv("FAST_MOVER_MIN_VOLUME_RATIO", "1.5"))

# ── Chase-risk thresholds ─────────────────────────────────────────────────────
CHASE_RISK_CHANGE_PCT = float(os.getenv("CHASE_RISK_CHANGE_PCT", "5.0"))   # intraday move >= this → CHASE_RISK
CHASE_RISK_GAP_PCT = float(os.getenv("CHASE_RISK_GAP_PCT", "3.0"))         # gap-up >= this → CHASE_RISK
PROFIT_BOOKING_CHANGE_PCT = float(os.getenv("PROFIT_BOOKING_CHANGE_PCT", "8.0"))  # move >= this → next-day warning
VALID_ENTRY_MIN_RR = float(os.getenv("VALID_ENTRY_MIN_RR", "1.5"))          # min RR to allow BUY after big move

# ── Avoid / Risky threshold ──────────────────────────────────────────────────
AVOID_MAX_QUALITY = os.getenv("AVOID_MAX_QUALITY", "D")


class ScanCache:
    def __init__(self, ttl_minutes: int = 10):
        self._store: Dict[str, tuple[datetime, Dict]] = {}
        self._ttl = timedelta(minutes=ttl_minutes)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Dict]:
        with self._lock:
            entry = self._store.get(key)
            if entry and (datetime.utcnow() - entry[0]) < self._ttl:
                return entry[1]
            return None

    def set(self, key: str, value: Dict):
        with self._lock:
            self._store[key] = (datetime.utcnow(), value)

    def clear(self):
        with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)


class ModelCache:
    """Thread-safe cache for trained StockPredictor models with TTL."""

    def __init__(self, ttl_minutes: int = MODEL_CACHE_TTL_MIN):
        self._store: Dict[str, tuple[datetime, StockPredictor]] = {}
        self._ttl = timedelta(minutes=ttl_minutes)
        self._lock = threading.Lock()
        self._training_locks: Dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    def _symbol_lock(self, symbol: str) -> threading.Lock:
        with self._meta_lock:
            if symbol not in self._training_locks:
                self._training_locks[symbol] = threading.Lock()
            return self._training_locks[symbol]

    def get(self, symbol: str) -> Optional[StockPredictor]:
        with self._lock:
            entry = self._store.get(symbol)
            if entry and (datetime.utcnow() - entry[0]) < self._ttl:
                return entry[1]
            return None

    def set(self, symbol: str, model: StockPredictor):
        with self._lock:
            self._store[symbol] = (datetime.utcnow(), model)

    def get_or_train(self, symbol: str, df) -> StockPredictor:
        """Return cached model or train one, ensuring only one training per symbol at a time."""
        cached = self.get(symbol)
        if cached is not None:
            return cached

        sym_lock = self._symbol_lock(symbol)
        with sym_lock:
            # Double-check after acquiring lock
            cached = self.get(symbol)
            if cached is not None:
                return cached
            model = StockPredictor()
            model.train_model(df)
            self.set(symbol, model)
            return model

    def precompute_background(self, symbol: str, df):
        """Fire-and-forget background training for a symbol."""
        def _train():
            try:
                self.get_or_train(symbol, df)
            except Exception as exc:
                print(f"[MODEL_CACHE] Background training failed {symbol}: {exc}")

        t = threading.Thread(target=_train, daemon=True, name=f"train-{symbol}")
        t.start()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level shared model cache (survives across scan calls)
_model_cache = ModelCache()


def _passes_top_call(
    quality: str,
    mtf_aligned: bool,
    regime: str,
    vol_ratio: float,
    has_breakout: bool,
    rr: float,
    action: str,
) -> tuple[bool, List[str]]:
    """Strict filter — all conditions must pass for a Top Call."""
    reasons: List[str] = []
    if quality != TOP_CALL_MIN_QUALITY:
        reasons.append(f"Quality {quality} (need A).")
    if TOP_CALL_REQUIRE_MTF and not mtf_aligned:
        reasons.append("MTF not aligned.")
    if TOP_CALL_REQUIRE_TRENDING and "trending" not in regime:
        reasons.append(f"Regime {regime} not trending.")
    if vol_ratio < TOP_CALL_MIN_VOLUME_RATIO:
        reasons.append(f"Volume {vol_ratio:.2f}x < {TOP_CALL_MIN_VOLUME_RATIO}x.")
    if TOP_CALL_REQUIRE_BREAKOUT and not has_breakout:
        reasons.append("No breakout/breakdown strength.")
    if rr < TOP_CALL_MIN_RR:
        reasons.append(f"RR {rr:.2f} < {TOP_CALL_MIN_RR}.")
    if action not in ("BUY", "SELL"):
        reasons.append(f"Action {action}.")
    return len(reasons) == 0, reasons


def _passes_candidate(
    quality: str,
    vol_ratio: float,
    rr: float,
    action: str,
) -> bool:
    """Medium filter — watchlist / setup forming."""
    grade_order = {"A": 4, "B": 3, "C": 2, "D": 1}
    min_grade = grade_order.get(CANDIDATE_MIN_QUALITY, 3)
    return (
        grade_order.get(quality, 1) >= min_grade
        and vol_ratio >= CANDIDATE_MIN_VOLUME_RATIO
        and rr >= CANDIDATE_MIN_RR
        and action in ("BUY", "SELL")
    )


def _is_fast_mover(vol_ratio: float, change_pct: float) -> bool:
    """Loose filter — big move or volume shock, regardless of setup quality."""
    return (
        abs(change_pct) >= FAST_MOVER_MIN_CHANGE_PCT
        or vol_ratio >= FAST_MOVER_MIN_VOLUME_RATIO
    )


def _is_avoid(quality: str, action: str) -> bool:
    """Flag weak/risky setups explicitly."""
    return quality == AVOID_MAX_QUALITY or action == "NO_TRADE"


def _chase_risk_assessment(
    change_pct: float,
    gap_pct: float,
    vol_ratio: float,
    above_vwap: bool,
    rr: float,
    action: str,
    pred: Dict,
) -> Dict:
    """
    Evaluate chase risk for a fast mover.
    Returns entry_label, display_action, warnings, next_day_risk, is_valid_entry.
    """
    abs_change = abs(change_pct)
    abs_gap = abs(gap_pct)

    # ── Determine base risk level ────────────────────────────────────────────
    is_chase_risk = abs_change >= CHASE_RISK_CHANGE_PCT or abs_gap >= CHASE_RISK_GAP_PCT
    is_profit_booking_risk = abs_change >= PROFIT_BOOKING_CHANGE_PCT

    warnings: List[str] = []
    next_day_risk: List[str] = []

    if is_profit_booking_risk:
        warnings.append(
            f"Stock already moved {abs_change:.1f}% today. "
            "High chance of profit booking tomorrow."
        )
        next_day_risk.append("High profit booking risk next session.")
        next_day_risk.append("Suggest trailing stop if already holding.")
        next_day_risk.append("Do not suggest fresh entry unless new confirmation forms.")
    elif is_chase_risk:
        warnings.append(
            f"Stock moved {abs_change:.1f}% intraday. "
            "Avoid late entry unless it holds VWAP/support."
        )

    # ── Check if entry is still valid after the big move ────────────────────
    volume_strong = vol_ratio >= 1.2
    pullback_controlled = abs_change < PROFIT_BOOKING_CHANGE_PCT  # not extreme
    rr_ok = rr >= VALID_ENTRY_MIN_RR
    stop_nearby = pred.get("trade_plan", {}).get("stop_distance_pct", 999) <= 3.0

    is_valid_entry = (
        is_chase_risk
        and above_vwap
        and volume_strong
        and pullback_controlled
        and rr_ok
        and stop_nearby
        and action in ("BUY", "SELL")
        and not is_profit_booking_risk
    )

    # ── Assign entry label ───────────────────────────────────────────────────
    if not is_chase_risk:
        # Normal move — check if it's an early or valid breakout
        breakout_str = pred.get("market_structure", {}).get("breakout_strength", 0)
        if breakout_str > 0.5 and above_vwap and volume_strong:
            entry_label = "VALID_BREAKOUT"
        else:
            entry_label = "EARLY_ENTRY"
        display_action = action
    elif is_profit_booking_risk:
        entry_label = "PROFIT_BOOKING_RISK"
        display_action = "WAIT_FOR_PULLBACK"
    elif is_valid_entry:
        entry_label = "VALID_BREAKOUT"
        display_action = action  # BUY/SELL allowed — conditions met
    else:
        entry_label = "CHASE_RISK" if not is_profit_booking_risk else "AVOID_LATE_ENTRY"
        display_action = "WAIT_FOR_PULLBACK"
        if not above_vwap:
            warnings.append("Price is below VWAP — no support for late entry.")
        if not rr_ok:
            warnings.append(f"Risk/reward {rr:.2f} is too low after the move.")

    # ── UI guidance strings ──────────────────────────────────────────────────
    ui_guidance: List[str] = []
    if display_action == "WAIT_FOR_PULLBACK":
        ui_guidance.append("⚠️ Attention only — do not chase this move.")
        ui_guidance.append("Wait for a pullback to VWAP or support before entry.")
    elif entry_label == "VALID_BREAKOUT":
        ui_guidance.append("✅ Entry conditions still valid after the move.")
        ui_guidance.append("Volume and VWAP support confirm the breakout.")
    elif entry_label == "EARLY_ENTRY":
        ui_guidance.append("🔍 Setup forming — monitor for confirmation.")

    return {
        "entry_label": entry_label,
        "display_action": display_action,
        "is_chase_risk": is_chase_risk,
        "is_profit_booking_risk": is_profit_booking_risk,
        "is_valid_entry_after_move": is_valid_entry,
        "chase_warnings": warnings,
        "next_day_risk": next_day_risk,
        "ui_guidance": ui_guidance,
        "entry_conditions": {
            "above_vwap": above_vwap,
            "volume_strong": volume_strong,
            "pullback_controlled": pullback_controlled,
            "rr_ok": rr_ok,
            "stop_nearby": stop_nearby,
        },
    }


class StockScanner:
    def __init__(self):
        self.live_service = LiveDataService()
        self.market_universe = MarketUniverseService()
        self.mtf_analyzer = MultiTimeframeAnalyzer()
        self.result_cache = ScanCache(ttl_minutes=CACHE_TTL_MIN)
        self.scan_results: List[Dict] = []
        self.last_scan_time: Optional[datetime] = None
        self._active_symbol_meta: Dict[str, Dict] = {}
        self._scan_started_at = 0.0
        self._skipped: List[Dict] = []
        self._progress = self._blank_progress()

    # ── Progress helpers ─────────────────────────────────────────────────────

    def _blank_progress(self) -> Dict:
        return {
            "stage": "idle",
            "message": "Idle",
            "total": 0,
            "done": 0,
            "batch": 0,
            "current_symbol": "",
            "top_calls_found": 0,
            "candidates_found": 0,
            "fast_movers_found": 0,
            "avoid_found": 0,
            "rejected": 0,
            "errors": 0,
            "skipped": 0,
            "elapsed_sec": 0.0,
            "candidate_pool": 0,
            "selected": 0,
            "partial_top_calls": [],
            "partial_candidates": [],
            "discovery": {},
        }

    def _set_progress(self, **kwargs):
        self._progress.update(kwargs)

    def _reset_progress(self):
        self._progress = self._blank_progress()
        self._skipped = []

    def _push_preview(self, key: str, result: Dict):
        preview = list(self._progress.get(key, []))
        preview.append({
            "symbol": result.get("symbol"),
            "action": result.get("action"),
            "quality_grade": result.get("quality_grade"),
            "quality_score": result.get("quality_score"),
            "entry_price": result.get("entry_price"),
            "stop_loss": result.get("stop_loss"),
            "target_1": result.get("target_1"),
            "rr": result.get("rr"),
            "volume_ratio": result.get("volume_ratio"),
            "bucket": result.get("bucket"),
            "reasons": result.get("reasons", [])[:2],
        })
        preview.sort(key=lambda x: x.get("quality_score", 0), reverse=True)
        self._progress[key] = preview[:PREVIEW_LIMIT]

    # ── Shortlist builder ────────────────────────────────────────────────────

    def _build_shortlist(self, discovery: Dict) -> List[str]:
        candidate_pool = discovery.get("scan_symbols", []) or discovery.get("symbols", [])
        if len(candidate_pool) <= DEEP_SCAN_LIMIT:
            return candidate_pool

        symbol_meta = discovery.get("symbol_meta", {})
        # Sort by discovery_score desc, then volume desc
        ranked = sorted(
            candidate_pool,
            key=lambda s: (
                -float(symbol_meta.get(s, {}).get("discovery_score") or 0),
                -float(symbol_meta.get(s, {}).get("volume") or 0),
            ),
        )
        return ranked[:DEEP_SCAN_LIMIT]

    # ── Data fetch ───────────────────────────────────────────────────────────

    def _fetch_with_retry(self, symbol: str, period: str = "1y"):
        for attempt in range(RETRY_COUNT):
            try:
                df = self.live_service.get_historical_data(symbol, period)
                if not df.empty and len(df) >= 60:
                    return df
            except Exception as exc:
                if attempt < RETRY_COUNT - 1:
                    time.sleep(RETRY_DELAY)
                else:
                    print(f"[SCANNER] {symbol} failed after {RETRY_COUNT} retries: {exc}")
        return None

    # ── Core scan for one symbol ─────────────────────────────────────────────

    def _scan_one(self, symbol: str) -> Optional[Dict]:
        cached = self.result_cache.get(symbol)
        if cached:
            return cached

        if REQUEST_DELAY > 0:
            time.sleep(REQUEST_DELAY)

        try:
            df = self._fetch_with_retry(symbol)
            if df is None:
                self._skipped.append({"symbol": symbol, "reason": "insufficient_history_or_fetch_failed"})
                return None

            # Use cached model; train inline only if not cached (background precompute for next scan)
            model = _model_cache.get(symbol.upper())
            if model is None:
                model = StockPredictor()
                model.train_model(df)
                _model_cache.set(symbol.upper(), model)
                # Precompute for next scan cycle in background
                _model_cache.precompute_background(symbol.upper(), df)
            elif not model.is_trained:
                model.train_model(df)
                _model_cache.set(symbol.upper(), model)

            pred = model.predict(df)
            backtest = model.backtest_metrics

            mtf_aligned = False
            mtf_sync = "UNKNOWN"
            vix_value = 0.0
            vix_regime = "unknown"
            try:
                mtf = self.mtf_analyzer.analyze_multi_timeframe(symbol)
                mtf_aligned = mtf.get("tradeable", False)
                mtf_sync = mtf.get("sync_status", "CONFLICTING")
                vix_value = mtf.get("volatility_index", {}).get("value", 0)
                vix_regime = mtf.get("volatility_index", {}).get("regime", "unknown")
            except Exception:
                pass

            strategy = pred.get("strategy", {})
            trade_plan = pred.get("trade_plan", {})
            quality = strategy.get("quality_grade", "D")
            action = strategy.get("action", "WATCH")
            regime = pred.get("market_regime", {}).get("regime", "sideways")
            rr = trade_plan.get("risk_reward_ratio", 0)
            vol_ratio = pred.get("volume_analysis", {}).get("volume_ratio", 0)
            breakout_str = pred.get("market_structure", {}).get("breakout_strength", 0)
            breakdown_str = pred.get("market_structure", {}).get("breakdown_strength", 0)
            has_breakout = breakout_str > 0.5 or breakdown_str > 0.5
            change_pct = float(df["Close"].pct_change().iloc[-1] * 100) if len(df) > 1 else 0.0
            gap_pct = float(
                (df["Open"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2] * 100
            ) if len(df) > 1 else 0.0

            # above_vwap from pred volume_analysis (vwap_distance_pct > 0 means above)
            vwap_dist = pred.get("volume_analysis", {}).get("vwap_distance_pct", 0)
            above_vwap = vwap_dist >= 0

            # ── Bucket classification ────────────────────────────────────────
            top_call, top_call_fail_reasons = _passes_top_call(
                quality, mtf_aligned, regime, vol_ratio, has_breakout, rr, action
            )
            candidate = (not top_call) and _passes_candidate(quality, vol_ratio, rr, action)
            is_fast = _is_fast_mover(vol_ratio, change_pct)
            fast_mover = (not top_call) and (not candidate) and is_fast
            avoid = _is_avoid(quality, action)

            if top_call:
                bucket = "top_call"
            elif candidate:
                bucket = "candidate"
            elif fast_mover:
                bucket = "fast_mover"
            elif avoid:
                bucket = "avoid"
            else:
                bucket = "rejected"

            # ── Chase-risk assessment (for fast_movers AND candidates) ───────
            chase = _chase_risk_assessment(
                change_pct=change_pct,
                gap_pct=gap_pct,
                vol_ratio=vol_ratio,
                above_vwap=above_vwap,
                rr=rr,
                action=action,
                pred=pred,
            )

            # Top Calls are NEVER overridden by chase logic — they passed strict filters
            # For fast_movers: display_action replaces raw action in UI
            # For candidates with chase risk: warn but keep as candidate
            effective_action = action if bucket == "top_call" else chase["display_action"]

            result = {
                "symbol": symbol,
                "action": effective_action,
                "raw_action": action,  # original ML action, always preserved
                "setup": strategy.get("setup", ""),
                "quality_grade": quality,
                "quality_score": strategy.get("quality_score", 0),
                "confidence": round(pred.get("confidence", 0), 4),
                "direction": pred.get("direction", "neutral"),
                "regime": regime,
                "mtf_aligned": mtf_aligned,
                "mtf_sync": mtf_sync,
                "volume_ratio": round(vol_ratio, 2),
                "has_breakout": has_breakout,
                "rr": rr,
                "change_pct": round(change_pct, 2),
                "gap_pct": round(gap_pct, 2),
                "above_vwap": above_vwap,
                "entry_price": trade_plan.get("entry_price", 0),
                "stop_loss": trade_plan.get("stop_loss", 0),
                "target_1": trade_plan.get("target_1", 0),
                "target_2": trade_plan.get("target_2", 0),
                "expected_move_pct": trade_plan.get("expected_move_pct", 0),
                "vix_value": vix_value,
                "vix_regime": vix_regime,
                "backtest_win_rate": backtest.get("win_rate", 0),
                "backtest_pf": backtest.get("profit_factor", 0),
                "backtest_max_dd": backtest.get("max_drawdown", 0),
                "probability_up": round(pred.get("probability_up", 0), 4),
                "probability_down": round(pred.get("probability_down", 0), 4),
                "position_size_factor": trade_plan.get(
                    "position_size_factor", strategy.get("position_size_factor", 0)
                ),
                "support": round(pred.get("support", 0), 2),
                "resistance": round(pred.get("resistance", 0), 2),
                "reasons": pred.get("reasons", [])[:4],
                "risk_management": pred.get("risk_management", [])[:4],
                "no_trade_reason": strategy.get("no_trade_reason", ""),
                "top_call_fail_reasons": top_call_fail_reasons[:5],
                # ── Chase-risk fields ────────────────────────────────────────
                "entry_label": chase["entry_label"],
                "is_chase_risk": chase["is_chase_risk"],
                "is_profit_booking_risk": chase["is_profit_booking_risk"],
                "is_valid_entry_after_move": chase["is_valid_entry_after_move"],
                "chase_warnings": chase["chase_warnings"],
                "next_day_risk": chase["next_day_risk"],
                "ui_guidance": chase["ui_guidance"],
                "entry_conditions": chase["entry_conditions"],
                "bucket": bucket,
                "passed_filter": top_call,  # legacy compat
                "scanned_at": datetime.utcnow().isoformat(),
            }

            self.result_cache.set(symbol, result)
            return result

        except Exception as exc:
            print(f"[SCANNER] Error scanning {symbol}: {exc}")
            self._skipped.append({"symbol": symbol, "reason": f"exception: {exc}"})
            return None

    # ── Batch runner ─────────────────────────────────────────────────────────

    def _scan_batch(self, batch: List[str]) -> tuple[List[Dict], int]:
        results: List[Dict] = []
        errors = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self._scan_one, symbol): symbol for symbol in batch}
            for future in as_completed(futures):
                symbol = futures[future]
                self._set_progress(
                    current_symbol=symbol,
                    elapsed_sec=round(time.time() - self._scan_started_at, 1),
                )
                try:
                    result = future.result()
                    if result:
                        result = self._attach_discovery_meta(result)
                        results.append(result)
                        bucket = result.get("bucket", "rejected")
                        if bucket == "top_call":
                            self._progress["top_calls_found"] += 1
                            self._push_preview("partial_top_calls", result)
                        elif bucket == "candidate":
                            self._progress["candidates_found"] += 1
                            self._push_preview("partial_candidates", result)
                        elif bucket == "fast_mover":
                            self._progress["fast_movers_found"] += 1
                        elif bucket == "avoid":
                            self._progress["avoid_found"] += 1
                        else:
                            self._progress["rejected"] += 1
                    else:
                        errors += 1
                        self._progress["errors"] += 1
                except Exception:
                    errors += 1
                    self._progress["errors"] += 1
                finally:
                    self._progress["done"] += 1

        return results, errors

    def _attach_discovery_meta(self, result: Dict) -> Dict:
        meta = self._active_symbol_meta.get(result["symbol"], {})
        result["discovered_by"] = meta.get("tags", result.get("discovered_by", []))
        result["discovery_snapshot"] = {
            "price": meta.get("price"),
            "change_pct": meta.get("change_pct"),
            "volume": meta.get("volume"),
            "market_cap": meta.get("market_cap"),
        }
        return result

    # ── Main scan entry ──────────────────────────────────────────────────────

    def scan_all(self, stocks: Optional[List[str]] = None) -> Dict:
        self._reset_progress()
        discovery = None
        symbol_meta: Dict[str, Dict] = {}

        if stocks:
            stock_list = list(dict.fromkeys(stocks))
            self._set_progress(
                stage="manual_scan",
                message="Scanning requested symbols.",
                candidate_pool=len(stock_list),
                selected=len(stock_list),
            )
        else:
            self._set_progress(stage="discovering", message="Loading live Yahoo market universe.")
            discovery = self.market_universe.discover_market()
            symbol_meta = discovery.get("symbol_meta", {})
            candidate_pool = discovery.get("scan_symbols", []) or discovery.get("symbols", [])
            shortlisted = self._build_shortlist(discovery)
            stock_list = list(dict.fromkeys(INDICES + shortlisted))
            self._set_progress(
                stage="shortlisting",
                message=f"Shortlisted {len(shortlisted)} from {len(candidate_pool)} candidates.",
                candidate_pool=len(candidate_pool),
                selected=len(shortlisted),
                discovery={
                    "source_mode": discovery.get("source_mode"),
                    "bucket_counts": discovery.get("bucket_counts", {}),
                    "note": discovery.get("note"),
                },
            )

        total = len(stock_list)
        start = time.time()
        self._scan_started_at = start
        self._active_symbol_meta = symbol_meta
        all_results: List[Dict] = []
        total_errors = 0
        batch_num = 0

        self._set_progress(
            stage="deep_scanning",
            message="Running deep scan.",
            total=total,
            done=0,
            batch=0,
            current_symbol="",
            top_calls_found=0,
            candidates_found=0,
            fast_movers_found=0,
            avoid_found=0,
            rejected=0,
            errors=0,
            elapsed_sec=0.0,
        )

        for i in range(0, total, BATCH_SIZE):
            batch = stock_list[i: i + BATCH_SIZE]
            batch_num += 1
            self._set_progress(batch=batch_num)
            print(
                f"[SCANNER] Batch {batch_num}: {len(batch)} stocks "
                f"({i + 1}-{min(i + BATCH_SIZE, total)}/{total})"
            )
            results, errors = self._scan_batch(batch)
            all_results.extend(results)
            total_errors += errors
            if i + BATCH_SIZE < total:
                time.sleep(BATCH_COOLDOWN)

        elapsed = round(time.time() - start, 1)
        self.scan_results = all_results
        self.last_scan_time = datetime.utcnow()

        # ── Bucket separation ────────────────────────────────────────────────
        top_calls = sorted(
            [r for r in all_results if r["bucket"] == "top_call"],
            key=lambda r: r["quality_score"],
            reverse=True,
        )
        candidates = sorted(
            [r for r in all_results if r["bucket"] == "candidate"],
            key=lambda r: r["quality_score"],
            reverse=True,
        )
        fast_movers = sorted(
            [r for r in all_results if r["bucket"] == "fast_mover"],
            key=lambda r: abs(r.get("change_pct", 0)),
            reverse=True,
        )
        avoid = sorted(
            [r for r in all_results if r["bucket"] == "avoid"],
            key=lambda r: r["quality_score"],
        )
        rejected = [r for r in all_results if r["bucket"] == "rejected"]

        # ── Scan stats ───────────────────────────────────────────────────────
        scan_stats = {
            "total_universe": total,
            "scanned_count": len(all_results),
            "top_calls_count": len(top_calls),
            "candidates_count": len(candidates),
            "fast_movers_count": len(fast_movers),
            "avoid_count": len(avoid),
            "rejected_count": len(rejected),
            "errors_count": total_errors,
            "skipped_count": len(self._skipped),
            "cache_hits": self.result_cache.size,
            "model_cache_size": _model_cache.size,
            "elapsed_sec": elapsed,
            "batches": batch_num,
            "scanned_at": self.last_scan_time.isoformat(),
        }

        print(
            f"[SCANNER] Done: universe={total} scanned={len(all_results)} "
            f"top_calls={len(top_calls)} candidates={len(candidates)} "
            f"fast_movers={len(fast_movers)} avoid={len(avoid)} "
            f"errors={total_errors} elapsed={elapsed}s"
        )

        discovery_payload = discovery or {"source_mode": "manual", "symbols": stock_list}
        if discovery:
            discovery_payload = {
                **discovery,
                "candidate_pool": len(discovery.get("scan_symbols", []) or discovery.get("symbols", [])),
                "selected_for_deep_scan": max(total - len(INDICES), 0),
                "deep_scan_limit": DEEP_SCAN_LIMIT,
            }

        self._set_progress(
            stage="completed",
            message="Scan complete.",
            current_symbol="",
            elapsed_sec=elapsed,
            total=total,
            done=total,
            top_calls_found=len(top_calls),
            candidates_found=len(candidates),
            fast_movers_found=len(fast_movers),
            avoid_found=len(avoid),
            rejected=len(rejected),
            errors=total_errors,
            skipped=len(self._skipped),
        )

        return {
            "scan_stats": scan_stats,
            "discovery": discovery_payload,
            "top_calls": top_calls,
            "candidates": candidates,
            "fast_movers": fast_movers,
            "avoid": avoid,
            "skipped": self._skipped[:50],
            # Legacy fields for backward compat
            "total_scanned": total,
            "successful_scans": len(all_results),
            "signals": len(top_calls),
            "rejected": len(rejected),
            "errors": total_errors,
            "elapsed_sec": elapsed,
            "batches": batch_num,
            "cache_size": self.result_cache.size,
            "scanned_at": self.last_scan_time.isoformat(),
            "passed": top_calls,
            "top_rejected": sorted(rejected, key=lambda r: r["quality_score"], reverse=True)[:10],
        }

    def get_passed_signals(self) -> List[Dict]:
        return [r for r in self.scan_results if r.get("bucket") == "top_call"]

    def get_candidates(self) -> List[Dict]:
        return [r for r in self.scan_results if r.get("bucket") == "candidate"]

    def get_fast_movers(self) -> List[Dict]:
        return [r for r in self.scan_results if r.get("bucket") == "fast_mover"]

    def get_avoid(self) -> List[Dict]:
        return [r for r in self.scan_results if r.get("bucket") == "avoid"]

    def get_progress(self) -> Dict:
        return {
            **self._progress,
            "partial_top_calls": list(self._progress.get("partial_top_calls", [])),
            "partial_candidates": list(self._progress.get("partial_candidates", [])),
        }


_scanner: Optional[StockScanner] = None
_scanner_lock = threading.Lock()


def get_scanner() -> StockScanner:
    global _scanner
    if _scanner is None:
        with _scanner_lock:
            if _scanner is None:
                _scanner = StockScanner()
    return _scanner
