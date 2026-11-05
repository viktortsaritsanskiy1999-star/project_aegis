from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.config import settings
from app.database import engine, init_db
from app.routers import (
    auth_router,
    health_router,
    cycles_router,
    reports_router,
    cascade_router,
    vitals_router,
    client_cascade_router,
    client_chat_router,
)
from app.services.clinical_automation_service import ClinicalAutomationWorker


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

    worker = None
    enable_worker = getattr(
        app.state,
        "enable_automation_worker",
        settings.env != "testing" and getattr(app.state, "engine", None) is None,
    )
    if enable_worker:
        worker_session_factory = sessionmaker(
            autocommit=False, autoflush=False, bind=db_engine
        )
        worker = ClinicalAutomationWorker(
            session_factory=worker_session_factory,
            enable_weekly_plan=settings.enable_weekly_plan,
            enable_daily_journal=settings.enable_daily_journal,
            enable_wellness_daily=settings.enable_wellness_daily,
            enable_automatic_weekly=settings.enable_automatic_weekly,
        )
        app.state.automation_worker = worker
        await worker.start()
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()
            app.state.automation_worker = None


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
app.include_router(auth_router)
app.include_router(cycles_router)
app.include_router(reports_router)
app.include_router(cascade_router)
app.include_router(vitals_router)
app.include_router(client_cascade_router)
app.include_router(client_chat_router)
