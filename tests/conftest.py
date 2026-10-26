"""Shared test database.

By default tests run on an in-memory SQLite DB. To run the same suite against
Postgres, point TEST_DATABASE_URL at a *throwaway* database (rows are deleted
between tests):

    TEST_DATABASE_URL=postgresql://user:pass@host/db pytest -q
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, configure_sqlite, get_db, normalize_url
from app.main import app

TEST_DB_URL = os.getenv("TEST_DATABASE_URL")

if TEST_DB_URL:
    engine = create_engine(normalize_url(TEST_DB_URL), pool_pre_ping=True)
else:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
TestSession = sessionmaker(bind=engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


def _wipe():
    with engine.begin() as conn:
        conn.execute(delete(models.Transaction))
        conn.execute(delete(models.Account))


@pytest.fixture(scope="session", autouse=True)
def schema():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def clean_db():
    app.dependency_overrides[get_db] = override_get_db
    _wipe()
    yield
    _wipe()


@pytest.fixture
def file_db():
    """A session factory where every thread gets its own connection.
    SQLite: a temp file (the in-memory test DB can't be shared across threads).
    Postgres (TEST_DATABASE_URL): the same test database."""
    if TEST_DB_URL:
        yield TestSession
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        eng = create_engine(
            f"sqlite:///{d}/race.db", connect_args={"check_same_thread": False, "timeout": 30}
        )
        configure_sqlite(eng)
        Base.metadata.create_all(eng)
        yield sessionmaker(bind=eng)
        eng.dispose()


def run_parallel(session_factory, fn, jobs, workers=10):
    """Run fn(session, job) for every job on its own thread/session; exceptions are returned, not raised."""

    def wrap(job):
        db = session_factory()
        try:
            return fn(db, job)
        except Exception as exc:  # noqa: BLE001 - we want to count every outcome
            return exc
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(wrap, jobs))
