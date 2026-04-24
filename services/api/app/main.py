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

app = FastAPI(
    title="Market Intelligence API",
    description="Real-time market scanning, explainable prediction, and dashboard-first stock intelligence.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(market_router)
app.include_router(stocks_router)
app.include_router(evaluation_router)
app.include_router(tracker_router)
app.include_router(legacy_router)
