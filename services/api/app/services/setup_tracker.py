from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import time
from typing import Any
from uuid import uuid4

import pandas as pd

from app.core.settings import Settings
from app.services.data_provider import MarketDataService
from app.services.lifecycle import build_lifecycle_advice, directional_return, target_progress
from app.services.setup_store import SetupStore


TRACKABLE_BUCKETS = ("top_opportunities", "breakout_candidates", "unusual_volume")
AUTO_CALL_BUCKETS = ("top_opportunities", "breakout_candidates", "unusual_volume", "bearish_risks")
logger = logging.getLogger(__name__)

# ── ATR Chandelier Exit config ────────────────────────────────────────────────
CHANDELIER_MULTIPLIER = float(os.getenv("CHANDELIER_ATR_MULTIPLIER", "3.0"))
CHANDELIER_PERIOD     = int(os.getenv("CHANDELIER_ATR_PERIOD", "22"))
ENABLE_DYNAMIC_TRAIL  = os.getenv("ENABLE_DYNAMIC_TRAILING_STOP", "true").lower() == "true"


def chandelier_exit(frame: pd.DataFrame, direction: str = "bullish") -> float | None:
    """
    ATR-based Chandelier Exit trailing stop.
    Long:  highest_high(period) - multiplier * ATR(period)
    Short: lowest_low(period)  + multiplier * ATR(period)
    Returns the trailing stop price or None if insufficient data.
    """
    if not ENABLE_DYNAMIC_TRAIL or frame is None or frame.empty or len(frame) < CHANDELIER_PERIOD:
        return None
    try:
        high  = frame["High"].astype(float)
        low   = frame["Low"].astype(float)
        close = frame["Close"].astype(float)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(CHANDELIER_PERIOD).mean().iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return None
        if direction == "bullish":
            return round(float(high.rolling(CHANDELIER_PERIOD).max().iloc[-1]) - CHANDELIER_MULTIPLIER * atr, 2)
        else:
            return round(float(low.rolling(CHANDELIER_PERIOD).min().iloc[-1]) + CHANDELIER_MULTIPLIER * atr, 2)
    except Exception as exc:
        logger.debug("chandelier_exit failed: %s", exc)
        return None


class SetupTrackerService:
    def __init__(self, settings: Settings, data: MarketDataService):
        self.settings = settings
        self.data = data
        self.store = SetupStore(settings.tracked_setup_db_path)
        self._last_dashboard_eval_at = 0.0

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _parse_dt(self, value: str | None) -> datetime:
        if not value:
            return self._now()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _period_for_days(self, days: int) -> str:
        if days <= 5:
            return "1mo"
        if days <= 15:
            return "3mo"
        return "6mo"

    def _directional_return(self, direction: str, entry: float, current: float) -> float:
        return directional_return(direction, entry, current)

    def _target_progress(self, direction: str, entry: float, current: float, target: float) -> float:
        return target_progress(direction, entry, current, target)

    def _lifecycle_advice(self, setup: dict[str, Any], current_price: float) -> tuple[str, str, str]:
        lifecycle = self._build_lifecycle(setup, current_price)
        return lifecycle["hold_or_exit"], lifecycle["scanner_call_status"], lifecycle["reason_for_exit_decision"]

    def _build_lifecycle(self, setup: dict[str, Any], current_price: float, *, expired: bool = False) -> dict[str, Any]:
        return build_lifecycle_advice(
            direction=setup["direction"],
            entry_price=float(setup.get("entry_price") or current_price or 0),
            current_price=float(current_price or 0),
            target_1=float(setup.get("target_1") or setup.get("target_price") or 0),
            target_2=float(setup.get("target_2") or setup.get("extended_target_price") or 0),
            stop_loss=float(setup.get("stop_loss") or setup.get("invalidation") or 0),
            trailing_stop=float(setup.get("trailing_stop") or setup.get("stop_loss") or setup.get("invalidation") or 0),
            expired=expired,
        )

    def _timestamp_updates(self, setup: dict[str, Any], lifecycle: dict[str, Any], now_iso: str) -> dict[str, Any]:
        updates: dict[str, Any] = {"last_checked_at": now_iso}
        status = lifecycle.get("scanner_call_status")
        reason = lifecycle.get("exit_reason")
        if status in {"TARGET_1_HIT", "TARGET_2_HIT"} and not setup.get("target_hit_at"):
            updates["target_hit_at"] = now_iso
        if lifecycle.get("hold_or_exit") == "PARTIAL_BOOK" and not setup.get("partial_book_at"):
            updates["partial_book_at"] = now_iso
        if status == "EXIT_SUGGESTED" and not setup.get("exit_suggested_at"):
            updates["exit_suggested_at"] = now_iso
        if status == "STOP_LOSS_HIT" and not setup.get("stop_loss_hit_at"):
            updates["stop_loss_hit_at"] = now_iso
        if status == "EXPIRED" and not setup.get("expired_at"):
            updates["expired_at"] = now_iso
        if lifecycle.get("hold_or_exit") == "EXIT" and not setup.get("closed_at"):
            updates["closed_at"] = now_iso
        if reason:
            updates["exit_reason"] = reason
        return updates

    def _tracking_label(self, signal: dict[str, Any]) -> str:
        if signal["direction"] == "neutral":
            return "Avoid / High Risk"
        if not signal.get("allow_buy_call", True) or signal.get("attention_only", False):
            return "Avoid / High Risk"
        if signal.get("current_price", 0) < self.settings.min_price or signal.get("volume", 0) < self.settings.min_volume:
            return "Low Liquidity"
        if signal.get("risk_reward", 0) < 1.25 or (signal.get("risk_level") == "high" and signal.get("confidence", 0) < 72):
            return "Avoid / High Risk"
        if (signal["direction"] == "bullish" and signal.get("rsi", 50) >= 72) or (
            signal["direction"] == "bearish" and signal.get("rsi", 50) <= 28
        ):
            return "Overextended"
        if signal.get("relative_volume", 1.0) < 1.15 or signal.get("intraday_volume_ratio", 1.0) < 1.0:
            return "Needs Volume Confirmation"
        if (
            signal.get("confidence", 0) >= self.settings.tracked_promotion_confidence_min
            and signal.get("move_quality", 0) >= self.settings.tracked_promotion_move_quality_min
            and signal.get("risk_reward", 0) >= 1.6
        ):
            return "Confirmed Setup"
        return "Early Watch"

    def _status_for_label(self, tracking_label: str) -> str | None:
        if tracking_label == "Confirmed Setup":
            return "active"
        if tracking_label in {"Early Watch", "Overextended", "Needs Volume Confirmation"}:
            return "watch_only"
        return None

    def _setup_note(self, signal: dict[str, Any]) -> str:
        watch_level = "above" if signal["direction"] == "bullish" else "below"
        trigger = signal.get("stop_loss") or signal.get("invalidation") or signal.get("current_price")
        return (
            f"{signal['setup_label']}. {signal['direction'].capitalize()} watch for {signal['timeframe_label']}. "
            f"Valid while price holds {watch_level} {trigger:.2f}."
        )

    def _base_record(
        self,
        signal: dict[str, Any],
        scanner_bucket: str,
        company_name: str,
        sector: str | None,
        source_mode: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        timeframe_days = max(1, int(signal.get("timeframe_days") or 3))
        tracking_label = self._tracking_label(signal)
        return {
            "id": uuid4().hex,
            "symbol": signal["symbol"],
            "company_name": company_name or signal["symbol"],
            "sector": sector,
            "direction": signal["direction"],
            "setup_label": signal["setup_label"],
            "tracking_label": tracking_label,
            "scanner_bucket": scanner_bucket,
            "source_mode": source_mode,
            "detected_at": now.isoformat(),
            "suggested_at": now.isoformat() if source_mode == "scanner_suggested" else None,
            "last_seen_at": now.isoformat(),
            "last_evaluated_at": None,
            "expires_at": (now + timedelta(days=max(1, int(timeframe_days * 1.5)))).isoformat(),
            "timeframe_label": signal.get("timeframe_label"),
            "timeframe_days": timeframe_days,
            "entry_price": signal.get("current_price"),
            "current_price": signal.get("current_price"),
            "target_price": signal.get("target_price"),
            "target_1": signal.get("target_1") or signal.get("target_price"),
            "target_2": signal.get("target_2") or signal.get("extended_target_price"),
            "extended_target_price": signal.get("extended_target_price"),
            "stop_loss": signal.get("stop_loss"),
            "trailing_stop": signal.get("trailing_stop") or signal.get("stop_loss") or signal.get("invalidation"),
            "invalidation": signal.get("invalidation"),
            "confidence": signal.get("confidence"),
            "model_confidence": signal.get("model_confidence"),
            "evidence_confidence": signal.get("evidence_confidence"),
            "evidence_status": signal.get("historical_evidence_status"),
            "expected_move_pct": signal.get("expected_move_pct"),
            "risk_level": signal.get("risk_level"),
            "risk_reward": signal.get("risk_reward"),
            "move_quality": signal.get("move_quality"),
            "relative_volume": signal.get("relative_volume"),
            "intraday_volume_ratio": signal.get("intraday_volume_ratio"),
            "change_pct": signal.get("change_pct"),
            "reason_summary": signal.get("signal_summary") or self._setup_note(signal),
            "reasons": signal.get("reasons", []),
            "risk_factors": signal.get("risk_factors", []),
            "tags": signal.get("tags", []),
            "status": self._status_for_label(tracking_label) or "watch_only",
            "scanner_call_status": "ACTIVE" if source_mode == "scanner_suggested" else None,
            "hold_or_exit": signal.get("hold_or_exit") or "WAIT",
            "reason_for_exit_decision": signal.get("reason_for_exit_decision") or self._setup_note(signal),
            "target_hit_at": None,
            "partial_book_at": None,
            "exit_suggested_at": None,
            "stop_loss_hit_at": None,
            "expired_at": None,
            "last_checked_at": None,
            "closed_at": None,
            "exit_reason": None,
            "result_pct": 0.0,
            "current_pnl_pct": 0.0,
            "target_progress_pct": 0.0,
            "max_favorable_move": 0.0,
            "max_adverse_move": 0.0,
            "last_update_label": "Fresh setup",
            "last_update_note": self._setup_note(signal),
            "notes": notes or "",
            "pinned": False,
            "ignored": False,
            "archived": False,
        }

    def _merge_update_label(self, existing: dict[str, Any], signal: dict[str, Any], tracking_label: str) -> tuple[str, str]:
        if tracking_label == "Confirmed Setup" and existing.get("tracking_label") != "Confirmed Setup":
            return "Setup improved", "Confirmation stack strengthened and the setup moved into confirmed territory."
        if signal.get("relative_volume", 1.0) < max(1.0, (existing.get("relative_volume") or 1.0) * 0.7):
            return "Volume faded", "Participation cooled off versus the prior scan."
        if signal.get("confidence", 0) >= (existing.get("confidence") or 0) + 5:
            return "Setup improved", "Confidence improved versus the prior scan."
        if signal.get("confidence", 0) <= (existing.get("confidence") or 0) - 5:
            return "Setup weakened", "Confidence slipped versus the prior scan."
        return "Still active", "Scanner no longer needs a symbol to stay visible once the setup is being tracked."

    def _collect_candidates(self, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        candidates: list[tuple[str, dict[str, Any]]] = []
        seen: set[tuple[str, str]] = set()
        for bucket in TRACKABLE_BUCKETS:
            for item in payload.get(bucket, []):
                key = (item["symbol"], item["direction"])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((bucket, item))
        return candidates

    def _collect_auto_calls(self, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        candidates: list[tuple[str, dict[str, Any]]] = []
        seen: set[tuple[str, str]] = set()
        for bucket in AUTO_CALL_BUCKETS:
            for item in payload.get(bucket, []):
                if item.get("direction") == "neutral":
                    continue
                if not item.get("allow_buy_call", True) or item.get("attention_only", False):
                    continue
                key = (item["symbol"], item["direction"])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((bucket, item))
        candidates.sort(
            key=lambda pair: (
                pair[1].get("move_quality", 0),
                pair[1].get("confidence", 0),
                pair[1].get("relative_volume", 0),
            ),
            reverse=True,
        )
        selected: list[tuple[str, dict[str, Any]]] = []
        selected_keys: set[tuple[str, str]] = set()
        for direction in ("bullish", "bearish"):
            first = next((item for item in candidates if item[1].get("direction") == direction), None)
            if first:
                key = (first[1]["symbol"], first[1]["direction"])
                selected.append(first)
                selected_keys.add(key)
        for item in candidates:
            key = (item[1]["symbol"], item[1]["direction"])
            if key in selected_keys:
                continue
            selected.append(item)
            selected_keys.add(key)
            if len(selected) >= 10:
                break
        return selected[:10]

    def _has_daily_scanner_call(self, symbol: str, direction: str, day_prefix: str) -> bool:
        rows = self.store.list_setups(
            """
            SELECT *
            FROM tracked_setups
            WHERE symbol = ?
              AND direction = ?
              AND source_mode = 'scanner_suggested'
              AND suggested_at LIKE ?
            LIMIT 1
            """,
            (symbol.upper(), direction, f"{day_prefix}%"),
        )
        return bool(rows)

    def sync_scan_payload(
        self,
        payload: dict[str, Any],
        symbol_meta: dict[str, dict[str, Any]] | None = None,
        company_profiles: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        promoted = 0
        updated = 0
        meta = symbol_meta or {}
        profiles = company_profiles or {}

        for bucket, signal in self._collect_candidates(payload):
            tracking_label = self._tracking_label(signal)
            status = self._status_for_label(tracking_label)
            if not status:
                continue

            symbol = signal["symbol"]
            company_name = profiles.get(symbol, {}).get("company_name") or meta.get(symbol, {}).get("short_name") or symbol
            sector = profiles.get(symbol, {}).get("sector")
            existing = self.store.get_open_setup(symbol, signal["direction"])
            if existing:
                label, note = self._merge_update_label(existing, signal, tracking_label)
                refreshed = self.store.update_setup(
                    existing["id"],
                    {
                        "company_name": company_name,
                        "sector": sector or existing.get("sector"),
                        "setup_label": signal["setup_label"],
                        "tracking_label": tracking_label,
                        "scanner_bucket": bucket,
                        "last_seen_at": self._now().isoformat(),
                        "current_price": signal.get("current_price"),
                        "target_price": signal.get("target_price"),
                        "target_1": signal.get("target_1") or signal.get("target_price"),
                        "target_2": signal.get("target_2") or signal.get("extended_target_price"),
                        "extended_target_price": signal.get("extended_target_price"),
                        "stop_loss": signal.get("stop_loss"),
                        "trailing_stop": signal.get("trailing_stop") or signal.get("stop_loss") or signal.get("invalidation"),
                        "invalidation": signal.get("invalidation"),
                        "timeframe_label": signal.get("timeframe_label"),
                        "timeframe_days": signal.get("timeframe_days"),
                        "confidence": signal.get("confidence"),
                        "model_confidence": signal.get("model_confidence"),
                        "evidence_confidence": signal.get("evidence_confidence"),
                        "evidence_status": signal.get("historical_evidence_status"),
                        "expected_move_pct": signal.get("expected_move_pct"),
                        "risk_level": signal.get("risk_level"),
                        "risk_reward": signal.get("risk_reward"),
                        "move_quality": signal.get("move_quality"),
                        "relative_volume": signal.get("relative_volume"),
                        "intraday_volume_ratio": signal.get("intraday_volume_ratio"),
                        "change_pct": signal.get("change_pct"),
                        "reason_summary": signal.get("signal_summary"),
                        "reasons": signal.get("reasons", []),
                        "risk_factors": signal.get("risk_factors", []),
                        "tags": signal.get("tags", []),
                        "status": status,
                        "hold_or_exit": signal.get("hold_or_exit") or existing.get("hold_or_exit"),
                        "reason_for_exit_decision": signal.get("reason_for_exit_decision") or existing.get("reason_for_exit_decision"),
                        "last_update_label": label,
                        "last_update_note": note,
                    },
                )
                if refreshed and label != existing.get("last_update_label"):
                    self.store.record_update(refreshed["id"], self._now().isoformat(), label, note, {"bucket": bucket})
                updated += 1
                continue

            record = self._base_record(signal, bucket, company_name, sector, "scanner")
            created = self.store.insert_setup(record)
            self.store.record_update(created["id"], record["detected_at"], "Fresh setup", record["last_update_note"], {"bucket": bucket})
            promoted += 1

        auto_created = 0
        today = self._now().date().isoformat()
        for bucket, signal in self._collect_auto_calls(payload):
            symbol = signal["symbol"]
            if self._has_daily_scanner_call(symbol, signal["direction"], today):
                continue
            company_name = profiles.get(symbol, {}).get("company_name") or meta.get(symbol, {}).get("short_name") or symbol
            sector = profiles.get(symbol, {}).get("sector")
            record = self._base_record(signal, bucket, company_name, sector, "scanner_suggested")
            record["tracking_label"] = "Scanner Suggested Call"
            record["status"] = "active"
            record["reason_summary"] = signal.get("signal_summary") or self._setup_note(signal)
            created = self.store.insert_setup(record)
            self.store.record_update(
                created["id"],
                record["detected_at"],
                "Scanner Suggested Call",
                "Automatically selected as one of today's top 10 scanner calls.",
                {"bucket": bucket, "ranked_daily_top_10": True},
            )
            auto_created += 1

        return {"promoted": promoted, "updated": updated, "auto_created": auto_created}

    def create_manual_watch(
        self,
        detail: dict[str, Any],
        notes: str | None = None,
        pinned: bool = False,
        timeframe_label: str | None = None,
    ) -> dict[str, Any]:
        signal = dict(detail["prediction"])
        if timeframe_label:
            signal["timeframe_label"] = timeframe_label
        existing = self.store.get_open_setup(detail["symbol"], signal["direction"])
        if existing:
            return self.store.update_setup(
                existing["id"],
                {
                    "source_mode": "manual",
                    "notes": notes if notes is not None else existing.get("notes", ""),
                    "pinned": pinned or existing.get("pinned", False),
                    "company_name": detail.get("company_context", {}).get("company_name") or existing.get("company_name"),
                    "sector": detail.get("company_context", {}).get("sector") or existing.get("sector"),
                },
            ) or existing

        record = self._base_record(
            signal,
            "manual_watchlist",
            detail.get("company_context", {}).get("company_name") or detail["symbol"],
            detail.get("company_context", {}).get("sector"),
            "manual",
            notes=notes,
        )
        record["pinned"] = pinned
        created = self.store.insert_setup(record)
        self.store.record_update(
            created["id"],
            record["detected_at"],
            "Fresh setup",
            "Manual watchlist entry created from the current stock detail view.",
            {"source": "manual"},
        )
        return created

    def _evaluate_row(self, setup: dict[str, Any], frame: pd.DataFrame) -> tuple[str, str, float, float, float]:
        if frame.empty:
            return setup["status"], "Still active", setup.get("result_pct") or 0.0, 0.0, 0.0

        start = self._parse_dt(setup["detected_at"]).replace(tzinfo=None)
        start_date = start.date()
        window = frame.loc[pd.Series(frame.index.date, index=frame.index) >= start_date].copy()
        if window.empty:
            latest_close = float(frame["Close"].iloc[-1])
            result_pct = self._directional_return(setup["direction"], float(setup.get("entry_price") or 0), latest_close)
            return setup["status"], "Still active", result_pct, setup.get("max_favorable_move") or 0.0, setup.get("max_adverse_move") or 0.0
        entry = float(setup.get("entry_price") or 0)
        direction = setup["direction"]
        target = float(setup.get("target_price") or entry)
        stop = float(setup.get("stop_loss") or setup.get("invalidation") or entry)
        bars = window.copy()

        if direction == "bullish":
            favorable = (((bars["High"] / entry) - 1) * 100).max()
            adverse = (((bars["Low"] / entry) - 1) * 100).min()
        else:
            favorable = (((entry / bars["Low"]) - 1) * 100).max()
            adverse = -(((bars["High"] / entry) - 1) * 100).max()

        status = setup["status"]
        label = "Still active"
        result_pct = self._directional_return(direction, entry, float(frame["Close"].iloc[-1]))

        for _, bar in bars.iterrows():
            low = float(bar["Low"])
            high = float(bar["High"])
            if direction == "bullish":
                if stop and low <= stop:
                    return "failed", "Invalidated", ((stop / entry) - 1) * 100, favorable, adverse
                if target and high >= target:
                    return "passed", "Target hit", ((target / entry) - 1) * 100, favorable, adverse
            else:
                if stop and high >= stop:
                    return "failed", "Invalidated", -(((stop / entry) - 1) * 100), favorable, adverse
                if target and low <= target:
                    return "passed", "Target hit", ((entry / target) - 1) * 100, favorable, adverse

        expires_at = self._parse_dt(setup.get("expires_at"))
        if self._now() >= expires_at:
            status = "expired"
            label = "No follow-through yet"
        elif status == "watch_only" and favorable > max(0.5, abs(setup.get("expected_move_pct") or 0) * 0.4):
            label = "Setup improved"

        return status, label, result_pct, favorable, adverse

    def evaluate_open_setups(self) -> dict[str, Any]:
        open_setups = self.store.list_setups(
            """
            SELECT *
            FROM tracked_setups
            WHERE archived = 0
              AND ignored = 0
              AND status IN ('active', 'watch_only')
            ORDER BY detected_at DESC
            """
        )
        if not open_setups:
            return {"evaluated": 0}

        max_days = max(int(item.get("timeframe_days") or 3) for item in open_setups) + 5
        frames = self.data.fetch_batch_history(
            [item["symbol"] for item in open_setups],
            period=self._period_for_days(max_days),
        )

        evaluated = 0
        for setup in open_setups:
            frame = frames.get(setup["symbol"], pd.DataFrame())
            status, label, result_pct, favorable, adverse = self._evaluate_row(setup, frame)
            current_price = float(frame["Close"].iloc[-1]) if not frame.empty else setup.get("current_price")

            # Dynamic trailing stop via Chandelier Exit
            if not frame.empty and ENABLE_DYNAMIC_TRAIL:
                dynamic_trail = chandelier_exit(frame, setup["direction"])
                if dynamic_trail:
                    existing_trail = float(setup.get("trailing_stop") or 0)
                    if setup["direction"] == "bullish" and dynamic_trail > existing_trail:
                        self.store.update_setup(setup["id"], {"trailing_stop": dynamic_trail})
                    elif setup["direction"] == "bearish" and (existing_trail == 0 or dynamic_trail < existing_trail):
                        self.store.update_setup(setup["id"], {"trailing_stop": dynamic_trail})

            is_expired = status == "expired"
            lifecycle = self._build_lifecycle(setup, float(current_price or 0), expired=is_expired)
            hold_or_exit = lifecycle["hold_or_exit"]
            scanner_call_status = lifecycle["scanner_call_status"]
            lifecycle_note = lifecycle["reason_for_exit_decision"]
            entry = float(setup.get("entry_price") or 0)
            target_1 = float(setup.get("target_1") or setup.get("target_price") or 0)
            current_pnl = lifecycle["current_pnl_pct"]
            target_progress = lifecycle["target_progress_pct"]
            note = {
                "Target hit": "Price reached the primary target before invalidation within the tracked timeframe.",
                "Invalidated": "Price broke the invalidation level before the target could complete.",
                "No follow-through yet": "The timeframe expired without enough directional progress.",
                "Setup improved": "Price is moving in the expected direction, but the idea is still open.",
                "Still active": "Setup remains active while price continues respecting the invalidation level.",
            }.get(label, "Tracked idea evaluated against the latest available price history.")
            now_iso = self._now().isoformat()
            timestamp_updates = self._timestamp_updates(setup, lifecycle, now_iso)
            updated = self.store.update_setup(
                setup["id"],
                {
                    "status": status,
                    "current_price": current_price,
                    "result_pct": round(result_pct, 2),
                    "current_pnl_pct": round(current_pnl, 2),
                    "target_progress_pct": round(target_progress, 2),
                    "scanner_call_status": scanner_call_status if setup.get("source_mode") == "scanner_suggested" else setup.get("scanner_call_status"),
                    "hold_or_exit": hold_or_exit,
                    "reason_for_exit_decision": lifecycle_note,
                    "max_favorable_move": round(float(favorable or 0.0), 2),
                    "max_adverse_move": round(float(adverse or 0.0), 2),
                    "last_evaluated_at": now_iso,
                    "last_update_label": label,
                    "last_update_note": note,
                    **timestamp_updates,
                },
            )
            if updated and (label != setup.get("last_update_label") or status != setup.get("status")):
                self.store.record_update(updated["id"], self._now().isoformat(), label, note, {"status": status})
            evaluated += 1

        return {"evaluated": evaluated}

    def normalize_lifecycle_rows(self) -> int:
        rows = self.store.list_setups(
            """
            SELECT *
            FROM tracked_setups
            WHERE archived = 0
              AND source_mode = 'scanner_suggested'
              AND status IN ('passed', 'failed', 'expired')
            """
        )
        updated_count = 0
        for setup in rows:
            current_price = float(setup.get("current_price") or setup.get("entry_price") or 0)
            was_wrongly_expired = setup.get("status") == "expired" and self._now() < self._parse_dt(setup.get("expires_at"))
            lifecycle = self._build_lifecycle(setup, current_price, expired=setup.get("status") == "expired" and not was_wrongly_expired)
            next_status = "active" if was_wrongly_expired else setup.get("status")
            if next_status == "passed" and lifecycle["scanner_call_status"] == "ACTIVE":
                lifecycle["scanner_call_status"] = "TARGET_1_HIT"
                lifecycle["hold_or_exit"] = "PARTIAL_BOOK"
                lifecycle["reason_for_exit_decision"] = "Target hit, book partial profit."
                lifecycle["exit_reason"] = "target_1_hit"
            if next_status == "failed":
                lifecycle["scanner_call_status"] = "STOP_LOSS_HIT"
                lifecycle["hold_or_exit"] = "EXIT"
                lifecycle["reason_for_exit_decision"] = "Stop-loss hit."
                lifecycle["exit_reason"] = "stop_loss_hit"
            now_iso = self._now().isoformat()
            values = {
                "status": next_status,
                "scanner_call_status": lifecycle["scanner_call_status"],
                "hold_or_exit": lifecycle["hold_or_exit"],
                "reason_for_exit_decision": lifecycle["reason_for_exit_decision"],
                "current_pnl_pct": lifecycle["current_pnl_pct"],
                "target_progress_pct": lifecycle["target_progress_pct"],
                **self._timestamp_updates(setup, lifecycle, now_iso),
            }
            if was_wrongly_expired:
                values["expired_at"] = None
                values["exit_reason"] = lifecycle.get("exit_reason")
                values["last_update_label"] = "Still active"
                values["last_update_note"] = "Call restored because timeframe has not expired yet."
            self.store.update_setup(setup["id"], values)
            updated_count += 1
        return updated_count

    def get_dashboard(self) -> dict[str, Any]:
        if time.time() - self._last_dashboard_eval_at > max(60, self.settings.scan_cache_ttl_sec):
            try:
                self._last_dashboard_eval_at = time.time()
                self.evaluate_open_setups()
                self.normalize_lifecycle_rows()
            except Exception:
                logger.exception("tracker dashboard evaluation failed")
        review_since = (self._now() - timedelta(days=1)).isoformat()
        watchlists = {
            "fresh_setups": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0
                  AND detected_at >= ?
                ORDER BY pinned DESC, confidence DESC, detected_at DESC
                LIMIT 8
                """,
                (review_since,),
            ),
            "active_bullish": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0 AND ignored = 0 AND status IN ('active', 'watch_only') AND direction = 'bullish'
                ORDER BY pinned DESC, confidence DESC, detected_at DESC
                LIMIT 8
                """
            ),
            "active_bearish": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0 AND ignored = 0 AND status IN ('active', 'watch_only') AND direction = 'bearish'
                ORDER BY pinned DESC, confidence DESC, detected_at DESC
                LIMIT 8
                """
            ),
            "passed_calls": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0 AND status = 'passed'
                ORDER BY detected_at DESC
                LIMIT 8
                """
            ),
            "failed_calls": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0 AND status = 'failed'
                ORDER BY detected_at DESC
                LIMIT 8
                """
            ),
            "expired_calls": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0 AND status = 'expired'
                ORDER BY detected_at DESC
                LIMIT 8
                """
            ),
            "manual_watchlist": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0 AND source_mode = 'manual' AND status IN ('active', 'watch_only')
                ORDER BY pinned DESC, detected_at DESC
                LIMIT 8
                """
            ),
            "todays_top_10_scanner_calls": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0
                  AND source_mode = 'scanner_suggested'
                  AND suggested_at >= ?
                ORDER BY confidence DESC, move_quality DESC, suggested_at DESC
                LIMIT 10
                """,
                (review_since,),
            ),
            "active_scanner_calls": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0
                  AND ignored = 0
                  AND source_mode = 'scanner_suggested'
                  AND scanner_call_status IN ('ACTIVE', 'TARGET_1_HIT', 'PARTIAL_BOOK', 'EXIT_SUGGESTED')
                  AND status != 'expired'
                ORDER BY suggested_at DESC
                LIMIT 12
                """
            ),
            "past_target_hit": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0
                  AND source_mode = 'scanner_suggested'
                  AND scanner_call_status IN ('TARGET_1_HIT', 'TARGET_2_HIT')
                ORDER BY suggested_at DESC
                LIMIT 12
                """
            ),
            "past_stop_loss_failed": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0
                  AND source_mode = 'scanner_suggested'
                  AND scanner_call_status = 'STOP_LOSS_HIT'
                ORDER BY suggested_at DESC
                LIMIT 12
                """
            ),
            "expired_no_followthrough": self.store.list_setups(
                """
                SELECT *
                FROM tracked_setups
                WHERE archived = 0
                  AND source_mode = 'scanner_suggested'
                  AND scanner_call_status = 'EXPIRED'
                ORDER BY suggested_at DESC
                LIMIT 12
                """
            ),
        }
        all_rows = self.store.list_setups(
            "SELECT * FROM tracked_setups WHERE archived = 0 ORDER BY detected_at DESC"
        )
        passed = [item for item in all_rows if item["status"] == "passed"]
        failed = [item for item in all_rows if item["status"] == "failed"]
        expired = [item for item in all_rows if item["status"] == "expired"]
        active = [item for item in all_rows if item["status"] in {"active", "watch_only"}]
        resolved = [item for item in all_rows if item["status"] in {"passed", "failed", "expired"} and item.get("result_pct") is not None]
        win_base = len(passed) + len(failed)
        average_return = sum(item.get("result_pct") or 0 for item in resolved) / len(resolved) if resolved else 0.0
        updates = self.store.latest_updates(review_since, self.settings.tracked_review_limit)
        return {
            "generated_at": self._now().isoformat(),
            "summary": {
                "total_calls": len(all_rows),
                "active_calls": len(active),
                "passed_calls": len(passed),
                "failed_calls": len(failed),
                "expired_calls": len(expired),
                "target_hit_count": len(passed),
                "stop_loss_count": len(failed),
                "win_rate": round((len(passed) / win_base) * 100, 1) if win_base else 0.0,
                "average_return": round(average_return, 2),
                "best_call": max(resolved, key=lambda item: item.get("result_pct") or -9999, default=None),
                "worst_call": min(resolved, key=lambda item: item.get("result_pct") or 9999, default=None),
            },
            "watchlists": watchlists,
            "todays_review": {
                "updates": updates,
                "what_worked": [item for item in updates if item["label"] == "Target hit"][:4],
                "what_failed": [item for item in updates if item["label"] == "Invalidated"][:4],
                "what_changed": [item for item in updates if item["label"] in {"Setup improved", "Setup weakened", "Volume faded"}][:6],
            },
        }

    def get_symbol_history(self, symbol: str) -> dict[str, Any]:
        self.evaluate_open_setups()
        self.normalize_lifecycle_rows()
        rows = self.store.list_setups(
            """
            SELECT *
            FROM tracked_setups
            WHERE symbol = ?
            ORDER BY detected_at DESC
            LIMIT 8
            """,
            (symbol.upper(),),
        )
        return {
            "symbol": symbol.upper(),
            "setups": [
                {**item, "updates": self.store.get_updates(item["id"], limit=5)}
                for item in rows
            ],
            "generated_at": self._now().isoformat(),
        }

    def update_setup(self, setup_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        setup = self.store.get_setup(setup_id)
        if not setup:
            return None
        updated = self.store.update_setup(setup_id, values)
        if updated:
            note = "Tracked setup preferences were updated."
            self.store.record_update(setup_id, self._now().isoformat(), "Still active", note, {"manual_edit": True})
        return updated

    def archive_setup(self, setup_id: str) -> dict[str, Any] | None:
        updated = self.store.update_setup(setup_id, {"archived": True})
        if updated:
            self.store.record_update(setup_id, self._now().isoformat(), "Archived", "Setup archived by the user.", {"archived": True})
        return updated

    def ignore_setup(self, setup_id: str) -> dict[str, Any] | None:
        updated = self.store.update_setup(setup_id, {"ignored": True, "archived": True})
        if updated:
            self.store.record_update(setup_id, self._now().isoformat(), "Ignored", "Setup ignored by the user.", {"ignored": True})
        return updated
