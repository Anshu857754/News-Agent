"""Database engine and session handling.

One `DATABASE_URL` decides everything. It defaults to a SQLite file so the
project runs with nothing installed, and points at PostgreSQL in any real
deployment - the models and queries are the same either way, because nothing
here uses a dialect-specific feature.

    DATABASE_URL=sqlite:///./data/startuppulse.db          (default)
    DATABASE_URL=postgresql+psycopg://user:pass@host/db    (production)

Tables are created on startup with `create_all`. That is honest for a project
at this stage: there is no migration history to preserve yet. The moment the
schema has to change under real data, this becomes Alembic.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import PROJECT_ROOT, get_settings

log = logging.getLogger(__name__)


def _prepare_sqlite_path(url: str) -> str:
    """Make sure the folder for a SQLite file exists before connecting."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return url

    raw = url[len(prefix):]
    if raw == ":memory:" or raw.startswith(":memory:"):
        return url

    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / raw.lstrip("./")
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"{prefix}{path.as_posix()}"


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """The process-wide engine."""
    settings = get_settings()
    url = _prepare_sqlite_path(settings.database_url)

    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        # FastAPI serves requests from a threadpool, and SQLite objects are
        # otherwise pinned to the thread that created them.
        kwargs["connect_args"] = {"check_same_thread": False}

    log.info("database: %s", url.split("@")[-1])   # never log credentials
    return create_engine(url, **kwargs)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create any missing tables. Safe to call on every startup."""
    from app.models import db as db_models   # noqa: F401 - registers the mappers

    db_models.Base.metadata.create_all(bind=get_engine())
    log.info("database ready: %d tables", len(db_models.Base.metadata.tables))


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For code outside a request - commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
