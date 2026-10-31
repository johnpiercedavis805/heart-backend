# HeArt v1 API — invented by John Davis.
"""Application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import routes
from .config import settings
from .db import init_db
from .storage import LocalStorage


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # local dev / tests only; production uses supabase/migrations/
    LocalStorage(settings.UPLOAD_DIR)  # ensure the upload dir exists
    yield


app = FastAPI(
    title="HeArt v1 API",
    description="Creativity-first matching for disclosed AI agents. Invented by John Davis.",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(routes.router)
