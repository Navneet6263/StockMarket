from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import yfinance as yf


class MarketIntelligenceService:
    def __init__(self):
        self.cache: Dict[str, tuple[datetime, Dict]] = {}
        self.cache_ttl = timedelta(minutes=5)
        self.news_ttl = timedelta(minutes=10)
        self.symbol_map = {
            "NIFTY": "^NSEI",
            "NIFTY50": "^NSEI",
            "BANKNIFTY": "^NSEBANK",
            "SENSEX": "^BSESN",
        }

    def _resolve_symbol(self, symbol: str) -> str:
        clean = (symbol or "").upper().replace(" ", "")
        if not clean:
            return "^NSEI"
        if clean in self.symbol_map:
            return self.symbol_map[clean]
        if clean.startswith("^") or "." in clean:
            return clean
        return f"{clean}.NS"

    def _cache_get(self, key: str, ttl: timedelta) -> Optional[Dict]:
        cached = self.cache.get(key)
        if cached and datetime.now(timezone.utc) - cached[0] < ttl:
            return cached[1]
        return None

    def _cache_set(self, key: str, value: Dict):
        self.cache[key] = (datetime.now(timezone.utc), value)

    def _safe_metric(self, ticker: str, period: str = "5d", normalize_yield: bool = False) -> Optional[Dict]:
        try:
            metric = self._fetch_metric(ticker, period=period)
            return self._normalize_yield(metric) if normalize_yield else metric
        except Exception:
            return None

    def _fetch_metric(self, ticker: str, period: str = "5d") -> Dict:
        hist = yf.Ticker(ticker).history(period=period, auto_adjust=False)
        if hist.empty:
            raise ValueError(f"no data for {ticker}")

        latest = float(hist["Close"].iloc[-1])
        previous = float(hist["Close"].iloc[-2]) if len(hist) > 1 else latest
        change = latest - previous
        change_pct = ((change / previous) * 100) if previous else 0.0

        return {
            "symbol": ticker,
            "value": latest,
            "previous": previous,
            "change": change,
            "change_pct": change_pct,
            "as_of": hist.index[-1].isoformat() if hasattr(hist.index[-1], "isoformat") else datetime.now(timezone.utc).isoformat(),
        }

    def _normalize_yield(self, metric: Dict) -> Dict:
        normalized = dict(metric)
        for key in ("value", "previous", "change"):
            value = float(normalized.get(key, 0))
            if key != "change" and value > 20:
                normalized[key] = round(value / 10, 3)
            elif key == "change" and abs(value) > 2:
                normalized[key] = round(value / 10, 3)
            else:
                normalized[key] = round(value, 3)

        previous = normalized.get("previous", 0) or 0
        value = normalized.get("value", 0) or 0
        normalized["change_pct"] = round(((value - previous) / previous) * 100, 3) if previous else 0.0
        normalized["change_bps"] = round((value - previous) * 100, 1)
        return normalized

    def _interpret_crude(self, metric: Dict) -> Dict:
        change_pct = float(metric.get("change_pct", 0))
        if change_pct <= -1:
            bias = "bullish"
            signal = "Cooling"
            note = "Falling crude reduces inflation pressure and usually helps import-heavy Indian sectors."
        elif change_pct >= 1:
            bias = "bearish"
            signal = "Heating"
            note = "Rising crude can pressure margins, inflation expectations, and risk appetite."
        else:
            bias = "neutral"
            signal = "Stable"
            note = "Crude is not adding a strong macro tailwind or headwind right now."

        return {
            "key": "crude",
            "label": "Brent Crude",
            "symbol": metric.get("symbol"),
            "value": round(float(metric.get("value", 0)), 2),
            "change_pct": round(change_pct, 2),
            "bias": bias,
            "signal": signal,
            "implication": note,
        }

    def _interpret_bond_yield(self, metric: Dict) -> Dict:
        change_bps = float(metric.get("change_bps", 0))
        if change_bps <= -4:
            bias = "bullish"
            signal = "Yields Falling"
            note = "Lower yields generally support equities by easing discount-rate pressure."
        elif change_bps >= 4:
            bias = "bearish"
            signal = "Yields Rising"
            note = "Higher yields often tighten financial conditions and can cap equity upside."
        else:
            bias = "neutral"
            signal = "Rangebound"
            note = "Bond yields are not sending a decisive macro message."

        return {
            "key": "bond_yield",
            "label": "US 10Y Yield",
            "symbol": metric.get("symbol"),
            "value": round(float(metric.get("value", 0)), 2),
            "change_bps": round(change_bps, 1),
            "bias": bias,
            "signal": signal,
            "implication": note,
        }

    def _interpret_vix(self, metric: Dict) -> Dict:
        value = float(metric.get("value", 0))
        change_pct = float(metric.get("change_pct", 0))

        if value >= 18 or change_pct >= 8:
            bias = "bearish"
            signal = "Risk Off"
            note = "Volatility is elevated. Reduce size and prefer only high-conviction breakouts."
        elif value <= 14 and change_pct <= 0:
            bias = "bullish"
            signal = "Risk On"
            note = "Volatility is contained. Breakouts have a better chance of following through."
        else:
            bias = "neutral"
            signal = "Caution"
            note = "Volatility is mixed. Wait for clean price-volume confirmation."

        return {
            "key": "vix",
            "label": "India VIX",
            "symbol": metric.get("symbol"),
            "value": round(value, 2),
            "change_pct": round(change_pct, 2),
            "bias": bias,
            "signal": signal,
            "implication": note,
        }

    def get_macro_snapshot(self) -> Dict:
        cached = self._cache_get("macro_snapshot", self.cache_ttl)
        if cached is not None:
            return cached

        crude = self._fetch_metric("BZ=F")
        bond = self._normalize_yield(self._fetch_metric("^TNX"))

        try:
            vix_metric = self._fetch_metric("^INDIAVIX", period="10d")
        except Exception:
            vix_metric = self._fetch_metric("^VIX", period="10d")
        vix = dict(vix_metric)

        checklist = [
            self._interpret_crude(crude),
            self._interpret_bond_yield(bond),
            self._interpret_vix(vix),
        ]

        bullish = sum(1 for item in checklist if item["bias"] == "bullish")
        bearish = sum(1 for item in checklist if item["bias"] == "bearish")
        if bullish >= 2:
            risk_mode = "risk_on"
            summary = "At least two macro gauges are supportive. Favor breakout continuation over counter-trend trades."
        elif bearish >= 2:
            risk_mode = "risk_off"
            summary = "At least two macro gauges are risk-off. Reduce size and avoid forcing trades."
        else:
            risk_mode = "mixed"
            summary = "Macro is mixed. Let price, volume, and stop distance decide the trade."

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "risk_mode": risk_mode,
            "summary": summary,
            "checklist": checklist,
            "routine": [
                "Ignore noisy headlines first. Start with crude, bond yields, and VIX.",
                "If two of the three turn risk-off, cut size and wait for cleaner setups.",
                "Use headlines only as a secondary catalyst check after macro is aligned.",
            ],
        }
        self._cache_set("macro_snapshot", payload)
        return payload

    def _extract_news_item(self, raw: Dict, symbol: str) -> Optional[Dict]:
        content = raw.get("content", {})
        title = content.get("title")
        if not title:
            return None

        provider = content.get("provider", {}) or {}
        click_url = content.get("clickThroughUrl", {}) or {}
        canonical_url = content.get("canonicalUrl", {}) or {}
        published_at = content.get("pubDate") or content.get("displayTime")
        summary = (content.get("summary") or "").strip()
        link = click_url.get("url") or canonical_url.get("url")
        bias = self._headline_bias(f"{title} {summary}")

        return {
            "symbol": symbol,
            "title": title.strip(),
            "publisher": provider.get("displayName", "Unknown"),
            "published_at": published_at,
            "summary": summary[:220],
            "link": link,
            "bias": bias,
        }

    def _headline_bias(self, text: str) -> str:
        lower = text.lower()
        bullish_words = ("beat", "surge", "growth", "record", "profit", "upgrade", "order", "expands", "win")
        bearish_words = ("miss", "fall", "drop", "slump", "loss", "downgrade", "warning", "probe", "cuts")
        bullish_score = sum(1 for word in bullish_words if word in lower)
        bearish_score = sum(1 for word in bearish_words if word in lower)
        if bullish_score > bearish_score:
            return "bullish"
        if bearish_score > bullish_score:
            return "bearish"
        return "neutral"

    def get_news_feed(self, symbol: str = "NIFTY", limit: int = 6) -> Dict:
        resolved = self._resolve_symbol(symbol)
        cache_key = f"news:{resolved}:{limit}"
        cached = self._cache_get(cache_key, self.news_ttl)
        if cached is not None:
            return cached

        feeds: List[Dict] = []
        seen_links = set()
        sources = [resolved]
        if resolved != "^NSEI":
            sources.append("^NSEI")

        for source in sources:
            try:
                raw_items = yf.Ticker(source).news or []
            except Exception:
                raw_items = []

            for raw in raw_items:
                item = self._extract_news_item(raw, source)
                if not item:
                    continue
                dedupe_key = item.get("link") or item.get("title")
                if dedupe_key in seen_links:
                    continue
                seen_links.add(dedupe_key)
                feeds.append(item)

        feeds.sort(key=lambda item: item.get("published_at") or "", reverse=True)
        items = feeds[:limit]

        bullish = sum(1 for item in items if item["bias"] == "bullish")
        bearish = sum(1 for item in items if item["bias"] == "bearish")
        if bullish > bearish:
            sentiment = "bullish"
        elif bearish > bullish:
            sentiment = "bearish"
        else:
            sentiment = "mixed"

        payload = {
            "symbol": symbol.upper(),
            "sentiment": sentiment,
            "note": "Use headlines as secondary confirmation after macro and price structure are aligned.",
            "items": items,
        }
        self._cache_set(cache_key, payload)
        return payload

    def _global_theme(self, text: str) -> str:
        lower = text.lower()
        escalation_words = (
            "war",
            "attack",
            "missile",
            "strike",
            "sanction",
            "tariff",
            "shipping",
            "drone",
            "oil spike",
            "conflict",
        )
        cooling_words = (
            "ceasefire",
            "truce",
            "peace",
            "de-escalation",
            "talks",
            "negotiation",
            "rate cut",
            "disinflation",
        )
        escalation_score = sum(1 for word in escalation_words if word in lower)
        cooling_score = sum(1 for word in cooling_words if word in lower)
        if escalation_score > cooling_score and escalation_score > 0:
            return "escalation"
        if cooling_score > escalation_score and cooling_score > 0:
            return "cooling"
        if any(word in lower for word in ("yield", "inflation", "rate", "earnings", "growth", "recession")):
            return "macro"
        return "market"

    def get_global_news_feed(self, limit: int = 8) -> Dict:
        cache_key = f"global_news:{limit}"
        cached = self._cache_get(cache_key, self.news_ttl)
        if cached is not None:
            return cached

        feeds: List[Dict] = []
        seen_links = set()
        sources = ["^GSPC", "^IXIC", "BZ=F", "^VIX", "GC=F"]

        for source in sources:
            try:
                raw_items = yf.Ticker(source).news or []
            except Exception:
                raw_items = []

            for raw in raw_items:
                item = self._extract_news_item(raw, source)
                if not item:
                    continue
                dedupe_key = item.get("link") or item.get("title")
                if dedupe_key in seen_links:
                    continue
                seen_links.add(dedupe_key)
                item["theme"] = self._global_theme(f"{item.get('title', '')} {item.get('summary', '')}")
                feeds.append(item)

        feeds.sort(key=lambda item: item.get("published_at") or "", reverse=True)
        items = feeds[:limit]
        payload = {
            "scope": "global",
            "note": "Geopolitical headlines are unstable. Use them only after macro and price agree.",
            "items": items,
        }
        self._cache_set(cache_key, payload)
        return payload

    def get_sector_scenario_view(self, sector: str = "", industry: str = "") -> Dict:
        text = f"{sector or ''} {industry or ''}".lower()
        cooling_tailwinds: List[str] = []
        escalation_tailwinds: List[str] = []
        structural_note = "Execution and balance sheet quality matter more than headlines alone."
        recovery_rank = "middle"

        if any(word in text for word in ("airline", "aviation", "travel", "hotel", "hospitality")):
            cooling_tailwinds.append("Lower oil and calmer headlines can quickly help demand and margins.")
            recovery_rank = "early"
        if any(word in text for word in ("paint", "chemical", "consumer", "retail", "auto", "tyre")):
            cooling_tailwinds.append("Cooling crude usually improves input costs and supports demand.")
            recovery_rank = "early"
        if any(word in text for word in ("bank", "financial", "insurance", "real estate", "capital goods")):
            cooling_tailwinds.append("Risk appetite and capex confidence normally recover early when macro stress cools.")
            if recovery_rank == "middle":
                recovery_rank = "early"
        if any(word in text for word in ("oil", "gas", "energy", "exploration", "coal")):
            escalation_tailwinds.append("Supply shock and higher energy prices can keep the group supported, but volatile.")
        if any(word in text for word in ("defence", "defense", "aerospace", "shipbuilding")):
            escalation_tailwinds.append("Conflict urgency can keep order books strong and budgets elevated.")
        if any(word in text for word in ("technology", "software", "exchange", "capital markets", "electronics", "semiconductor")):
            structural_note = "This group is driven more by long-cycle execution and earnings compounding than short conflict headlines."
        if any(word in text for word in ("metal", "steel", "cement", "mining", "commodity")):
            structural_note = "This group is cyclical. Global growth and input-price swings can dominate the story."

        war_cools_impact = "mixed"
        if cooling_tailwinds:
            war_cools_impact = "beneficiary"
        elif "technology" in text or "software" in text:
            war_cools_impact = "steady"

        war_escalates_impact = "mixed"
        if escalation_tailwinds:
            war_escalates_impact = "relative beneficiary"
        elif cooling_tailwinds:
            war_escalates_impact = "headwind"

        return {
            "war_cools_impact": war_cools_impact,
            "war_escalates_impact": war_escalates_impact,
            "recovery_rank": recovery_rank,
            "cooling_tailwinds": cooling_tailwinds[:3],
            "escalation_tailwinds": escalation_tailwinds[:3],
            "structural_note": structural_note,
        }

    def get_global_scenario(self) -> Dict:
        cache_key = "global_scenario"
        cached = self._cache_get(cache_key, self.cache_ttl)
        if cached is not None:
            return cached

        try:
            macro = self.get_macro_snapshot()
        except Exception as exc:
            macro = {
                "risk_mode": "mixed",
                "summary": f"Macro snapshot unavailable: {exc}",
                "checklist": [],
            }

        gold = self._safe_metric("GC=F", period="10d")
        us_vix = self._safe_metric("^VIX", period="10d")
        spx = self._safe_metric("^GSPC", period="10d")
        nasdaq = self._safe_metric("^IXIC", period="10d")
        news = self.get_global_news_feed(limit=8)

        gauges: List[Dict] = []
        stress_score = 0

        if macro.get("risk_mode") == "risk_off":
            stress_score += 2
        elif macro.get("risk_mode") == "mixed":
            stress_score += 1

        if gold:
            gold_change = float(gold.get("change_pct", 0))
            if gold_change >= 1:
                stress_score += 1
                gold_signal = "Safety Bid"
                gold_note = "Gold rising suggests money is hiding in safety."
            elif gold_change <= -1:
                gold_signal = "Risk Appetite"
                gold_note = "Gold cooling can mean panic demand is fading."
            else:
                gold_signal = "Stable"
                gold_note = "Gold is not sending a strong fear signal right now."
            gauges.append(
                {
                    "key": "gold",
                    "label": "Gold",
                    "value": round(float(gold.get("value", 0)), 2),
                    "change_pct": round(gold_change, 2),
                    "signal": gold_signal,
                    "implication": gold_note,
                }
            )

        if us_vix:
            vix_value = float(us_vix.get("value", 0))
            vix_change = float(us_vix.get("change_pct", 0))
            if vix_value >= 20 or vix_change >= 8:
                stress_score += 2
                vix_signal = "Global Fear"
                vix_note = "US volatility is elevated. Global risk appetite is fragile."
            elif vix_value <= 16 and vix_change <= 0:
                vix_signal = "Calmer Tape"
                vix_note = "US volatility is controlled. Recovery trades can breathe."
            else:
                vix_signal = "Watch"
                vix_note = "Volatility is not broken, but it is not fully calm either."
            gauges.append(
                {
                    "key": "us_vix",
                    "label": "US VIX",
                    "value": round(vix_value, 2),
                    "change_pct": round(vix_change, 2),
                    "signal": vix_signal,
                    "implication": vix_note,
                }
            )

        for key, label, metric, threshold in (
            ("spx", "S&P 500", spx, -1.0),
            ("nasdaq", "Nasdaq", nasdaq, -1.2),
        ):
            if not metric:
                continue
            change_pct = float(metric.get("change_pct", 0))
            if change_pct <= threshold:
                stress_score += 1
                signal = "Risk Reduced"
                note = "Global equities are selling off, which usually hurts broad risk appetite."
            elif change_pct >= abs(threshold):
                signal = "Recovery Bid"
                note = "Global equities are absorbing risk better."
            else:
                signal = "Range"
                note = "The index is not giving a decisive message."
            gauges.append(
                {
                    "key": key,
                    "label": label,
                    "value": round(float(metric.get("value", 0)), 2),
                    "change_pct": round(change_pct, 2),
                    "signal": signal,
                    "implication": note,
                }
            )

        escalation_count = sum(1 for item in news.get("items", []) if item.get("theme") == "escalation")
        cooling_count = sum(1 for item in news.get("items", []) if item.get("theme") == "cooling")
        if escalation_count > cooling_count:
            stress_score += 1
        elif cooling_count > escalation_count:
            stress_score -= 1

        if stress_score >= 5:
            state = "elevated_geopolitical_risk"
            summary = "Global markets are still pricing geopolitical stress. Favor selective trades, smaller size, and faster risk control."
        elif stress_score >= 2:
            state = "fragile_calm"
            summary = "The tape is not in panic, but recovery is still fragile. Markets want proof that stress is cooling before rewarding weaker hands."
        else:
            state = "risk_recovery_window"
            summary = "Markets are leaning toward a recovery window. If crude and volatility stay soft, cyclicals and growth names can extend."

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "state": state,
            "summary": summary,
            "timing_note": (
                "No model can know the exact date when a war or macro shock ends. "
                "Track whether crude, volatility, and yields cool together for 2 to 3 weeks."
            ),
            "current_setup": [
                macro.get("summary", "Macro snapshot unavailable."),
                f"Escalation headlines: {escalation_count}. Cooling headlines: {cooling_count}.",
                "Use price confirmation first. Headlines only tell you why a move is happening, not whether it is tradeable.",
            ],
            "what_to_watch": [
                "Crude staying lower instead of spiking back up.",
                "US and India VIX cooling, not expanding.",
                "Bond yields stabilizing or drifting lower.",
                "Ceasefire or de-escalation headlines outperforming sanction or attack headlines.",
            ],
            "if_conflict_cools": [
                "Travel, aviation, hotels, and discretionary consumption usually recover early.",
                "Autos, paints, chemicals, and other input-cost-sensitive names often get margin relief.",
                "Banks, NBFCs, and real estate can recover quickly if risk appetite broadens.",
            ],
            "if_conflict_worsens": [
                "Defense and select aerospace names can keep relative strength.",
                "Upstream energy and other supply-shock beneficiaries can stay bid, but volatility remains high.",
                "Cheap balance-sheet-weak names usually get hit first when fear expands.",
            ],
            "recovery_order": [
                "1. Travel, discretionary, and input-cost-sensitive sectors.",
                "2. Financials, capital goods, and domestic cyclicals.",
                "3. Broader small caps after volatility cools and liquidity returns.",
            ],
            "gauges": gauges,
            "headline_watch": [
                f"{item.get('publisher')}: {item.get('title')}"
                for item in news.get("items", [])[:4]
            ],
        }
        self._cache_set(cache_key, payload)
        return payload
