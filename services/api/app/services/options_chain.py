from __future__ import annotations

import os
import asyncio
import logging
import json
import threading
from datetime import datetime, timezone
from typing import Dict, List, Any

from app.services.angelone_live import get_angelone_live
from app.services.broker_adapter import get_broker_adapter, AngelOneAdapter

logger = logging.getLogger(__name__)

# God-Tier Delta Filter for ATM/ITM selection
MIN_DELTA_FOR_OPTION_BUY = float(os.getenv("MIN_DELTA_FOR_OPTION_BUY", "0.45"))
IV_CRUSH_BLOCK_THRESHOLD = float(os.getenv("IV_CRUSH_BLOCK_THRESHOLD", "60.0"))


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
        self._chain_data: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._chain_meta: Dict[str, Dict[str, Any]] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._background_tasks: Dict[str, asyncio.Task] = {}

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

        options_df = [
            row for row in df
            if row.get("name") == symbol and row.get("exch_seg") == "NFO" and row.get("instrumenttype") in ("OPTSTK", "OPTIDX")
        ]

        if not options_df:
            raise ValueError(f"No options found for symbol {symbol}")

        expiries = set(row.get("expiry") for row in options_df if row.get("expiry"))
        if not expiries:
            raise ValueError(f"No expiries found for symbol {symbol}")

        def parse_date(date_str):
            try:
                return datetime.strptime(date_str, "%d%b%Y")
            except ValueError:
                return datetime.max

        sorted_expiries = sorted(expiries, key=parse_date)
        now = datetime.now()
        target_expiry = sorted_expiries[0]

        # SMART EXPIRY SELECTION: Avoid zero-gamma traps / extreme theta decay
        if len(sorted_expiries) > 1:
            try:
                nearest_date = parse_date(target_expiry)
                days_to_expiry = (nearest_date.date() - now.date()).days

                if days_to_expiry <= 1:
                    is_wednesday_afternoon = now.weekday() == 2 and now.hour >= 12
                    is_thursday = now.weekday() == 3

                    if is_wednesday_afternoon or is_thursday:
                        logger.info(f"[OPTIONS] Skipping current expiry {target_expiry} for {symbol} due to Theta/Gamma risk. Selecting next expiry.")
                        target_expiry = sorted_expiries[1]
            except Exception as e:
                logger.warning(f"Smart expiry logic failed for {symbol}: {e}")

        nearest_expiry = target_expiry

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
            "token_map": token_map,
        }

    def _on_tick(self, token_symbol: str, data: Dict):
        target_symbol = None
        strike_info = None

        for sym, meta in self._chain_meta.items():
            if data["symbol"] in [tm["symbol"] for tm in meta["token_map"].values()]:
                target_symbol = sym
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
            "iv": existing.get("iv", 0),
        }

    def _calculate_iv_percentile(self, symbol: str, spot_price: float, T: float) -> float:
        if symbol not in self._chain_data:
            return 50.0
        ivs = []
        for strike_str, data in self._chain_data[symbol].items():
            if data.get("CE", {}).get("iv"):
                ivs.append(data["CE"]["iv"])
            if data.get("PE", {}).get("iv"):
                ivs.append(data["PE"]["iv"])
        if not ivs:
            return 50.0
        historical_avg = sum(ivs) / len(ivs)
        percentile = min(100, (min(ivs) / (historical_avg + 0.01)) * 50)
        return round(percentile, 1)

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
                "itm": False,
                "institutional_flag": ce.get("institutional_flag", False) or pe.get("institutional_flag", False),
            })

        spot_data = get_angelone_live().get_live_price(symbol)
        spot_price = spot_data.get("ltp", 0) if spot_data else 0

        expiry_str = self._chain_meta[symbol]["expiry"]
        try:
            expiry_date = datetime.strptime(expiry_str, "%d%b%Y").replace(hour=15, minute=30, tzinfo=timezone.utc)
            T = max(0.001, (expiry_date - datetime.now(timezone.utc)).total_seconds() / (365.25 * 86400))
        except Exception:
            T = 0.01

        from app.services.greeks import black_scholes_iv_and_greeks

        for s in strikes:
            if spot_price > 0:
                s["itm"] = (s["strike"] < spot_price and s["ce_ltp"] > 0) or (s["strike"] > spot_price and s["pe_ltp"] > 0)

            if spot_price > 0 and s["ce_ltp"] > 0:
                greeks_ce = black_scholes_iv_and_greeks("CE", spot_price, s["strike"], T, 0.07, s["ce_ltp"])
                s["ce_iv"] = greeks_ce["iv"]
                s["ce_delta"] = greeks_ce["delta"]
                s["ce_greeks"] = greeks_ce
            else:
                s["ce_greeks"] = {"iv": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

            if spot_price > 0 and s["pe_ltp"] > 0:
                greeks_pe = black_scholes_iv_and_greeks("PE", spot_price, s["strike"], T, 0.07, s["pe_ltp"])
                s["pe_iv"] = greeks_pe["iv"]
                s["pe_delta"] = greeks_pe["delta"]
                s["pe_greeks"] = greeks_pe
            else:
                s["pe_greeks"] = {"iv": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

        pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else 1.0

        max_pain = 0
        min_loss = float('inf')

        for eval_strike in [s["strike"] for s in strikes]:
            total_loss = 0
            for s_inner in strikes:
                if eval_strike > s_inner["strike"]:
                    total_loss += (eval_strike - s_inner["strike"]) * s_inner["ce_oi"]
                if eval_strike < s_inner["strike"]:
                    total_loss += (s_inner["strike"] - eval_strike) * s_inner["pe_oi"]

            if total_loss < min_loss:
                min_loss = total_loss
                max_pain = eval_strike

        strikes.sort(key=lambda x: x["strike"])

        iv_percentile = self._calculate_iv_percentile(symbol, spot_price, T)
        days_to_expiry = 0
        try:
            expiry_date = datetime.strptime(expiry_str, "%d%b%Y").replace(hour=15, minute=30)
            days_to_expiry = (expiry_date - datetime.now()).days + 1
        except Exception:
            pass

        return {
            "type": "OPTIONS_SNAPSHOT",
            "symbol": symbol,
            "expiry": expiry_str,
            "pcr": round(pcr, 2),
            "max_pain": max_pain,
            "spot_price": spot_price,
            "days_to_expiry": days_to_expiry,
            "theta_risk": days_to_expiry <= 2,
            "iv_percentile": iv_percentile,
            "iv_crush_blocked": iv_percentile > IV_CRUSH_BLOCK_THRESHOLD,
            "strikes": strikes,
        }

    def get_recommended_strike(self, symbol: str, option_side: str) -> float | None:
        """God-Tier: Return ATM/ITM strike with Delta > 0.45 for optimal option buying."""
        spot_data = get_angelone_live().get_live_price(symbol)
        if not spot_data:
            return None
        spot_price = spot_data.get("ltp", 0)
        if spot_price <= 0:
            return None

        expiry_str = self._chain_meta.get(symbol, {}).get("expiry")
        if not expiry_str:
            return None
        try:
            expiry_date = datetime.strptime(expiry_str, "%d%b%Y")
            T = max(0.001, (expiry_date - datetime.now()).total_seconds() / (365.25 * 86400))
        except Exception:
            T = 0.1

        if symbol not in self._chain_data:
            return None

        from app.services.greeks import black_scholes_iv_and_greeks
        best_strike = None
        best_delta = 0

        side_key = "CE" if option_side == "CE" else "PE"
        for strike_str, data in self._chain_data[symbol].items():
            strike = float(strike_str)
            ltp = data.get(side_key, {}).get("ltp", 0)
            if ltp <= 0:
                continue
            greeks = black_scholes_iv_and_greeks(side_key, spot_price, strike, T, 0.07, ltp)
            delta = greeks.get("delta", 0)
            if abs(delta) >= MIN_DELTA_FOR_OPTION_BUY and abs(delta) > best_delta:
                best_delta = abs(delta)
                best_strike = strike

        return best_strike

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
                get_angelone_live().feed._add_symbols([tm["symbol"] for tm in meta["token_map"].values()])
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