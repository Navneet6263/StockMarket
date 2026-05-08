# WS Service

Run locally:

```bash
npm run dev:ws
```

## AngelOne Tick Stream

The WS service uses AngelOne SmartAPI WebSocket V2 when configured. Browser clients still subscribe with the same message:

```json
{ "type": "subscribe", "symbol": "RELIANCE" }
```

With AngelOne enabled, `live_update` is pushed on every broker tick. If credentials are missing, a token is not found, or the broker stream becomes stale, the service falls back to the existing HTTP polling path.

Env is loaded in this order:

1. repo root `.env`
2. `services/api/.env`
3. `services/ws/.env`

Values in `services/ws/.env` override the earlier files.

Required env:

```env
BROKER_PROVIDER=angelone
ENABLE_ANGELONE_WS=true
ANGELONE_API_KEY=...
ANGELONE_CLIENT_ID=...
ANGELONE_PASSWORD=...
ANGELONE_TOTP_KEY=...
ANGELONE_CA_BUNDLE=certifi
ANGELONE_DISABLE_SSL_VERIFY=false
```

Use `ANGELONE_WS_MODE=1` for the fastest LTP-only stream. Modes `2` and `3` include more fields but are heavier.

Fast fallback tuning:

```env
ANGELONE_WS_STALE_MS=1500
FALLBACK_POLL_INTERVAL_MS=500
FALLBACK_POLL_CONCURRENCY=10
```

For `CERTIFICATE_VERIFY_FAILED` on local Windows, keep `ANGELONE_CA_BUNDLE=certifi` first and restart API/WS. If the local certificate store or proxy still blocks AngelOne, `ANGELONE_DISABLE_SSL_VERIFY=true` can bypass it, but use that only as a local last resort because it disables TLS verification.
