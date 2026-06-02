from __future__ import annotations

import os
import logging

from dotenv import load_dotenv


CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
load_dotenv(os.path.join(PARENT_DIR, ".env"), override=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.dependencies import get_market_hub
from app.routers import (
    evaluation_router,
    health_router,
    legacy_router,
    market_router,
    stocks_router,
    tracker_router,
)
from app.routers.live import router as live_router
from app.routers.ai import router as ai_router
from app.routers.performance import router as performance_router

logger = logging.getLogger(__name__)

DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://stock-market-web-eight.vercel.app",
    "https://bullalways.in",
    "https://www.bullalways.in",
)


def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "")
    configured = [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]
    return list(dict.fromkeys([*DEFAULT_CORS_ORIGINS, *configured]))


def test_mongo_connection():
    """Test MongoDB connection on startup."""
    try:
        from pymongo import MongoClient
        MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        DB_NAME = os.getenv("MONGO_DB_NAME", "stock_predictor_ml")
        
        logger.info(f"[MONGO] Testing connection to: {MONGO_URI}")
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.admin.command('ping')
        db = client[DB_NAME]
        collections = db.list_collection_names()
        logger.info(f"[MONGO] ✅ Connected successfully to database: {DB_NAME}")
        logger.info(f"[MONGO] Collections found: {len(collections)}")
        client.close()
        return True
    except Exception as e:
        logger.error(f"[MONGO] ❌ Connection FAILED!")
        logger.error(f"[MONGO] URI attempted: {os.getenv('MONGO_URI', 'mongodb://localhost:27017')}")
        logger.error(f"[MONGO] Error: {e}")
        logger.error(f"[MONGO] Please check your MONGO_URI environment variable")
        return False


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
fastapi_app.include_router(live_router)
fastapi_app.include_router(ai_router)
fastapi_app.include_router(performance_router)


@fastapi_app.on_event("startup")
async def start_market_background_scanner():
    # Test MongoDB connection first
    test_mongo_connection()
    
    get_market_hub().start_background_scanner()
    
    # Start Performance Auto-Exit Loop
    async def auto_exit_loop():
        import asyncio
        from app.services.performance_tracker import get_performance_tracker
        while True:
            await asyncio.sleep(300) # 5 minutes
            try:
                await get_performance_tracker().check_auto_exits()
            except Exception as e:
                logger.error(f"[PERFORMANCE] Auto-exit check failed: {e}")
            
    # Start Broker Sync Loop
    async def broker_sync_loop():
        import asyncio
        from app.services.performance_tracker import get_performance_tracker
        while True:
            await asyncio.sleep(120) # 2 minutes
            try:
                await get_performance_tracker().sync_broker_orders()
            except Exception as e:
                logger.error(f"[PERFORMANCE] Broker sync failed: {e}")
            
    import asyncio
    asyncio.create_task(auto_exit_loop())
    asyncio.create_task(broker_sync_loop())


app = CORSMiddleware(
    fastapi_app,
    allow_origins=_cors_origins(),
    allow_origin_regex=r"http://localhost:\d+|http://127\.0\.0\.1:\d+|https://.*\.vercel\.app|https://.*\.bullalways\.in",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
