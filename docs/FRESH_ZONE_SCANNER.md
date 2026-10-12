# Fresh Demand & Resistance Zone Scanner

Implementation of the *Fresh Demand and Resistance Scanner* specification.
Code: `services/api/app/fresh_zones/`. Tests: `services/api/tests/test_fresh_zones.py`.

> The scanner classifies price structure using explicit, configurable rules.
> It does not know institutional orders, does not recommend trades, and its
> quality score is **not** a probability of success. Every threshold is an
> initial hypothesis that has **not** been validated on real market data yet.

## Status

| Release | Scope | State |
|---|---|---|
| 1 | Universe filters, W/M/Y zone detection, freshness, scoring, ATR proximity alerts, touch / gap-through detection, alert records & logs | Implemented, unit-tested on synthetic data |
| 2 | Reaction confirmation, consolidating / under-pressure / failed states, price-path clarity | Implemented, unit-tested. Event-date flags: hook only (no reliable source yet) |
| 3 | Breakout/breakdown confirmation, retest, false break, flipped zones, continuation, replay | Implemented, unit-tested. Backtest *reports* (hit rates etc.) not built |

Not validated against manually reviewed charts or real history yet (spec §17).
That has to happen before anyone relies on it.

## Module map

| Spec section | Module |
|---|---|
| §2, §16 parameter set & version | `params.py` (`ScannerParams`, version = content hash, e.g. `fz1-0f0dfe91bd`) |
| §3 universe & data | `data.py` (providers, `eligibility`, symbol normalisation, owner exclusion list) |
| §4 timeframes, ATR, boundaries, session | `timeframes.py`, `detector.py` |
| §5 zone detection, FVG, tick rounding | `detector.py` |
| §6 freshness, tests, invalidation | `freshness.py` (history), `events.py` (live) |
| §7 proximity | `proximity.py` |
| §8 touch / gap-through | `events.py` (`on_session_open`, `on_intraday_candle`, `on_price`) |
| §9 reaction | `events.py` |
| §10 path clarity | `path_clarity.py` |
| §11 breakouts & flipped zones | `events.py` (`on_zone_candle_close`, `make_flipped_zone`) |
| §12 two state machines | `models.ZoneStatus` (zone) and `models.EventState` + `events.ALLOWED` (event) |
| §13 scoring | `scoring.py` |
| §14–15 alerts | `alerts.py` |
| §16 nightly engine & live monitor | `engine.py`, `service.py` (scheduler) |
| §17 replay | `replay.py` |
| persistence | `store.py` (SQLite, default `services/api/data/fresh_zones.db`) |

## Running it

```bash
cd services/api
# Nightly zone build (default universe: FRESH_ZONE_UNIVERSE_FILE, else the repo's Nifty-200 seed list)
python -m app.fresh_zones.cli nightly
python -m app.fresh_zones.cli nightly --symbols RELIANCE,INFY --all   # ignore universe filters
# One live-monitor cycle (only acts during 09:15–15:30 IST)
python -m app.fresh_zones.cli monitor
# Ranked fresh zones near the last close
python -m app.fresh_zones.cli opportunities --limit 20
# Walk-forward end-of-day replay (no look-ahead)
python -m app.fresh_zones.cli replay TCS --start 2024-01-01 --out tcs_replay.json
```

API (FastAPI, prefix `/api/fresh-zones`): `GET /params`, `GET /opportunities`,
`GET /zones`, `GET /zones/{zone_id}` (zone + watch + event history),
`GET /alerts`, `GET /events`, `GET /runs`, `POST /nightly`, `POST /monitor`.

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `FRESH_ZONE_PARAMS_FILE` | — | JSON overrides for any `ScannerParams` field |
| `FRESH_ZONE_DB_PATH` | `services/api/data/fresh_zones.db` | SQLite store |
| `FRESH_ZONE_PROVIDER` | `yfinance` | `yfinance` or `angelone` (broker quotes + Yahoo history) |
| `FRESH_ZONE_LIVE_CONFIRMED` | `false` | Only when `true` are broker quotes labelled LIVE |
| `FRESH_ZONE_UNIVERSE_FILE` | — | NSE constituents CSV with a `Symbol` column (e.g. Nifty 500 list) |
| `FRESH_ZONE_EXCLUSIONS_FILE` | — | Owner-maintained ASM/GSM/restricted symbols, one per line |
| `FRESH_ZONE_SCHEDULER_ENABLED` | `false` | Run monitor + nightly inside the API process |
| `FRESH_ZONE_MONITOR_INTERVAL_SEC` | `60` | Monitor cadence during the session |
| `FRESH_ZONE_NIGHTLY_AT` | `18:30` | Nightly run time (IST) |
| `FRESH_ZONE_TELEGRAM_ENABLED` | `false` | Also deliver alerts via the existing `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` |

## Owner decisions still open (spec §19)

Defaults are placeholders so the code can run; none of them is a decision.

| Decision | Current default | Where |
|---|---|---|
| Market-data provider, cost, permitted usage, refresh rate | Yahoo Finance (free, unofficial, delayed); AngelOne optional | `data.py` |
| Is the AngelOne feed licensed for this use (LIVE label)? | No — DELAYED until `FRESH_ZONE_LIVE_CONFIRMED=true` | `data.py` |
| Alert delivery channel | Log only; Telegram opt-in | `alerts.py` |
| Minimum average traded value | ₹10 crore (20-session average) | `min_avg_traded_value_cr` |
| Minimum history | 500 sessions | `min_history_sessions` |
| Calendar vs financial year | Calendar | `year_definition` |
| Earlier daily-close breakout alerts | Off | `enable_daily_close_provisional` |
| Exclude all ASM/GSM securities | Yes, from an owner-supplied list (no reliable feed is wired) | `exclude_asm_gsm`, `FRESH_ZONE_EXCLUSIONS_FILE` |
| Personal use vs distribution | Unknown — affects data licensing | — |
| Universe source of ~500 symbols | Repo seed list has ~200; supply the NSE Nifty 500 CSV | `FRESH_ZONE_UNIVERSE_FILE` |
| Exchange holiday / special-session calendar | Weekends only; holidays must be supplied | `SessionCalendar(holidays=…, special_sessions=…)` |
| NSE price-band tick sizes | Flat ₹0.05 | `tick_bands` |
| Corporate-event (results / board meeting) dates | Not available; field is `null` with a limitation note | `alerts.build_alert` |

## Interpretations made by the developer

The spec leaves these points open; each choice is configurable or isolated in one place.

1. **ATR_Z reference point.** Leg-in, departure and width thresholds use ATR_Z as of the
   *last base candle*, so the departure never inflates its own yardstick.
2. **Completed candles.** A W/M/Y candle is complete once its last calendar day's
   session has closed (weeks are Monday–Sunday, so a weekly candle completes on
   Sunday night). A history that starts part-way into a period drops that first,
   partial candle.
3. **Leg-in / departure adjacency.** The candle immediately before the base and the
   first departure candle must not themselves be base-like (body ≤ 50 % of range);
   otherwise they belong to the base. "Combined directional body" = sum of
   `close − open` over the candles.
4. **Base selection.** For each departure start the smallest qualifying base, then
   shortest leg-in and departure, is chosen; candidates are accepted in formation
   order and a later one whose base overlaps an accepted zone's base/departure is
   dropped.
5. **Tick rounding** is outward (demand proximal up, distal down; resistance the
   reverse) so the rounded zone always contains the selected structure; raw values
   are stored too.
6. **FVG** is searched over the three-candle windows centred on each departure
   candle; the largest gap is recorded.
7. **Freshness evidence.** Every daily bar after the departure is checked (daily
   high/low covers every trade). Gaps > 15 calendar days or data more than 3
   business days stale make freshness UNKNOWN (zone status `UNVERIFIED`), which
   never alerts.
8. **Test counting.** Excursion away from the proximal edge is measured only on bars
   that did not overlap the zone, because the order of high and low inside one
   overlapping bar is unknown.
9. **Zones touched before discovery** are stored (for history and path barriers) but
   not watched; only zones fresh at discovery get an event lifecycle.
10. **Price-path "opposing" structures** are those opposing the direction of travel:
    supports (demand zones, swing lows) on the way down to demand; resistances on
    the way up to resistance. A structure counts if its near edge lies strictly
    between price and the destination's proximal boundary.
11. **Structural clarity (8 pts).** 4 pts for a one-candle departure (2 for two);
    4 pts when no earlier opposite-type zone overlaps or lies within 1 × ATR_Z.
12. **Reaction window.** The touch session counts as session 1 of 2. In
    END_OF_DAY mode (no 15-minute candles) a daily close beyond the threshold may
    confirm, and the event says `confirmation_basis: completed daily close`.
13. **False break** = a completed daily close back inside the zone (or on its
    original side). **Retest** = price trading back to the distal edge after
    confirmation. The default retest holding rule is "no daily close back inside".
14. **Flipped zones** reuse the broken zone's edges (old distal becomes the new
    proximal), start fresh at the confirmation candle, are tracked with their own
    lifecycle, and do **not** raise fresh-zone approach alerts (they have no
    formation score).
15. **Daily cap ordering.** Within a batch, lifecycle events on zones already being
    followed rank ahead of new approach alerts; approach alerts then rank by
    IMMINENT/NEAR, score, ATR_D distance. The cooldown never suppresses a different
    state or a NEAR → IMMINENT upgrade.
16. **Lifecycle merging.** The nightly recomputation is authoritative for
    measurements, but a zone's status never moves backwards and live-observed
    touches are kept. A change in the stored price basis (likely a corporate
    action) is logged in the symbol's `data_issues`.

## Known limitations

* Yahoo daily data is split/bonus adjusted but not dividend adjusted; its quotes
  are delayed by an unspecified amount and carry the retrieval time, which is
  labelled (`timestamp_is_retrieval_time`).
* Without an exchange holiday calendar, a holiday is detected only indirectly
  (the quote is not from today, so the cycle is skipped as stale).
* The yearly "derived from monthly structures" method is not implemented; only
  native annual candles are used (`construction_method = native_candles`).
* Delisted symbols are only covered if the data source still serves them.
* The replay reproduces end-of-day behaviour only; intraday timing is not replayed.
