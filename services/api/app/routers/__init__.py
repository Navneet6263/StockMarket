from app.routers.evaluation import router as evaluation_router
from app.routers.health import router as health_router
from app.routers.legacy import router as legacy_router
from app.routers.market import router as market_router
from app.routers.stocks import router as stocks_router
from app.routers.tracker import router as tracker_router

__all__ = [
    "evaluation_router",
    "health_router",
    "legacy_router",
    "market_router",
    "stocks_router",
    "tracker_router",
]
