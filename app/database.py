"""Database engine/session setup.

Defaults to a local SQLite file so a reviewer can run this with zero setup.
Set DATABASE_URL to a Postgres URL (e.g. postgresql+psycopg2://user:pass@host/db)
to run against Postgres instead -- the SQLAlchemy models work unchanged
against either, since they stick to portable column types.
"""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ledger.db")

def configure_sqlite(engine):
    """Take SQLite's write lock when a transaction starts (BEGIN IMMEDIATE).

    SQLite ignores SELECT ... FOR UPDATE, so without this two requests can both
    read balance=100, both pass the check and both write. With it, writers queue
    up and the read-check-write sequence in crud.py is effectively serialised.
    Standard recipe from the SQLAlchemy docs (pysqlite serializable isolation).
    """

    @event.listens_for(engine, "connect")
    def _no_implicit_begin(dbapi_conn, _record):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


is_sqlite = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
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
