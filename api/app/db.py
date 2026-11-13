# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Database engine + session. SQLite locally, Postgres (Supabase) in production."""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables. Local dev / tests only — production uses supabase/migrations/."""
    from . import models  # noqa: F401  (register models)

    Base.metadata.create_all(bind=engine)
