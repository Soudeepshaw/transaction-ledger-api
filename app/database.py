"""Database engine and session setup."""
# SQLite by default so the project runs with zero setup; set DATABASE_URL (env var or .env,
# see .env.example) for Postgres. The models only use portable column types.
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()  # real environment variables win over .env


def normalize_url(url: str) -> str:
    """Point bare Postgres URLs at the driver we install (psycopg2)."""
    # Hosts hand out postgres://, which SQLAlchemy 2 rejects, and SQLAlchemy 2.1 made psycopg 3
    # the default for postgresql://, which isn't installed. An explicit +driver is left alone.
    for bare in ("postgres://", "postgresql://"):
        if url.startswith(bare):
            return "postgresql+psycopg2://" + url[len(bare):]
    return url


DATABASE_URL = normalize_url(os.getenv("DATABASE_URL", "sqlite:///./ledger.db"))


def configure_sqlite(engine):
    """Take SQLite's write lock when a transaction starts (BEGIN IMMEDIATE)."""
    # SQLite ignores SELECT ... FOR UPDATE, so two requests could both read balance=100 and
    # both pass the check. Locking up front makes writers queue. Recipe from the SQLAlchemy docs.

    @event.listens_for(engine, "connect")
    def _no_implicit_begin(dbapi_conn, _record):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


is_sqlite = DATABASE_URL.startswith("sqlite")
if is_sqlite:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})
else:
    # pre_ping: serverless Postgres (Neon) suspends idle connections; check before reuse
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
if is_sqlite:
    configure_sqlite(engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
