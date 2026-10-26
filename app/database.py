"""Database engine/session setup.

Defaults to a local SQLite file so a reviewer can run this with zero setup.
Set DATABASE_URL to a Postgres URL (e.g. postgresql+psycopg2://user:pass@host/db)
to run against Postgres instead -- the SQLAlchemy models work unchanged
against either, since they stick to portable column types.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ledger.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
