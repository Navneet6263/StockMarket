"""AI Stock Intelligence — combines Finnhub data + Gemini reasoning + Technical analysis.
Single entry point for the smartest possible stock recommendation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict

from app.services.finnhub_data import get_stock_intelligence
from app.services.gemini_ai import analyze_stock_with_ai

logger = logging.getLogger(__name__)


def get_ai_analysis(symbol: str, technical_signal: Dict, market_context: Dict = None) -> Dict:
    """
    Full AI-powered stock analysis:
    1. Fetch Finnhub data (news, earnings, insider, recommendations)
    2. Send everything to Gemini AI for reasoning
    3. Combine AI verdict with technical signal
    4. Return final recommendation
    """
    # Step 1: Finnhub data
    intel = get_stock_intelligence(symbol)
    news = intel.get("news", {})
    earnings = intel.get("earnings", {})
    insider = intel.get("insider", {})
    recommendations = intel.get("recommendations", {})

    # Step 2: Gemini AI reasoning
    ai_result = analyze_stock_with_ai(
        symbol=symbol,
        technical=technical_signal,
        news=news,
        earnings=earnings,
        insider=insider,
        recommendations=recommendations,
        market_context=market_context,
    )

    # Step 3: Combine scores
    technical_confidence = float(technical_signal.get("confidence") or 50)
    ai_conviction = float(ai_result.get("aiConviction") or 50)
    news_score = _news_to_score(news)
    insider_score = _insider_to_score(insider)
    analyst_score = _analyst_to_score(recommendations)

    # Weighted final score
    final_score = round(
        technical_confidence * 0.35
        + ai_conviction * 0.30
        + news_score * 0.15
        + insider_score * 0.10
        + analyst_score * 0.10,
        1,
    )

    # Final action decision
    technical_action = technical_signal.get("action") or "WATCH"
    ai_action = ai_result.get("aiAction") or "WATCH"

    if final_score >= 78 and ai_action in {"BUY", "WATCH"} and technical_action in {"BUY", "WATCH", "ALERT_ABOVE_LEVEL"}:
        final_action = "HIGH_CONVICTION_BUY"
    elif final_score >= 65 and ai_action != "AVOID":
        final_action = "CANDIDATE"
    elif ai_action == "AVOID" or final_score < 40:
        final_action = "AVOID"
    elif news.get("newsSentiment") == "bearish" and final_score < 60:
        final_action = "CAUTION"
    else:
        final_action = "WATCH"

    # Warnings
    warnings = []
    if earnings.get("earningsNear"):
        warnings.append("Earnings within 7 days — avoid fresh entry or reduce size.")
    if insider.get("signal") == "insider_selling":
        warnings.append("Insider selling detected — be cautious.")
    if news.get("newsSentiment") == "bearish":
        warnings.append("Recent news sentiment is negative.")
    if (market_context or {}).get("marketMood") == "bearish":
        warnings.append("Market breadth is bearish — fresh buys risky.")

    return {
        "symbol": symbol.upper(),
        "finalScore": final_score,
        "finalAction": final_action,
        "scoreBreakdown": {
            "technical": round(technical_confidence, 1),
            "aiConviction": round(ai_conviction, 1),
            "newsSentiment": round(news_score, 1),
            "insiderActivity": round(insider_score, 1),
            "analystConsensus": round(analyst_score, 1),
        },
        "weights": {
            "technical": 0.35,
            "ai": 0.30,
            "news": 0.15,
            "insider": 0.10,
            "analyst": 0.10,
        },
        "aiAnalysis": ai_result,
        "newsData": news,
        "earningsData": earnings,
        "insiderData": insider,
        "recommendationsData": recommendations,
        "warnings": warnings,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def _news_to_score(news: Dict) -> float:
    sentiment = news.get("newsSentiment", "unknown")
    if sentiment == "bullish":
        return 80.0
    if sentiment == "bearish":
        return 25.0
    if sentiment == "neutral":
        return 55.0
    return 50.0


def _insider_to_score(insider: Dict) -> float:
    signal = insider.get("signal", "unknown")
    if signal == "insider_buying":
        return 85.0
    if signal == "insider_selling":
        return 20.0
    return 50.0


def _analyst_to_score(reco: Dict) -> float:
    consensus = reco.get("consensus", "unknown")
    if consensus == "strong_buy":
        return 90.0
    if consensus == "buy":
        return 75.0
    if consensus == "hold":
        return 50.0
    if consensus == "sell":
        return 20.0
    return 50.0
