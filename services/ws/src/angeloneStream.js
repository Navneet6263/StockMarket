const axios = require("axios");
const crypto = require("crypto");
const os = require("os");
const WebSocket = require("ws");

const ROOT_URI = "wss://smartapisocket.angelone.in/smart-stream";
const LOGIN_URL = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword";
const MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json";

const SUBSCRIBE_ACTION = 1;
const UNSUBSCRIBE_ACTION = 0;
const NSE_CM = 1;

function nowIso() {
  return new Date().toISOString();
}

function cleanSymbol(symbol) {
  return String(symbol || "").toUpperCase().replace(".NS", "").replace(/\s+/g, "");
}

function localIp() {
  const nets = os.networkInterfaces();
  for (const rows of Object.values(nets)) {
    for (const row of rows || []) {
      if (row.family === "IPv4" && !row.internal) return row.address;
    }
  }
  return "127.0.0.1";
}

function localMac() {
  const nets = os.networkInterfaces();
  for (const rows of Object.values(nets)) {
    for (const row of rows || []) {
      if (row.family === "IPv4" && !row.internal && row.mac && row.mac !== "00:00:00:00:00:00") {
        return row.mac;
      }
    }
  }
  return "00:00:00:00:00:00";
}

function base32Decode(input) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const clean = String(input || "").replace(/=+$/g, "").replace(/\s+/g, "").toUpperCase();
  let bits = "";
  for (const char of clean) {
    const value = alphabet.indexOf(char);
    if (value < 0) continue;
    bits += value.toString(2).padStart(5, "0");
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.slice(i, i + 8), 2));
  }
  return Buffer.from(bytes);
}

function totp(secret) {
  if (!secret) return "";
  const counter = Math.floor(Date.now() / 30000);
  const buffer = Buffer.alloc(8);
  buffer.writeBigUInt64BE(BigInt(counter));
  const hmac = crypto.createHmac("sha1", base32Decode(secret)).update(buffer).digest();
  const offset = hmac[hmac.length - 1] & 0x0f;
  const code = (
    ((hmac[offset] & 0x7f) << 24) |
    ((hmac[offset + 1] & 0xff) << 16) |
    ((hmac[offset + 2] & 0xff) << 8) |
    (hmac[offset + 3] & 0xff)
  ) % 1000000;
  return String(code).padStart(6, "0");
}

function tokenFromBuffer(buffer, start = 2, end = 27) {
  const slice = buffer.subarray(start, end);
  const zero = slice.indexOf(0);
  return slice.subarray(0, zero >= 0 ? zero : slice.length).toString("ascii");
}

function readInt64(buffer, offset) {
  if (buffer.length < offset + 8) return 0;
  return Number(buffer.readBigInt64LE(offset));
}

function readDouble(buffer, offset) {
  if (buffer.length < offset + 8) return 0;
  return buffer.readDoubleLE(offset);
}

function parseTick(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 51) return null;
  const mode = buffer.readUInt8(0);
  const data = {
    subscription_mode: mode,
    exchange_type: buffer.readUInt8(1),
    token: tokenFromBuffer(buffer),
    sequence_number: readInt64(buffer, 27),
    exchange_timestamp: readInt64(buffer, 35),
    last_traded_price: readInt64(buffer, 43),
    subscription_mode_val: mode === 1 ? "LTP" : mode === 2 ? "QUOTE" : mode === 3 ? "SNAP_QUOTE" : "DEPTH",
  };

  if ((mode === 2 || mode === 3) && buffer.length >= 123) {
    data.last_traded_quantity = readInt64(buffer, 51);
    data.average_traded_price = readInt64(buffer, 59);
    data.volume_trade_for_the_day = readInt64(buffer, 67);
    data.total_buy_quantity = readDouble(buffer, 75);
    data.total_sell_quantity = readDouble(buffer, 83);
    data.open_price_of_the_day = readInt64(buffer, 91);
    data.high_price_of_the_day = readInt64(buffer, 99);
    data.low_price_of_the_day = readInt64(buffer, 107);
    data.closed_price = readInt64(buffer, 115);
  }

  return data;
}

class AngelOneStream {
  constructor() {
    this.enabled = process.env.ENABLE_ANGELONE_WS === "true" || process.env.BROKER_PROVIDER === "angelone";
    this.apiKey = process.env.ANGELONE_API_KEY || "";
    this.clientId = process.env.ANGELONE_CLIENT_ID || "";
    this.password = process.env.ANGELONE_PASSWORD || "";
    this.totpKey = process.env.ANGELONE_TOTP_KEY || "";
    this.mode = Number(process.env.ANGELONE_WS_MODE || 1);
    this.maxTokens = Number(process.env.ANGELONE_WS_MAX_TOKENS || 200);
    this.fallbackPollWhenStaleMs = Number(process.env.ANGELONE_WS_STALE_MS || 5000);
    this.session = null;
    this.ws = null;
    this.connected = false;
    this.connecting = false;
    this.reconnectTimer = null;
    this.heartbeatTimer = null;
    this.instruments = null;
    this.instrumentBySymbol = new Map();
    this.symbolByToken = new Map();
    this.desiredSymbols = new Set();
    this.subscribedTokens = new Set();
    this.latest = new Map();
    this.listeners = new Set();
  }

  isConfigured() {
    return Boolean(this.enabled && this.apiKey && this.clientId && this.password);
  }

  onTick(callback) {
    this.listeners.add(callback);
    return () => this.listeners.delete(callback);
  }

  getLatest(symbol) {
    return this.latest.get(cleanSymbol(symbol));
  }

  shouldUseFallback(symbol) {
    const tick = this.getLatest(symbol);
    if (!tick) return true;
    return Date.now() - (tick.server_received_at_ms || 0) > this.fallbackPollWhenStaleMs;
  }

  status() {
    return {
      enabled: this.enabled,
      configured: this.isConfigured(),
      connected: this.connected,
      connecting: this.connecting,
      mode: this.mode,
      desiredSymbols: this.desiredSymbols.size,
      subscribedTokens: this.subscribedTokens.size,
      latestTicks: this.latest.size,
    };
  }

  async subscribe(symbol) {
    const clean = cleanSymbol(symbol);
    if (!clean || !this.isConfigured()) return false;
    this.desiredSymbols.add(clean);
    await this.ensureConnected();
    const instrument = await this.resolveInstrument(clean);
    if (!instrument) return false;
    this.symbolByToken.set(String(instrument.token), clean);
    if (this.connected && !this.subscribedTokens.has(String(instrument.token))) {
      this.sendSubscription(SUBSCRIBE_ACTION, [instrument]);
    }
    return true;
  }

  async unsubscribe(symbol) {
    const clean = cleanSymbol(symbol);
    this.desiredSymbols.delete(clean);
    const instrument = this.instrumentBySymbol.get(clean);
    if (!instrument || !this.connected) return;
    this.sendSubscription(UNSUBSCRIBE_ACTION, [instrument]);
    this.subscribedTokens.delete(String(instrument.token));
  }

  async ensureConnected() {
    if (this.connected || this.connecting || !this.isConfigured()) return;
    this.connecting = true;
    try {
      if (!this.session) this.session = await this.login();
      this.openSocket();
    } catch (error) {
      console.error("[AngelOneWS] connect failed:", error.message);
      this.connecting = false;
      this.scheduleReconnect();
    }
  }

  requestHeaders(authToken) {
    const publicIp = process.env.ANGELONE_CLIENT_PUBLIC_IP || process.env.CLIENT_PUBLIC_IP || localIp();
    return {
      "Content-Type": "application/json",
      Accept: "application/json",
      "X-ClientLocalIP": process.env.ANGELONE_CLIENT_LOCAL_IP || localIp(),
      "X-ClientPublicIP": publicIp,
      "X-MACAddress": process.env.ANGELONE_CLIENT_MAC || localMac(),
      "X-PrivateKey": this.apiKey,
      "X-UserType": "USER",
      "X-SourceID": "WEB",
      ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
    };
  }

  async login() {
    const response = await axios.post(
      LOGIN_URL,
      {
        clientcode: this.clientId,
        password: this.password,
        totp: totp(this.totpKey),
      },
      { headers: this.requestHeaders(), timeout: 10000 }
    );
    if (!response.data?.status) {
      throw new Error(response.data?.message || "angelone_login_failed");
    }
    const data = response.data.data || {};
    return {
      jwtToken: data.jwtToken,
      refreshToken: data.refreshToken,
      feedToken: data.feedToken,
      loggedInAt: Date.now(),
    };
  }

  openSocket() {
    if (!this.session?.jwtToken || !this.session?.feedToken) throw new Error("missing_session_tokens");
    this.ws = new WebSocket(ROOT_URI, {
      headers: {
        Authorization: this.session.jwtToken,
        "x-api-key": this.apiKey,
        "x-client-code": this.clientId,
        "x-feed-token": this.session.feedToken,
      },
    });

    this.ws.on("open", async () => {
      this.connected = true;
      this.connecting = false;
      console.log("[AngelOneWS] connected");
      this.startHeartbeat();
      await this.resubscribeDesired();
    });

    this.ws.on("message", (message, isBinary) => {
      if (!isBinary) return;
      this.handleTick(Buffer.from(message));
    });

    this.ws.on("error", (error) => {
      console.error("[AngelOneWS] socket error:", error.message);
    });

    this.ws.on("close", (code, reason) => {
      console.warn("[AngelOneWS] closed:", code, reason?.toString?.() || "");
      this.connected = false;
      this.connecting = false;
      this.subscribedTokens.clear();
      this.stopHeartbeat();
      this.scheduleReconnect();
    });
  }

  startHeartbeat() {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.ping("ping");
      }
    }, 10000);
  }

  stopHeartbeat() {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }

  scheduleReconnect() {
    if (!this.isConfigured() || this.reconnectTimer || this.desiredSymbols.size === 0) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.session = null;
      this.ensureConnected();
    }, 3000);
  }

  async resubscribeDesired() {
    const instruments = [];
    for (const symbol of this.desiredSymbols) {
      const instrument = await this.resolveInstrument(symbol);
      if (instrument) instruments.push(instrument);
      if (instruments.length >= this.maxTokens) break;
    }
    if (instruments.length) this.sendSubscription(SUBSCRIBE_ACTION, instruments);
  }

  sendSubscription(action, instruments) {
    if (!this.connected || this.ws?.readyState !== WebSocket.OPEN) return;
    const tokens = Array.from(new Set(instruments.map((item) => String(item.token)).filter(Boolean)));
    if (!tokens.length) return;
    const tokenList = [{ exchangeType: NSE_CM, tokens }];
    const payload = {
      correlationID: `crm_${Date.now()}`.slice(0, 20),
      action,
      params: {
        mode: this.mode,
        tokenList,
      },
    };
    this.ws.send(JSON.stringify(payload));
    for (const token of tokens) {
      if (action === SUBSCRIBE_ACTION) this.subscribedTokens.add(token);
      if (action === UNSUBSCRIBE_ACTION) this.subscribedTokens.delete(token);
    }
    console.log(`[AngelOneWS] ${action === SUBSCRIBE_ACTION ? "subscribed" : "unsubscribed"} tokens=${tokens.length}`);
  }

  async resolveInstrument(symbol) {
    const clean = cleanSymbol(symbol);
    if (this.instrumentBySymbol.has(clean)) return this.instrumentBySymbol.get(clean);
    const instruments = await this.loadInstruments();
    const match = instruments.find((item) => {
      if (item.exch_seg !== "NSE") return false;
      if (item.name !== clean) return false;
      const tradingSymbol = String(item.symbol || "");
      const instrumentType = String(item.instrumenttype || "");
      return tradingSymbol.endsWith("-EQ") || instrumentType === "EQ" || instrumentType === "";
    });
    if (!match?.token) {
      console.warn(`[AngelOneWS] token not found for ${clean}`);
      return null;
    }
    const instrument = {
      symbol: clean,
      tradingSymbol: match.symbol,
      token: String(match.token),
      exchangeType: NSE_CM,
    };
    this.instrumentBySymbol.set(clean, instrument);
    this.symbolByToken.set(instrument.token, clean);
    return instrument;
  }

  async loadInstruments() {
    if (this.instruments) return this.instruments;
    const url = process.env.ANGELONE_MASTER_URL || MASTER_URL;
    const response = await axios.get(url, { timeout: 15000 });
    this.instruments = Array.isArray(response.data) ? response.data : [];
    return this.instruments;
  }

  handleTick(buffer) {
    const parsed = parseTick(buffer);
    if (!parsed?.token) return;
    const symbol = this.symbolByToken.get(String(parsed.token));
    if (!symbol) return;

    const receivedAt = Date.now();
    const exchangeTs = parsed.exchange_timestamp || null;
    const tick = {
      type: "tick",
      source: "angelone_ws",
      symbol,
      token: String(parsed.token),
      subscription_mode: parsed.subscription_mode,
      subscription_mode_val: parsed.subscription_mode_val,
      price: parsed.last_traded_price / 100,
      ltp: parsed.last_traded_price / 100,
      volume: parsed.volume_trade_for_the_day || 0,
      open: parsed.open_price_of_the_day ? parsed.open_price_of_the_day / 100 : undefined,
      high: parsed.high_price_of_the_day ? parsed.high_price_of_the_day / 100 : undefined,
      low: parsed.low_price_of_the_day ? parsed.low_price_of_the_day / 100 : undefined,
      previous_close: parsed.closed_price ? parsed.closed_price / 100 : undefined,
      sequence_number: parsed.sequence_number,
      exchange_timestamp: exchangeTs,
      server_received_at_ms: receivedAt,
      server_received_at: nowIso(),
      latency_ms: exchangeTs && exchangeTs > 0 ? Math.max(0, receivedAt - exchangeTs) : null,
      raw: parsed,
    };

    this.latest.set(symbol, tick);
    for (const callback of this.listeners) {
      try {
        callback(symbol, tick);
      } catch (error) {
        console.error("[AngelOneWS] tick callback failed:", error.message);
      }
    }
  }
}

module.exports = { AngelOneStream, parseTick, totp };
