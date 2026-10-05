const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");

// Test the actual TypeScript helper without a new runtime/test dependency.
const filename = path.resolve(__dirname, "../app/lib/stock-card-model.ts");
const source = ts.transpileModule(readFileSync(filename, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const compiled = new Module(filename, module);
compiled.filename = filename;
compiled.paths = module.paths;
compiled._compile(source, filename);
const { stockCardModel, finiteNumber } = compiled.exports;

const live = {
  direction: "bullish", liveTriggerStatus: "CONFIRMED",
  livePrice: 101, entryLevel: 100, stopLoss: 97, target: 107,
};

test("camelCase live payload uses real price, entry and stop; missing change stays unknown", () => {
  const card = stockCardModel(live, "live-action");
  assert.equal(card.price, 101);
  assert.equal(card.entryValue, 100);
  assert.equal(card.stopValue, 97);
  assert.equal(card.change, null);
  assert.equal(card.action, "ENTRY_READY");
  assert.equal(stockCardModel({ ...live, change_pct: 0 }).change, 0);
});

test("explicit scanner action is never upgraded by live confirmation", () => {
  for (const action of ["WATCH", "WAIT", "AVOID", "SELL", "BUY"]) {
    assert.equal(stockCardModel({ ...live, action }).action, action);
  }
  assert.equal(stockCardModel({ ...live, recommended_action: "WATCH" }).inferredReady, false);
});

test("legacy live-entry banner does not contradict a WATCH or missing risk plan", () => {
  const label = "🟢 LIVE ENTRY CONFIRMED";
  assert.match(stockCardModel({ ...live, label, action: "WATCH" }).label, /LIVE GATE CONFIRMED/);
  assert.match(stockCardModel({ ...live, label, stopLoss: null }).label, /CHECK PLAN/);
  assert.equal(stockCardModel({ ...live, label }).label, label);
});

test("confirmation cannot infer entry readiness with absent or invalid risk geometry", () => {
  for (const overrides of [
    { stopLoss: null }, { stopLoss: 0 }, { stopLoss: NaN }, { stopLoss: Infinity },
    { stopLoss: 103 }, { target: 99 }, { livePrice: 106 }, { entryLevel: undefined },
    { direction: "neutral" }, { entry_plan_blocked: true }, { trap_risk: "high" },
    { liveTriggerStatus: "WAIT" },
  ]) {
    assert.equal(stockCardModel({ ...live, ...overrides }).action, "WATCH", JSON.stringify(overrides));
  }
});

test("bearish geometry is symmetric and requires target below both entry and price", () => {
  assert.equal(stockCardModel({ ...live, direction: "bearish", livePrice: 99, stopLoss: 103, target: 93 }).action, "ENTRY_READY");
  assert.equal(stockCardModel({ ...live, direction: "bearish" }).action, "WATCH");
});

test("optional candle gate must be explicitly ready before inferred readiness", () => {
  for (const status of ["WATCH", "WAIT", "INVALID", "UNAVAILABLE"]) {
    assert.equal(stockCardModel({ ...live, candle_setup: { status, entry_ready: false } }).action, "WATCH");
  }
  assert.equal(stockCardModel({ ...live, candle_setup: { status: "READY", entry_ready: true } }).action, "ENTRY_READY");
  assert.equal(stockCardModel({ ...live, candle_setup: { status: "READY", entry_ready: false } }).action, "WATCH");
});

test("current live risk replaces old RR; explicit unknown never falls back", () => {
  const card = stockCardModel({ ...live, risk_reward: 2, riskPct: 3, liveRiskReward: 1.25, liveRiskPct: 3.96 });
  assert.equal(card.riskReward, 1.25);
  assert.equal(card.riskPct, 3.96);
  assert.equal(card.hasLiveRisk, true);
  assert.equal(stockCardModel({ ...live, rr: 2, liveRiskReward: null }).riskReward, null);
  assert.equal(stockCardModel({ ...live, rr: 2, liveConfirmation: { liveRiskReward: 1.1 } }).riskReward, 1.1);
});

test("entry readiness requires current geometric RR and any stricter live minimum", () => {
  const lowRiskReward = stockCardModel({ ...live, target: 105, rr: 3, liveRiskReward: 3 });
  assert.equal(lowRiskReward.currentRiskReward, 1);
  assert.equal(lowRiskReward.action, "WATCH");
  assert.equal(stockCardModel({ ...live, minimumRiskReward: 2 }).action, "WATCH");
  assert.equal(stockCardModel({ ...live, liveConfirmation: { minimumRiskReward: 2 } }).action, "WATCH");
  assert.equal(stockCardModel({ ...live, minimumRiskReward: 0.5 }).minimumRiskReward, 1.5);
});

test("unavailable or insufficient reported live RR cannot infer entry readiness", () => {
  for (const liveRiskReward of [null, undefined, NaN, 1.0]) {
    assert.equal(stockCardModel({ ...live, liveRiskReward, rr: 3 }).action, "WATCH");
    assert.equal(stockCardModel({ ...live, liveConfirmation: { liveRiskReward }, rr: 3 }).action, "WATCH");
  }
});

test("live price overrides stale scanner aliases and cannot fall back when explicitly missing", () => {
  const item = { ...live, current_price: 100, currentPrice: 100, livePrice: 102, liveRiskReward: 2 };
  const card = stockCardModel(item);
  assert.equal(card.price, 102);
  assert.equal(card.currentRiskReward, 1);
  assert.equal(card.action, "WATCH");
  assert.equal(stockCardModel({ ...item, livePrice: null }).price, null);
  assert.equal(stockCardModel({ ...item, livePrice: null }).action, "WATCH");
});

test("legacy dashboard action, geometry fields and numeric change are preserved", () => {
  const card = stockCardModel({
    current_price: 100, change_pct: -1.25, proposed_entry: 99,
    structural_invalidation: 96, opposing_structure_target: 106,
    action: "BUY", dashboardAction: "REVERSAL BUY", risk_reward: 2,
  }, "dashboard");
  assert.equal(card.action, "REVERSAL BUY");
  assert.equal(card.entryValue, 99);
  assert.equal(card.stopValue, 96);
  assert.equal(card.targetValue, 106);
  assert.equal(card.change, -1.25);
  assert.equal(card.riskReward, 2);
});

test("formatted dashboard display price does not shadow the numeric price", () => {
  const card = stockCardModel({ currentPrice: "₹1,234.50", current_price: 1234.5, price: 1234.5 });
  assert.equal(card.price, 1234.5);
  assert.equal(stockCardModel({ currentPrice: "-", price: 101 }).price, 101);
});

test("invalid numeric placeholders are unknown, not zero", () => {
  for (const value of [null, undefined, "", " ", "-", false, {}, NaN, Infinity]) {
    assert.equal(finiteNumber(value), null);
  }
  assert.equal(finiteNumber("0"), 0);
  assert.equal(finiteNumber("28.01"), 28.01);
});
