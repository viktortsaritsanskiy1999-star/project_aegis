from app.routers.auth import router as auth_router
from app.routers.health import router as health_router
from app.routers.cycles import router as cycles_router
from app.routers.reports import router as reports_router
from app.routers.cascade import router as cascade_router
from app.routers.vitals import router as vitals_router

__all__ = [
    "auth_router",
    "health_router",
    "cycles_router",
    "reports_router",
    "cascade_router",
    "vitals_router",
]
