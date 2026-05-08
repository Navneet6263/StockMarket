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

Required env:

```env
BROKER_PROVIDER=angelone
ENABLE_ANGELONE_WS=true
ANGELONE_API_KEY=...
ANGELONE_CLIENT_ID=...
ANGELONE_PASSWORD=...
ANGELONE_TOTP_KEY=...
```

Use `ANGELONE_WS_MODE=1` for the fastest LTP-only stream. Modes `2` and `3` include more fields but are heavier.
