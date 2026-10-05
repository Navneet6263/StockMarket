# Book-inspired candle and live-entry rules

Implemented for research/paper trading. This is not a validated profitable system,
an institution-identification model, or a promise to catch every reversal.

## Sources and attribution

- Mark Douglas, *Trading in the Zone*: previously inspected user-provided PDF
  passages distinguish psychological discipline from a trading system (PDF viewer
  pages 11, 130, 133–140). Principles used: objective conditions, predefined
  structural risk, accepting uncertainty, and not moving stops to fit a desired
  position. The book's 20-trade discipline exercise does not establish profitability.
- The user-provided Nison PDF was unavailable during implementation. This is NOT
  claimed to be a complete or exact implementation of that book. Nison's public
  author material supports interpreting candles with prior trend, location,
  support/resistance, confirmation and reward/risk:
  [The Nison Advantage](https://candlecharts.com/about/the-nison-advantage/),
  [author presentation hosted by CMT](https://cmtassociation.org/wp-content/uploads/2024/01/1025-nison-1.pdf),
  [author's candle-chart mistakes report](https://s3.amazonaws.com/nisoncandlehighlighter/The3CommonAndCostlyMistakesWithCandleCharts.pdf).
- All numeric cutoffs below and in code are **engineering research heuristics**,
  not quotations, calibrated probabilities, or thresholds proven by either book.

## Decision path

Existing universe ranking → capped intraday shortlist → completed daily trend +
completed 15-minute candle context → shared scanner safety gate → fresh live
price/volume/depth gate → research entry alert.

The preliminary universe scan does not run additional dataframe candle analysis.
The existing intraday fetch and bounded live pool are reused. There is no new
network request or language-model call on each tick. Intraday disk cache freshness
is interval-aware; a stale disk file cannot endlessly renew its RAM TTL.

## Candle gate (`candle_context_v1`)

| Condition | Behavior |
| --- | --- |
| Incomplete candle | Excluded; never confirms its own pattern |
| Daily trend | Completed close versus EMA20 and its five-session slope; insufficient daily history cannot arm |
| Candle context | Hammer versus hanging-man, engulfing and support/resistance reclaim/rejection interpreted in prior local trend |
| Location | Observed prior support/resistance; target from nearest observed opposing structure, not a fabricated R multiple |
| Confirmation | Subsequent closed directional candle beyond trigger, strong close and adequate relative volume |
| Risk | Current-close raw RR ≥ 1.5; no more than 0.5 initial R beyond trigger; existing structural stop cap retained (default 4%) |
| Failure | Stop breach invalidates; already-touched target or lost trigger hold requires a new setup |
| Freshness | Confirmation window up to three bars; current-session confirmation required; completed intraday bar age ≤ three intervals during session |
| Conflict | Higher-timeframe opposition or scanner/candle direction disagreement stays WAIT, not a silent direction flip |

`WATCH` means no qualified contextual pattern yet. `WAIT` means an identified
condition remains incomplete. `UNAVAILABLE` means required data/analysis is absent.
`INVALID` means malformed data or invalidated structure. `READY` means only the
candle gate passed, not that an order should be placed.

MarketDataService's timezone-naive intraday cache represents UTC; orchestration
restores UTC before the candle engine converts to Asia/Kolkata. The standalone
candle API interprets naive timestamps as exchange-local. Daily session dates
remain exchange-local. NSE regular-session times are used; there is no new
exchange holiday/special-session calendar in this change.

## Shared and live gates

- Static candle setups remain WATCH and request separate live confirmation.
  Reapplying enrichment cannot resurrect BUY or momentum-ready aliases.
- Existing market, trap/supply, chase and structural-risk vetoes remain binding.
  Quiet candle candidates have a dedicated display/enrichment bucket, so they do
  not vanish merely because the static BUY lists are empty.
- Entry, stop and target must be finite, positive and on the correct directional
  sides. Current-price RR is recomputed before confirmation; planned RR is shown
  separately. Missing targets are not manufactured and stops are not tightened
  inside structure to force an attractive ratio.
- Missing timestamps, stale, duplicate, out-of-order or invalid packets cannot
  build fresh evidence. Invalid quotes revoke previous confirmation.
- Engineering live minimums: three valid observations across at least eight
  seconds of event AND receipt time, at least 100 observed traded shares and
  INR 10,000 observed notional, plus directional evidence checks. These are only
  minimum evidence filters, not proof of adequate order-size liquidity.
- Book-gated setups require executed-volume and market-depth fields. Price-only
  or depth-only data stays WAIT. Volume-only legacy paths are explicitly labeled
  `EXECUTED_VOLUME`, never `FULL_TICK`.
- Timed candle snapshots expire independently of continued fresh broker ticks.
  New scans revoke changed/removed plans immediately.
- Scores describe evidence strength, NOT a probability of winning. Displayed
  depth can be withdrawn; aggregate volume cannot identify FII/DII/institutions.
- Live cards show current entry/stop/target and current RR. Missing daily change
  is unknown (`—`), not an invented 0.00%.

## Limits and validation

No automatic-order setting, production deployment or broker permission is changed.
An underlying stock signal is not a tradeable option-contract recommendation.
Contract spreads, expiry, IV/Greeks, fees, slippage, gaps and order-size liquidity
still need independent handling. Stops are invalidation levels, not guaranteed
fill prices; a crash or gap can cause a larger realized loss.

Synthetic regression tests cover closed-bar timing, bullish/bearish geometry,
lookahead avoidance, context/confirmation, finite risk levels, rescan revocation,
stale snapshots, UI field mapping and scan-to-live integration. Passing software
tests establishes rule behavior, **not trading accuracy**.

Before using real capital: freeze the rule version, replay timestamp-correct data
without future candles, include failed/delisted candidates and transaction costs,
evaluate separate bull/bear/sideways periods, then forward-test with paper trades.
Record every candidate/skip as well as entries, MAE/MFE, actual stop slippage,
drawdown, expectancy and missed moves. Do not judge only a few stocks that rallied
after falling, and do not describe the existing legacy backtest as validation of
this new rule version.

No new mandatory environment variables or paid AI dependencies are introduced.
