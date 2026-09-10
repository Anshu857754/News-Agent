"""Shared test setup.

The one thing that must happen before anything imports the app: point the
database at a throwaway file. Without this the suite would create and mutate
the project's real `data/startuppulse.db`, and tests that assume an empty
database would pass or fail depending on what a previous run left behind.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Set before `app.core.config` is imported anywhere: Settings is cached, so a
# later change would not be picked up.
_TEST_DB = Path(tempfile.gettempdir()) / "startuppulse_tests.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"


@pytest.fixture(scope="session", autouse=True)
def _fresh_database():
    """One empty database for the session, removed afterwards."""
    _TEST_DB.unlink(missing_ok=True)

    from app.core.database import init_db

    init_db()
    yield

    from app.core.database import get_engine

    get_engine().dispose()
    _TEST_DB.unlink(missing_ok=True)


@pytest.fixture()
def db_session():
    """A session for tests that touch the database directly."""
    from app.core.database import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
