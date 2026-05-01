from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, invoices, raw_events, shipments, webhooks
from app.core.config import get_settings
from app.core.db import dispose_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    logger.info("api.startup", extra={"llm_model": settings.llm_model})
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("api.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Glacis Webhook Ingestion",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.include_router(health.router)
    app.include_router(webhooks.router, prefix="/api/v1")
    app.include_router(shipments.router, prefix="/api/v1")
    app.include_router(invoices.router, prefix="/api/v1")
    app.include_router(raw_events.router, prefix="/api/v1")
    return app


app = create_app()
