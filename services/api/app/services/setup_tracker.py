from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pandas as pd

from app.core.settings import Settings
from app.services.data_provider import MarketDataService
from app.services.setup_store import SetupStore


TRACKABLE_BUCKETS = ("top_opportunities", "breakout_candidates", "unusual_volume")


class SetupTrackerService:
    def __init__(self, settings: Settings, data: MarketDataService):
        self.settings = settings
        self.data = data
        self.store = SetupStore(settings.tracked_setup_db_path)

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
        if not entry or not current:
            return 0.0
        if direction == "bullish":
            return ((current / entry) - 1) * 100
        if direction == "bearish":
            return ((entry / current) - 1) * 100
        return 0.0

    def _tracking_label(self, signal: dict[str, Any]) -> str:
        if signal["direction"] == "neutral":
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
            "last_seen_at": now.isoformat(),
            "last_evaluated_at": None,
            "expires_at": (now + timedelta(days=max(1, int(timeframe_days * 1.5)))).isoformat(),
            "timeframe_label": signal.get("timeframe_label"),
            "timeframe_days": timeframe_days,
            "entry_price": signal.get("current_price"),
            "current_price": signal.get("current_price"),
            "target_price": signal.get("target_price"),
            "extended_target_price": signal.get("extended_target_price"),
            "stop_loss": signal.get("stop_loss"),
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
            "result_pct": 0.0,
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
                        "extended_target_price": signal.get("extended_target_price"),
                        "stop_loss": signal.get("stop_loss"),
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

        return {"promoted": promoted, "updated": updated}

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
        window = frame.loc[frame.index >= start].copy()
        if window.empty:
            window = frame.tail(max(2, int(setup.get("timeframe_days") or 3)))
        entry = float(setup.get("entry_price") or 0)
        direction = setup["direction"]
        target = float(setup.get("target_price") or entry)
        stop = float(setup.get("stop_loss") or setup.get("invalidation") or entry)
        bars = window.head(max(1, int(setup.get("timeframe_days") or 3)))

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

        if len(window) >= max(1, int(setup.get("timeframe_days") or 3)):
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
            note = {
                "Target hit": "Price reached the primary target before invalidation within the tracked timeframe.",
                "Invalidated": "Price broke the invalidation level before the target could complete.",
                "No follow-through yet": "The timeframe expired without enough directional progress.",
                "Setup improved": "Price is moving in the expected direction, but the idea is still open.",
                "Still active": "Setup remains active while price continues respecting the invalidation level.",
            }.get(label, "Tracked idea evaluated against the latest available price history.")
            updated = self.store.update_setup(
                setup["id"],
                {
                    "status": status,
                    "current_price": float(frame["Close"].iloc[-1]) if not frame.empty else setup.get("current_price"),
                    "result_pct": round(result_pct, 2),
                    "max_favorable_move": round(float(favorable or 0.0), 2),
                    "max_adverse_move": round(float(adverse or 0.0), 2),
                    "last_evaluated_at": self._now().isoformat(),
                    "last_update_label": label,
                    "last_update_note": note,
                },
            )
            if updated and (label != setup.get("last_update_label") or status != setup.get("status")):
                self.store.record_update(updated["id"], self._now().isoformat(), label, note, {"status": status})
            evaluated += 1

        return {"evaluated": evaluated}

    def get_dashboard(self) -> dict[str, Any]:
        self.evaluate_open_setups()
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
