"""Walk-forward end-of-day replay for validation (Release 3 groundwork).

For each session in the replay window the engine sees only the daily candles
up to and including that session, runs its nightly cycle "after the close"
and records the events and alerts it would have produced.  Nothing from a
later session is visible, so swing points, zones and freshness cannot use
look-ahead.  Intraday timing is not reproduced: every event is EOD-inferred.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

import pandas as pd

from app.fresh_zones.alerts import AlertManager
from app.fresh_zones.data import FrameProvider
from app.fresh_zones.engine import FreshZoneEngine
from app.fresh_zones.models import DataMode
from app.fresh_zones.params import ScannerParams
from app.fresh_zones.store import FreshZoneStore
from app.fresh_zones.timeframes import IST, SessionCalendar, validate_daily


class CollectingSink:
    name = "replay"

    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def deliver(self, alert: dict[str, Any]) -> bool:
        self.alerts.append(alert)
        return True


def replay(
    symbol: str,
    daily: pd.DataFrame,
    start: str,
    end: str | None = None,
    *,
    params: ScannerParams,
    calendar: SessionCalendar | None = None,
) -> dict[str, Any]:
    clean, issues = validate_daily(daily)
    store = FreshZoneStore(":memory:")
    sink = CollectingSink()
    provider = FrameProvider({symbol: clean}, mode=DataMode.END_OF_DAY)
    engine = FreshZoneEngine(provider, store, params, calendar or SessionCalendar.from_params(params),
                             AlertManager(store, params, [sink]))
    window = clean.loc[start:end] if end else clean.loc[start:]
    for ts in window.index:
        provider.daily = {symbol: clean.loc[:ts]}
        engine.run_nightly([symbol], datetime.combine(ts.date(), time(18, 0), tzinfo=IST), enforce_universe=False)
    events = sorted(store.list_events(symbol=symbol, limit=100_000), key=lambda e: e["at"])
    return {
        "symbol": symbol,
        "window": [window.index[0].date().isoformat(), window.index[-1].date().isoformat()] if len(window) else None,
        "param_version": params.version,
        "data_issues": issues,
        "zones": [z.to_dict() for z in store.list_zones(symbol=symbol)],
        "events": events,
        "alerts": sink.alerts,
        "suppressed_alerts": store.list_alerts(delivered=False, limit=100_000),
        "limitations": ["end-of-day replay: intraday timing and live touches are not reproduced"],
    }
