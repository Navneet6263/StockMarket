from __future__ import annotations

from typing import Dict, List


class NarrativeService:
    def build(
        self,
        signal: Dict,
        macro_context: Dict,
        news_context: Dict,
        global_context: Dict,
        company_context: Dict,
    ) -> Dict:
        setup_label = signal.get("setup_label", "No clear edge")
        symbol = signal.get("symbol", "")
        company_name = company_context.get("company_name") or symbol
        direction = signal.get("direction", "neutral")
        move = signal.get("expected_move_pct", 0)
        invalidation = signal.get("invalidation")
        risk_level = signal.get("risk_level", "medium")
        confidence = signal.get("confidence", 0)
        evidence_status = signal.get("historical_evidence_status", "not_loaded")
        timeframe = signal.get("timeframe_label", "3-5 days")
        target_price = signal.get("target_price")

        if direction == "bullish":
            summary = (
                f"{company_name} shows a {setup_label.lower()} with {confidence:.0f}% confidence. "
                f"The current read favors upside continuation of about {abs(move):.2f}% over {timeframe} if support holds."
            )
            watch_next = [
                "Watch whether price keeps holding above VWAP and short-term support.",
                "A clean push through nearby resistance would strengthen continuation odds.",
                "If volume fades while price stalls, confidence should be reduced quickly.",
            ]
        elif direction == "bearish":
            summary = (
                f"{company_name} shows a {setup_label.lower()} with {confidence:.0f}% confidence. "
                f"The current read favors downside continuation of about {abs(move):.2f}% over {timeframe} if resistance holds."
            )
            watch_next = [
                "Watch whether price stays below VWAP and keeps failing near resistance.",
                "A decisive break under support would confirm the bearish continuation case.",
                "If buyers reclaim the breakdown area, the signal weakens fast.",
            ]
        else:
            summary = (
                f"{company_name} is on watch, but the current setup is not strong enough to justify conviction. "
                "The system sees mixed confirmation rather than a clean directional edge."
            )
            watch_next = [
                "Wait for price to leave the current support-resistance pocket.",
                "Volume and momentum need to expand together before the setup becomes actionable.",
                "Neutral readings should be treated as watchlist names, not forced trades.",
            ]

        if evidence_status == "unavailable":
            summary += " Historical validation is unavailable, so this should be treated as a live model read."
        elif evidence_status == "limited":
            summary += " Historical validation is still limited, so the sample size remains thin."
        if invalidation is not None:
            summary += f" Invalid below/above {invalidation:.2f} depending on direction."
        if target_price:
            summary += f" First target sits near {target_price:.2f}."

        macro_line = macro_context.get("summary", "Macro context is mixed.")
        global_line = global_context.get("summary", "Global risk context is neutral.")
        business_note = company_context.get("business_model", {}).get("note", "Business quality note unavailable.")

        risk_items: List[str] = list(signal.get("risk_factors", []))
        if invalidation is not None:
            risk_items.append(f"Setup invalidates near {invalidation:.2f}.")
        if not risk_items:
            risk_items.append(f"Risk is currently assessed as {risk_level}.")

        return {
            "summary": summary,
            "why_it_is_flagged": signal.get("reasons", [])[:5],
            "what_is_strong": signal.get("reasons", [])[:3],
            "what_is_weak": signal.get("weaknesses", [])[:4],
            "risk_factors": risk_items[:5],
            "watch_next": watch_next,
            "macro_take": macro_line,
            "global_take": global_line,
            "business_take": business_note,
            "news_headlines": [
                f"{item.get('publisher')}: {item.get('title')}"
                for item in news_context.get("items", [])[:3]
            ],
        }
