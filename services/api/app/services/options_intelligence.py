"""
Options Intelligence Engine — Proprietary Market-Reading Signals
================================================================
4 deep signals that go beyond standard PCR/IV retail filters:

1. OI Change Velocity  — 1-min OI rate of change per strike (institutional build detection)
2. IV Skew Asymmetry   — Put/Call IV imbalance = smart money directional bet
3. Gamma Exposure      — MM hedging acceleration zones (gamma squeeze probability)
4. Max Pain Gravity    — Option writer's magnetic pin zone (expiry gravity band)

Works for: NIFTY, BANKNIFTY, FINNIFTY + individual F&O stocks
"""
from __future__ import annotations

import calendar
import logging
import threading
import time
from collections import deque
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
OI_BUFFER_MINUTES = 5          # keep last 5 snapshots (= 5 mins of 1-min history)
OI_VELOCITY_MIN_STRIKES = 3    # need at least 3 strikes with OI data to emit signal
SKEW_BULLISH_THRESHOLD = 1.15  # CE IV / PE IV > 1.15 = call skew (bullish institutions)
SKEW_BEARISH_THRESHOLD = 0.87  # CE IV / PE IV < 0.87 = put skew (smart money protection)
GAMMA_SQUEEZE_THRESHOLD = 0.08 # gamma > 0.08 at a strike = high-gamma zone
MAX_PAIN_GRAVITY_PCT = 0.01    # ±1% around max pain = gravity band


def get_instrument_expiry_info(symbol: str, raw_expiry_dates: list | None = None) -> dict:
    """
    Returns exact nearest expiry info for Indian indices and F&O stocks:
    Under SEBI regulations:
    - NIFTY 50: Weekly expiry every Tuesday (e.g., 06-Oct-2026)
    - SENSEX:   Weekly expiry every Thursday (e.g., 08-Oct-2026)
    - BANKNIFTY, FINNIFTY, MIDCPNIFTY: Weekly contracts discontinued by SEBI.
      Monthly contracts expire on the last Tuesday of the month.
    - F&O Stocks: Monthly contracts expire on the last Thursday of the month.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    today = now.date()

    # 1. If explicit unexpired expiry dates are provided in the option chain, parse and use the nearest one:
    if raw_expiry_dates:
        parsed_dates = []
        for d_str in raw_expiry_dates:
            for fmt in ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(str(d_str).strip(), fmt).date()
                    if dt >= today:
                        parsed_dates.append((dt, str(d_str)))
                    break
                except ValueError:
                    continue
        if parsed_dates:
            parsed_dates.sort(key=lambda x: x[0])
            nearest_date, original_str = parsed_dates[0]
            days = (nearest_date - today).days
            return {
                "expiry_date": nearest_date.strftime("%d-%b-%Y"),
                "days_to_expiry": max(0, days),
                "is_expiry_today": days == 0,
                "expiry_day_name": nearest_date.strftime("%A"),
            }

    # 2. Precise Exchange Rules based on Symbol:
    sym = symbol.upper().strip()

    def get_last_weekday_of_month(year: int, month: int, target_weekday: int) -> date:
        last_day = calendar.monthrange(year, month)[1]
        last_d = date(year, month, last_day)
        while last_d.weekday() != target_weekday:
            last_d -= timedelta(days=1)
        return last_d

    # NIFTY 50: Weekly Expiry on Tuesday (Weekday 1)
    if sym in ("NIFTY", "NIFTY 50"):
        target_wd = 1  # Tuesday
        today_wd = today.weekday()
        if today_wd == target_wd:
            days = 7 if now.time() > dtime(15, 30) else 0
        else:
            days = (target_wd - today_wd) % 7
        target_date = today + timedelta(days=days)
        return {
            "expiry_date": target_date.strftime("%d-%b-%Y"),
            "days_to_expiry": days,
            "is_expiry_today": days == 0,
            "expiry_day_name": "Tuesday (Weekly)",
        }

    # SENSEX: Weekly Expiry on Thursday (Weekday 3)
    if sym in ("SENSEX", "BSE:SENSEX"):
        target_wd = 3  # Thursday
        today_wd = today.weekday()
        if today_wd == target_wd:
            days = 7 if now.time() > dtime(15, 30) else 0
        else:
            days = (target_wd - today_wd) % 7
        target_date = today + timedelta(days=days)
        return {
            "expiry_date": target_date.strftime("%d-%b-%Y"),
            "days_to_expiry": days,
            "is_expiry_today": days == 0,
            "expiry_day_name": "Thursday (Weekly)",
        }

    # BANKNIFTY, FINNIFTY, MIDCPNIFTY: Monthly Expiry on Last Tuesday of the month
    if sym in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        year = today.year
        month = today.month
        last_tuesday = get_last_weekday_of_month(year, month, 1)

        if last_tuesday < today or (last_tuesday == today and now.time() > dtime(15, 30)):
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
            last_tuesday = get_last_weekday_of_month(year, month, 1)

        days = (last_tuesday - today).days
        return {
            "expiry_date": last_tuesday.strftime("%d-%b-%Y"),
            "days_to_expiry": max(0, days),
            "is_expiry_today": days == 0,
            "expiry_day_name": "Tuesday (Monthly)",
        }

    # F&O Stock: Monthly Expiry (Last Thursday of the current month)
    year = today.year
    month = today.month
    last_thursday = get_last_weekday_of_month(year, month, 3)

    if last_thursday < today or (last_thursday == today and now.time() > dtime(15, 30)):
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        last_thursday = get_last_weekday_of_month(year, month, 3)

    days = (last_thursday - today).days
    return {
        "expiry_date": last_thursday.strftime("%d-%b-%Y"),
        "days_to_expiry": max(0, days),
        "is_expiry_today": days == 0,
        "expiry_day_name": "Thursday (Monthly)",
    }


# ── Rolling OI Snapshot Buffer ───────────────────────────────────────────────
class OIVelocityTracker:
    """
    Stores 1-minute OI snapshots per symbol and computes per-strike velocity.
    Thread-safe singleton per symbol (shared across requests).
    """
    _instances: Dict[str, "OIVelocityTracker"] = {}
    _global_lock = threading.Lock()

    @classmethod
    def get(cls, symbol: str) -> "OIVelocityTracker":
        sym = symbol.upper()
        with cls._global_lock:
            if sym not in cls._instances:
                cls._instances[sym] = cls(sym)
            return cls._instances[sym]

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._lock = threading.Lock()
        # deque of (timestamp_unix, chain_snapshot_dict)
        self._buffer: deque[Tuple[float, Dict]] = deque(maxlen=OI_BUFFER_MINUTES)

    def push_snapshot(self, chain_snapshot: Dict[str, Any]) -> None:
        """Call this every ~1 minute with the latest chain snapshot from options_chain.py"""
        now = time.time()
        with self._lock:
            self._buffer.append((now, chain_snapshot))
        logger.debug("[OI_VELOCITY] %s snapshot pushed, buffer size=%d", self.symbol, len(self._buffer))

    def calculate_velocity(self) -> Dict[str, Any]:
        """
        Compare latest snapshot vs oldest in buffer.
        Returns per-strike OI change rate and top movers.
        """
        with self._lock:
            buf = list(self._buffer)

        if len(buf) < 2:
            return {"available": False, "reason": "insufficient_snapshots", "snapshots_held": len(buf)}

        oldest_ts, oldest_snap = buf[0]
        latest_ts, latest_snap = buf[-1]
        elapsed_min = max((latest_ts - oldest_ts) / 60.0, 0.5)  # at least 30s

        oldest_strikes = {str(s.get("strike", "")): s for s in oldest_snap.get("strikes", [])}
        latest_strikes_list = latest_snap.get("strikes", [])

        if not latest_strikes_list or len(oldest_strikes) < OI_VELOCITY_MIN_STRIKES:
            return {"available": False, "reason": "insufficient_strike_data"}

        velocity_data: List[Dict] = []
        total_ce_oi_added = 0
        total_pe_oi_added = 0

        for s in latest_strikes_list:
            strike_key = str(s.get("strike", ""))
            old = oldest_strikes.get(strike_key, {})

            ce_oi_now = s.get("ce_oi", 0) or 0
            pe_oi_now = s.get("pe_oi", 0) or 0
            ce_oi_old = old.get("ce_oi", 0) or 0
            pe_oi_old = old.get("pe_oi", 0) or 0

            ce_delta = ce_oi_now - ce_oi_old
            pe_delta = pe_oi_now - pe_oi_old
            ce_rate_pct = (ce_delta / ce_oi_old * 100) if ce_oi_old > 1000 else 0.0
            pe_rate_pct = (pe_delta / pe_oi_old * 100) if pe_oi_old > 1000 else 0.0

            total_ce_oi_added += max(0, ce_delta)
            total_pe_oi_added += max(0, pe_delta)

            if abs(ce_delta) >= 1000 or abs(pe_delta) >= 1000:
                velocity_data.append({
                    "strike": s.get("strike"),
                    "ce_oi_added": ce_delta,
                    "pe_oi_added": pe_delta,
                    "ce_rate_pct": round(ce_rate_pct, 2),
                    "pe_rate_pct": round(pe_rate_pct, 2),
                    "net_activity": "CE_BUILD" if ce_delta > pe_delta * 1.5 else
                                   "PE_BUILD" if pe_delta > ce_delta * 1.5 else
                                   "MIXED",
                })

        velocity_data.sort(key=lambda x: max(abs(x["ce_oi_added"]), abs(x["pe_oi_added"])), reverse=True)
        top_movers = velocity_data[:5]

        # Determine overall signal direction
        bullish_strikes = sum(1 for v in velocity_data if v["net_activity"] == "CE_BUILD")
        bearish_strikes = sum(1 for v in velocity_data if v["net_activity"] == "PE_BUILD")

        if total_pe_oi_added > total_ce_oi_added * 1.3 and bearish_strikes >= 1:
            signal = "BEARISH_BUILD"
            strike_str = f"at strike {top_movers[0]['strike']}" if bearish_strikes == 1 and top_movers else f"across {bearish_strikes} strikes"
            label = f"🏦 Smart money adding PE {strike_str} — institutional short building"
            direction = "bearish"
        elif total_ce_oi_added > total_pe_oi_added * 1.3 and bullish_strikes >= 1:
            signal = "BULLISH_BUILD"
            strike_str = f"at strike {top_movers[0]['strike']}" if bullish_strikes == 1 and top_movers else f"across {bullish_strikes} strikes"
            label = f"🏦 Smart money adding CE {strike_str} — institutional long building"
            direction = "bullish"
        elif total_pe_oi_added > 0 or total_ce_oi_added > 0:
            signal = "MIXED"
            label = "⚖️ Mixed OI activity — no clear institutional direction"
            direction = "neutral"
        else:
            signal = "INACTIVE"
            label = "📉 Low OI activity — market makers not positioning actively"
            direction = "neutral"

        confidence = min(100, int(
            max(total_ce_oi_added, total_pe_oi_added) /
            max(max(total_ce_oi_added, total_pe_oi_added) + 1, 1) * 50
            + min(max(bullish_strikes, bearish_strikes) * 10, 50)
        ))

        return {
            "available": True,
            "signal": signal,
            "direction": direction,
            "label": label,
            "confidence": confidence,
            "elapsed_minutes": round(elapsed_min, 1),
            "total_ce_oi_added": total_ce_oi_added,
            "total_pe_oi_added": total_pe_oi_added,
            "top_strikes": top_movers,
            "snapshots_used": len(buf),
        }


# ── Signal 2: IV Skew Asymmetry ─────────────────────────────────────────────
def calculate_iv_skew(chain_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compare IV of equidistant CE and PE strikes around ATM.
    Put skew (PE IV >> CE IV) = institutions buying downside protection → bearish lean.
    Call skew (CE IV >> PE IV) = aggressive call buying → bullish institutional bet.
    """
    strikes = chain_snapshot.get("strikes", [])
    spot = chain_snapshot.get("spot_price", 0)

    if not strikes or spot <= 0:
        return {"available": False, "reason": "no_chain_data"}

    # Find ATM strike
    atm = min(strikes, key=lambda s: abs(s.get("strike", 0) - spot), default=None)
    if not atm:
        return {"available": False, "reason": "no_atm_found"}

    atm_strike = atm.get("strike", spot)

    # Get strikes within ±5% of spot
    window_pct = 0.05
    near_strikes = [s for s in strikes if abs(s.get("strike", 0) - spot) / spot <= window_pct]

    if len(near_strikes) < 4:
        return {"available": False, "reason": "insufficient_near_strikes"}

    # Calculate weighted IV averages for CE and PE
    ce_iv_list, pe_iv_list = [], []
    for s in near_strikes:
        ce_iv = s.get("ce_iv") or s.get("ce_greeks", {}).get("iv", 0)
        pe_iv = s.get("pe_iv") or s.get("pe_greeks", {}).get("iv", 0)
        if ce_iv and ce_iv > 0.01:
            ce_iv_list.append(ce_iv)
        if pe_iv and pe_iv > 0.01:
            pe_iv_list.append(pe_iv)

    if not ce_iv_list or not pe_iv_list:
        return {"available": False, "reason": "no_iv_data"}

    avg_ce_iv = sum(ce_iv_list) / len(ce_iv_list)
    avg_pe_iv = sum(pe_iv_list) / len(pe_iv_list)

    # Skew ratio: CE/PE
    skew_ratio = round(avg_ce_iv / avg_pe_iv, 3) if avg_pe_iv > 0 else 1.0

    if skew_ratio >= SKEW_BULLISH_THRESHOLD:
        signal = "CALL_SKEW"
        direction = "bullish"
        label = f"📈 Call IV elevated ({skew_ratio:.2f}x PE IV) — institutions aggressively buying calls"
        confidence = min(100, int((skew_ratio - 1.0) * 200))
    elif skew_ratio <= SKEW_BEARISH_THRESHOLD:
        signal = "PUT_SKEW"
        direction = "bearish"
        label = f"📉 Put IV elevated ({(1/skew_ratio):.2f}x CE IV) — smart money buying downside protection"
        confidence = min(100, int((1.0 - skew_ratio) * 200))
    else:
        signal = "NEUTRAL_SKEW"
        direction = "neutral"
        label = f"⚖️ Balanced IV skew ({skew_ratio:.2f}) — no strong institutional directional bias"
        confidence = 0

    return {
        "available": True,
        "signal": signal,
        "direction": direction,
        "label": label,
        "confidence": confidence,
        "skew_ratio": skew_ratio,
        "avg_ce_iv_pct": round(avg_ce_iv * 100, 1),
        "avg_pe_iv_pct": round(avg_pe_iv * 100, 1),
        "atm_strike": atm_strike,
        "strikes_analyzed": len(near_strikes),
    }


# ── Signal 3: Gamma Exposure ─────────────────────────────────────────────────
def calculate_gamma_exposure(chain_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Find strikes with very high gamma concentration.
    When price approaches a high-gamma strike, MM delta-hedging creates
    self-fulfilling price acceleration (gamma squeeze).
    Also find the "gamma flip point" — where net gamma changes sign.
    """
    strikes = chain_snapshot.get("strikes", [])
    spot = chain_snapshot.get("spot_price", 0)

    if not strikes or spot <= 0:
        return {"available": False, "reason": "no_chain_data"}

    high_gamma_strikes: List[Dict] = []
    net_gamma_by_strike: List[Tuple[float, float]] = []  # (strike, net_gamma)

    for s in strikes:
        strike = s.get("strike", 0)
        ce_greeks = s.get("ce_greeks", {})
        pe_greeks = s.get("pe_greeks", {})

        ce_gamma = float(ce_greeks.get("gamma", 0) or 0)
        pe_gamma = float(pe_greeks.get("gamma", 0) or 0)
        ce_oi = s.get("ce_oi", 0) or 0
        pe_oi = s.get("pe_oi", 0) or 0

        # Gamma exposure = gamma × OI (in lots) — measures MM hedging pressure
        ce_gex = ce_gamma * ce_oi
        pe_gex = pe_gamma * pe_oi
        net_gex = ce_gex - pe_gex  # positive = calls dominate hedging

        if strike > 0:
            net_gamma_by_strike.append((strike, net_gex))

        # High gamma threshold: either side
        if ce_gamma > GAMMA_SQUEEZE_THRESHOLD or pe_gamma > GAMMA_SQUEEZE_THRESHOLD:
            dist_pct = round(abs(strike - spot) / spot * 100, 2)
            high_gamma_strikes.append({
                "strike": strike,
                "ce_gamma": round(ce_gamma, 4),
                "pe_gamma": round(pe_gamma, 4),
                "ce_gex": round(ce_gex, 0),
                "pe_gex": round(pe_gex, 0),
                "distance_pct": dist_pct,
                "label": f"{'CE' if ce_gamma > pe_gamma else 'PE'} gamma wall at {strike} — fast move if touched",
                "squeeze_probability": "high" if dist_pct < 1.0 else "medium" if dist_pct < 2.0 else "low",
            })

    high_gamma_strikes.sort(key=lambda x: x["distance_pct"])

    # Find gamma flip point (where net gamma crosses zero)
    flip_point = None
    net_gamma_by_strike.sort(key=lambda x: x[0])
    for i in range(len(net_gamma_by_strike) - 1):
        s1, g1 = net_gamma_by_strike[i]
        s2, g2 = net_gamma_by_strike[i + 1]
        if g1 * g2 < 0:  # sign change = zero crossing
            # Linear interpolation for flip point
            flip_point = round(s1 + (s2 - s1) * abs(g1) / (abs(g1) + abs(g2) + 0.001), 0)
            break

    nearest = high_gamma_strikes[0] if high_gamma_strikes else None

    return {
        "available": True,
        "high_gamma_strikes": high_gamma_strikes[:5],
        "flip_point": flip_point,
        "nearest_gamma_wall": nearest,
        "squeeze_imminent": bool(nearest and nearest["distance_pct"] < 0.5),
        "label": (
            f"⚡ Gamma wall at {nearest['strike']} ({nearest['distance_pct']}% away) — expect acceleration"
            if nearest
            else "No significant gamma concentration near spot"
        ),
    }


# ── Signal 4: Max Pain Gravity ───────────────────────────────────────────────
def analyze_max_pain_gravity(chain_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Max pain = strike where option writers (who sold options) lose the least.
    Price tends to gravitate toward max pain as expiry approaches.
    Returns gravity band and pin probability (higher near expiry).
    """
    max_pain = chain_snapshot.get("max_pain")
    spot = chain_snapshot.get("spot_price", 0)
    days_to_expiry = chain_snapshot.get("days_to_expiry", 10)

    if not max_pain or spot <= 0:
        return {"available": False, "reason": "max_pain_or_spot_missing"}

    gravity_low = round(max_pain * (1 - MAX_PAIN_GRAVITY_PCT), 0)
    gravity_high = round(max_pain * (1 + MAX_PAIN_GRAVITY_PCT), 0)
    distance_pct = round(abs(spot - max_pain) / spot * 100, 2)
    spot_in_band = gravity_low <= spot <= gravity_high

    # Pin probability increases as expiry approaches and price is near max pain
    expiry_date_str = chain_snapshot.get("expiry_date") or ""
    if days_to_expiry == 0:
        pin_prob = "very_high"
        if distance_pct < 0.5:
            pin_label = f"🧲 EXPIRY TODAY ({expiry_date_str}) — Pinned to max pain. Extreme theta decay."
        else:
            pin_label = f"🧲 EXPIRY TODAY ({expiry_date_str}) — Max pain gravity strong at ₹{max_pain:.0f}"
    elif days_to_expiry == 1:
        pin_prob = "high"
        pin_label = f"⚡ Expiry Tomorrow ({expiry_date_str}) — Strong magnet to ₹{max_pain:.0f}"
    elif days_to_expiry <= 3:
        pin_prob = "high" if distance_pct < 1.0 else "medium"
        pin_label = f"📌 {days_to_expiry}d to expiry ({expiry_date_str}) — Pulling toward ₹{max_pain:.0f}"
    else:
        pin_prob = "low"
        pin_label = f"📅 {days_to_expiry}d to expiry ({expiry_date_str}) — Max pain at ₹{max_pain:.0f}"

    # Direction from spot to max pain
    if spot > gravity_high:
        gravity_direction = "bearish"
        gravity_label = f"Price above max pain — expect sellers near ₹{gravity_high:.0f}"
    elif spot < gravity_low:
        gravity_direction = "bullish"
        gravity_label = f"Price below max pain — expect buyers near ₹{gravity_low:.0f}"
    else:
        gravity_direction = "neutral"
        gravity_label = f"Price inside max pain gravity band — expiry pin likely near ₹{max_pain:.0f}"

    return {
        "available": True,
        "max_pain": max_pain,
        "gravity_band": [gravity_low, gravity_high],
        "spot": spot,
        "distance_pct": distance_pct,
        "spot_in_band": spot_in_band,
        "pin_probability": pin_prob,
        "pin_label": pin_label,
        "gravity_direction": gravity_direction,
        "gravity_label": gravity_label,
        "days_to_expiry": days_to_expiry,
    }


# ── Combined Verdict ─────────────────────────────────────────────────────────
def synthesize_verdict(
    oi_velocity: Dict,
    iv_skew: Dict,
    gamma_exposure: Dict,
    max_pain_gravity: Dict,
) -> Dict[str, Any]:
    """
    Combine all 4 signals into one actionable verdict.
    Weights: OI velocity 35%, IV skew 30%, gamma 20%, max pain 15%
    """
    direction_votes = {"bullish": 0, "bearish": 0, "neutral": 0}
    total_weight = 0
    weighted_confidence = 0
    reasons = []

    def add(signal_dict: Dict, weight: float, field: str = "direction"):
        nonlocal total_weight, weighted_confidence
        if not signal_dict.get("available"):
            return
        d = signal_dict.get(field, "neutral")
        conf = signal_dict.get("confidence", 0)
        direction_votes[d] = direction_votes.get(d, 0) + weight
        weighted_confidence += conf * weight
        total_weight += weight
        label = signal_dict.get("label", "")
        if label:
            reasons.append(label)

    add(oi_velocity, 0.35, "direction")
    add(iv_skew, 0.30, "direction")
    add(max_pain_gravity, 0.15, "gravity_direction")

    # Gamma: squeeze imminent = amplifier, not direction
    if gamma_exposure.get("available") and gamma_exposure.get("nearest_gamma_wall"):
        total_weight += 0.20
        weighted_confidence += 50 * 0.20  # always adds uncertainty
        gex_label = gamma_exposure.get("label", "")
        if gex_label:
            reasons.append(gex_label)

    if total_weight < 0.3:
        return {
            "direction": "neutral",
            "confidence": 0,
            "action": "WAIT",
            "reason": "Insufficient options data for smart signals",
            "reasons": reasons,
        }

    final_confidence = int(weighted_confidence / max(total_weight, 0.01))
    final_direction = max(direction_votes, key=lambda k: direction_votes[k])

    if final_confidence < 30 or direction_votes[final_direction] < 0.4:
        final_direction = "neutral"

    if final_direction == "bullish" and final_confidence >= 60:
        action = "CE_WATCH"
        action_label = "📈 Watch CE — Smart signals bullish"
    elif final_direction == "bearish" and final_confidence >= 60:
        action = "PE_WATCH"
        action_label = "📉 Watch PE — Smart signals bearish"
    elif final_direction == "bullish":
        action = "CE_BIAS"
        action_label = "🟡 Slight CE bias — confirm with price action"
    elif final_direction == "bearish":
        action = "PE_BIAS"
        action_label = "🟡 Slight PE bias — confirm with price action"
    else:
        action = "WAIT"
        action_label = "⛔ No clear direction — avoid option buying"

    return {
        "direction": final_direction,
        "confidence": final_confidence,
        "action": action,
        "action_label": action_label,
        "reason": reasons[0] if reasons else "Multiple signals analyzed",
        "reasons": reasons[:4],
    }


# ── Master Function ──────────────────────────────────────────────────────────
def run_options_intelligence(
    symbol: str,
    chain_snapshot: Dict[str, Any],
    push_to_buffer: bool = True,
) -> Dict[str, Any]:
    """
    Main entry point — call with current chain snapshot.
    Returns full smart_signals dict ready for API response.
    """
    tracker = OIVelocityTracker.get(symbol)

    if push_to_buffer:
        tracker.push_snapshot(chain_snapshot)

    oi_velocity = tracker.calculate_velocity()
    iv_skew = calculate_iv_skew(chain_snapshot)
    gamma_exposure = calculate_gamma_exposure(chain_snapshot)
    max_pain_gravity = analyze_max_pain_gravity(chain_snapshot)
    verdict = synthesize_verdict(oi_velocity, iv_skew, gamma_exposure, max_pain_gravity)

    return {
        "symbol": symbol.upper(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "oi_velocity": oi_velocity,
        "iv_skew": iv_skew,
        "gamma_exposure": gamma_exposure,
        "max_pain_gravity": max_pain_gravity,
        "verdict": verdict,
    }
