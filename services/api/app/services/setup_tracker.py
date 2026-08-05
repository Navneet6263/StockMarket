from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.settings import Settings
from app.services.lifecycle import build_lifecycle_advice, directional_return, target_progress
from app.services.setup_store import SetupStore

if TYPE_CHECKING:
    from app.services.data_provider import MarketDataService


TRACKABLE_BUCKETS = ("top_opportunities", "breakout_candidates", "unusual_volume")
AUTO_CALL_BUCKETS = ("top_opportunities", "breakout_candidates", "unusual_volume", "bearish_risks")
logger = logging.getLogger(__name__)

# ── ATR Chandelier Exit config ────────────────────────────────────────────────
CHANDELIER_MULTIPLIER = float(os.getenv("CHANDELIER_ATR_MULTIPLIER", "3.0"))
CHANDELIER_PERIOD     = int(os.getenv("CHANDELIER_ATR_PERIOD", "22"))
ENABLE_DYNAMIC_TRAIL  = os.getenv("ENABLE_DYNAMIC_TRAILING_STOP", "true").lower() == "true"
REARM_COOLDOWN_SESSIONS = max(1, int(os.getenv("SETUP_REARM_COOLDOWN_SESSIONS", "3")))


def chandelier_exit_series(frame: pd.DataFrame, direction: str = "bullish") -> pd.Series:
    """Return a no-look-ahead Chandelier value for each historical bar."""
    if not ENABLE_DYNAMIC_TRAIL or frame is None or frame.empty or len(frame) < CHANDELIER_PERIOD:
        return pd.Series(dtype=float)
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    close = frame["Close"].astype(float)
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(CHANDELIER_PERIOD).mean()
    if direction == "bullish":
        return (high.rolling(CHANDELIER_PERIOD).max() - CHANDELIER_MULTIPLIER * atr).round(2)
    return (low.rolling(CHANDELIER_PERIOD).min() + CHANDELIER_MULTIPLIER * atr).round(2)


def chandelier_exit(frame: pd.DataFrame, direction: str = "bullish") -> float | None:
    """
    ATR-based Chandelier Exit trailing stop.
    Long:  highest_high(period) - multiplier * ATR(period)
    Short: lowest_low(period)  + multiplier * ATR(period)
    Returns the trailing stop price or None if insufficient data.
    """
    try:
        values = chandelier_exit_series(frame, direction)
        if values.empty or pd.isna(values.iloc[-1]):
            return None
        return round(float(values.iloc[-1]), 2)
    except Exception as exc:
        logger.debug("chandelier_exit failed: %s", exc)
        return None


class SetupTrackerService:
    def __init__(self, settings: Settings, data: "MarketDataService"):
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

    def _memory_sessions(self) -> int:
        # Keep enough history for a second base/retest, while bounding stale
        # ideas.  The existing SETUP_MEMORY_DAYS setting is interpreted as
        # trading sessions for lifecycle memory.
        configured = int(getattr(self.settings, "setup_memory_days", 60) or 60)
        return max(30, min(60, configured))

    def _add_business_days(self, value: datetime, sessions: int) -> datetime:
        cursor = value
        remaining = max(0, int(sessions))
        while remaining:
            cursor += timedelta(days=1)
            if cursor.weekday() < 5:
                remaining -= 1
        return cursor

    def _memory_expiry(self, value: datetime | None = None) -> str:
        return self._add_business_days(value or self._now(), self._memory_sessions()).isoformat()

    def _business_sessions_since(self, value: str | None) -> int:
        if not value:
            return 0
        cursor = self._parse_dt(value).date()
        end = self._now().date()
        completed = 0
        while cursor < end:
            cursor += timedelta(days=1)
            if cursor.weekday() < 5:
                completed += 1
        return completed

    def _daily_bar_is_complete(self, index: Any) -> bool:
        if not hasattr(index, "date"):
            return True
        market_now = self._now().astimezone(ZoneInfo("Asia/Kolkata"))
        bar_date = index.date()
        if bar_date < market_now.date():
            return True
        if bar_date > market_now.date():
            return False
        return (market_now.hour, market_now.minute) >= (15, 31)

    def _entry_confirmed_at(self, setup: dict[str, Any]) -> str | None:
        """Return persisted fill/arming evidence, never a watch-only timestamp.

        ``suggested_at`` is the existing database column used as the
        backwards-compatible armed-at timestamp.  The aliases let callers
        move to a dedicated field later without weakening this boundary.
        """
        if setup.get("status") == "watch_only" or setup.get("lifecycle_state") == "WATCH":
            return None
        for key in ("entry_confirmed_at", "armed_at", "suggested_at"):
            value = setup.get(key)
            if value:
                return str(value)
        return None

    def _legacy_confirmation_at(self, setup: dict[str, Any]) -> str | None:
        """One-time compatibility path for old active, confirmed rows."""
        if setup.get("status") != "active":
            return None
        if setup.get("tracking_label") not in {"Confirmed Setup", "Scanner Suggested Call"}:
            return None
        if setup.get("lifecycle_state") == "WATCH" or float(setup.get("entry_price") or 0) <= 0:
            return None
        return str(setup.get("detected_at") or "") or None

    def _ensure_entry_confirmation(self, setup: dict[str, Any]) -> str | None:
        confirmed_at = self._entry_confirmed_at(setup)
        if confirmed_at:
            return confirmed_at
        legacy_at = self._legacy_confirmation_at(setup)
        if not legacy_at:
            return None
        # Persist the compatibility backfill before any trade evaluation so
        # subsequent evaluations always have explicit armed-at evidence.
        if setup.get("id"):
            self.store.update_setup(setup["id"], {"suggested_at": legacy_at})
        setup["suggested_at"] = legacy_at
        return legacy_at

    def _is_timestamp_resolved_frame(self, frame: pd.DataFrame) -> bool:
        """Distinguish intraday timestamps from exchange-session daily bars."""
        if frame is None or frame.empty or len(frame.index) < 2:
            return False
        try:
            index = pd.DatetimeIndex(pd.to_datetime(frame.index)).sort_values().unique()
            if len(index) < 2:
                return False
            index_ns = index.to_numpy(dtype="datetime64[ns]").astype("int64")
            positive_ns = sorted(
                int(value)
                for value in (index_ns[1:] - index_ns[:-1])
                if int(value) > 0
            )
            if not positive_ns:
                return False
            median_ns = positive_ns[len(positive_ns) // 2]
            return median_ns < 12 * 60 * 60 * 1_000_000_000
        except (TypeError, ValueError):
            return False

    def _post_confirmation_window(
        self,
        frame: pd.DataFrame,
        confirmed_at: str,
    ) -> pd.DataFrame:
        """Return only bars whose information became available after entry.

        A daily candle contains the whole session, so the detection-day row
        is excluded.  Timestamp-resolved frames may use later bars from the
        same date, but only when their timestamp is strictly after arming.
        """
        if frame is None or frame.empty:
            return pd.DataFrame()
        confirmation = self._parse_dt(confirmed_at)
        if not self._is_timestamp_resolved_frame(frame):
            session_date = confirmation.astimezone(ZoneInfo("Asia/Kolkata")).date()
            mask = pd.Series(frame.index.date, index=frame.index) > session_date
            return frame.loc[mask].copy()

        confirmation_utc = pd.Timestamp(confirmation.astimezone(timezone.utc)).tz_localize(None)
        normalized: list[pd.Timestamp] = []
        for value in pd.to_datetime(frame.index):
            timestamp = pd.Timestamp(value)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.tz_convert("UTC").tz_localize(None)
            normalized.append(timestamp)
        mask = pd.Series([value > confirmation_utc for value in normalized], index=frame.index)
        return frame.loc[mask].copy()

    def _directional_return(self, direction: str, entry: float, current: float) -> float:
        return directional_return(direction, entry, current)

    def _target_progress(self, direction: str, entry: float, current: float, target: float) -> float:
        return target_progress(direction, entry, current, target)

    def _lifecycle_advice(self, setup: dict[str, Any], current_price: float) -> tuple[str, str, str]:
        lifecycle = self._build_lifecycle(setup, current_price)
        return lifecycle["hold_or_exit"], lifecycle["scanner_call_status"], lifecycle["reason_for_exit_decision"]

    def _build_lifecycle(
        self,
        setup: dict[str, Any],
        current_price: float,
        *,
        expired: bool = False,
        target_1_hit_now: bool = False,
        target_2_hit_now: bool = False,
    ) -> dict[str, Any]:
        return build_lifecycle_advice(
            direction=setup["direction"],
            entry_price=float(setup.get("entry_price") or current_price or 0),
            current_price=float(current_price or 0),
            target_1=float(setup.get("target_1") or setup.get("target_price") or 0),
            target_2=float(setup.get("target_2") or setup.get("extended_target_price") or 0),
            stop_loss=float(setup.get("stop_loss") or setup.get("invalidation") or 0),
            trailing_stop=float(setup.get("trailing_stop") or setup.get("stop_loss") or setup.get("invalidation") or 0),
            expired=expired,
            lifecycle_state=setup.get("lifecycle_state"),
            target_1_hit_before=bool(setup.get("target_hit_at")),
            target_1_hit_now=target_1_hit_now,
            target_2_hit_before=bool(setup.get("target_2_hit_at")),
            target_2_hit_now=target_2_hit_now,
            trailing_active=bool(setup.get("target_hit_at")),
        )

    def _timestamp_updates(self, setup: dict[str, Any], lifecycle: dict[str, Any], now_iso: str) -> dict[str, Any]:
        updates: dict[str, Any] = {"last_checked_at": now_iso}
        status = lifecycle.get("scanner_call_status")
        reason = lifecycle.get("exit_reason")
        if status in {"TARGET_1_HIT", "TARGET_2_HIT"} and not setup.get("target_hit_at"):
            updates["target_hit_at"] = now_iso
            updates["memory_expires_at"] = self._memory_expiry(self._parse_dt(now_iso))
            updates["continuation_pivot"] = float(setup.get("target_1") or setup.get("target_price") or 0)
        if status == "TARGET_2_HIT" and not setup.get("target_2_hit_at"):
            updates["target_2_hit_at"] = now_iso
        if lifecycle.get("hold_or_exit") == "PARTIAL_BOOK" and not setup.get("partial_book_at"):
            updates["partial_book_at"] = now_iso
        if lifecycle.get("lifecycle_state") in {"TRAILING_HOLD", "RETEST", "REARMED", "CONTINUATION"} and not setup.get("trailing_activated_at"):
            updates["trailing_activated_at"] = now_iso
        if lifecycle.get("lifecycle_state") == "RETEST":
            updates["last_retest_at"] = now_iso
        if lifecycle.get("lifecycle_state") == "REARMED" and not setup.get("continuation_rearmed_at"):
            updates["continuation_rearmed_at"] = now_iso
        if status == "EXIT_SUGGESTED" and not setup.get("exit_suggested_at"):
            updates["exit_suggested_at"] = now_iso
        if status == "STOP_LOSS_HIT" and not setup.get("stop_loss_hit_at"):
            updates["stop_loss_hit_at"] = now_iso
        if status == "EXPIRED" and not setup.get("expired_at"):
            updates["expired_at"] = now_iso
        if lifecycle.get("hold_or_exit") == "EXIT" and not setup.get("closed_at"):
            updates["closed_at"] = now_iso
        if reason and lifecycle.get("exit_signal"):
            updates["exit_reason"] = reason
        elif not lifecycle.get("exit_signal"):
            updates["exit_reason"] = None
            updates["closed_at"] = None
        return updates

    def _ratcheted_trailing_stop(self, setup: dict[str, Any], candidate: Any) -> float | None:
        existing = float(setup.get("trailing_stop") or setup.get("stop_loss") or setup.get("invalidation") or 0)
        try:
            proposed = float(candidate or 0)
        except (TypeError, ValueError):
            proposed = 0.0
        if proposed <= 0:
            return existing or None
        if existing <= 0:
            return proposed
        if setup.get("direction") == "bullish":
            return max(existing, proposed)
        return min(existing, proposed)

    def _tracking_label(self, signal: dict[str, Any]) -> str:
        if signal["direction"] == "neutral":
            return "Avoid / High Risk"
        if signal.get("tradePlanGateBlocked") or signal.get("entry_plan_blocked"):
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
        setup_status = self._status_for_label(tracking_label) or "watch_only"
        entry_confirmed = setup_status == "active"
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
            # Existing storage uses suggested_at as the durable armed-at
            # timestamp. Discovery/watch rows intentionally leave it empty.
            "suggested_at": now.isoformat() if entry_confirmed else None,
            "last_seen_at": now.isoformat(),
            "last_evaluated_at": None,
            "expires_at": (now + timedelta(days=max(1, int(timeframe_days * 1.5)))).isoformat(),
            "timeframe_label": signal.get("timeframe_label"),
            "timeframe_days": timeframe_days,
            "entry_price": signal.get("current_price") if entry_confirmed else None,
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
            "status": setup_status,
            "lifecycle_state": "ARMED" if setup_status == "active" else "WATCH",
            "scanner_call_status": "ACTIVE" if source_mode == "scanner_suggested" and entry_confirmed else None,
            "hold_or_exit": (signal.get("hold_or_exit") or "WAIT") if entry_confirmed else "WAIT",
            "reason_for_exit_decision": signal.get("reason_for_exit_decision") or self._setup_note(signal),
            "target_hit_at": None,
            "partial_book_at": None,
            "exit_suggested_at": None,
            "stop_loss_hit_at": None,
            "expired_at": None,
            "last_checked_at": None,
            "closed_at": None,
            "exit_reason": None,
            "memory_sessions": self._memory_sessions(),
            "memory_expires_at": self._memory_expiry(now),
            "parent_setup_id": None,
            "setup_generation": 1,
            "rearmed_at": None,
            "rearmed_setup_id": None,
            "continuation_rearmed_at": None,
            "target_2_hit_at": None,
            "trailing_activated_at": None,
            "last_retest_at": None,
            "continuation_pivot": None,
            "post_target_high": None,
            "post_target_low": None,
            "rearm_reason": None,
            "rearm_evidence": {},
            "rearm_blocked_at": None,
            "rearm_block_reason": None,
            "last_bar_date": None,
            "last_bar_low": None,
            "last_bar_high": None,
            "last_bar_close": None,
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
                if self._tracking_label(item) != "Confirmed Setup":
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
              AND archived = 0
              AND ignored = 0
              AND status IN ('active', 'watch_only')
              AND suggested_at LIKE ?
            LIMIT 1
            """,
            (symbol.upper(), direction, f"{day_prefix}%"),
        )
        return bool(rows)

    def _rearm_decision(self, parent: dict[str, Any], signal: dict[str, Any]) -> dict[str, Any]:
        def number(value: Any, fallback: float = 0.0) -> float:
            try:
                return float(value if value is not None else fallback)
            except (TypeError, ValueError):
                return fallback

        direction = str(signal.get("direction") or parent.get("direction") or "").lower()
        tags = {str(item).lower() for item in signal.get("tags", [])}
        setup_stage = str(signal.get("setup_stage") or "").upper()
        signal_stage = str(signal.get("signal_stage") or "").upper()
        action = str(signal.get("action") or signal.get("recommended_action") or "").upper()
        continuation_action = str(signal.get("continuation_action") or "").upper()
        chart = signal.get("chart_features") or {}
        relative_volume = number(signal.get("relative_volume"), 1.0)
        intraday_volume = number(signal.get("intraday_volume_ratio"), 1.0)
        risk_reward = number(signal.get("risk_reward"))
        continuation_rr = number(signal.get("continuation_risk_reward"), risk_reward)
        confidence = number(signal.get("confidence"))
        move_quality = number(signal.get("move_quality"))
        change_pct = number(signal.get("change_pct"))
        resolved_at = (
            parent.get("closed_at")
            or parent.get("stop_loss_hit_at")
            or parent.get("expired_at")
            or parent.get("last_evaluated_at")
            or parent.get("detected_at")
        )
        cooldown_sessions = self._business_sessions_since(resolved_at)
        tracking_label = self._tracking_label(signal)
        confirmed_call = (
            self._status_for_label(tracking_label) == "active"
            and bool(signal.get("allow_buy_call", True))
            and not bool(signal.get("attention_only", False))
        )

        strong_continuation = bool(
            signal.get("is_momentum_continuation")
            and continuation_action == "REENTRY_BUY"
            and continuation_rr >= 1.5
            and (relative_volume >= 1.2 or intraday_volume >= 1.3)
            and action not in {"WAIT", "ALERT", "WAIT_FOR_REENTRY", "WAIT_FOR_RETEST"}
        )

        bullish_reversal = number(signal.get("bullish_reversal_score") or signal.get("reversal_risk_score"))
        bearish_reversal = number(signal.get("bearish_reversal_score") or signal.get("reversal_risk_score"))
        reversal_score = bullish_reversal if direction == "bullish" else bearish_reversal
        reversal_bias = str(signal.get("reversal_bias") or "").lower()
        directional_response = change_pct > 0 if direction == "bullish" else change_pct < 0
        confirmed_reversal = bool(
            signal.get("reversal_watch")
            and reversal_bias == direction
            and reversal_score >= 60
            and directional_response
            and (relative_volume >= 1.3 or intraday_volume >= 1.4)
            and signal_stage in {"BULLISH_REVERSAL_CANDIDATE", "BEARISH_REVERSAL_CANDIDATE", "SUPPORT_BOUNCE"}
        )

        base_tags = tags.intersection({"base_building", "pre_breakout", "accumulation_watch", "demand_absorption"})
        base_quality = number(signal.get("baseQualityScore") or signal.get("base_quality_score"))
        tight_base = number(chart.get("tight_consolidation_pct"), 99.0) <= 7.0
        base_structure = bool(chart.get("higher_lows") or chart.get("volume_dryup") or tight_base or base_quality >= 55)
        fresh_base = bool(
            base_tags
            and base_structure
            and (
                signal.get("is_pre_breakout")
                or setup_stage in {"PRE_BREAKOUT", "RETEST_ENTRY", "LIVE_PATTERN_READY"}
                or signal_stage in {"ALERT_ABOVE_LEVEL", "PATTERN_FORMING", "RETEST_ENTRY"}
            )
        )

        confirmed_reclaim = bool(
            (relative_volume >= 1.25 or intraday_volume >= 1.35)
            and (
                chart.get("breakout_confirmed")
                or signal_stage in {"CONFIRMED_BREAKOUT", "LIVE_PATTERN_READY", "RETEST_ENTRY", "RE_ENTRY_SETUP"}
                or (setup_stage == "RETEST_ENTRY" and action in {"BUY", "REENTRY_BUY"})
            )
        )
        late_or_chasing = bool(
            setup_stage in {"CHASE_RISK", "AVOID_LATE_ENTRY", "PROFIT_BOOKING_RISK"}
            or signal_stage in {"AVOID_CHASE", "AVOID_LATE_ENTRY", "WATCH"}
            or tags.intersection({"chase_risk", "profit_booking_risk", "late_entry"})
        )

        evidence: list[str] = []
        if strong_continuation:
            evidence.append("strong_confirmed_continuation")
        if confirmed_reversal:
            evidence.append("confirmed_directional_reversal")
        if fresh_base:
            evidence.append("fresh_base_structure")
        if confirmed_reclaim:
            evidence.append("confirmed_reclaim")

        details = {
            "cooldown_sessions": cooldown_sessions,
            "required_cooldown_sessions": REARM_COOLDOWN_SESSIONS,
            "tracking_label": tracking_label,
            "setup_stage": setup_stage,
            "signal_stage": signal_stage,
            "relative_volume": relative_volume,
            "intraday_volume_ratio": intraday_volume,
            "confidence": confidence,
            "move_quality": move_quality,
            "risk_reward": risk_reward,
            "reversal_score": reversal_score,
            "evidence": evidence,
        }
        if not self._entry_confirmed_at(parent):
            return {
                "allowed": False,
                "reason": "The previous row was never entry-confirmed, so it cannot create a re-armed trade generation.",
                **details,
            }
        if not confirmed_call:
            return {"allowed": False, "reason": "Fresh signal is still watch-only; wait for a confirmed recovery trigger.", **details}
        if late_or_chasing and not strong_continuation:
            return {"allowed": False, "reason": "Signal remains late/chasing near supply; require a fresh base, reversal, or reclaim.", **details}
        if strong_continuation:
            return {"allowed": True, "reason": "Strong continuation evidence bypassed the normal re-arm cooldown.", **details}
        if cooldown_sessions < REARM_COOLDOWN_SESSIONS:
            return {
                "allowed": False,
                "reason": f"Re-arm cooldown active ({cooldown_sessions}/{REARM_COOLDOWN_SESSIONS} completed sessions).",
                **details,
            }
        if not (confirmed_reversal or fresh_base or confirmed_reclaim):
            return {
                "allowed": False,
                "reason": "No meaningful new base, confirmed reversal, or reclaim is present yet.",
                **details,
            }
        return {"allowed": True, "reason": "Fresh recovery structure confirmed after cooldown.", **details}

    def _mark_record_rearmed(
        self,
        record: dict[str, Any],
        parent: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        now_iso = self._now().isoformat()
        record.update(
            {
                "parent_setup_id": parent["id"],
                "setup_generation": max(1, int(parent.get("setup_generation") or 1)) + 1,
                "lifecycle_state": "REARMED",
                "continuation_rearmed_at": now_iso,
                "rearm_reason": decision.get("reason"),
                "rearm_evidence": decision,
                "rearm_blocked_at": None,
                "rearm_block_reason": None,
                "last_update_label": "Setup re-armed",
                "last_update_note": "A fresh confirmed signal re-armed this symbol after the previous setup resolved.",
            }
        )
        return record

    def _insert_new_or_rearmed(
        self,
        record: dict[str, Any],
        *,
        signal: dict[str, Any] | None = None,
        include_manual_parent: bool = False,
    ) -> tuple[dict[str, Any], bool, bool]:
        now_iso = self._now().isoformat()
        open_setup = self.store.get_open_setup(record["symbol"], record["direction"])
        if open_setup:
            return open_setup, False, False
        parent = self.store.get_rearm_candidate(
            record["symbol"],
            record["direction"],
            now_iso,
            include_manual=include_manual_parent,
        )
        if not parent:
            created, inserted = self.store.insert_setup_if_no_open(record)
            return created, inserted, False

        # A legacy watch/outcome must not become the parent of a filled trade.
        # If today's signal is confirmed it starts a clean generation instead.
        if not self._ensure_entry_confirmation(parent):
            created, inserted = self.store.insert_setup_if_no_open(record)
            return created, inserted, False

        if record.get("source_mode") == "manual":
            decision = {
                "allowed": True,
                "reason": "Manual re-arm requested by the user.",
                "evidence": ["manual_override"],
            }
        else:
            decision = self._rearm_decision(parent, signal or record)
        if not decision["allowed"]:
            now_iso = self._now().isoformat()
            previous_reason = parent.get("rearm_block_reason")
            self.store.update_setup(
                parent["id"],
                {"rearm_blocked_at": now_iso, "rearm_block_reason": decision["reason"]},
            )
            if previous_reason != decision["reason"]:
                self.store.record_update(
                    parent["id"],
                    now_iso,
                    "Re-arm waiting",
                    decision["reason"],
                    decision,
                )
            return parent, False, False

        record = self._mark_record_rearmed(record, parent, decision)
        created, inserted = self.store.insert_rearmed_setup(record, parent["id"], now_iso)
        if inserted:
            self.store.record_update(
                parent["id"],
                now_iso,
                "Setup re-armed",
                "A fresh setup generation is now tracking the symbol; the prior outcome remains preserved.",
                {"rearmed_setup_id": created["id"]},
            )
        return created, inserted, inserted

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
                confirmed_at = self._ensure_entry_confirmation(existing)
                promotes_to_confirmed = status == "active" and not confirmed_at
                entry_confirmed = bool(confirmed_at or promotes_to_confirmed)
                confirmation_at = confirmed_at or (self._now().isoformat() if promotes_to_confirmed else None)
                label, note = self._merge_update_label(existing, signal, tracking_label)
                target_was_hit = bool(entry_confirmed and existing.get("target_hit_at"))
                # Once T1 is hit, only the sequential candle evaluator may
                # raise the persisted trail. A scanner snapshot contains the
                # current candle and would otherwise apply a future level to
                # that candle's earlier low/high.
                next_trail = (
                    float(existing.get("trailing_stop") or 0) or None
                    if target_was_hit
                    else self._ratcheted_trailing_stop(
                        existing,
                        signal.get("trailing_stop") or signal.get("stop_loss") or signal.get("invalidation"),
                    )
                )
                lifecycle_state = (
                    (existing.get("lifecycle_state") or "ARMED")
                    if confirmed_at
                    else ("ARMED" if promotes_to_confirmed else "WATCH")
                )
                scanner_call_status = existing.get("scanner_call_status") if confirmed_at else None
                if promotes_to_confirmed and existing.get("source_mode") == "scanner_suggested":
                    scanner_call_status = "ACTIVE"
                continuation_rearmed_at = existing.get("continuation_rearmed_at")
                pivot = float(existing.get("continuation_pivot") or existing.get("target_1") or existing.get("target_price") or 0)
                current_signal_price = float(signal.get("current_price") or 0)
                reclaimed = target_was_hit and lifecycle_state == "RETEST" and (
                    (signal["direction"] == "bullish" and current_signal_price >= pivot)
                    or (signal["direction"] == "bearish" and current_signal_price <= pivot)
                )
                if reclaimed:
                    lifecycle_state = "REARMED"
                    scanner_call_status = "REARMED" if existing.get("source_mode") == "scanner_suggested" else scanner_call_status
                    continuation_rearmed_at = self._now().isoformat()
                    label = "Setup re-armed"
                    note = "The post-target retest reclaimed its continuation pivot with a fresh scanner signal."
                if promotes_to_confirmed:
                    label = "Entry confirmed"
                    note = "The watch met the confirmed-setup gate and is armed from this scan onward."
                next_status = "active" if entry_confirmed else "watch_only"
                refresh_values: dict[str, Any] = {
                        "company_name": company_name,
                        "sector": sector or existing.get("sector"),
                        "setup_label": signal["setup_label"],
                        "tracking_label": tracking_label,
                        "scanner_bucket": bucket,
                        "suggested_at": confirmation_at,
                        "last_seen_at": self._now().isoformat(),
                        "entry_price": (
                            existing.get("entry_price")
                            if confirmed_at
                            else (signal.get("current_price") if promotes_to_confirmed else None)
                        ),
                        "current_price": signal.get("current_price"),
                        "target_price": existing.get("target_price") if target_was_hit else signal.get("target_price"),
                        "target_1": existing.get("target_1") if target_was_hit else (signal.get("target_1") or signal.get("target_price")),
                        "target_2": signal.get("target_2") or signal.get("extended_target_price"),
                        "extended_target_price": signal.get("extended_target_price"),
                        "stop_loss": existing.get("stop_loss") if target_was_hit else signal.get("stop_loss"),
                        "trailing_stop": next_trail,
                        "invalidation": existing.get("invalidation") if target_was_hit else signal.get("invalidation"),
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
                        "status": next_status,
                        "lifecycle_state": lifecycle_state,
                        "scanner_call_status": scanner_call_status,
                        "continuation_pivot": pivot or existing.get("continuation_pivot"),
                        "continuation_rearmed_at": continuation_rearmed_at,
                        "hold_or_exit": (
                            signal.get("hold_or_exit") or existing.get("hold_or_exit") or "WAIT"
                            if entry_confirmed
                            else "WAIT"
                        ),
                        "reason_for_exit_decision": signal.get("reason_for_exit_decision") or existing.get("reason_for_exit_decision"),
                        "last_update_label": label,
                        "last_update_note": note,
                    }
                if promotes_to_confirmed:
                    # Start trade accounting at the confirmation scan. Any
                    # legacy watch-only outcome data is not a real trade.
                    refresh_values.update(
                        {
                            "result_pct": 0.0,
                            "current_pnl_pct": 0.0,
                            "target_progress_pct": 0.0,
                            "max_favorable_move": 0.0,
                            "max_adverse_move": 0.0,
                            "target_hit_at": None,
                            "target_2_hit_at": None,
                            "partial_book_at": None,
                            "trailing_activated_at": None,
                            "exit_suggested_at": None,
                            "stop_loss_hit_at": None,
                            "expired_at": None,
                            "closed_at": None,
                            "exit_reason": None,
                            "last_evaluated_at": None,
                            "last_bar_date": None,
                            "last_bar_low": None,
                            "last_bar_high": None,
                            "last_bar_close": None,
                            "expires_at": (
                                self._now()
                                + timedelta(days=max(1, int(float(signal.get("timeframe_days") or 3) * 1.5)))
                            ).isoformat(),
                        }
                    )
                elif not entry_confirmed:
                    refresh_values.update(
                        {
                            "result_pct": 0.0,
                            "current_pnl_pct": 0.0,
                            "target_progress_pct": 0.0,
                            "max_favorable_move": 0.0,
                            "max_adverse_move": 0.0,
                            "scanner_call_status": None,
                            "target_hit_at": None,
                            "target_2_hit_at": None,
                            "partial_book_at": None,
                            "trailing_activated_at": None,
                            "exit_suggested_at": None,
                            "stop_loss_hit_at": None,
                            "expired_at": None,
                            "closed_at": None,
                            "exit_reason": None,
                            "continuation_pivot": None,
                            "post_target_high": None,
                            "post_target_low": None,
                            "parent_setup_id": None,
                            "setup_generation": 1,
                            "continuation_rearmed_at": None,
                            "rearm_reason": None,
                            "rearm_evidence": {},
                        }
                    )
                refreshed = self.store.update_setup(existing["id"], refresh_values)
                if refreshed and label != existing.get("last_update_label"):
                    self.store.record_update(refreshed["id"], self._now().isoformat(), label, note, {"bucket": bucket})
                updated += 1
                continue

            record = self._base_record(signal, bucket, company_name, sector, "scanner")
            created, inserted, rearmed = self._insert_new_or_rearmed(record, signal=signal)
            if inserted:
                update_label = "Setup re-armed" if rearmed else "Fresh setup"
                self.store.record_update(created["id"], record["detected_at"], update_label, created["last_update_note"], {"bucket": bucket})
                promoted += 1
            else:
                updated += 1

        auto_created = 0
        today = self._now().date().isoformat()
        for bucket, signal in self._collect_auto_calls(payload):
            symbol = signal["symbol"]
            if self._has_daily_scanner_call(symbol, signal["direction"], today):
                continue
            company_name = profiles.get(symbol, {}).get("company_name") or meta.get(symbol, {}).get("short_name") or symbol
            sector = profiles.get(symbol, {}).get("sector")
            existing = self.store.get_open_setup(symbol, signal["direction"])
            if existing and existing.get("source_mode") in {"scanner", "scanner_suggested"}:
                confirmed_at = self._ensure_entry_confirmation(existing)
                if not confirmed_at or existing.get("status") != "active":
                    continue
                if existing.get("source_mode") == "scanner_suggested":
                    continue
                now_iso = self._now().isoformat()
                promoted_call = self.store.update_setup(
                    existing["id"],
                    {
                        "source_mode": "scanner_suggested",
                        "suggested_at": confirmed_at,
                        "tracking_label": "Scanner Suggested Call",
                        "scanner_call_status": existing.get("scanner_call_status")
                        or ("REARMED" if existing.get("lifecycle_state") == "REARMED" else "ACTIVE"),
                        "last_update_label": "Scanner Suggested Call",
                        "last_update_note": "Promoted into today's top scanner calls without creating a duplicate lifecycle.",
                    },
                )
                if promoted_call:
                    self.store.record_update(
                        promoted_call["id"],
                        now_iso,
                        "Scanner Suggested Call",
                        "Promoted into today's top scanner calls without creating a duplicate lifecycle.",
                        {"bucket": bucket, "ranked_daily_top_10": True},
                    )
                    auto_created += 1
                continue
            record = self._base_record(signal, bucket, company_name, sector, "scanner_suggested")
            record["tracking_label"] = "Scanner Suggested Call"
            record["reason_summary"] = signal.get("signal_summary") or self._setup_note(signal)
            created, inserted, rearmed = self._insert_new_or_rearmed(record, signal=signal)
            if not inserted:
                continue
            self.store.record_update(
                created["id"],
                record["detected_at"],
                "Setup re-armed" if rearmed else "Scanner Suggested Call",
                created["last_update_note"] if rearmed else "Automatically selected as one of today's top 10 scanner calls.",
                {"bucket": bucket, "ranked_daily_top_10": True, "rearmed": rearmed},
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
        record.update(
            {
                "tracking_label": "Early Watch",
                "status": "watch_only",
                "lifecycle_state": "WATCH",
                "suggested_at": None,
                "entry_price": None,
                "scanner_call_status": None,
                "hold_or_exit": "WAIT",
            }
        )
        created, inserted = self.store.insert_setup_if_no_open(record)
        rearmed = False
        if not inserted:
            return created
        self.store.record_update(
            created["id"],
            record["detected_at"],
            "Setup re-armed" if rearmed else "Fresh setup",
            created["last_update_note"] if rearmed else "Manual watchlist entry created from the current stock detail view.",
            {"source": "manual", "rearmed": rearmed},
        )
        return created

    def _evaluate_row(
        self,
        setup: dict[str, Any],
        frame: pd.DataFrame,
    ) -> tuple[str, str, float, float, float, bool, bool, float | None, float | None]:
        confirmed_at = self._entry_confirmed_at(setup)
        if not confirmed_at:
            return (
                "watch_only",
                "Watch only",
                0.0,
                0.0,
                0.0,
                False,
                False,
                None,
                None,
            )
        if frame.empty:
            return (
                setup["status"],
                "Still active",
                setup.get("result_pct") or 0.0,
                setup.get("max_favorable_move") or 0.0,
                setup.get("max_adverse_move") or 0.0,
                False,
                False,
                setup.get("post_target_high"),
                setup.get("post_target_low"),
            )

        window = self._post_confirmation_window(frame, confirmed_at)
        if window.empty:
            return (
                setup["status"],
                "Still active",
                setup.get("result_pct") or 0.0,
                setup.get("max_favorable_move") or 0.0,
                setup.get("max_adverse_move") or 0.0,
                False,
                False,
                setup.get("post_target_high"),
                setup.get("post_target_low"),
            )
        entry = float(setup.get("entry_price") or 0)
        if entry <= 0:
            return setup["status"], "Still active", 0.0, 0.0, 0.0, False, False, None, None
        direction = setup["direction"]
        target = float(setup.get("target_1") or setup.get("target_price") or entry)
        target_2 = float(setup.get("target_2") or setup.get("extended_target_price") or 0)
        stop = float(setup.get("stop_loss") or setup.get("invalidation") or entry)
        active_trail = float(setup.get("trailing_stop") or stop or 0)
        dynamic_trails = chandelier_exit_series(frame, direction) if ENABLE_DYNAMIC_TRAIL else pd.Series(dtype=float)

        def ratchet_after_bar(index: Any) -> None:
            nonlocal active_trail
            if not self._daily_bar_is_complete(index) or dynamic_trails.empty or index not in dynamic_trails.index:
                return
            candidate = dynamic_trails.loc[index]
            if isinstance(candidate, pd.Series):
                candidate = candidate.iloc[-1]
            if pd.isna(candidate) or float(candidate) <= 0:
                return
            candidate_value = float(candidate)
            if direction == "bullish":
                active_trail = max(active_trail, candidate_value)
            elif active_trail <= 0:
                active_trail = candidate_value
            else:
                active_trail = min(active_trail, candidate_value)
            setup["trailing_stop"] = round(active_trail, 2)

        bars = window.copy()

        if direction == "bullish":
            favorable = (((bars["High"] / entry) - 1) * 100).max()
            adverse = (((bars["Low"] / entry) - 1) * 100).min()
        else:
            favorable = (((entry / bars["Low"]) - 1) * 100).max()
            adverse = -(((bars["High"] / entry) - 1) * 100).max()

        latest_close = float(bars["Close"].iloc[-1])
        status = setup["status"]
        label = "Still active"
        result_pct = self._directional_return(direction, entry, latest_close)
        target_was_hit = bool(setup.get("target_hit_at"))
        target_2_was_hit = bool(setup.get("target_2_hit_at"))
        target_hit_now = False
        target_2_hit_now = False
        target_seen = target_was_hit
        target_2_seen = target_2_was_hit
        post_high = float(setup["post_target_high"]) if setup.get("post_target_high") is not None else None
        post_low = float(setup["post_target_low"]) if setup.get("post_target_low") is not None else None
        recorded_target_date = self._parse_dt(setup.get("target_hit_at")).date() if target_was_hit else None
        last_evaluated_date = self._parse_dt(setup.get("last_evaluated_at")).date() if target_was_hit and setup.get("last_evaluated_at") else None
        processed_through_date = max(
            (item for item in (recorded_target_date, last_evaluated_date) if item is not None),
            default=None,
        )
        latest_index = bars.index[-1]
        latest_bar_date = latest_index.date().isoformat() if hasattr(latest_index, "date") else None
        latest_low = float(bars["Low"].iloc[-1])
        latest_high = float(bars["High"].iloc[-1])
        previous_bar_date = setup.get("last_bar_date")
        previous_low = float(setup["last_bar_low"]) if setup.get("last_bar_low") is not None else None
        previous_high = float(setup["last_bar_high"]) if setup.get("last_bar_high") is not None else None
        setup["last_bar_date"] = latest_bar_date
        setup["last_bar_low"] = latest_low
        setup["last_bar_high"] = latest_high
        setup["last_bar_close"] = latest_close

        # Daily candles mutate during the session. If a later evaluation sees
        # a genuinely new extreme, compare it only with the trail that was
        # already persisted at the earlier evaluation. This catches a 2pm
        # breach after a 10am check without applying a newly raised trail to
        # the candle's earlier range.
        same_session = bool(target_was_hit and latest_bar_date and previous_bar_date == latest_bar_date)
        if same_session and active_trail:
            if direction == "bullish":
                new_extreme_broke_trail = previous_low is not None and latest_low < previous_low and latest_low <= active_trail
                live_price_broke_trail = latest_close <= active_trail
            else:
                new_extreme_broke_trail = previous_high is not None and latest_high > previous_high and latest_high >= active_trail
                live_price_broke_trail = latest_close >= active_trail
            if new_extreme_broke_trail or live_price_broke_trail:
                post_high = latest_high if post_high is None else max(post_high, latest_high)
                post_low = latest_low if post_low is None else min(post_low, latest_low)
                trailed_return = (
                    ((active_trail / entry) - 1) * 100
                    if direction == "bullish"
                    else ((entry / active_trail) - 1) * 100
                )
                return (
                    "passed",
                    "Trailing exit",
                    trailed_return,
                    favorable,
                    adverse,
                    False,
                    False,
                    post_high,
                    post_low,
                )

        for index, bar in bars.iterrows():
            bar_date = index.date() if hasattr(index, "date") else None
            if processed_through_date and bar_date and bar_date <= processed_through_date:
                continue
            low = float(bar["Low"])
            high = float(bar["High"])
            stop_touched = (direction == "bullish" and stop and low <= stop) or (
                direction == "bearish" and stop and high >= stop
            )
            trail = active_trail
            trail_touched = bool(trail) and (
                (direction == "bullish" and low <= trail)
                or (direction == "bearish" and high >= trail)
            )
            target_touched = (direction == "bullish" and target and high >= target) or (
                direction == "bearish" and target and low <= target
            )
            target_2_touched = bool(target_2) and (
                (direction == "bullish" and high >= target_2)
                or (direction == "bearish" and low <= target_2)
            )

            # Before T1, same-bar stop/target ambiguity is resolved
            # conservatively in favour of the stop.  T1 itself is never a
            # terminal state; later bars are managed as the remaining runner.
            if not target_seen:
                if stop_touched:
                    stopped_return = ((stop / entry) - 1) * 100 if direction == "bullish" else ((entry / stop) - 1) * 100
                    return "failed", "Invalidated", stopped_return, favorable, adverse, False, False, post_high, post_low
                if not target_touched:
                    continue
                target_seen = True
                target_hit_now = True
                post_high = high if post_high is None else max(post_high, high)
                post_low = low if post_low is None else min(post_low, low)
                if target_2_touched and not target_2_seen:
                    target_2_seen = True
                    target_2_hit_now = True
                ratchet_after_bar(index)
                continue

            post_high = high if post_high is None else max(post_high, high)
            post_low = low if post_low is None else min(post_low, low)
            if trail_touched:
                trailed_return = ((trail / entry) - 1) * 100 if direction == "bullish" else ((entry / trail) - 1) * 100
                return "passed", "Trailing exit", trailed_return, favorable, adverse, target_hit_now, target_2_hit_now, post_high, post_low
            if stop_touched:
                stopped_return = ((stop / entry) - 1) * 100 if direction == "bullish" else ((entry / stop) - 1) * 100
                return "passed", "Trailing exit", stopped_return, favorable, adverse, target_hit_now, target_2_hit_now, post_high, post_low
            if target_2_touched and not target_2_seen:
                target_2_seen = True
                target_2_hit_now = True
            ratchet_after_bar(index)

        if target_seen:
            setup["trailing_stop"] = round(active_trail, 2) if active_trail else setup.get("trailing_stop")
            memory_expires_at = self._parse_dt(setup.get("memory_expires_at"))
            if self._now() >= memory_expires_at:
                return (
                    "passed",
                    "Tracking window completed",
                    result_pct,
                    favorable,
                    adverse,
                    target_hit_now,
                    target_2_hit_now,
                    post_high,
                    post_low,
                )
            if target_2_hit_now:
                label = "Target 2 hit"
            elif target_hit_now:
                label = "Target hit"
            elif (direction == "bullish" and latest_close < target) or (direction == "bearish" and latest_close > target):
                label = "Retest active"
            else:
                label = "Winner trailing"
            return "active", label, result_pct, favorable, adverse, target_hit_now, target_2_hit_now, post_high, post_low

        expires_at = self._parse_dt(setup.get("expires_at"))
        if self._now() >= expires_at:
            status = "expired"
            label = "No follow-through yet"
        elif status == "watch_only" and favorable > max(0.5, abs(setup.get("expected_move_pct") or 0) * 0.4):
            label = "Setup improved"

        return status, label, result_pct, favorable, adverse, False, False, post_high, post_low

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
            return {"evaluated": 0, "observed": 0}

        max_days = max(int(item.get("timeframe_days") or 3) for item in open_setups) + 5
        if any(item.get("target_hit_at") for item in open_setups):
            max_days = max(max_days, self._memory_sessions() * 2)
        frames = self.data.fetch_batch_history(
            [item["symbol"] for item in open_setups],
            period=self._period_for_days(max_days),
        )

        evaluated = 0
        observed = 0
        for setup in open_setups:
            setup = dict(setup)
            frame = frames.get(setup["symbol"], pd.DataFrame())

            if not self._ensure_entry_confirmation(setup):
                current_price = float(frame["Close"].iloc[-1]) if not frame.empty else setup.get("current_price")
                now_iso = self._now().isoformat()
                watch_note = "Watch-only idea observed; no entry, P&L, target, stop outcome, or re-arm is recorded."
                updated = self.store.update_setup(
                    setup["id"],
                    {
                        "status": "watch_only",
                        "lifecycle_state": "WATCH",
                        "suggested_at": None,
                        "entry_price": None,
                        "current_price": current_price,
                        "result_pct": 0.0,
                        "current_pnl_pct": 0.0,
                        "target_progress_pct": 0.0,
                        "max_favorable_move": 0.0,
                        "max_adverse_move": 0.0,
                        "scanner_call_status": None,
                        "hold_or_exit": "WAIT",
                        "reason_for_exit_decision": watch_note,
                        "target_hit_at": None,
                        "target_2_hit_at": None,
                        "partial_book_at": None,
                        "trailing_activated_at": None,
                        "exit_suggested_at": None,
                        "stop_loss_hit_at": None,
                        "expired_at": None,
                        "closed_at": None,
                        "exit_reason": None,
                        "continuation_pivot": None,
                        "post_target_high": None,
                        "post_target_low": None,
                        "parent_setup_id": None,
                        "setup_generation": 1,
                        "rearmed_at": None,
                        "rearmed_setup_id": None,
                        "continuation_rearmed_at": None,
                        "rearm_reason": None,
                        "rearm_evidence": {},
                        "rearm_blocked_at": None,
                        "rearm_block_reason": None,
                        "last_bar_date": None,
                        "last_bar_low": None,
                        "last_bar_high": None,
                        "last_bar_close": None,
                        "last_evaluated_at": None,
                        "last_checked_at": now_iso,
                        "last_update_label": "Watch only",
                        "last_update_note": watch_note,
                    },
                )
                if updated and setup.get("last_update_label") != "Watch only":
                    self.store.record_update(
                        setup["id"],
                        now_iso,
                        "Watch only",
                        watch_note,
                        {"entry_confirmed": False},
                    )
                observed += 1
                continue

            (
                status,
                label,
                result_pct,
                favorable,
                adverse,
                target_1_hit_now,
                target_2_hit_now,
                post_target_high,
                post_target_low,
            ) = self._evaluate_row(setup, frame)
            current_price = setup.get("last_bar_close") or setup.get("current_price")

            is_expired = status == "expired" or label == "Tracking window completed"
            lifecycle = self._build_lifecycle(
                setup,
                float(current_price or 0),
                expired=is_expired,
                target_1_hit_now=target_1_hit_now,
                target_2_hit_now=target_2_hit_now,
            )
            if status == "failed":
                lifecycle.update(
                    {
                        "hold_or_exit": "EXIT",
                        "scanner_call_status": "STOP_LOSS_HIT",
                        "lifecycle_state": "INVALIDATED",
                        "exit_signal": True,
                        "reason_for_exit_decision": "Stop-loss hit before T1; the setup is invalidated.",
                        "exit_reason": "stop_loss_hit",
                    }
                )
            elif status == "passed":
                memory_complete = label == "Tracking window completed"
                lifecycle.update(
                    {
                        "hold_or_exit": "EXIT",
                        "scanner_call_status": "EXIT_SUGGESTED",
                        "lifecycle_state": "MEMORY_COMPLETE" if memory_complete else "CLOSED",
                        "exit_signal": True,
                        "reason_for_exit_decision": (
                            "Winner-management window completed; review or exit the remaining position."
                            if memory_complete
                            else "The post-target protective stop was hit; the remaining runner is closed."
                        ),
                        "exit_reason": "winner_memory_complete" if memory_complete else "post_target_stop_hit",
                    }
                )
            hold_or_exit = lifecycle["hold_or_exit"]
            scanner_call_status = lifecycle["scanner_call_status"]
            lifecycle_note = lifecycle["reason_for_exit_decision"]
            current_pnl = lifecycle["current_pnl_pct"]
            target_progress = lifecycle["target_progress_pct"]
            if setup.get("target_hit_at") or target_1_hit_now or target_2_hit_now:
                target_progress = max(100.0, float(target_progress or 0))
            note = {
                "Target hit": "Price reached the primary target before invalidation within the tracked timeframe.",
                "Target 2 hit": "The runner reached its extended target; partial profit is protected and trailing remains active.",
                "Winner trailing": "T1 is complete; the remaining position stays open behind a ratcheting trailing stop.",
                "Retest active": "The old target is being treated as a continuation pivot while the protective trail holds.",
                "Trailing exit": "After partial profit, the remaining position reached its protective stop.",
                "Tracking window completed": "The 30-60 session winner-management window completed.",
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
                    "lifecycle_state": lifecycle["lifecycle_state"],
                    "current_price": current_price,
                    "result_pct": round(result_pct, 2),
                    "current_pnl_pct": round(current_pnl, 2),
                    "target_progress_pct": round(target_progress, 2),
                    "scanner_call_status": scanner_call_status if setup.get("source_mode") == "scanner_suggested" else setup.get("scanner_call_status"),
                    "hold_or_exit": hold_or_exit,
                    "reason_for_exit_decision": lifecycle_note,
                    "max_favorable_move": round(float(favorable or 0.0), 2),
                    "max_adverse_move": round(float(adverse or 0.0), 2),
                    "continuation_pivot": setup.get("continuation_pivot")
                    or (float(setup.get("target_1") or setup.get("target_price") or 0) if (setup.get("target_hit_at") or target_1_hit_now) else None),
                    "post_target_high": post_target_high,
                    "post_target_low": post_target_low,
                    "trailing_stop": setup.get("trailing_stop"),
                    "last_bar_date": setup.get("last_bar_date"),
                    "last_bar_low": setup.get("last_bar_low"),
                    "last_bar_high": setup.get("last_bar_high"),
                    "last_bar_close": setup.get("last_bar_close"),
                    "last_evaluated_at": now_iso,
                    "last_update_label": label,
                    "last_update_note": note,
                    **timestamp_updates,
                },
            )
            if updated and (
                label != setup.get("last_update_label")
                or status != setup.get("status")
                or lifecycle.get("lifecycle_state") != setup.get("lifecycle_state")
                or scanner_call_status != setup.get("scanner_call_status")
            ):
                self.store.record_update(
                    updated["id"],
                    self._now().isoformat(),
                    label,
                    note,
                    {"status": status, "lifecycle_state": lifecycle.get("lifecycle_state")},
                )
            evaluated += 1

        return {"evaluated": evaluated, "observed": observed}

    def normalize_lifecycle_rows(self) -> int:
        rows = self.store.list_setups(
            """
            SELECT *
            FROM tracked_setups
            WHERE archived = 0
              AND status IN ('passed', 'failed', 'expired')
            """
        )
        updated_count = 0
        for setup in rows:
            setup = dict(setup)
            current_price = float(setup.get("current_price") or setup.get("entry_price") or 0)
            was_wrongly_expired = setup.get("status") == "expired" and self._now() < self._parse_dt(setup.get("expires_at"))
            next_status = "active" if was_wrongly_expired else setup.get("status")
            old_target_pass = next_status == "passed" and (
                bool(setup.get("target_hit_at"))
                or setup.get("scanner_call_status") in {"TARGET_1_HIT", "TARGET_2_HIT", "PARTIAL_BOOK"}
                or setup.get("lifecycle_state") in {"TARGET_1_HIT", "TARGET_2_HIT", "PARTIAL_BOOK"}
                or setup.get("exit_reason") in {"target_1_hit", "target_2_hit"}
                or setup.get("last_update_label") in {"Target hit", "Target 2 hit"}
            )
            genuinely_closed = setup.get("exit_reason") in {
                "post_target_stop_hit",
                "trailing_stop_broken",
                "winner_memory_complete",
            } or setup.get("lifecycle_state") in {"CLOSED", "MEMORY_COMPLETE"}
            memory_is_live = self._now() < self._parse_dt(setup.get("memory_expires_at"))
            if old_target_pass and not genuinely_closed and memory_is_live:
                setup["target_hit_at"] = setup.get("target_hit_at") or setup.get("last_evaluated_at") or setup.get("detected_at")
                setup["continuation_pivot"] = setup.get("continuation_pivot") or setup.get("target_1") or setup.get("target_price")
                next_status = "active"

            lifecycle = self._build_lifecycle(
                setup,
                current_price,
                expired=setup.get("status") == "expired" and not was_wrongly_expired,
            )
            if old_target_pass and next_status == "active" and lifecycle.get("exit_signal"):
                next_status = "passed"
            if next_status == "passed":
                memory_complete = not memory_is_live or setup.get("exit_reason") == "winner_memory_complete"
                lifecycle.update(
                    {
                        "scanner_call_status": "EXIT_SUGGESTED",
                        "hold_or_exit": "EXIT",
                        "lifecycle_state": "MEMORY_COMPLETE" if memory_complete else "CLOSED",
                        "exit_signal": True,
                        "reason_for_exit_decision": "Winner lifecycle is complete.",
                        "exit_reason": "winner_memory_complete" if memory_complete else (setup.get("exit_reason") or "post_target_stop_hit"),
                    }
                )
            elif next_status == "failed":
                lifecycle.update(
                    {
                        "scanner_call_status": "STOP_LOSS_HIT",
                        "hold_or_exit": "EXIT",
                        "lifecycle_state": "INVALIDATED",
                        "exit_signal": True,
                        "reason_for_exit_decision": "Stop-loss hit.",
                        "exit_reason": "stop_loss_hit",
                    }
                )
            now_iso = self._now().isoformat()
            values = {
                "status": next_status,
                "lifecycle_state": lifecycle["lifecycle_state"],
                "scanner_call_status": lifecycle["scanner_call_status"]
                if setup.get("source_mode") == "scanner_suggested"
                else setup.get("scanner_call_status"),
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
            if old_target_pass and next_status == "active":
                values.update(
                    {
                        "target_hit_at": setup["target_hit_at"],
                        "continuation_pivot": setup["continuation_pivot"],
                        "expires_at": setup.get("memory_expires_at"),
                        "closed_at": None,
                        "exit_reason": None,
                        "last_update_label": "Winner tracking restored",
                        "last_update_note": "Legacy T1 outcome restored as an active trailing winner inside its memory window.",
                    }
                )
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
                  AND scanner_call_status IN (
                      'ACTIVE', 'TARGET_1_HIT', 'TARGET_2_HIT', 'PARTIAL_BOOK',
                      'TRAILING_HOLD', 'RETEST', 'REARMED', 'CONTINUATION', 'EXIT_SUGGESTED'
                  )
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
                  AND target_hit_at IS NOT NULL
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
        target_hits = [item for item in all_rows if item.get("target_hit_at")]
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
                "target_hit_count": len(target_hits),
                "stop_loss_count": len(failed),
                "win_rate": round((len(passed) / win_base) * 100, 1) if win_base else 0.0,
                "average_return": round(average_return, 2),
                "best_call": max(resolved, key=lambda item: item.get("result_pct") or -9999, default=None),
                "worst_call": min(resolved, key=lambda item: item.get("result_pct") or 9999, default=None),
            },
            "watchlists": watchlists,
            "todays_review": {
                "updates": updates,
                "what_worked": [item for item in updates if item["label"] in {"Target hit", "Target 2 hit", "Winner trailing"}][:4],
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
        payload = dict(values)
        explicit_confirmation = payload.pop("entry_confirmed_at", None) or payload.pop("armed_at", None)
        if explicit_confirmation:
            confirmed_at = (
                self._now().isoformat()
                if explicit_confirmation is True
                else self._parse_dt(str(explicit_confirmation)).isoformat()
            )
            payload.update(
                {
                    "suggested_at": confirmed_at,
                    "status": "active",
                    "lifecycle_state": "ARMED",
                    "entry_price": payload.get("entry_price")
                    or payload.get("current_price")
                    or setup.get("current_price"),
                    "tracking_label": (
                        setup.get("tracking_label")
                        if setup.get("tracking_label") == "Scanner Suggested Call"
                        else "Confirmed Setup"
                    ),
                    "result_pct": 0.0,
                    "current_pnl_pct": 0.0,
                    "target_progress_pct": 0.0,
                    "max_favorable_move": 0.0,
                    "max_adverse_move": 0.0,
                    "target_hit_at": None,
                    "target_2_hit_at": None,
                    "partial_book_at": None,
                    "stop_loss_hit_at": None,
                    "closed_at": None,
                    "exit_reason": None,
                    "last_evaluated_at": None,
                }
            )
        elif payload.get("status") == "watch_only":
            payload.update(
                {
                    "suggested_at": None,
                    "entry_price": None,
                    "lifecycle_state": "WATCH",
                    "scanner_call_status": None,
                    "hold_or_exit": "WAIT",
                    "result_pct": 0.0,
                    "current_pnl_pct": 0.0,
                    "target_progress_pct": 0.0,
                }
            )
        updated = self.store.update_setup(setup_id, payload)
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
