from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env", override=True)

from app.services.ai_intelligence import get_ai_analysis  # noqa: E402


def _env_status(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        return "missing"
    if value.lower().startswith("your_"):
        return "placeholder"
    return "set"


def main() -> None:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "FORTIS").upper()
    technical = {
        "direction": "bullish",
        "confidence": 75,
        "rsi": 58,
        "relative_volume": 1.0,
        "change_pct": 1.08,
        "setup_label": "Ascending triangle",
        "risk_reward": 2.4,
        "entry_trigger": 968.63,
        "current_price": 961.20,
        "target_1": 1073.82,
        "stop_loss": 924.27,
        "chart_pattern": "Ascending triangle",
        "action": "WATCH",
        "reasons": [
            "Ascending triangle detected with high pattern score",
            "Higher lows show improving structure",
            "Prior move is consolidating in a controlled range",
        ],
    }

    result = get_ai_analysis(symbol, technical, {"marketMood": "bearish"})
    ai = result.get("aiAnalysis") or {}
    output = {
        "env": {
            "ENABLE_GEMINI_AI": _env_status("ENABLE_GEMINI_AI"),
            "GEMINI_API_KEY": _env_status("GEMINI_API_KEY"),
            "GEMINI_MODEL": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            "GEMINI_MAX_OUTPUT_TOKENS": os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "512"),
            "GEMINI_THINKING_BUDGET": os.getenv("GEMINI_THINKING_BUDGET", "0"),
            "ENABLE_FINNHUB": _env_status("ENABLE_FINNHUB"),
            "FINNHUB_API_KEY": _env_status("FINNHUB_API_KEY"),
        },
        "symbol": result.get("symbol"),
        "finalScore": result.get("finalScore"),
        "finalAction": result.get("finalAction"),
        "scoreBreakdown": result.get("scoreBreakdown"),
        "gemini": {
            "available": ai.get("available"),
            "status": "active" if ai.get("available") else ai.get("reason"),
            "model": ai.get("aiModel"),
            "finishReason": ai.get("aiFinishReason"),
            "tokenUsage": ai.get("aiTokenUsage"),
            "conviction": ai.get("aiConviction"),
            "action": ai.get("aiAction"),
            "reason": ai.get("aiReason"),
            "tradePlan": ai.get("aiTradePlan"),
            "risks": ai.get("aiRisks"),
            "timeframe": ai.get("aiTimeframe"),
            "newsImpact": ai.get("aiNewsImpact"),
        },
        "warnings": result.get("warnings"),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
