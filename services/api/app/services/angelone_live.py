"""AngelOne Live Services — WebSocket feed, Delivery data, GTT Auto Orders.
Feature flags: ENABLE_LIVE_FEED, ENABLE_AUTO_ORDER, ENABLE_DELIVERY_FETCH
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
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
AUTO_ORDER_CAPITAL = float(os.getenv("AUTO_ORDER_CAPITAL", "0"))
AUTO_ORDER_MAX_ACCOUNT_RISK_PCT = float(os.getenv("AUTO_ORDER_MAX_ACCOUNT_RISK_PCT", "1.0"))
# AngelOne's GTT call below creates the entry rule only.  Keep execution locked
# until the integration can atomically attach broker-native protective exits.
AUTO_ORDER_NATIVE_PROTECTION_READY = False
LIVE_FEED_MAX_SYMBOLS = max(1, int(os.getenv("LIVE_WATCH_MAX", "100")))
LIVE_RECONNECT_BASE_SEC = max(0.1, float(os.getenv("LIVE_RECONNECT_BASE_SEC", "1.0")))
LIVE_RECONNECT_MAX_SEC = min(
    60.0,
    max(LIVE_RECONNECT_BASE_SEC, float(os.getenv("LIVE_RECONNECT_MAX_SEC", "30.0"))),
)


def _get_angel_session():
    """Reuse broker_adapter session."""
    from app.services.broker_adapter import get_broker_adapter, AngelOneAdapter
    adapter = get_broker_adapter()
    if isinstance(adapter, AngelOneAdapter) and adapter.is_available():
        return adapter._api
    return None


def _exchange_tick_timestamp(data: Dict[str, Any]) -> tuple[Any, str | None]:
    """Return the raw exchange event time and a normalized UTC ISO value."""

    raw = data.get("exchange_timestamp")
    if raw in (None, "", 0, "0"):
        raw = data.get("last_traded_timestamp")
    try:
        epoch = float(raw)
        while epoch > 10_000_000_000:
            epoch /= 1000.0
        if epoch > 0:
            return raw, datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        pass
    return raw, None


def _visible_depth_quantities(data: Dict[str, Any]) -> tuple[float, float, str]:
    """Classify best-five quantities by their side flag, not container name."""

    buy_qty = 0.0
    sell_qty = 0.0
    classified = False
    for container in (data.get("best_5_buy_data") or [], data.get("best_5_sell_data") or []):
        for level in container:
            flag = level.get("flag")
            try:
                quantity = float(level.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0.0
            if flag == 0:
                buy_qty += quantity
                classified = True
            elif flag == 1:
                sell_qty += quantity
                classified = True
    if classified:
        return buy_qty, sell_qty, "best_5_flag_classified"
    try:
        buy_qty = float(data.get("total_buy_quantity") or 0)
    except (TypeError, ValueError):
        buy_qty = 0.0
    try:
        sell_qty = float(data.get("total_sell_quantity") or 0)
    except (TypeError, ValueError):
        sell_qty = 0.0
    return buy_qty, sell_qty, "aggregate_queue"


def _broker_open_interest_fields(data: Dict[str, Any]) -> Dict[str, float | int]:
    """Preserve optional SnapQuote OI fields exactly; never manufacture them."""

    fields: Dict[str, float | int] = {}
    raw_oi = data.get("open_interest")
    if raw_oi not in (None, ""):
        try:
            fields["open_interest"] = int(raw_oi)
        except (TypeError, ValueError, OverflowError):
            pass
    raw_change = data.get("open_interest_change_percentage")
    if raw_change not in (None, ""):
        try:
            fields["open_interest_change_percentage_raw"] = float(raw_change)
        except (TypeError, ValueError, OverflowError):
            pass
    return fields


# ═══════════════════════════════════════════════════════════════════════════════
# A) LIVE WEBSOCKET PRICE FEED
# ═══════════════════════════════════════════════════════════════════════════════

class LivePriceFeed:
    """Real-time prices via one supervised, reconnecting WebSocket."""

    def __init__(self):
        self._prices: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._subscription_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._ws_thread: threading.Thread | None = None
        self._sws = None
        self._running = False
        self._connected = False
        self._connection_state = "stopped"
        self._subscribers: list[Callable] = []
        self._tokens: Dict[str, str] = {}  # symbol -> token
        self._token_map: Dict[str, str] = {}  # token -> symbol
        self._token_exchange_map: Dict[tuple[int, str], str] = {}
        self._exchange_types: Dict[str, int] = {}
        self._desired_symbols: list[str] = []
        self._subscribed_symbols: set[str] = set()
        self._reconnect_attempts = 0
        self._last_error: str | None = None
        self._last_tick_at: str | None = None
        self._last_tick_monotonic: float | None = None

    @staticmethod
    def _normalise_symbols(symbols: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in symbols
                if str(symbol or "").strip()
            )
        )[:LIVE_FEED_MAX_SYMBOLS]

    def get_price(self, symbol: str) -> Dict | None:
        with self._lock:
            return self._prices.get(symbol.upper())

    def get_all_prices(self) -> Dict[str, Dict]:
        with self._lock:
            return dict(self._prices)

    def get_connection_status(self) -> Dict[str, Any]:
        """Return a thread-safe health snapshot for the status API."""

        with self._lock:
            tick_monotonic = self._last_tick_monotonic
            status = {
                "running": self._running,
                "connected": self._connected,
                "state": self._connection_state,
                "desiredSymbols": len(self._desired_symbols),
                "subscribedSymbols": len(self._subscribed_symbols),
                "symbolCap": LIVE_FEED_MAX_SYMBOLS,
                "reconnectAttempts": self._reconnect_attempts,
                "lastError": self._last_error,
                "lastTickAt": self._last_tick_at,
            }
        status["lastTickAgeSec"] = (
            round(max(0.0, time.monotonic() - tick_monotonic), 3)
            if tick_monotonic is not None
            else None
        )
        return status

    def subscribe(self, callback: Callable[[str, Dict], None]):
        with self._lock:
            self._subscribers.append(callback)

    def start(self, symbols: list[str]):
        desired = self._normalise_symbols(symbols)
        with self._lock:
            self._desired_symbols = desired
            if not ENABLE_LIVE_FEED:
                self._connection_state = "disabled"
                return

        should_sync = False
        with self._lifecycle_lock:
            thread = self._ws_thread
            if self._running and thread is not None and thread.is_alive():
                should_sync = True
            elif thread is not None and thread.is_alive():
                # Never create a second owner while a stopped supervisor is
                # still unwinding its socket.
                logger.info("[LIVE_FEED] Start deferred while prior supervisor stops")
                return
            else:
                api = _get_angel_session()
                if not api:
                    with self._lock:
                        self._running = False
                        self._connected = False
                        self._connection_state = "unavailable"
                        self._last_error = "angelone_not_available"
                    logger.warning("[LIVE_FEED] AngelOne not available - feed disabled")
                    return
                with self._lock:
                    self._running = True
                    self._connected = False
                    self._connection_state = "starting"
                    self._reconnect_attempts = 0
                    self._last_error = None
                    self._subscribed_symbols.clear()
                self._stop_event.clear()
                self._ws_thread = threading.Thread(
                    target=self._run_ws,
                    args=(api, desired),
                    daemon=True,
                    name="live-price-feed",
                )
                self._ws_thread.start()
                logger.info("[LIVE_FEED] Started for %d symbols", len(desired))

        if should_sync:
            self._sync_symbols(desired)

    def _get_token_adapter(self):
        from app.services.broker_adapter import get_broker_adapter

        return get_broker_adapter()

    def _create_websocket(self, api):
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        sws = SmartWebSocketV2(
            api.access_token,
            os.getenv("ANGELONE_API_KEY", ""),
            os.getenv("ANGELONE_CLIENT_ID", ""),
            api.feed_token,
        )
        # SmartWebSocketV2 has its own synchronous retry/sleep path and keeps
        # some retry/subscription state as class attributes.  This service is
        # the sole reconnect owner, so reset per-socket state and disable the
        # SDK retry to prevent nested sockets or its blocking retry sleep.
        sws.MAX_RETRY_ATTEMPT = 0
        sws.current_retry_attempt = 0
        sws.input_request_dict = {}
        sws.RESUBSCRIBE_FLAG = False
        return sws

    @staticmethod
    def _close_socket(sws) -> None:
        if sws is None:
            return
        for method_name in ("close_connection", "close"):
            method = getattr(sws, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception as exc:
                    logger.debug("[LIVE_FEED] Socket close failed: %s", exc)
                return

    def _backoff_delay(self, failure_count: int) -> float:
        exponent = min(max(0, failure_count - 1), 8)
        base_delay = min(
            LIVE_RECONNECT_MAX_SEC,
            LIVE_RECONNECT_BASE_SEC * (2 ** exponent),
        )
        return min(
            LIVE_RECONNECT_MAX_SEC,
            max(0.05, base_delay * random.uniform(0.8, 1.2)),
        )

    def _wait_for_reconnect(self, delay: float) -> bool:
        """Return true if stop was requested during the bounded wait."""

        return self._stop_event.wait(min(max(0.0, delay), 60.0))

    def _remember_token(self, symbol: str, token: str, exchange_type: int) -> None:
        upper = symbol.upper()
        token_str = str(token)
        with self._lock:
            self._tokens[upper] = token_str
            self._token_map[token_str] = upper
            self._token_exchange_map[(int(exchange_type), token_str)] = upper
            self._exchange_types[upper] = int(exchange_type)

    def _ensure_token_metadata(self, symbols: list[str], adapter=None) -> None:
        adapter = adapter if adapter is not None else self._get_token_adapter()
        for symbol in symbols:
            upper = symbol.upper()
            with self._lock:
                token = self._tokens.get(upper)
                exchange_type = self._exchange_types.get(upper)
            if token and exchange_type is not None:
                continue
            token, exchange_type = self._resolve_token_with_exchange(upper, adapter)
            if token:
                self._remember_token(upper, token, exchange_type)

    def _add_symbols(self, symbols: list[str]):
        """Dynamically subscribe symbols without exceeding the live cap."""

        requested = list(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in symbols
                if str(symbol or "").strip()
            )
        )
        with self._subscription_lock:
            adapter = self._get_token_adapter()
            exchange_token_map: dict[int, list[str]] = {}
            grouped_symbols: dict[int, list[str]] = {}
            with self._lock:
                capacity = max(0, LIVE_FEED_MAX_SYMBOLS - len(self._subscribed_symbols))
            if capacity <= 0:
                return 0

            added = 0
            for upper in requested:
                with self._lock:
                    if upper in self._subscribed_symbols:
                        continue
                    token = self._tokens.get(upper)
                    exchange_type = self._exchange_types.get(upper)
                if not token or exchange_type is None:
                    token, exchange_type = self._resolve_token_with_exchange(upper, adapter)
                if not token:
                    continue
                exchange_type = int(exchange_type)
                self._remember_token(upper, token, exchange_type)
                exchange_token_map.setdefault(exchange_type, []).append(str(token))
                grouped_symbols.setdefault(exchange_type, []).append(upper)
                added += 1
                if added >= capacity:
                    break

            with self._lock:
                sws = self._sws if self._connected else None
            if added and sws:
                try:
                    token_list = [
                        {"exchangeType": exchange_type, "tokens": tokens}
                        for exchange_type, tokens in exchange_token_map.items()
                    ]
                    sws.subscribe("abc123", 3, token_list)
                    with self._lock:
                        if self._sws is sws and self._connected:
                            for grouped in grouped_symbols.values():
                                self._subscribed_symbols.update(grouped)
                    logger.info(
                        "[LIVE_FEED] Added %d symbols (subscribed: %d)",
                        added,
                        len(self._subscribed_symbols),
                    )
                except Exception as exc:
                    with self._lock:
                        self._last_error = f"subscribe_failed: {exc}"
                    logger.warning("[LIVE_FEED] Dynamic subscribe failed: %s", exc)
                    return 0
            elif added:
                # These are supplemental symbols, not the scanner's durable
                # desired pool.  Reporting them as queued would make callers
                # treat an incomplete option chain as live after reconnect.
                logger.info("[LIVE_FEED] Did not add %d supplemental symbols; socket not connected", added)
                return 0
            return added

    def add_resolved_symbols(
        self,
        resolved: Dict[str, tuple[str, int]],
    ) -> int:
        """Add already-resolved broker symbols without slow/wrong cash lookup.

        Used for NFO option contracts whose trading symbols are not NSE cash
        equities.  The shared cap still applies, so options never displace the
        scanner's existing stock subscriptions.
        """

        for symbol, (token, exchange_type) in resolved.items():
            if token:
                self._remember_token(symbol, str(token), int(exchange_type))
        return self._add_symbols(list(resolved))

    def _sync_symbols(self, symbols: list[str]) -> None:
        """Replace the broker subscription set with the current capped pool."""

        desired_list = self._normalise_symbols(symbols)
        desired = set(desired_list)
        with self._subscription_lock:
            with self._lock:
                sws = self._sws if self._connected else None
                removed = sorted(self._subscribed_symbols - desired)

            if removed and sws:
                exchange_token_map: dict[int, list[str]] = {}
                with self._lock:
                    for symbol in removed:
                        token = self._tokens.get(symbol)
                        exchange_type = self._exchange_types.get(symbol)
                        if token and exchange_type is not None:
                            exchange_token_map.setdefault(int(exchange_type), []).append(str(token))
                if exchange_token_map:
                    try:
                        sws.unsubscribe(
                            "abc123",
                            3,
                            [
                                {"exchangeType": exchange_type, "tokens": tokens}
                                for exchange_type, tokens in exchange_token_map.items()
                            ],
                        )
                        with self._lock:
                            if self._sws is sws:
                                self._subscribed_symbols.difference_update(removed)
                    except Exception as exc:
                        with self._lock:
                            self._last_error = f"unsubscribe_failed: {exc}"
                        logger.warning("[LIVE_FEED] Dynamic unsubscribe failed: %s", exc)

            # Failed broker unsubscriptions stay recorded for a later retry.
            with self._lock:
                removable = set(self._tokens) - desired - self._subscribed_symbols
                for symbol in removable:
                    token = self._tokens.pop(symbol, None)
                    exchange_type = self._exchange_types.pop(symbol, None)
                    self._prices.pop(symbol, None)
                    if token and self._token_map.get(token) == symbol:
                        self._token_map.pop(token, None)
                    if token and exchange_type is not None:
                        self._token_exchange_map.pop((int(exchange_type), str(token)), None)

            self._add_symbols(desired_list)
            logger.info(
                "[LIVE_FEED] Synced desired=%d subscribed=%d",
                len(desired),
                len(self._subscribed_symbols),
            )

    def stop(self, wait: bool = True, timeout: float = 2.0):
        """Stop reconnects, close the active socket, and optionally join."""

        with self._lifecycle_lock:
            with self._lock:
                self._running = False
                self._connected = False
                self._connection_state = "stopping"
                sws = self._sws
                thread = self._ws_thread
            self._stop_event.set()
            self._close_socket(sws)

        if (
            wait
            and thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=max(0.0, min(float(timeout), 60.0)))
        with self._lock:
            if thread is None or not thread.is_alive():
                self._connection_state = "stopped"
                self._subscribed_symbols.clear()

    def _handle_tick(self, sws, message) -> None:
        """Parse and publish one tick; deliberately constant-time per tick."""

        try:
            data = json.loads(message) if isinstance(message, str) else message
            if not isinstance(data, dict):
                return
            token = str(data.get("token", ""))
            try:
                exchange_type = int(data.get("exchange_type") or 0)
            except (TypeError, ValueError):
                exchange_type = 0
            with self._lock:
                if self._sws is not sws or not self._connected:
                    return
                symbol = self._token_exchange_map.get((exchange_type, token)) or self._token_map.get(token)
            if not symbol:
                return

            received_at = datetime.now(timezone.utc)
            raw_exchange_timestamp, exchange_time_iso = _exchange_tick_timestamp(data)
            price_data = {
                "symbol": symbol,
                "ltp": float(data.get("last_traded_price", 0)) / 100,
                "volume": int(data.get("volume_trade_for_the_day", 0)),
                "open": float(data.get("open_price_of_the_day", 0)) / 100,
                "high": float(data.get("high_price_of_the_day", 0)) / 100,
                "low": float(data.get("low_price_of_the_day", 0)) / 100,
                "close": float(data.get("closed_price", 0)) / 100,
                "timestamp": exchange_time_iso,
                "exchange_timestamp": raw_exchange_timestamp,
                "last_traded_timestamp": data.get("last_traded_timestamp"),
                "received_at": received_at.isoformat(),
            }
            buy_qty, sell_qty, depth_source = _visible_depth_quantities(data)
            price_data["total_buy_qty"] = buy_qty
            price_data["total_sell_qty"] = sell_qty
            price_data["bid_ask_ratio"] = (
                round(buy_qty / sell_qty, 2)
                if sell_qty > 0
                else (99.0 if buy_qty > 0 else 1.0)
            )
            price_data["depth_source"] = depth_source
            price_data.update(_broker_open_interest_fields(data))

            tick_monotonic = time.monotonic()
            with self._lock:
                self._prices[symbol] = price_data
                self._last_tick_at = received_at.isoformat()
                self._last_tick_monotonic = tick_monotonic
                subscribers = tuple(self._subscribers)
            for callback in subscribers:
                try:
                    callback(symbol, price_data)
                except Exception:
                    logger.debug("[LIVE_FEED] Tick subscriber failed", exc_info=True)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.debug("[LIVE_FEED] Ignored malformed tick: %s", exc)

    def _run_ws(self, api, symbols: list[str]):
        """Own the socket lifecycle and reconnect until explicitly stopped."""

        failure_count = 0
        first_attempt = True
        terminal_state: str | None = None
        try:
            while True:
                with self._lock:
                    if not self._running or self._stop_event.is_set():
                        break

                if not first_attempt:
                    delay = self._backoff_delay(failure_count)
                    with self._lock:
                        self._reconnect_attempts += 1
                        self._connection_state = "reconnect_wait"
                        attempt_number = self._reconnect_attempts
                    logger.info(
                        "[LIVE_FEED] Reconnect attempt %d in %.2fs",
                        attempt_number,
                        delay,
                    )
                    if self._wait_for_reconnect(delay):
                        break
                first_attempt = False

                opened = threading.Event()
                sws = None
                try:
                    with self._lock:
                        if not self._running or self._stop_event.is_set():
                            break
                        self._connected = False
                        self._connection_state = "connecting"
                        self._subscribed_symbols.clear()
                        desired = list(self._desired_symbols)

                    adapter = self._get_token_adapter()
                    self._ensure_token_metadata(desired, adapter)
                    sws = self._create_websocket(api)
                    with self._lock:
                        if not self._running or self._stop_event.is_set():
                            self._close_socket(sws)
                            break
                        self._sws = sws

                    def on_data(wsapp, message):
                        self._handle_tick(sws, message)

                    def on_open(wsapp):
                        with self._lock:
                            if self._sws is not sws or not self._running:
                                return
                            self._connected = True
                            self._connection_state = "connected"
                            desired_now = list(self._desired_symbols)
                        opened.set()
                        self._ensure_token_metadata(desired_now, adapter)
                        self._sync_symbols(desired_now)
                        logger.info(
                            "[LIVE_FEED] Connected; subscribed=%d desired=%d",
                            len(self._subscribed_symbols),
                            len(desired_now),
                        )

                    def on_error(wsapp, error):
                        error_text = str(error or "unknown_websocket_error")
                        with self._lock:
                            if self._sws is not sws:
                                return
                            self._connected = False
                            self._subscribed_symbols.clear()
                            self._last_error = error_text
                            self._connection_state = (
                                "disconnected" if self._running else "stopping"
                            )
                        logger.warning("[LIVE_FEED] WS error: %s", error_text)
                        # Some client versions do not return from connect after
                        # on_error. Closing the current socket lets this sole
                        # supervisor advance to the reconnect loop.
                        self._close_socket(sws)

                    def on_close(wsapp, *args):
                        with self._lock:
                            if self._sws is not sws:
                                return
                            self._connected = False
                            self._subscribed_symbols.clear()
                            if self._running:
                                self._connection_state = "disconnected"
                                if self._last_error is None:
                                    self._last_error = "websocket_closed"
                            else:
                                self._connection_state = "stopping"
                        logger.info("[LIVE_FEED] WS closed")

                    sws.on_data = on_data
                    sws.on_open = on_open
                    sws.on_error = on_error
                    sws.on_close = on_close
                    sws.connect()
                    if not opened.is_set():
                        with self._lock:
                            if self._last_error is None:
                                self._last_error = "connection_ended_before_open"
                        failure_count += 1
                    else:
                        # A completed handshake resets exponential escalation;
                        # its later close retries from the base delay.
                        failure_count = 1
                except ImportError:
                    terminal_state = "unavailable"
                    with self._lock:
                        self._last_error = "smartwebsocketv2_not_available"
                    logger.warning("[LIVE_FEED] SmartWebSocketV2 not available")
                    break
                except Exception as exc:
                    failure_count += 1
                    with self._lock:
                        self._last_error = str(exc)
                        self._connected = False
                        self._connection_state = "disconnected"
                    logger.warning("[LIVE_FEED] WS failed: %s", exc)
                finally:
                    with self._lock:
                        if self._sws is sws:
                            self._sws = None
                        self._connected = False
                        self._subscribed_symbols.clear()
                        if self._running and terminal_state is None:
                            self._connection_state = "disconnected"
        finally:
            with self._lock:
                self._running = False
                self._connected = False
                self._sws = None
                self._subscribed_symbols.clear()
                self._connection_state = terminal_state or "stopped"
            logger.info("[LIVE_FEED] Supervisor stopped")

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
    """Return broker volume context without mislabelling it as delivery.

    AngelOne daily candles expose OHLC plus total traded volume.  They do not
    expose deliverable quantity/percentage, so ``available`` must remain false
    for delivery even when useful traded-volume context is returned.
    """
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

        # Fetch candles for traded-volume context only.
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
            "available": False,
            "deliveryAvailable": False,
            "volumeAvailable": bool(volumes),
            "reason": "broker_candles_contain_traded_volume_only",
            "source": "angelone_daily_candle",
            "symbol": symbol.upper(),
            "latestVolume": latest_vol,
            "avgVolume5d": round(avg_vol),
            "volumeRatio": round(latest_vol / avg_vol, 2) if avg_vol > 0 else 1.0,
            "dataPoints": len(volumes),
            "identityInferenceSupported": False,
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
        if not AUTO_ORDER_NATIVE_PROTECTION_READY:
            return False, "native_protective_exits_not_implemented"
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
        """Validate a plan and place it only when native protection is ready."""
        clean_symbol = str(symbol or "").strip().upper()
        clean_direction = str(direction or "").strip().lower()
        if not re.fullmatch(r"[A-Z0-9&.-]{1,30}", clean_symbol):
            return {"success": False, "reason": "invalid_symbol"}
        if clean_direction not in {"bullish", "bearish"}:
            return {"success": False, "reason": "invalid_direction"}
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            return {"success": False, "reason": "invalid_order_parameters"}
        try:
            entry_price = float(entry_price)
            stop_loss = float(stop_loss)
            target = float(target)
        except (TypeError, ValueError):
            return {"success": False, "reason": "invalid_order_parameters"}
        prices = (entry_price, stop_loss, target)
        if not all(math.isfinite(value) and value > 0 for value in prices):
            return {"success": False, "reason": "invalid_order_parameters"}
        if clean_direction == "bullish" and not stop_loss < entry_price < target:
            return {"success": False, "reason": "invalid_bullish_risk_plan"}
        if clean_direction == "bearish" and not target < entry_price < stop_loss:
            return {"success": False, "reason": "invalid_bearish_risk_plan"}
        if AUTO_ORDER_CAPITAL <= 0:
            return {"success": False, "reason": "auto_order_capital_not_configured"}
        max_notional = AUTO_ORDER_CAPITAL * max(0.0, AUTO_ORDER_MAX_CAPITAL_PCT) / 100.0
        requested_notional = entry_price * quantity
        if max_notional <= 0 or requested_notional > max_notional:
            return {
                "success": False,
                "reason": "auto_order_notional_limit_exceeded",
                "requestedNotional": round(requested_notional, 2),
                "maxAllowedNotional": round(max_notional, 2),
            }
        price_risk = abs(entry_price - stop_loss) * quantity
        max_price_risk = AUTO_ORDER_CAPITAL * max(0.0, AUTO_ORDER_MAX_ACCOUNT_RISK_PCT) / 100.0
        if max_price_risk <= 0 or price_risk > max_price_risk:
            return {
                "success": False,
                "reason": "auto_order_account_risk_limit_exceeded",
                "requestedRisk": round(price_risk, 2),
                "maxAllowedRisk": round(max_price_risk, 2),
            }

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

            token = adapter._nse_token(clean_symbol)
            if not token:
                return {"success": False, "reason": f"token_not_found_{clean_symbol}"}

            transaction_type = "BUY" if clean_direction == "bullish" else "SELL"

            # GTT Rule creation
            gtt_params = {
                "tradingsymbol": f"{clean_symbol}-EQ",
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
                    "symbol": clean_symbol,
                    "direction": clean_direction,
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
                logger.info("[AUTO_ORDER] GTT placed: %s %s @ %.2f", transaction_type, clean_symbol, entry_price)
                return {"success": True, "order": order_record, "response": resp}
            else:
                return {"success": False, "reason": resp.get("message", "unknown"), "response": resp}

        except Exception as exc:
            logger.warning("[AUTO_ORDER] GTT failed symbol=%s: %s", clean_symbol, exc)
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
        feed_status = self.feed.get_connection_status()
        return {
            "live_feed_running": feed_status["running"],
            "live_feed_connected": feed_status["connected"],
            "live_feed_connection_state": feed_status["state"],
            "live_feed_symbols": len(self.feed.get_all_prices()),
            "live_feed_desired_symbols": feed_status["desiredSymbols"],
            "live_feed_subscribed_symbols": feed_status["subscribedSymbols"],
            "live_feed_symbol_cap": LIVE_FEED_MAX_SYMBOLS,
            "live_feed_reconnect_attempts": feed_status["reconnectAttempts"],
            "live_feed_last_error": feed_status["lastError"],
            "live_feed_last_tick_at": feed_status["lastTickAt"],
            "live_feed_last_tick_age_sec": feed_status["lastTickAgeSec"],
            "live_feed_connection": feed_status,
            "auto_order_enabled": ENABLE_AUTO_ORDER,
            "auto_order_execution_ready": AUTO_ORDER_NATIVE_PROTECTION_READY,
            "auto_order_block_reason": (
                None if AUTO_ORDER_NATIVE_PROTECTION_READY
                else "native_protective_exits_not_implemented"
            ),
            "orders_today": len(self.orders.get_today_orders()),
            "max_orders_per_day": AUTO_ORDER_MAX_PER_DAY,
            "broker_volume_context_enabled": ENABLE_DELIVERY_FETCH,
            "broker_delivery_available": False,
            "delivery_fetch_enabled": False,
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
