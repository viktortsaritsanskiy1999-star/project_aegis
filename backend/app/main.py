from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.config import settings
from app.database import engine, init_db
from app.routers import health_router, cycles_router, reports_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database using Alembic migrations (with safe in-memory/test fallback)
    init_db(engine)
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
