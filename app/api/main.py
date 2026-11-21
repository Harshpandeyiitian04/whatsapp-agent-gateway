"""Gateway API: webhook, metrics, health, admin."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import admin, metrics, webhook
from app.api.state import AppState
from app.api.timing import WebhookTimingMiddleware
from app.clock import SystemClock
from app.config import get_settings
from app.db import open_pool
from app.obs.logs import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    pool = await open_pool(settings.database_url, min_size=10, max_size=30)
    app.state.app_state = AppState(settings=settings, pool=pool, clock=SystemClock())
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="WhatsApp Agent Gateway", lifespan=lifespan)
app.add_middleware(WebhookTimingMiddleware)
app.include_router(webhook.router)
app.include_router(metrics.router)
app.include_router(admin.router)


@app.get("/health")
async def health() -> JSONResponse:
    state: AppState = app.state.app_state
    try:
        async with state.pool.connection(timeout=2) as conn:
            await conn.execute("SELECT 1")
        return JSONResponse({"status": "ok", "db": "ok"})
    except Exception:
        return JSONResponse({"status": "degraded", "db": "error"}, status_code=503)
