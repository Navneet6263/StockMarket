from __future__ import annotations

import threading
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import angelone_live
from app.services.angelone_live import LIVE_FEED_MAX_SYMBOLS, LivePriceFeed


class _FakeSocket:
    def __init__(self, sequence: int, connected_event: threading.Event):
        self.sequence = sequence
        self.connected_event = connected_event
        self.subscriptions: list[tuple[str, int, list[dict]]] = []
        self.unsubscriptions: list[tuple[str, int, list[dict]]] = []
        self.on_data = None
        self.on_open = None
        self.on_error = None
        self.on_close = None
        self._closed = threading.Event()
        self._close_notified = False

    def subscribe(self, correlation_id, mode, token_list):
        self.subscriptions.append((correlation_id, mode, token_list))

    def unsubscribe(self, correlation_id, mode, token_list):
        self.unsubscriptions.append((correlation_id, mode, token_list))

    def connect(self):
        self.on_open(self)
        if self.sequence == 0:
            self._notify_close()
            return

        # Emit one real-looking tick so freshness is observable in status.
        first_token = self.subscriptions[0][2][0]["tokens"][0]
        self.on_data(
            self,
            {
                "token": first_token,
                "exchange_type": 1,
                "last_traded_price": 12_345,
                "volume_trade_for_the_day": 99,
                "exchange_timestamp": 1_786_000_000_000,
                "total_buy_quantity": 20,
                "total_sell_quantity": 10,
            },
        )
        self.connected_event.set()
        self._closed.wait(2.0)

    def _notify_close(self):
        if not self._close_notified:
            self._close_notified = True
            self.on_close(self)

    def close_connection(self):
        self._closed.set()
        self._notify_close()


class _ErrorSocket(_FakeSocket):
    def connect(self):
        self.on_open(self)
        self.on_error(self, "simulated_feed_error")


class LiveFeedReconnectTests(unittest.TestCase):
    def _configured_feed(self) -> LivePriceFeed:
        feed = LivePriceFeed()
        feed._get_token_adapter = lambda: object()
        feed._resolve_token_with_exchange = (
            lambda symbol, adapter=None: (f"T-{symbol}", 1)
        )
        feed._backoff_delay = lambda failure_count: 0.001
        return feed

    def test_socket_factory_disables_nested_sdk_reconnect_state(self):
        class FakeSdkSocket:
            def __init__(self, *args):
                self.args = args
                self.MAX_RETRY_ATTEMPT = 9
                self.current_retry_attempt = 4
                self.input_request_dict = {3: {1: ["stale"]}}
                self.RESUBSCRIBE_FLAG = True

        package = types.ModuleType("SmartApi")
        child = types.ModuleType("SmartApi.smartWebSocketV2")
        child.SmartWebSocketV2 = FakeSdkSocket
        with patch.dict(
            sys.modules,
            {"SmartApi": package, "SmartApi.smartWebSocketV2": child},
        ):
            socket = LivePriceFeed()._create_websocket(
                SimpleNamespace(access_token="auth", feed_token="feed")
            )

        self.assertEqual(socket.MAX_RETRY_ATTEMPT, 0)
        self.assertEqual(socket.current_retry_attempt, 0)
        self.assertEqual(socket.input_request_dict, {})
        self.assertFalse(socket.RESUBSCRIBE_FLAG)

    @patch.object(angelone_live, "ENABLE_LIVE_FEED", True)
    @patch.object(
        angelone_live,
        "_get_angel_session",
        return_value=SimpleNamespace(access_token="auth", feed_token="feed"),
    )
    def test_close_reconnects_and_resubscribes_latest_capped_pool(self, _session):
        feed = self._configured_feed()
        second_connected = threading.Event()
        reconnect_waiting = threading.Event()
        allow_reconnect = threading.Event()
        sockets: list[_FakeSocket] = []

        def factory(_api):
            socket = _FakeSocket(len(sockets), second_connected)
            sockets.append(socket)
            return socket

        feed._create_websocket = factory
        requested = [f"SYM{i:03d}" for i in range(LIVE_FEED_MAX_SYMBOLS + 7)]
        rotated = [f"NEW{i:03d}" for i in range(LIVE_FEED_MAX_SYMBOLS + 9)]

        def wait_for_reconnect(_delay):
            reconnect_waiting.set()
            return not allow_reconnect.wait(2.0)

        feed._wait_for_reconnect = wait_for_reconnect

        feed.start(requested)
        self.assertTrue(reconnect_waiting.wait(2.0), "first close was not supervised")
        thread = feed._ws_thread
        feed.start(rotated)  # scanner rotation while disconnected is retained
        self.assertIs(feed._ws_thread, thread)
        allow_reconnect.set()
        self.assertTrue(second_connected.wait(2.0), "reconnected socket did not open")

        feed.start(rotated)  # repeated scanner refresh must reuse the owner
        self.assertIs(feed._ws_thread, thread)
        self.assertEqual(len(sockets), 2)
        self.assertEqual(len(feed._desired_symbols), LIVE_FEED_MAX_SYMBOLS)

        expected_prefixes = ("T-SYM", "T-NEW")
        for socket, expected_prefix in zip(sockets, expected_prefixes):
            self.assertEqual(len(socket.subscriptions), 1)
            subscribed_tokens = [
                token
                for group in socket.subscriptions[0][2]
                for token in group["tokens"]
            ]
            self.assertEqual(len(subscribed_tokens), LIVE_FEED_MAX_SYMBOLS)
            self.assertTrue(subscribed_tokens[0].startswith(expected_prefix))

        self.assertEqual(sockets[0].subscriptions[0][2][0]["tokens"][0], "T-SYM000")
        self.assertEqual(sockets[1].subscriptions[0][2][0]["tokens"][0], "T-NEW000")
        self.assertEqual(
            sockets[1].subscriptions[0][2][0]["tokens"][-1],
            f"T-NEW{LIVE_FEED_MAX_SYMBOLS - 1:03d}",
        )

        status = feed.get_connection_status()
        self.assertTrue(status["running"])
        self.assertTrue(status["connected"])
        self.assertEqual(status["state"], "connected")
        self.assertEqual(status["reconnectAttempts"], 1)
        self.assertEqual(status["lastError"], "websocket_closed")
        self.assertIsNotNone(status["lastTickAt"])
        self.assertIsNotNone(status["lastTickAgeSec"])
        self.assertLess(status["lastTickAgeSec"], 1.0)

        feed.stop()
        self.assertFalse(feed._ws_thread.is_alive())
        stopped = feed.get_connection_status()
        self.assertFalse(stopped["running"])
        self.assertFalse(stopped["connected"])
        self.assertEqual(stopped["state"], "stopped")

    @patch.object(angelone_live, "ENABLE_LIVE_FEED", True)
    @patch.object(
        angelone_live,
        "_get_angel_session",
        return_value=SimpleNamespace(access_token="auth", feed_token="feed"),
    )
    def test_error_is_exposed_and_stop_cancels_pending_reconnect(self, _session):
        feed = self._configured_feed()
        reconnect_waiting = threading.Event()
        sockets: list[_ErrorSocket] = []

        def factory(_api):
            socket = _ErrorSocket(len(sockets), threading.Event())
            sockets.append(socket)
            return socket

        def wait_for_reconnect(_delay):
            reconnect_waiting.set()
            return feed._stop_event.wait(2.0)

        feed._create_websocket = factory
        feed._wait_for_reconnect = wait_for_reconnect
        feed.start(["RBA"])

        self.assertTrue(reconnect_waiting.wait(2.0))
        status = feed.get_connection_status()
        self.assertFalse(status["connected"])
        self.assertEqual(status["state"], "reconnect_wait")
        self.assertEqual(status["lastError"], "simulated_feed_error")

        feed.stop()
        self.assertEqual(len(sockets), 1)
        self.assertFalse(feed._ws_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
