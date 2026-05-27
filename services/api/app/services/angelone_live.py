"""AngelOne Live Services — WebSocket feed, Delivery data, GTT Auto Orders.
Feature flags: ENABLE_LIVE_FEED, ENABLE_AUTO_ORDER, ENABLE_DELIVERY_FETCH
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

ENABLE_LIVE_FEED = os.getenv("ENABLE_LIVE_FEED", "true").lower() == "true"
ENABLE_AUTO_ORDER = os.getenv("ENABLE_AUTO_ORDER", "false").lower() == "true"  # OFF by default — safety
ENABLE_DELIVERY_FETCH = os.getenv("ENABLE_DELIVERY_FETCH", "true").lower() == "true"
AUTO_ORDER_MAX_PER_DAY = int(os.getenv("AUTO_ORDER_MAX_PER_DAY", "3"))
AUTO_ORDER_MAX_CAPITAL_PCT = float(os.getenv("AUTO_ORDER_MAX_CAPITAL_PCT", "5.0"))  # max 5% per trade


def _get_angel_session():
    """Reuse broker_adapter session."""
    from app.services.broker_adapter import get_broker_adapter, AngelOneAdapter
    adapter = get_broker_adapter()
    if isinstance(adapter, AngelOneAdapter) and adapter.is_available():
        return adapter._api
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# A) LIVE WEBSOCKET PRICE FEED
# ═══════════════════════════════════════════════════════════════════════════════

class LivePriceFeed:
    """Real-time price updates via AngelOne WebSocket. Thread-safe."""

    def __init__(self):
        self._prices: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._ws_thread: threading.Thread | None = None
        self._running = False
        self._subscribers: list[Callable] = []
        self._tokens: Dict[str, str] = {}  # symbol -> token

    def get_price(self, symbol: str) -> Dict | None:
        with self._lock:
            return self._prices.get(symbol.upper())

    def get_all_prices(self) -> Dict[str, Dict]:
        with self._lock:
            return dict(self._prices)

    def subscribe(self, callback: Callable[[str, Dict], None]):
        self._subscribers.append(callback)

    def start(self, symbols: list[str]):
        if not ENABLE_LIVE_FEED:
            return
        if self._running:
            # Feed already running — add new symbols dynamically
            self._add_symbols(symbols)
            return
        api = _get_angel_session()
        if not api:
            logger.warning("[LIVE_FEED] AngelOne not available — feed disabled")
            return
        self._running = True
        self._sws = None  # will be set inside _run_ws
        self._ws_thread = threading.Thread(target=self._run_ws, args=(api, symbols), daemon=True, name="live-price-feed")
        self._ws_thread.start()
        logger.info("[LIVE_FEED] Started for %d symbols", len(symbols))

    def _add_symbols(self, symbols: list[str]):
        """Dynamically subscribe new symbols to a running WebSocket."""
        from app.services.broker_adapter import get_broker_adapter
        adapter = get_broker_adapter()
        exchange_token_map: dict[int, list[str]] = {}
        added = 0
        for sym in symbols:
            if sym.upper() in self._tokens:
                continue  # already subscribed
            token, ex_type = self._resolve_token_with_exchange(sym, adapter)
            if token:
                exchange_token_map.setdefault(ex_type, []).append(token)
                self._tokens[sym.upper()] = token
                added += 1
        if added and self._sws:
            try:
                token_list = [
                    {"exchangeType": ex_type, "tokens": tokens}
                    for ex_type, tokens in exchange_token_map.items()
                ]
                self._sws.subscribe("abc123", 1, token_list)
                logger.info("[LIVE_FEED] Dynamically added %d new symbols (total: %d)", added, len(self._tokens))
            except Exception as exc:
                logger.warning("[LIVE_FEED] Dynamic subscribe failed: %s", exc)
        elif added:
            logger.info("[LIVE_FEED] Queued %d symbols (WS not ready yet)", added)

    def stop(self):
        self._running = False

    def _run_ws(self, api, symbols: list[str]):
        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2
            from app.services.broker_adapter import (
                AngelOneAdapter, get_broker_adapter,
                _exchange_segment_for_symbol, _ws_exchange_type,
            )
            auth_token = api.access_token
            feed_token = api.feed_token
            client_code = os.getenv("ANGELONE_CLIENT_ID", "")

            sws = SmartWebSocketV2(auth_token, os.getenv("ANGELONE_API_KEY", ""), client_code, feed_token)
            self._sws = sws  # store reference for dynamic subscriptions

            # Build per-exchange-type token groups (NSE=1, BSE=3) — ALL symbols, no limit
            adapter = get_broker_adapter()
            exchange_token_map: dict[int, list[str]] = {}
            for sym in symbols:  # subscribe to ALL symbols in universe
                token, ex_type = self._resolve_token_with_exchange(sym, adapter)
                if token:
                    exchange_token_map.setdefault(ex_type, []).append(token)
                    self._tokens[sym.upper()] = token

            token_list = [
                {"exchangeType": ex_type, "tokens": tokens}
                for ex_type, tokens in exchange_token_map.items()
            ]
            logger.info("[LIVE_FEED] Resolved %d tokens for WebSocket subscription", len(self._tokens))

            def on_data(wsapp, message):
                try:
                    data = json.loads(message) if isinstance(message, str) else message
                    token = str(data.get("token", ""))
                    symbol = next((s for s, t in self._tokens.items() if t == token), None)
                    if not symbol:
                        return
                    price_data = {
                        "symbol": symbol,
                        "ltp": float(data.get("last_traded_price", 0)) / 100,
                        "volume": int(data.get("volume_trade_for_the_day", 0)),
                        "open": float(data.get("open_price_of_the_day", 0)) / 100,
                        "high": float(data.get("high_price_of_the_day", 0)) / 100,
                        "low": float(data.get("low_price_of_the_day", 0)) / 100,
                        "close": float(data.get("closed_price", 0)) / 100,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    with self._lock:
                        self._prices[symbol] = price_data
                    for cb in self._subscribers:
                        try:
                            cb(symbol, price_data)
                        except Exception:
                            pass
                except Exception:
                    pass

            def on_open(wsapp):
                sws.subscribe("abc123", 1, token_list)  # mode 1 = LTP

            def on_error(wsapp, error):
                logger.warning("[LIVE_FEED] WS error: %s", error)

            def on_close(wsapp):
                logger.info("[LIVE_FEED] WS closed")

            sws.on_data = on_data
            sws.on_open = on_open
            sws.on_error = on_error
            sws.on_close = on_close
            sws.connect()

        except ImportError:
            logger.warning("[LIVE_FEED] SmartWebSocketV2 not available")
        except Exception as exc:
            logger.warning("[LIVE_FEED] WS failed: %s", exc)
            self._running = False

    def _resolve_token_with_exchange(self, symbol: str, adapter=None) -> tuple[str | None, int]:
        """Return (token, ws_exchange_type) for a symbol.

        Detects whether the symbol is an NSE or BSE listing and returns the
        correct AngelOne WebSocket exchangeType (1=NSE, 3=BSE).
        """
        try:
            from app.services.broker_adapter import (
                AngelOneAdapter, get_broker_adapter,
                _exchange_segment_for_symbol, _ws_exchange_type,
                _YF_SUFFIX_NSE, _YF_SUFFIX_BSE,
                _AO_EXCHANGE_NSE, _AO_EXCHANGE_BSE,
            )
            if adapter is None:
                adapter = get_broker_adapter()
            if not isinstance(adapter, AngelOneAdapter):
                return None, 1
            exchange_seg = _exchange_segment_for_symbol(symbol)
            clean = symbol.upper().replace(_YF_SUFFIX_NSE, "").replace(_YF_SUFFIX_BSE, "")
            token = adapter._resolve_token(clean, exchange_seg)
            return token, _ws_exchange_type(exchange_seg)
        except Exception:
            pass
        return None, 1

    def _resolve_token(self, symbol: str) -> str | None:
        """Legacy helper — always resolves on NSE. Use _resolve_token_with_exchange for multi-exchange."""
        token, _ = self._resolve_token_with_exchange(symbol)
        return token


# ═══════════════════════════════════════════════════════════════════════════════
# B) DELIVERY VOLUME FETCH
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_delivery_data(symbol: str, days: int = 5) -> Dict:
    """Fetch delivery volume from AngelOne historical data."""
    if not ENABLE_DELIVERY_FETCH:
        return {"available": False, "reason": "delivery_fetch_disabled"}

    api = _get_angel_session()
    if not api:
        return {"available": False, "reason": "angelone_not_connected"}

    try:
        from app.services.broker_adapter import AngelOneAdapter, get_broker_adapter
        adapter = get_broker_adapter()
        if not isinstance(adapter, AngelOneAdapter):
            return {"available": False, "reason": "not_angelone_adapter"}

        token = adapter._nse_token(symbol)
        if not token:
            return {"available": False, "reason": f"token_not_found_{symbol}"}

        # Fetch trade book for delivery info
        from datetime import timedelta
        now = datetime.now()
        from_date = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        to_date = now.strftime("%Y-%m-%d %H:%M")

        params = {
            "exchange": "NSE",
            "symboltoken": token,
            "interval": "ONE_DAY",
            "fromdate": from_date,
            "todate": to_date,
        }
        resp = api.getCandleData(params)
        if not resp.get("status") or not resp.get("data"):
            return {"available": False, "reason": "no_candle_data"}

        # AngelOne candle data: [datetime, open, high, low, close, volume]
        rows = resp["data"]
        volumes = [int(r[5]) for r in rows if len(r) >= 6]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        latest_vol = volumes[-1] if volumes else 0

        return {
            "available": True,
            "symbol": symbol.upper(),
            "latestVolume": latest_vol,
            "avgVolume5d": round(avg_vol),
            "volumeRatio": round(latest_vol / avg_vol, 2) if avg_vol > 0 else 1.0,
            "dataPoints": len(volumes),
        }
    except Exception as exc:
        logger.warning("[DELIVERY] fetch failed symbol=%s: %s", symbol, exc)
        return {"available": False, "reason": f"error: {exc}"}


# ═══════════════════════════════════════════════════════════════════════════════
# C) GTT AUTO ORDER
# ═══════════════════════════════════════════════════════════════════════════════

class AutoOrderService:
    """Place GTT orders via AngelOne when scanner confirms a setup."""

    def __init__(self):
        self._orders_today: list[Dict] = []
        self._lock = threading.Lock()

    def can_place_order(self) -> tuple[bool, str]:
        if not ENABLE_AUTO_ORDER:
            return False, "auto_order_disabled"
        api = _get_angel_session()
        if not api:
            return False, "angelone_not_connected"
        with self._lock:
            today = datetime.now().date().isoformat()
            today_orders = [o for o in self._orders_today if o.get("date") == today]
            if len(today_orders) >= AUTO_ORDER_MAX_PER_DAY:
                return False, f"max_orders_reached ({AUTO_ORDER_MAX_PER_DAY}/day)"
        return True, "ok"

    def place_gtt_order(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        target: float,
        quantity: int = 1,
    ) -> Dict:
        """Place a GTT (Good Till Triggered) order on AngelOne."""
        can, reason = self.can_place_order()
        if not can:
            return {"success": False, "reason": reason}

        api = _get_angel_session()
        if not api:
            return {"success": False, "reason": "no_session"}

        try:
            from app.services.broker_adapter import AngelOneAdapter, get_broker_adapter
            adapter = get_broker_adapter()
            if not isinstance(adapter, AngelOneAdapter):
                return {"success": False, "reason": "not_angelone"}

            token = adapter._nse_token(symbol)
            if not token:
                return {"success": False, "reason": f"token_not_found_{symbol}"}

            transaction_type = "BUY" if direction == "bullish" else "SELL"

            # GTT Rule creation
            gtt_params = {
                "tradingsymbol": f"{symbol.upper()}-EQ",
                "symboltoken": token,
                "exchange": "NSE",
                "producttype": "DELIVERY",
                "transactiontype": transaction_type,
                "price": round(entry_price, 2),
                "qty": quantity,
                "triggerprice": round(entry_price, 2),
                "disclosedqty": 0,
                "timeperiod": 365,
            }

            resp = api.gttCreateRule(gtt_params)
            if resp.get("status"):
                order_record = {
                    "symbol": symbol.upper(),
                    "direction": direction,
                    "entry": entry_price,
                    "stop_loss": stop_loss,
                    "target": target,
                    "quantity": quantity,
                    "gtt_id": resp.get("data", {}).get("id"),
                    "date": datetime.now().date().isoformat(),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                with self._lock:
                    self._orders_today.append(order_record)
                logger.info("[AUTO_ORDER] GTT placed: %s %s @ %.2f", transaction_type, symbol, entry_price)
                return {"success": True, "order": order_record, "response": resp}
            else:
                return {"success": False, "reason": resp.get("message", "unknown"), "response": resp}

        except Exception as exc:
            logger.warning("[AUTO_ORDER] GTT failed symbol=%s: %s", symbol, exc)
            return {"success": False, "reason": f"error: {exc}"}

    def get_today_orders(self) -> list[Dict]:
        with self._lock:
            today = datetime.now().date().isoformat()
            return [o for o in self._orders_today if o.get("date") == today]


# ═══════════════════════════════════════════════════════════════════════════════
# D) COMBINED SERVICE — single entry point
# ═══════════════════════════════════════════════════════════════════════════════

class AngelOneLiveService:
    """Combined service for live feed + delivery + auto orders."""

    def __init__(self):
        self.feed = LivePriceFeed()
        self.orders = AutoOrderService()

    def start_feed(self, symbols: list[str]):
        self.feed.start(symbols)

    def get_live_price(self, symbol: str) -> Dict | None:
        return self.feed.get_price(symbol)

    def get_all_live_prices(self) -> Dict[str, Dict]:
        return self.feed.get_all_prices()

    def get_delivery(self, symbol: str) -> Dict:
        return fetch_delivery_data(symbol)

    def place_order(self, symbol: str, direction: str, entry: float, sl: float, target: float, qty: int = 1) -> Dict:
        return self.orders.place_gtt_order(symbol, direction, entry, sl, target, qty)

    def get_status(self) -> Dict:
        return {
            "live_feed_running": self.feed._running,
            "live_feed_symbols": len(self.feed._prices),
            "auto_order_enabled": ENABLE_AUTO_ORDER,
            "orders_today": len(self.orders.get_today_orders()),
            "max_orders_per_day": AUTO_ORDER_MAX_PER_DAY,
            "delivery_fetch_enabled": ENABLE_DELIVERY_FETCH,
        }


# Singleton
_live_service: AngelOneLiveService | None = None
_live_lock = threading.Lock()


def get_angelone_live() -> AngelOneLiveService:
    global _live_service
    if _live_service is None:
        with _live_lock:
            if _live_service is None:
                _live_service = AngelOneLiveService()
    return _live_service
