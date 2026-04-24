"""Stock Scanner - batch processing, cache, retry, rate limiting."""

from __future__ import annotations

import os
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
MAX_WORKERS = int(os.getenv("SCAN_WORKERS", "5"))
MIN_RR = float(os.getenv("MIN_RISK_REWARD", "2.0"))
BATCH_COOLDOWN = float(os.getenv("BATCH_COOLDOWN_SEC", "2"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY_SEC", "0.1"))
RETRY_COUNT = int(os.getenv("SCAN_RETRY_COUNT", "3"))
RETRY_DELAY = float(os.getenv("SCAN_RETRY_DELAY", "1.0"))
CACHE_TTL_MIN = int(os.getenv("SCAN_CACHE_TTL_MIN", "5"))
DEEP_SCAN_LIMIT = int(os.getenv("SCAN_DEEP_LIMIT", "90"))
PREVIEW_LIMIT = int(os.getenv("SCAN_PREVIEW_LIMIT", "5"))


class ScanCache:
    """Simple TTL cache for scan results."""

    def __init__(self, ttl_minutes: int = 5):
        self._store: Dict[str, tuple[datetime, Dict]] = {}
        self._ttl = timedelta(minutes=ttl_minutes)

    def get(self, key: str) -> Optional[Dict]:
        entry = self._store.get(key)
        if entry and (datetime.utcnow() - entry[0]) < self._ttl:
            return entry[1]
        return None

    def set(self, key: str, value: Dict):
        self._store[key] = (datetime.utcnow(), value)

    def clear(self):
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


class StockScanner:
    def __init__(self):
        self.live_service = LiveDataService()
        self.market_universe = MarketUniverseService()
        self.mtf_analyzer = MultiTimeframeAnalyzer()
        self.trained_models: Dict[str, StockPredictor] = {}
        self.cache = ScanCache(ttl_minutes=CACHE_TTL_MIN)
        self.scan_results: List[Dict] = []
        self.last_scan_time: Optional[datetime] = None
        self._active_symbol_meta: Dict[str, Dict] = {}
        self._scan_started_at = 0.0
        self._progress = {
            "stage": "idle",
            "message": "Idle",
            "total": 0,
            "done": 0,
            "batch": 0,
            "current_symbol": "",
            "signals_found": 0,
            "rejected": 0,
            "errors": 0,
            "elapsed_sec": 0.0,
            "candidate_pool": 0,
            "selected": 0,
            "partial_signals": [],
            "partial_rejected": [],
            "discovery": {},
        }

    def _set_progress(self, **kwargs):
        self._progress.update(kwargs)

    def _reset_progress(self):
        self._progress = {
            "stage": "idle",
            "message": "Idle",
            "total": 0,
            "done": 0,
            "batch": 0,
            "current_symbol": "",
            "signals_found": 0,
            "rejected": 0,
            "errors": 0,
            "elapsed_sec": 0.0,
            "candidate_pool": 0,
            "selected": 0,
            "partial_signals": [],
            "partial_rejected": [],
            "discovery": {},
        }

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

    def _push_preview(self, key: str, result: Dict):
        preview = list(self._progress.get(key, []))
        preview.append(
            {
                "symbol": result.get("symbol"),
                "action": result.get("action"),
                "quality_grade": result.get("quality_grade"),
                "quality_score": result.get("quality_score"),
                "entry_price": result.get("entry_price"),
                "stop_loss": result.get("stop_loss"),
                "target_1": result.get("target_1"),
                "target_2": result.get("target_2"),
                "rr": result.get("rr"),
                "volume_ratio": result.get("volume_ratio"),
                "reasons": result.get("reasons", [])[:3],
                "rejection_reasons": result.get("rejection_reasons", [])[:3],
                "discovered_by": result.get("discovered_by", []),
            }
        )
        preview.sort(key=lambda item: item.get("quality_score", 0), reverse=True)
        self._progress[key] = preview[:PREVIEW_LIMIT]

    def _build_fast_shortlist(self, discovery: Dict) -> List[str]:
        candidate_pool = discovery.get("scan_symbols", []) or discovery.get("symbols", [])
        symbol_meta = discovery.get("symbol_meta", {})
        if len(candidate_pool) <= DEEP_SCAN_LIMIT:
            return candidate_pool

        selected: List[str] = []
        seen = set()
        bucket_plan = [
            ("breakout_candidates", 18),
            ("fundamental_growth", 16),
            ("quality_compounders", 12),
            ("value_growth", 12),
            ("most_active", 12),
            ("low_price_active", 10),
            ("turnaround_watch", 6),
            ("selling_pressure", 4),
        ]

        for tag, limit in bucket_plan:
            bucket_count = 0
            for symbol in candidate_pool:
                if len(selected) >= DEEP_SCAN_LIMIT or bucket_count >= limit:
                    break
                if symbol in seen:
                    continue
                tags = symbol_meta.get(symbol, {}).get("tags", [])
                if tag not in tags:
                    continue
                selected.append(symbol)
                seen.add(symbol)
                bucket_count += 1

        for symbol in candidate_pool:
            if len(selected) >= DEEP_SCAN_LIMIT:
                break
            if symbol in seen:
                continue
            selected.append(symbol)
            seen.add(symbol)

        return selected

    def _fetch_with_retry(self, symbol: str, period: str = "1y"):
        for attempt in range(RETRY_COUNT):
            try:
                df = self.live_service.get_historical_data(symbol, period)
                if not df.empty and len(df) >= 100:
                    return df
            except Exception as exc:
                if attempt < RETRY_COUNT - 1:
                    time.sleep(RETRY_DELAY)
                else:
                    print(f"[SCANNER] {symbol} failed after {RETRY_COUNT} retries: {exc}")
        return None

    def _scan_one(self, symbol: str) -> Optional[Dict]:
        cached = self.cache.get(symbol)
        if cached:
            return cached

        time.sleep(REQUEST_DELAY)

        try:
            df = self._fetch_with_retry(symbol)
            if df is None:
                return None

            key = symbol.upper()
            if key not in self.trained_models:
                model = StockPredictor()
                model.train_model(df)
                self.trained_models[key] = model

            pred = self.trained_models[key].predict(df)
            backtest = self.trained_models[key].backtest_metrics

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
            volume_spike = vol_ratio >= 1.2
            breakout_str = pred.get("market_structure", {}).get("breakout_strength", 0)
            breakdown_str = pred.get("market_structure", {}).get("breakdown_strength", 0)
            has_breakout = breakout_str > 0.5 or breakdown_str > 0.5
            rejection_reasons: List[str] = []

            if quality != "A":
                rejection_reasons.append(f"Quality grade is {quality}, not A.")
            if not mtf_aligned:
                rejection_reasons.append("Multi-timeframe alignment is not tradeable.")
            if "trending" not in regime:
                rejection_reasons.append(f"Market regime is {regime}.")
            if not volume_spike:
                rejection_reasons.append(f"Volume is only {vol_ratio:.2f}x of average.")
            if not has_breakout:
                rejection_reasons.append("Breakout or breakdown strength is still weak.")
            if rr < MIN_RR:
                rejection_reasons.append(f"Risk/reward is 1:{rr}, below 1:{MIN_RR}.")
            if action not in ("BUY", "SELL"):
                rejection_reasons.append(f"Strategy action is {action}.")

            passed = (
                quality == "A"
                and mtf_aligned
                and "trending" in regime
                and volume_spike
                and has_breakout
                and rr >= MIN_RR
                and action in ("BUY", "SELL")
            )

            result = {
                "symbol": symbol,
                "action": action,
                "setup": strategy.get("setup", ""),
                "quality_grade": quality,
                "quality_score": strategy.get("quality_score", 0),
                "confidence": round(pred.get("confidence", 0), 4),
                "direction": pred.get("direction", "neutral"),
                "regime": regime,
                "mtf_aligned": mtf_aligned,
                "mtf_sync": mtf_sync,
                "volume_ratio": round(vol_ratio, 2),
                "volume_spike": volume_spike,
                "has_breakout": has_breakout,
                "rr": rr,
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
                "rejection_reasons": rejection_reasons[:5],
                "passed_filter": passed,
                "scanned_at": datetime.utcnow().isoformat(),
            }

            self.cache.set(symbol, result)
            return result

        except Exception as exc:
            print(f"[SCANNER] Error scanning {symbol}: {exc}")
            return None

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
                        if result["passed_filter"]:
                            self._progress["signals_found"] += 1
                            self._push_preview("partial_signals", result)
                        else:
                            self._progress["rejected"] += 1
                            self._push_preview("partial_rejected", result)
                    else:
                        errors += 1
                        self._progress["errors"] += 1
                except Exception:
                    errors += 1
                    self._progress["errors"] += 1
                finally:
                    self._progress["done"] += 1

        return results, errors

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
            shortlisted = self._build_fast_shortlist(discovery)
            stock_list = INDICES + shortlisted
            self._set_progress(
                stage="shortlisting",
                message="Ranking live Yahoo candidates before deep scan.",
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
            message="Running deep scan on live Yahoo shortlist.",
            total=total,
            done=0,
            batch=0,
            current_symbol="",
            signals_found=0,
            rejected=0,
            errors=0,
            elapsed_sec=0.0,
        )

        for i in range(0, total, BATCH_SIZE):
            batch = stock_list[i : i + BATCH_SIZE]
            batch_num += 1
            self._set_progress(batch=batch_num)

            print(f"[SCANNER] Batch {batch_num}: {len(batch)} stocks ({i + 1}-{min(i + BATCH_SIZE, total)}/{total})")

            results, errors = self._scan_batch(batch)
            all_results.extend(results)
            total_errors += errors

            if i + BATCH_SIZE < total:
                time.sleep(BATCH_COOLDOWN)

        elapsed = round(time.time() - start, 1)
        self.scan_results = all_results
        self.last_scan_time = datetime.utcnow()

        passed = sorted(
            [result for result in all_results if result["passed_filter"]],
            key=lambda result: result["quality_score"],
            reverse=True,
        )
        rejected = [result for result in all_results if not result["passed_filter"]]

        print(
            f"[SCANNER] Done: {len(all_results)} scanned, "
            f"{len(passed)} signals, {total_errors} errors, "
            f"{elapsed}s, cache={self.cache.size}"
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
            signals_found=len(passed),
            rejected=len(rejected),
            errors=total_errors,
        )

        return {
            "total_scanned": total,
            "successful_scans": len(all_results),
            "signals": len(passed),
            "rejected": len(rejected),
            "errors": total_errors,
            "elapsed_sec": elapsed,
            "batches": batch_num,
            "cache_size": self.cache.size,
            "scanned_at": self.last_scan_time.isoformat(),
            "discovery": discovery_payload,
            "passed": passed,
            "top_rejected": sorted(rejected, key=lambda result: result["quality_score"], reverse=True)[:10],
        }

    def get_passed_signals(self) -> List[Dict]:
        return [result for result in self.scan_results if result["passed_filter"]]

    def get_progress(self) -> Dict:
        return {
            **self._progress,
            "partial_signals": list(self._progress.get("partial_signals", [])),
            "partial_rejected": list(self._progress.get("partial_rejected", [])),
        }


_scanner: Optional[StockScanner] = None


def get_scanner() -> StockScanner:
    global _scanner
    if _scanner is None:
        _scanner = StockScanner()
    return _scanner
