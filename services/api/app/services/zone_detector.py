import pandas as pd
import numpy as np
from typing import List, Dict

class ZoneDetector:
    def __init__(self):
        self.body_threshold = 0.5  # Base candle if body < 50% of range

    def _classify_candles(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['range'] = df['high'] - df['low']
        df['body'] = abs(df['close'] - df['open'])
        df['range'] = df['range'].replace(0, 0.0001)
        df['body_pct'] = df['body'] / df['range']
        df['is_exciting'] = df['body_pct'] > self.body_threshold
        df['is_base'] = df['body_pct'] <= self.body_threshold
        df['color'] = np.where(df['close'] >= df['open'], 'green', 'red')
        return df

    def detect_zones(self, history: pd.DataFrame, max_lookback: int = 150) -> List[Dict]:
        try:
            if history is None or history.empty or len(history) < 3:
                return []
            
            # Drop rows with None/NaN in OHLC columns before processing
            history = history.dropna(subset=['open', 'high', 'low', 'close'])
            if len(history) < 3:
                return []
                
            df = self._classify_candles(history.tail(max_lookback)).reset_index()
            zones = []
            
            i = 1
            while i < len(df) - 1:
                # We are looking for a base candle
                if df.iloc[i]['is_base']:
                    base_start = i
                    base_end = i
                    
                    # Group consecutive base candles
                    while base_end + 1 < len(df) and df.iloc[base_end + 1]['is_base']:
                        base_end += 1
                    
                    # We need a leg-in (candle before base_start) and leg-out (candle after base_end)
                    if base_start > 0 and base_end + 1 < len(df):
                        leg_in = df.iloc[base_start - 1]
                        leg_out = df.iloc[base_end + 1]
                        
                        if leg_in['is_exciting'] and leg_out['is_exciting']:
                            base_candles = df.iloc[base_start:base_end+1]
                            
                            # GARBAGE ZONE FILTER: Skip if > 6 base candles (too much chop)
                            if len(base_candles) > 6:
                                i = base_end + 1
                                continue
                                
                            # DEMAND ZONE
                            if leg_out['color'] == 'green':
                                # Highest body of ALL base candles
                                highest_body = max([max(row['open'], row['close']) for _, row in base_candles.iterrows()])
                                # Exceptional Zone Marking: Extend distal to leg-in or leg-out wicks if they are lower
                                base_lowest_wick = min([row['low'] for _, row in base_candles.iterrows()])
                                lowest_wick = min(base_lowest_wick, leg_in['low'], leg_out['low'])
                                
                                pattern = "DBR" if leg_in['color'] == 'red' else "RBR"
                                
                                # Reversal (DBR) is stronger than Continuation (RBR)
                                base_strength = 2 if pattern == "DBR" else 1
                                
                                # Closing Strength: Did it close near the high?
                                leg_out_range = max(leg_out['range'], 0.0001)
                                close_strength = (leg_out['close'] - leg_out['low']) / leg_out_range
                                if close_strength > 0.8:
                                    base_strength += 2  # Strong bullish close
                                    
                                # Gap Detection: Did the leg-out gap up above the base?
                                highest_base_high = max([row['high'] for _, row in base_candles.iterrows()])
                                if leg_out['open'] > highest_base_high:
                                    base_strength += 3  # Gap + Base
                                
                                # Add strength for explosive leg-out (multiple exciting candles)
                                consecutive_exciting = 1
                                for j in range(base_end + 2, min(len(df), base_end + 5)):
                                    if df.iloc[j]['is_exciting'] and df.iloc[j]['color'] == 'green':
                                        consecutive_exciting += 1
                                    else:
                                        break
                                        
                                # GARBAGE ZONE FILTER: Require strong displacement
                                # If only 1 leg-out and its body is smaller than the base range, skip
                                base_range_max = max([(row['high'] - row['low']) for _, row in base_candles.iterrows()])
                                if consecutive_exciting == 1 and leg_out['body'] < base_range_max * 1.2:
                                    i = base_end + 1
                                    continue
                                        
                                zones.append({
                                    "type": "demand",
                                    "pattern": pattern,
                                    "proximal": highest_body,
                                    "distal": lowest_wick,
                                    "start_idx": base_start,
                                    "end_idx": base_end,
                                    "strength": base_strength + consecutive_exciting,
                                    "is_tested": False
                                })
                                
                            # SUPPLY ZONE
                            elif leg_out['color'] == 'red':
                                # Lowest body of ALL base candles
                                lowest_body = min([min(row['open'], row['close']) for _, row in base_candles.iterrows()])
                                # Exceptional Zone Marking: Extend distal to leg-in or leg-out wicks if they are higher
                                base_highest_wick = max([row['high'] for _, row in base_candles.iterrows()])
                                highest_wick = max(base_highest_wick, leg_in['high'], leg_out['high'])
                                
                                pattern = "RBD" if leg_in['color'] == 'green' else "DBD"
                                
                                base_strength = 2 if pattern == "RBD" else 1
                                
                                # Closing Strength: Did it close near the low?
                                leg_out_range = max(leg_out['range'], 0.0001)
                                close_strength = (leg_out['high'] - leg_out['close']) / leg_out_range
                                if close_strength > 0.8:
                                    base_strength += 2  # Strong bearish close
                                    
                                # Gap Detection: Did the leg-out gap down below the base?
                                lowest_base_low = min([row['low'] for _, row in base_candles.iterrows()])
                                if leg_out['open'] < lowest_base_low:
                                    base_strength += 3  # Gap + Base
                                
                                consecutive_exciting = 1
                                for j in range(base_end + 2, min(len(df), base_end + 5)):
                                    if df.iloc[j]['is_exciting'] and df.iloc[j]['color'] == 'red':
                                        consecutive_exciting += 1
                                    else:
                                        break
                                        
                                # GARBAGE ZONE FILTER: Require strong displacement
                                base_range_max = max([(row['high'] - row['low']) for _, row in base_candles.iterrows()])
                                if consecutive_exciting == 1 and leg_out['body'] < base_range_max * 1.2:
                                    i = base_end + 1
                                    continue
                                        
                                zones.append({
                                    "type": "supply",
                                    "pattern": pattern,
                                    "proximal": lowest_body,
                                    "distal": highest_wick,
                                    "start_idx": base_start,
                                    "end_idx": base_end,
                                    "strength": base_strength + consecutive_exciting,
                                    "is_tested": False
                                })
                                
                    i = base_end + 1
                else:
                    i += 1

            # Evaluate freshness (tested vs untested)
            # "A critical rule for validity is freshness. If the price returns to touch the proximal line... it is tested"
            for zone in zones:
                leg_out_idx = zone['end_idx'] + zone['strength'] # roughly skip the leg out candles
                
                for j in range(leg_out_idx, len(df)):
                    candle = df.iloc[j]
                    if zone['type'] == 'demand':
                        # Pierced the proximal line
                        if candle['low'] <= zone['proximal']:
                            zone['is_tested'] = True
                            break
                    elif zone['type'] == 'supply':
                        if candle['high'] >= zone['proximal']:
                            zone['is_tested'] = True
                            break

            active_zones = [z for z in zones if not z['is_tested']]
            return active_zones
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("GTF zone detection failed: %s", exc)
            return []

    def apply_lotl_merging(self, zones: List[Dict], threshold_pct: float = 1.5) -> List[Dict]:
        """
        Level Over The Level (LOTL): Merge adjacent zones of the same type if they are within threshold_pct of each other.
        """
        if not zones:
            return []
            
        demand = sorted([z for z in zones if z['type'] == 'demand'], key=lambda x: x['proximal'])
        supply = sorted([z for z in zones if z['type'] == 'supply'], key=lambda x: x['proximal'])
        
        merged_zones = []
        
        def _merge_list(zone_list, is_demand):
            if not zone_list:
                return []
            merged = [zone_list[0]]
            for current in zone_list[1:]:
                prev = merged[-1]
                # For demand, prev is lower. Check if current's distal is close to prev's proximal
                if is_demand:
                    dist = abs(current['distal'] - prev['proximal']) / prev['proximal'] * 100
                    if dist <= threshold_pct:
                        # Merge: proximal is the higher one (current), distal is the lower one (prev)
                        prev['proximal'] = max(prev['proximal'], current['proximal'])
                        prev['distal'] = min(prev['distal'], current['distal'])
                        prev['strength'] += current['strength']
                    else:
                        merged.append(current)
                else:
                    # For supply, check if current's proximal is close to prev's distal
                    dist = abs(current['proximal'] - prev['distal']) / prev['distal'] * 100
                    if dist <= threshold_pct:
                        prev['proximal'] = min(prev['proximal'], current['proximal'])
                        prev['distal'] = max(prev['distal'], current['distal'])
                        prev['strength'] += current['strength']
                    else:
                        merged.append(current)
            return merged
            
        merged_zones.extend(_merge_list(demand, True))
        merged_zones.extend(_merge_list(supply, False))
        
        return merged_zones
