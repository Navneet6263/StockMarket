type CardPayload = Record<string, any>;

export function finiteNumber(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function firstAction(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function firstFinite(...values: unknown[]): number | null {
  for (const value of values) {
    const number = finiteNumber(value);
    if (number !== null) return number;
  }
  return null;
}

function riskField(item: CardPayload, key: string, fallback: unknown) {
  // An explicitly unavailable live risk must not silently become a stale plan RR.
  if (Object.prototype.hasOwnProperty.call(item, key)) return finiteNumber(item[key]);
  if (item.liveConfirmation && Object.prototype.hasOwnProperty.call(item.liveConfirmation, key)) {
    return finiteNumber(item.liveConfirmation[key]);
  }
  return finiteNumber(fallback);
}

export function stockCardModel(item: CardPayload, tab?: string) {
  const candle = item.candle_setup ?? item.raw?.candle_setup ?? null;
  const direction = String(item.direction ?? candle?.direction ?? "neutral").toLowerCase();
  const liveStatus = item.liveTriggerStatus ?? item.liveConfirmation?.status;
  // map_pick carries a formatted currentPrice (e.g. ₹1,234.50) alongside the
  // numeric current_price. Prefer numeric aliases; never parse a display range.
  const price = liveStatus && Object.prototype.hasOwnProperty.call(item, "livePrice")
    ? finiteNumber(item.livePrice)
    : firstFinite(item.current_price, item.currentPrice, item.livePrice, item.price, item.raw?.current_price, item.raw?.price);
  const change = finiteNumber(item.change_pct ?? item.changePct ?? item.raw?.change_pct);
  const entryValue = item.proposed_entry ?? item.entryLevel ?? item.entryZone ?? item.entry_zone_text ?? item.entry_trigger ?? item.entryTrigger ?? item.breakoutTrigger ?? price;
  const targetValue = item.opposing_structure_target ?? item.target_1 ?? item.target_price ?? item.target ?? item.keyResistance;
  const stopValue = item.structural_invalidation ?? item.stopLoss ?? item.stop_loss ?? item.stoploss ?? item.invalidation ?? item.invalidationLevel;
  // A price fallback can be displayed, but cannot create a missing entry plan.
  const plannedEntry = finiteNumber(item.proposed_entry ?? item.entryLevel ?? item.entry_trigger ?? item.entryTrigger ?? item.breakoutTrigger ?? item.entryZone);
  const stop = finiteNumber(stopValue);
  const target = finiteNumber(targetValue);
  const side = direction === "bullish" ? 1 : direction === "bearish" ? -1 : 0;
  const geometryValid = side !== 0 && plannedEntry !== null && price !== null && stop !== null && target !== null
    && Math.min(plannedEntry, price, stop, target) > 0
    && side * (plannedEntry - stop) > 0 && side * (target - plannedEntry) > 0
    && side * (price - stop) > 0 && side * (target - price) > 0;
  const currentRiskReward = geometryValid && price !== null && stop !== null && target !== null
    ? side * (target - price) / (side * (price - stop)) : null;
  const minimumRiskReward = Math.max(1.5, finiteNumber(item.minimumRiskReward ?? item.liveConfirmation?.minimumRiskReward) ?? 1.5);
  const riskReward = riskField(item, "liveRiskReward", item.risk_reward ?? item.rr);
  const riskPct = riskField(item, "liveRiskPct", item.riskPct);
  const hasLiveRisk = Object.prototype.hasOwnProperty.call(item, "liveRiskReward")
    || Object.prototype.hasOwnProperty.call(item.liveConfirmation ?? {}, "liveRiskReward");
  const riskValid = currentRiskReward !== null && currentRiskReward >= minimumRiskReward
    && (!hasLiveRisk || (riskReward !== null && riskReward >= minimumRiskReward));
  const candleReady = !candle || (candle.status === "READY" && candle.entry_ready === true);
  const trap = String(item.trapRisk ?? item.trap_risk ?? "").toLowerCase();
  const livePlanValid = geometryValid && riskValid && candleReady && !item.entry_plan_blocked
    && trap !== "high" && trap !== "very_high" && !item.bull_trap?.bull_trap_detected;
  const explicitAction = firstAction(item.action, item.effectiveAction, item.display_action, item.recommended_action, item.sourceAction);
  const inferredReady = !explicitAction && liveStatus === "CONFIRMED" && livePlanValid;
  const sourceAction = explicitAction ?? (inferredReady ? "ENTRY_READY" : "WATCH");
  const action = tab === "dashboard" ? (item.dashboardAction || sourceAction) : sourceAction;
  const actionableLabel = livePlanValid && ["ENTRY_READY", "BUY", "REENTRY_BUY", "SELL"].includes(sourceAction);
  const label = typeof item.label === "string" && item.label.includes("LIVE ENTRY CONFIRMED") && !actionableLabel
    ? item.label.replace("LIVE ENTRY CONFIRMED", "LIVE GATE CONFIRMED · CHECK PLAN")
    : item.label;

  return {
    candle, direction, price, change, entryValue, targetValue, stopValue,
    liveStatus, livePlanValid, explicitAction, inferredReady, sourceAction, action, label,
    riskReward, riskPct, hasLiveRisk, currentRiskReward, minimumRiskReward,
  };
}
