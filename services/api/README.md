# API Service

Run locally:
- `python -m uvicorn app.main:app --reload --port 8000 --app-dir services/api`

Production restart with PM2:
- `pm2 restart stock-market-api`
- If your PM2 process has a different name, run `pm2 list` and restart that API process.

Performance-related environment knobs:
- `SCAN_CACHE_TTL_SEC=120` keeps fresh market overview responses in memory for fast repeat reads.
- `STALE_SCAN_CACHE_TTL_SEC=1800` allows the API to return the last successful scan if a refresh fails or times out.
- `SCAN_REFRESH_TIMEOUT_SEC=45` caps how long HTTP requests wait for a forced refresh.
- `YAHOO_TIMEOUT_SEC=8` caps individual Yahoo/yfinance network calls where supported.
- `YAHOO_BATCH_CHUNK_SIZE=30` limits batch size for yfinance downloads.
- `SCANNER_MAX_WORKERS=8` limits concurrent symbol evaluation.
- `INVALID_SYMBOLS=GMRINFRA,TATAMOTORS` skips known delisted/no-data symbols.

Production smoke test:
- `curl -w "time_total=%{time_total}\n" -o /dev/null -s -D - -H "Origin: https://stock-market-web-eight.vercel.app" "https://172-105-41-9.sslip.io/api/market/overview?force_refresh=false"`
- Expected: `HTTP/1.1 200 OK`, `access-control-allow-origin: https://stock-market-web-eight.vercel.app`, and warm-cache `time_total` usually under 1 second.
- Forced refresh check: `curl -w "time_total=%{time_total}\n" -o /dev/null -s "https://172-105-41-9.sslip.io/api/market/overview?force_refresh=true"` should return before `SCAN_REFRESH_TIMEOUT_SEC`; if refresh is already running or fails, the API returns last successful cached data with a `warning` field.

## Market Intelligence Endpoints

Primary dashboard APIs:
- `GET /api/market/overview`
- `GET /api/market/opportunities`
- `GET /api/market/top-volume`
- `GET /api/market/top-movers`
- `GET /api/scanner/breakouts`
- `GET /api/scanner/bearish-risk`
- `GET /api/stocks/{symbol}`
- `GET /api/stocks/{symbol}/prediction`
- `GET /api/stocks/{symbol}/signals`
- `GET /api/stocks/{symbol}/history`
- `GET /api/evaluation/backtest/{symbol}`

Tracked idea and watchlist APIs:
- `GET /api/tracker/dashboard`
- `GET /api/tracker/stocks/{symbol}`
- `POST /api/tracker/evaluate`
- `POST /api/tracker/manual-watch`
- `PATCH /api/tracker/setups/{setup_id}`
- `POST /api/tracker/setups/{setup_id}/archive`
- `POST /api/tracker/setups/{setup_id}/ignore`

Legacy compatibility endpoints kept for older consumers:
- `GET /historical/{symbol}`
- `GET /live/{symbol}`
- `POST /predict`
- `GET /backtest/{symbol}`

## Architecture Notes

The upgraded backend is organized into:
- `app/services/data_provider.py`: cached live and historical data access
- `app/services/indicators.py`: vectorized indicator and feature engineering
- `app/services/scoring.py`: explainable multi-factor signal engine
- `app/services/backtest.py`: historical evaluation and confidence evidence
- `app/services/market_hub.py`: orchestration for market overview and stock detail
- `app/services/setup_store.py`: SQLite persistence for saved scanner ideas and updates
- `app/services/setup_tracker.py`: watchlist lifecycle, pass/fail evaluation, and review summaries
- `app/routers/*`: small FastAPI route modules instead of one monolithic `main.py`

## Persistent Tracking

The platform now persists promoted scanner ideas into SQLite rather than dropping them when the next scan changes:
- database path is controlled by `TRACK_DB_PATH`
- auto-promotion only saves ideas that pass multi-factor confirmation thresholds
- open setups remain visible until target, invalidation, expiry, ignore, or archive
- daily continuity is driven by status updates such as `Still active`, `Target hit`, `Invalidated`, `No follow-through yet`, `Setup improved`, and `Volume faded`

## Data loaders

`app/data_sources.py` still provides daily bhavcopy loaders for NSE and BSE.
