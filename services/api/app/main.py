from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import (
    evaluation_router,
    health_router,
    legacy_router,
    market_router,
    stocks_router,
    tracker_router,
)


CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
load_dotenv(os.path.join(PARENT_DIR, ".env"))

DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://stock-market-web-eight.vercel.app",
)


def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "")
    configured = [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]
    return list(dict.fromkeys([*DEFAULT_CORS_ORIGINS, *configured]))


fastapi_app = FastAPI(
    title="Market Intelligence API",
    description="Real-time market scanning, explainable prediction, and dashboard-first stock intelligence.",
)

fastapi_app.include_router(health_router)
fastapi_app.include_router(market_router)
fastapi_app.include_router(stocks_router)
fastapi_app.include_router(evaluation_router)
fastapi_app.include_router(tracker_router)
fastapi_app.include_router(legacy_router)

app = CORSMiddleware(
    fastapi_app,
    allow_origins=_cors_origins(),
    allow_origin_regex=r"http://localhost:\d+|http://127\.0\.0\.1:\d+|https://.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)
