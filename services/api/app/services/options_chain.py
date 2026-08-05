from __future__ import annotations

import os
import asyncio
import logging
import threading
from statistics import median
from datetime import datetime
from typing import Dict, List, Any
from zoneinfo import ZoneInfo

from app.services.angelone_live import get_angelone_live

logger = logging.getLogger(__name__)

# Strict delta filter for ATM/ITM selection
MIN_DELTA_FOR_OPTION_BUY = float(os.getenv("MIN_DELTA_FOR_OPTION_BUY", "0.45"))


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
        self._option_symbol_lookup: Dict[str, tuple[str, Dict[str, Any]]] = {}
        self._data_lock = threading.RLock()
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._background_tasks: Dict[str, asyncio.Task] = {}

        self.feed = get_angelone_live().feed
        self.feed.subscribe(self._on_tick)
        self._initialized = True

    def _resolve_nearest_expiry(self, symbol: str) -> Dict[str, Any]:
        from app.services.broker_adapter import get_broker_adapter, AngelOneAdapter

        adapter = get_broker_adapter()
        if not isinstance(adapter, AngelOneAdapter):
            raise ValueError("Broker adapter must be AngelOne for Options Chain")

        df = adapter._load_instruments()
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
                return None

        today = datetime.now().date()
        parsed_expiries = sorted(
            (parsed, raw)
            for raw in expiries
            if (parsed := parse_date(raw)) is not None and parsed.date() >= today
        )
        if not parsed_expiries:
            raise ValueError(f"No unexpired options found for symbol {symbol}")
        sorted_expiries = [raw for _, raw in parsed_expiries]
        now = datetime.now()
        target_expiry = sorted_expiries[0]

        # Use actual listed expiries. Avoid the final calendar day without
        # guessing which weekday an exchange contract should expire on.
        if len(sorted_expiries) > 1:
            try:
                nearest_date = parse_date(target_expiry)
                days_to_expiry = (nearest_date.date() - now.date()).days

                if days_to_expiry <= 1:
                    logger.info(f"[OPTIONS] Skipping near-expiry {target_expiry} for {symbol} due to Theta/Gamma risk. Selecting next listed expiry.")
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
        external_symbol = str(data.get("symbol") or token_symbol or "").upper()
        with self._data_lock:
            lookup = self._option_symbol_lookup.get(external_symbol)
        if not lookup:
            return
        target_symbol, strike_info = lookup

        strike = str(strike_info["strike"])
        side = strike_info["side"]

        with self._data_lock:
            symbol_chain = self._chain_data.setdefault(target_symbol, {})
            strike_chain = symbol_chain.setdefault(strike, {"CE": {}, "PE": {}})
            existing = strike_chain[side]
            prev_oi = existing.get("oi", 0)
            oi = data.get("open_interest", existing.get("oi", 0))
            vol = data.get("volume", existing.get("volume", 0))
            ltp = data.get("ltp", existing.get("ltp", 0))
            large_change = bool(prev_oi > 0 and oi > prev_oi * 1.2)
            strike_chain[side] = {
                "ltp": ltp,
                "volume": vol,
                "oi": oi,
                "large_oi_change_flag": large_change,
                "large_oi_change_observed_at": data.get("received_at") if large_change else None,
                "participant_identity": "UNKNOWN_FROM_MARKET_DATA",
                "iv": existing.get("iv", 0),
            }

    def _calculate_iv_cross_section_median(self, chain: Dict[str, Dict[str, Any]]) -> float | None:
        """Current-chain IV median; this is not a historical percentile."""

        ivs = []
        for data in chain.values():
            if data.get("CE", {}).get("iv"):
                ivs.append(data["CE"]["iv"])
            if data.get("PE", {}).get("iv"):
                ivs.append(data["PE"]["iv"])
        if not ivs:
            return None
        return round(float(median(ivs)), 2)

    def _calculate_snapshot(self, symbol: str) -> Dict:
        if symbol not in self._chain_data:
            return {}

        strikes = []
        total_ce_oi = 0
        total_pe_oi = 0

        with self._data_lock:
            chain = {
                strike: {side: dict(values) for side, values in sides.items()}
                for strike, sides in self._chain_data[symbol].items()
            }
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
                "ce_large_oi_change_flag": ce.get("large_oi_change_flag", False),
                "pe_large_oi_change_flag": pe.get("large_oi_change_flag", False),
                "participant_identity": "UNKNOWN_FROM_MARKET_DATA",
            })

        spot_data = get_angelone_live().get_live_price(symbol)
        spot_price = spot_data.get("ltp", 0) if spot_data else 0

        expiry_str = self._chain_meta[symbol]["expiry"]
        try:
            ist = ZoneInfo("Asia/Kolkata")
            expiry_date = datetime.strptime(expiry_str, "%d%b%Y").replace(hour=15, minute=30, tzinfo=ist)
            T = max(0.001, (expiry_date - datetime.now(ist)).total_seconds() / (365.25 * 86400))
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

        chain_complete = bool(self._chain_meta[symbol].get("chain_complete"))
        pcr = (total_pe_oi / total_ce_oi) if chain_complete and total_ce_oi > 0 else None

        max_pain = None
        min_loss = float('inf')

        for eval_strike in (
            [s["strike"] for s in strikes]
            if chain_complete and total_ce_oi + total_pe_oi > 0
            else []
        ):
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

        iv_cross_section_median = self._calculate_iv_cross_section_median(chain)
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
            "pcr": round(pcr, 2) if pcr is not None else None,
            "max_pain": max_pain,
            "spot_price": spot_price,
            "days_to_expiry": days_to_expiry,
            "theta_risk": days_to_expiry <= 2,
            "iv_percentile": None,
            "iv_percentile_available": False,
            "iv_cross_section_median": iv_cross_section_median,
            "iv_context_label": "current_chain_median_not_historical_percentile",
            "iv_crush_blocked": False,
            "participant_identity": "UNKNOWN_FROM_MARKET_DATA",
            "chain_complete": chain_complete,
            "chain_data_quality": "complete_live_subset" if chain_complete else "partial_chain_context_only",
            "requested_option_tokens": self._chain_meta[symbol].get("requested_token_count", 0),
            "subscribed_option_tokens": self._chain_meta[symbol].get("subscribed_token_count", 0),
            "strikes": strikes,
        }

    def get_recommended_strike(self, symbol: str, option_side: str) -> float | None:
        """Return an ATM/ITM strike meeting the configured delta filter."""
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
                with self._data_lock:
                    self._chain_meta[symbol] = meta
                    for token_meta in meta["token_map"].values():
                        self._option_symbol_lookup[str(token_meta["symbol"]).upper()] = (
                            symbol,
                            token_meta,
                        )
                resolved_options = {
                    str(token_meta["symbol"]): (str(token), 2)  # AngelOne NSE_FO
                    for token, token_meta in meta["token_map"].items()
                }
                live_feed = get_angelone_live().feed
                added_count = live_feed.add_resolved_symbols(resolved_options)
                meta["requested_token_count"] = len(resolved_options)
                meta["subscribed_token_count"] = added_count
                meta["chain_complete"] = bool(
                    resolved_options and added_count == len(resolved_options)
                )
                live_feed._add_symbols([symbol])
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
        if symbol in self._subscribers and not self._subscribers[symbol]:
            task = self._background_tasks.pop(symbol, None)
            if task:
                task.cancel()
            with self._data_lock:
                self._chain_meta.pop(symbol, None)
                self._chain_data.pop(symbol, None)
                self._option_symbol_lookup = {
                    option_symbol: lookup
                    for option_symbol, lookup in self._option_symbol_lookup.items()
                    if lookup[0] != symbol
                }


def get_live_options_chain() -> LiveOptionsChainService:
    return LiveOptionsChainService()
