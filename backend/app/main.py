from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from app.config import settings
from app.database import engine, init_db
from app.routers import health_router, cycles_router, reports_router, cascade_router, vitals_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # In test mode or when app.state.engine is provided, use the isolated test engine
    db_engine = getattr(app.state, "engine", None)
    if db_engine is None:
        if settings.env == "testing":
            db_engine = create_engine(
                "sqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            db_engine = engine

    backup_dir = getattr(app.state, "backup_dir", None)
    init_db(db_engine, backup_dir=backup_dir)
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Project Aegis Private Local Windows Backend for AI Clinical Reasoning & Vitals Ingestion",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Mount Routers
app.include_router(health_router)
app.include_router(cycles_router)
app.include_router(reports_router)
app.include_router(cascade_router)
app.include_router(vitals_router)
