"""Process-wide wiring: engine singleton, universe and optional scheduler.

The scheduler is disabled unless ``FRESH_ZONE_SCHEDULER_ENABLED=true``:
  * live monitor every ``FRESH_ZONE_MONITOR_INTERVAL_SEC`` (default 60) while
    the regular session is open;
  * nightly zone engine once per day after ``FRESH_ZONE_NIGHTLY_AT``
    (default 18:30 IST).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, time, timezone
from functools import lru_cache

from app.fresh_zones.data import get_provider, load_symbol_list, normalise_symbols
from app.fresh_zones.engine import FreshZoneEngine
from app.fresh_zones.params import get_default_params
from app.fresh_zones.store import FreshZoneStore
from app.fresh_zones.timeframes import to_ist


logger = logging.getLogger(__name__)


def universe_symbols() -> list[str]:
    path = os.getenv("FRESH_ZONE_UNIVERSE_FILE", "").strip()
    if path:
        return normalise_symbols(load_symbol_list(path))
    from app.market_universe_config import NIFTY_500_SEED

    return normalise_symbols(list(NIFTY_500_SEED))


@lru_cache
def get_engine() -> FreshZoneEngine:
    return FreshZoneEngine(get_provider(), FreshZoneStore(), get_default_params())


class FreshZoneScheduler:
    def __init__(self, engine: FreshZoneEngine):
        self.engine = engine
        self.interval = int(os.getenv("FRESH_ZONE_MONITOR_INTERVAL_SEC", "60"))
        hour, minute = os.getenv("FRESH_ZONE_NIGHTLY_AT", "18:30").split(":")
        self.nightly_at = time(int(hour), int(minute))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_nightly: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="fresh-zone-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            now = to_ist(datetime.now(timezone.utc))
            try:
                if self.engine.calendar.is_open(now):
                    self.engine.monitor_cycle(now)
                elif now.time() >= self.nightly_at and self._last_nightly != now.date().isoformat():
                    self._last_nightly = now.date().isoformat()
                    self.engine.run_nightly(universe_symbols(), now)
            except Exception:
                logger.exception("fresh-zone scheduler iteration failed")
            self._stop.wait(self.interval)


_scheduler: FreshZoneScheduler | None = None


def start_scheduler_if_enabled() -> bool:
    global _scheduler
    if os.getenv("FRESH_ZONE_SCHEDULER_ENABLED", "false").strip().lower() not in {"1", "true", "yes"}:
        return False
    if _scheduler is None:
        _scheduler = FreshZoneScheduler(get_engine())
        _scheduler.start()
    return True
