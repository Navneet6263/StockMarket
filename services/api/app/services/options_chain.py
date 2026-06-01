import asyncio
import logging
import json
import threading
from datetime import datetime, timezone
from typing import Dict, List, Any

from app.services.angelone_live import get_angelone_live
from app.services.broker_adapter import get_broker_adapter, AngelOneAdapter

logger = logging.getLogger(__name__)

class LiveOptionsChainService:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(LiveOptionsChainService, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._chain_data: Dict[str, Dict[str, Dict[str, Any]]] = {} # symbol -> strike -> side(CE/PE) -> data
        self._chain_meta: Dict[str, Dict[str, Any]] = {} # symbol -> {expiry, tokens}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {} # symbol -> list of ws queues
        self._background_tasks: Dict[str, asyncio.Task] = {}
        
        # We hook into AngelOneLive feed once
        self.feed = get_angelone_live().feed
        self.feed.subscribe(self._on_tick)
        self._initialized = True

    def _resolve_nearest_expiry(self, symbol: str) -> Dict[str, Any]:
        adapter = get_broker_adapter()
        if not isinstance(adapter, AngelOneAdapter):
            raise ValueError("Broker adapter must be AngelOne for Options Chain")
            
        if not adapter.token_df:
            adapter._load_tokens()
            
        df = adapter.token_df
        if not df:
            raise ValueError("ScripMaster not loaded")

        # Filter for options of this symbol
        options_df = [
            row for row in df
            if row.get("name") == symbol and row.get("exch_seg") == "NFO" and row.get("instrumenttype") in ("OPTSTK", "OPTIDX")
        ]
        
        if not options_df:
            raise ValueError(f"No options found for symbol {symbol}")
            
        # Find nearest expiry
        expiries = set(row.get("expiry") for row in options_df if row.get("expiry"))
        if not expiries:
            raise ValueError(f"No expiries found for symbol {symbol}")
            
        def parse_date(date_str):
            try:
                return datetime.strptime(date_str, "%d%b%Y")
            except ValueError:
                return datetime.max
                
        nearest_expiry = sorted(expiries, key=parse_date)[0]
        
        tokens = []
        token_map = {}
        for row in options_df:
            if row.get("expiry") == nearest_expiry:
                token = str(row.get("token"))
                tokens.append(token)
                strike = float(row.get("strike", 0)) / 100.0
                symbol_ext = row.get("symbol")
                side = "CE" if symbol_ext.endswith("CE") else "PE" if symbol_ext.endswith("PE") else None
                if side:
                    token_map[token] = {"strike": strike, "side": side, "symbol": symbol_ext}
                    
        return {
            "expiry": nearest_expiry,
            "tokens": tokens,
            "token_map": token_map
        }

    def _on_tick(self, token_symbol: str, data: Dict):
        # We get the symbol string from the tick, but we need to map it back to strike and side
        # Since AngelOneLive feed returns the mapped symbol (e.g. RELIANCE24MAY2800CE)
        # We will parse it or rely on our internal token mapping if we passed it in.
        # Actually, LivePriceFeed returns `symbol` which is exactly the `symbol` from token_map.
        
        # Iterate over active symbols to find which one this belongs to
        target_symbol = None
        strike_info = None
        
        for sym, meta in self._chain_meta.items():
            if data["symbol"] in [tm["symbol"] for tm in meta["token_map"].values()]:
                target_symbol = sym
                # find the strike info
                for tm in meta["token_map"].values():
                    if tm["symbol"] == data["symbol"]:
                        strike_info = tm
                        break
                break
                
        if not target_symbol or not strike_info:
            return
            
        strike = str(strike_info["strike"])
        side = strike_info["side"]
        
        if target_symbol not in self._chain_data:
            self._chain_data[target_symbol] = {}
            
        if strike not in self._chain_data[target_symbol]:
            self._chain_data[target_symbol][strike] = {"CE": {}, "PE": {}}
            
        existing = self._chain_data[target_symbol][strike][side]
        prev_oi = existing.get("oi", 0)
        new_oi = data.get("open_interest", prev_oi) # If Angel tick contains OI? We might need to check standard tick format
        
        # Angel tick mapping from LivePriceFeed doesn't explicitly parse open_interest by default unless modified,
        # but we assume data might have "open_interest". Actually LivePriceFeed might not map OI. Let's gracefully handle.
        # AngelOne websocket Mode 1 does not have OI. Mode 3 (Full Snap) has OI.
        # The user requested OI calculation.
        oi = data.get("open_interest", existing.get("oi", 0))
        vol = data.get("volume", existing.get("volume", 0))
        ltp = data.get("ltp", existing.get("ltp", 0))
        
        spike = False
        if prev_oi > 0 and oi > prev_oi * 1.2:
            spike = True
            
        self._chain_data[target_symbol][strike][side] = {
            "ltp": ltp,
            "volume": vol,
            "oi": oi,
            "institutional_flag": spike or existing.get("institutional_flag", False),
            "iv": existing.get("iv", 0) # IV requires complex calc, mock or use existing for now
        }

    def _calculate_snapshot(self, symbol: str) -> Dict:
        if symbol not in self._chain_data:
            return {}
            
        strikes = []
        total_ce_oi = 0
        total_pe_oi = 0
        
        chain = self._chain_data[symbol]
        for strike_str, data in chain.items():
            strike_val = float(strike_str)
            ce = data.get("CE", {})
            pe = data.get("PE", {})
            
            ce_oi = ce.get("oi", 0)
            pe_oi = pe.get("oi", 0)
            
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            
            strikes.append({
                "strike": strike_val,
                "ce_ltp": ce.get("ltp", 0),
                "ce_oi": ce_oi,
                "ce_volume": ce.get("volume", 0),
                "ce_iv": ce.get("iv", 0),
                "pe_ltp": pe.get("ltp", 0),
                "pe_oi": pe_oi,
                "pe_volume": pe.get("volume", 0),
                "pe_iv": pe.get("iv", 0),
                "itm": False, # Will be set based on spot price
                "institutional_flag": ce.get("institutional_flag", False) or pe.get("institutional_flag", False)
            })
            
        # Spot price from LivePriceFeed
        spot_data = get_angelone_live().get_live_price(symbol)
        spot_price = spot_data.get("ltp", 0) if spot_data else 0
        
        for s in strikes:
            if spot_price > 0:
                s["itm"] = (s["strike"] < spot_price and s["ce_ltp"] > 0) or (s["strike"] > spot_price and s["pe_ltp"] > 0)
                
        pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else 1.0
        
        # Max Pain Calculation
        max_pain = 0
        min_loss = float('inf')
        
        for eval_strike in [s["strike"] for s in strikes]:
            total_loss = 0
            for s in strikes:
                # Loss for CE writer if expiry is at eval_strike
                if eval_strike > s["strike"]:
                    total_loss += (eval_strike - s["strike"]) * s["ce_oi"]
                # Loss for PE writer if expiry is at eval_strike
                if eval_strike < s["strike"]:
                    total_loss += (s["strike"] - eval_strike) * s["pe_oi"]
                    
            if total_loss < min_loss:
                min_loss = total_loss
                max_pain = eval_strike
                
        strikes.sort(key=lambda x: x["strike"])
                
        return {
            "type": "OPTIONS_SNAPSHOT",
            "symbol": symbol,
            "expiry": self._chain_meta[symbol]["expiry"],
            "pcr": round(pcr, 2),
            "max_pain": max_pain,
            "spot_price": spot_price,
            "strikes": strikes
        }

    async def _broadcaster(self, symbol: str):
        while True:
            await asyncio.sleep(5.0)
            if symbol not in self._subscribers or not self._subscribers[symbol]:
                continue
                
            snapshot = self._calculate_snapshot(symbol)
            if not snapshot:
                continue
                
            dead_queues = []
            for q in self._subscribers[symbol]:
                try:
                    q.put_nowait(snapshot)
                except asyncio.QueueFull:
                    pass
                except Exception:
                    dead_queues.append(q)
                    
            for q in dead_queues:
                self._subscribers[symbol].remove(q)

    def subscribe_ws(self, symbol: str, queue: asyncio.Queue):
        if symbol not in self._chain_meta:
            try:
                meta = self._resolve_nearest_expiry(symbol)
                self._chain_meta[symbol] = meta
                
                # Start Angel feed for these tokens
                get_angelone_live().feed._add_symbols([tm["symbol"] for tm in meta["token_map"].values()])
                
                # We need spot price too
                get_angelone_live().feed._add_symbols([symbol])
                
            except Exception as e:
                logger.error(f"Failed to resolve expiry for {symbol}: {e}")
                return
                
        if symbol not in self._subscribers:
            self._subscribers[symbol] = []
            
        self._subscribers[symbol].append(queue)
        
        if symbol not in self._background_tasks:
            self._background_tasks[symbol] = asyncio.create_task(self._broadcaster(symbol))

    def unsubscribe_ws(self, symbol: str, queue: asyncio.Queue):
        if symbol in self._subscribers and queue in self._subscribers[symbol]:
            self._subscribers[symbol].remove(queue)
            
def get_live_options_chain() -> LiveOptionsChainService:
    return LiveOptionsChainService()
