import pytest

from app.database import normalize_url


@pytest.mark.parametrize(
    "given, expected",
    [
        ("postgres://u:p@host/db", "postgresql+psycopg2://u:p@host/db"),
        ("postgresql://u:p@host/db?sslmode=require", "postgresql+psycopg2://u:p@host/db?sslmode=require"),
        ("postgresql+psycopg2://u:p@host/db", "postgresql+psycopg2://u:p@host/db"),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("sqlite:///./ledger.db", "sqlite:///./ledger.db"),
    ],
)
def test_database_url_normalisation(given, expected):
    assert normalize_url(given) == expected


def test_bare_postgres_url_builds_an_engine_without_needing_psycopg3():
    from sqlalchemy import create_engine

    engine = create_engine(normalize_url("postgresql://u:p@localhost/db"))  # no connection is made
    assert engine.dialect.driver == "psycopg2"
