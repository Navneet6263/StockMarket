"""Fyers Adapter for ultra-fast Options WebSockets.
Requires: pip install fyers-apiv3
Expects: FYERS_ACCESS_TOKEN in env.
"""
import os
import logging
import json
import threading
from typing import Callable, Dict

logger = logging.getLogger(__name__)

FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN", "")

class FyersOptionsLiveFeed:
    """Fyers WebSocket feed strictly for Options Data."""
    
    def __init__(self):
        self._subscribers = []
        self._ws = None
        self._running = False
        self._lock = threading.Lock()
        
    def subscribe(self, callback: Callable[[str, Dict], None]):
        self._subscribers.append(callback)

    def start(self, fyers_symbols: list[str]):
        if not FYERS_ACCESS_TOKEN:
            logger.warning("[FYERS] FYERS_ACCESS_TOKEN not set. Fyers feed disabled.")
            return

        try:
            from fyers_apiv3.FyersWebsocket import data_ws
            
            def on_message(message):
                if not isinstance(message, list):
                    return
                for item in message:
                    if "symbol" in item:
                        # Convert Fyers dict to AngelOne format for seamless integration
                        data = {
                            "symbol": item["symbol"].split(":")[1] if ":" in item["symbol"] else item["symbol"],
                            "ltp": float(item.get("ltp", 0)),
                            "volume": int(item.get("vol_traded_today", 0)),
                            "open_interest": int(item.get("oi", 0)),
                        }
                        for cb in self._subscribers:
                            try:
                                cb(data["symbol"], data)
                            except Exception as e:
                                logger.error(f"[FYERS] Callback error: {e}")

            def on_error(message):
                logger.error(f"[FYERS] WS Error: {message}")

            def on_close(message):
                logger.info(f"[FYERS] WS Closed: {message}")

            def on_open():
                logger.info("[FYERS] WS Opened. Subscribing to symbols...")
                if fyers_symbols:
                    self._ws.subscribe(symbols=fyers_symbols, data_type="SymbolUpdate")
                    
            self._ws = data_ws.FyersDataSocket(
                access_token=FYERS_ACCESS_TOKEN,
                log_path="./logs",
                litemode=False,
                write_to_file=False,
                reconnect=True,
                on_connect=on_open,
                on_close=on_close,
                on_error=on_error,
                on_message=on_message
            )
            
            def run_ws():
                try:
                    self._ws.connect()
                except Exception as e:
                    logger.error(f"[FYERS] WS Crash: {e}")
                    
            threading.Thread(target=run_ws, daemon=True, name="fyers-ws").start()
            self._running = True
            
        except ImportError:
            logger.error("[FYERS] fyers_apiv3 not installed. Fallback to AngelOne.")
            
    def subscribe_symbols(self, fyers_symbols: list[str]):
        if self._ws and self._running and fyers_symbols:
            self._ws.subscribe(symbols=fyers_symbols, data_type="SymbolUpdate")

def get_fyers_feed() -> FyersOptionsLiveFeed:
    if not hasattr(get_fyers_feed, "feed"):
        get_fyers_feed.feed = FyersOptionsLiveFeed()
    return get_fyers_feed.feed
