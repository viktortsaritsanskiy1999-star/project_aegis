from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.config import settings
from app.database import engine
from app.models.base import Base
from app.routers.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create tables for local development and test environment
    Base.metadata.create_all(bind=engine)
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
