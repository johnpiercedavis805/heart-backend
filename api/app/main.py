# HeArt v1 API — invented by John Davis.
"""Application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import billing_routes, crypto_routes, growth_routes, routes, subscribe_routes
from .config import settings
from .db import init_db
from .storage import LocalStorage


@asynccontextmanager
async def lifespan(app: FastAPI):
    # init_db() is CREATE TABLE IF NOT EXISTS — safe for SQLite local dev
    # and tests only. Production (Postgres) MUST use supabase/migrations/
    # via scripts/apply_migrations.py; create_all would bypass migrations
    # and crash against Postgres (text->uuid FK mismatches, now fixed).
    if settings.DATABASE_URL.startswith("sqlite"):
        init_db()
    LocalStorage(settings.UPLOAD_DIR)  # ensure the upload dir exists
    yield


app = FastAPI(
    title="HeArt v1 API",
    description="Creativity-first matching for disclosed AI agents. Invented by John Davis.",
    version="1.0.0",
    lifespan=lifespan,
    # T2 red-pen item 9: interactive docs are off unless ENABLE_API_DOCS=1
    # (local dev only). An invite-only service should not publish its full
    # endpoint surface at /docs and /openapi.json by default.
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)
app.include_router(routes.router)
app.include_router(growth_routes.router)
app.include_router(billing_routes.router)
app.include_router(crypto_routes.router)
app.include_router(subscribe_routes.router)
