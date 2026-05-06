from __future__ import annotations
import logging
from typing import Dict, List, Optional
import numpy as np

logger = logging.getLogger(__name__)


def _safe(val, default=0.0):
    try:
        if val is None or (isinstance(val, float) and val != val):
            return default
        return float(val)
    except Exception:
        return default


def score_breakout_readiness(snapshot, nifty_bias='neutral'):
    tight = _safe(snapshot.get('tight_consolidation_pct'), 99.0)
    rel_vol = _safe(snapshot.get('relative_volume'), 1.0)
    bb_width = _safe(snapshot.get('bb_width_ratio'), 1.0)
    vcp = 0
    if tight <= 5.0: vcp += 3
    elif tight <= 8.0: vcp += 2
    if rel_vol <= 0.7: vcp += 2
    if snapshot.get('higher_lows'): vcp += 2
    if bb_width <= 0.75: vcp += 2
    vcp = min(vcp, 10)
    cmf = _safe(snapshot.get('cmf'), 0.0)
    obv = _safe(snapshot.get('obv_slope'), 0.0)
    delivery = _safe(snapshot.get('delivery_spike'), 1.0)
    accum = 0
    if cmf >= 0.10: accum += 3
    elif cmf >= 0.04: accum += 2
    if obv > 0: accum += 2
    if delivery >= 1.3: accum += 2
    if snapshot.get('above_vwap'): accum += 1
    accum = min(accum, 10)
    dist = _safe(snapshot.get('distance_to_resistance_pct'), 99.0)
    broke = bool(snapshot.get('breakout_20'))
    chg = _safe(snapshot.get('change_pct'), 0.0)
    prox = 0
    if not broke and abs(chg) < 5.0:
        if dist <= 1.0: prox += 5
        elif dist <= 2.0: prox += 4
        elif dist <= 3.0: prox += 3
        elif dist <= 5.0: prox += 2
        rsi = _safe(snapshot.get('rsi'), 50.0)
        if 45 <= rsi <= 65: prox += 2
    prox = min(prox, 10)
    trend = 0
    if snapshot.get('price_above_ema20') and snapshot.get('price_above_ema50'): trend += 3
    if snapshot.get('price_above_ema200'): trend += 2
    if snapshot.get('trend_regime') in ('uptrend', 'strong_uptrend'): trend += 2
    rs = _safe(snapshot.get('relative_strength_20d'), 0.0)
    if rs >= 2.0: trend += 2
    if nifty_bias == 'bullish': trend += 1
    elif nifty_bias == 'bearish': trend -= 3
    trend = max(0, min(trend, 10))
    score = max(0, min(int(vcp * 2.5 + accum * 2.5 + prox * 3.0 + trend * 2.0), 100))
    if score >= 75:
        readiness, label = 'imminent', 'Breakout Imminent (1-2 sessions)'
    elif score >= 60:
        readiness, label = 'forming', 'Setup Forming (2-5 sessions)'
    elif score >= 45:
        readiness, label = 'early', 'Early Stage (watch only)'
    else:
        readiness, label = 'not_ready', 'Not Ready'
    return {
        'breakout_readiness_score': score,
        'breakout_readiness': readiness,
        'breakout_readiness_label': label,
        'vcp_score': vcp,
        'accumulation_score': accum,
        'proximity_score': prox,
        'trend_alignment_score': trend,
        'is_breakout_candidate': score >= 60,
        'is_imminent_breakout': score >= 75,
    }


def build_breakout_radar(all_signals, nifty_context=None, max_results=10):
    nifty_bias = (nifty_context or {}).get('nifty_bias', 'neutral')
    nifty_level = (nifty_context or {}).get('level_signal', 'mid_range')
    nifty_regime = (nifty_context or {}).get('nifty_regime', 'unknown')
    if nifty_regime in ('strong_downtrend', 'downtrend'):
        max_results = max(3, max_results // 2)
    candidates = []
    for signal in all_signals:
        if signal.get('direction', 'neutral') != 'bullish':
            continue
        if signal.get('breakout_20') or abs(_safe(signal.get('change_pct'), 0)) >= 5.0:
            continue
        resistance = _safe(signal.get('resistance_20'), 0.0)
        price = _safe(signal.get('price') or signal.get('current_price'), 0.0)
        if not resistance or resistance <= price:
            continue
        r = score_breakout_readiness(signal, nifty_bias)
        if not r['is_breakout_candidate']:
            continue
        atr_pct = _safe(signal.get('atr_pct'), 2.0)
        support = _safe(signal.get('support_20'), price * 0.95)
        ema20 = _safe(signal.get('ema_20'), 0.0)
        ema50 = _safe(signal.get('ema_50'), 0.0)
        trigger = round(resistance * 1.002, 2)
        stops = [v for v in (support, ema20, ema50) if 0 < v < price]
        stop = round(max(stops) if stops else price * 0.95, 2)
        base_h = max(_safe(signal.get('tight_consolidation_pct'), 5.0), atr_pct * 1.5)
        t1 = round(trigger * (1 + base_h / 100), 2)
        t2 = round(trigger * (1 + base_h * 1.6 / 100), 2)
        sd = trigger - stop
        rr = round((t1 - trigger) / sd, 2) if sd > 0 else 0.0
        dist = _safe(signal.get('distance_to_resistance_pct'), 99.0)
        reasons = []
        if r['vcp_score'] >= 6:
            reasons.append('Classic VCP base - tight range with volume dry-up and higher lows.')
        elif r['vcp_score'] >= 4:
            reasons.append('Base forming with controlled volatility.')
        if r['accumulation_score'] >= 6:
            reasons.append('Strong accumulation - CMF and OBV both positive.')
        elif r['accumulation_score'] >= 3:
            reasons.append('Quiet accumulation visible in money flow data.')
        if dist <= 1.5:
            reasons.append(f'Price coiling at resistance {resistance:.2f} - trigger imminent.')
        elif dist <= 3.0:
            reasons.append(f'Price is {dist:.1f}% from resistance {resistance:.2f}.')
        if signal.get('higher_lows'):
            reasons.append('Higher lows confirm buyers stepping in.')
        rs = _safe(signal.get('relative_strength_20d'), 0.0)
        if rs >= 2.0:
            reasons.append(f'Outperforming Nifty by {rs:.1f}% - market leader.')
        if nifty_bias == 'bullish' and nifty_level in ('on_support', 'near_support'):
            nifty_note = 'Nifty at support in uptrend - ideal timing for breakout entries.'
        elif nifty_bias == 'bullish':
            nifty_note = 'Nifty uptrend provides tailwind.'
        elif nifty_bias == 'bearish':
            nifty_note = 'Caution: Nifty is weak. Only take very clean setups.'
        else:
            nifty_note = 'Nifty sideways - stock needs its own catalyst.'
        candidates.append({
            'symbol': signal.get('symbol', ''),
            'current_price': round(price, 2),
            'trigger_price': trigger,
            'stop_loss': stop,
            'target_1': t1,
            'target_2': t2,
            'risk_reward': rr,
            'resistance_level': round(resistance, 2),
            'support_level': stop,
            'breakout_readiness_score': r['breakout_readiness_score'],
            'breakout_readiness': r['breakout_readiness'],
            'breakout_readiness_label': r['breakout_readiness_label'],
            'vcp_score': r['vcp_score'],
            'accumulation_score': r['accumulation_score'],
            'proximity_score': r['proximity_score'],
            'trend_alignment_score': r['trend_alignment_score'],
            'distance_to_trigger_pct': round(dist, 2),
            'reasons': reasons[:4],
            'nifty_note': nifty_note,
            'setup_type': 'VCP Breakout' if r['vcp_score'] >= 6 else 'Base Breakout',
            'timeframe': '1-2 sessions' if r['is_imminent_breakout'] else '2-5 sessions',
            'direction': 'bullish',
            'rsi': _safe(signal.get('rsi'), 50.0),
            'volume_ratio': _safe(signal.get('relative_volume'), 1.0),
            'quality_grade': signal.get('quality_grade', 'C'),
            'sector': signal.get('sector', ''),
        })
    candidates.sort(
        key=lambda x: (x['breakout_readiness_score'], -x['distance_to_trigger_pct']),
        reverse=True,
    )
    return candidates[:max_results]
