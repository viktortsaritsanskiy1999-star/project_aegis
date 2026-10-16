from app.routers.health import router as health_router
from app.routers.cycles import router as cycles_router
from app.routers.reports import router as reports_router

__all__ = ["health_router", "cycles_router", "reports_router"]
