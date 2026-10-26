# Ledger API

Take-home for Confluencr — a wallet/ledger service in FastAPI + SQLite.

## What it does

- Create accounts with UUID-based IDs
- Credit and debit with idempotent `reference_id` (duplicate = 200 with original txn, balance unchanged)
- Transfer between accounts atomically (both legs in one DB commit)
- Transaction history with pagination and type filtering

## Setup

Needs **Python 3.11 or newer** (SQLAlchemy 2.1 requires it).

```bash
python -m venv venv
venv\Scripts\activate    # or source venv/bin/activate on mac/linux
pip install -r requirements-dev.txt     # runtime + test tools; use requirements.txt for the API only
```

All dependencies are pinned to the latest releases as of 26 Oct 2026 (FastAPI 0.162, SQLAlchemy 2.1, Pydantic 2.14, pytest 9, ...).

## Run

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for the interactive API.

## Run with Docker

The image is a two-stage build on `python:3.15-slim`: dependencies are installed with [uv](https://docs.astral.sh/uv/) into a virtualenv, and the runtime stage holds only Python, that virtualenv and `app/`. It runs as a non-root user and has a `HEALTHCHECK` on `GET /health`.

```bash
# API + Postgres 18 (data kept in a named volume)
POSTGRES_PASSWORD=choose-one docker compose up --build        # PowerShell: $env:POSTGRES_PASSWORD = "choose-one"
# -> http://127.0.0.1:8000/docs

# or just the API on SQLite, with the database file kept in a volume
docker build -t ledger-api .
docker run -p 8000:8000 -v ledger-data:/data ledger-api
```

Podman runs the same files (`podman compose up --build`). `compose.yaml` stores no password: set `POSTGRES_PASSWORD` (environment variable or a `.env` file, see `.env.example`) before running it, and compose refuses to start without it.

## Run on Postgres

SQLite is the default, so nothing is needed to try it. To use Postgres, set `DATABASE_URL` (as an environment variable or in a `.env` file, see `.env.example`) before starting the server:

```bash
# any Postgres, local or hosted (Neon, Supabase, RDS...)
export DATABASE_URL="postgresql://user:password@host:5432/dbname"      # PowerShell: $env:DATABASE_URL = "..."
uvicorn app.main:app
```

The tables are created on startup. `postgres://` and plain `postgresql://` URLs are accepted; they're rewritten to use `psycopg2` (SQLAlchemy 2.1 would otherwise default to the newer `psycopg` driver, which isn't installed). To run the test suite against Postgres, point `TEST_DATABASE_URL` at a throwaway database (the tests delete all rows in it) and run `pytest -q`.

## Test

```bash
pytest -q
```

339 tests, plus `scripts/curl_smoke.sh` (139 curl checks against a running server). What they cover, and the 14 bugs they found, is in [TESTING.md](TESTING.md).

## API

Full request/response examples are in [API_DOCUMENTATION.md](API_DOCUMENTATION.md). My assumptions are in [ASSUMPTIONS.md](ASSUMPTIONS.md), and what I tested in [TESTING.md](TESTING.md).

## Design decisions

1. **Idempotency via reference_id** — I went with Stripe's approach: duplicate POST returns 200 + the original transaction with a `duplicate: true` flag, an "already exists" message and an `Idempotent-Replayed: true` header, instead of an error. That's what the IETF Idempotency-Key draft (§2.6) and Stripe do for retries. The client doesn't need special error handling, it just gets back what it expects. The DB unique constraint `(account_id, reference_id)` is the real guardrail; the SELECT before INSERT is just a fast path.

2. **Balance column vs SUM(ledger)** — storing balance directly is O(1) per read instead of scanning the whole ledger. The tradeoff is that the balance and the ledger can drift if writes aren't atomic. I keep them in sync by updating both in a single `db.commit()`.

3. **422 for insufficient balance** — the request is valid JSON, just breaks a business rule. Some teams use 400 or 409, I don't think it matters much as long as you pick one.

4. **SQLite for dev, Postgres for prod** — SQLite means the reviewer can run this with zero setup. Just set `DATABASE_URL` to switch. Concurrent requests can't both pass the balance check: Postgres uses a row lock (`SELECT ... FOR UPDATE`), and SQLite takes its write lock at the start of each transaction (`BEGIN IMMEDIATE`). A threaded test caught this bug on SQLite before I added that, see [TESTING.md](TESTING.md).

5. **UUID IDs** — not guessable, no info leakage about how many accounts exist, safe to generate without coordinating across services.

6. **Decimal, not float** — `0.1 + 0.2 != 0.3` in IEEE 754. Money columns are `Numeric(18,2)`, Python side uses `Decimal`, and the API rejects amounts with more than 2 decimal places so nothing gets silently rounded.

7. **Offset pagination** — simple and good enough for this scope. I'd switch to cursor/keyset (`WHERE created_at < :cursor ORDER BY created_at DESC, id`) for production since offset gets slow on deep pages.

8. **Reusing a `reference_id` with a different type or amount returns 422** (IETF Idempotency-Key draft §2.7). A replay of the same request is harmless (200), but the same reference with different values is almost certainly a client bug and shouldn't silently succeed.

9. **Strict input, on purpose.** Amounts must be plain decimals (no `1e2`, `+10`, `NaN`, spaces, non-ASCII digits), capped at 1 trillion per transaction, with a balance ceiling of 9,999,999,999,999.99. The first version accepted `1e30` and a 400-digit amount; the edge-case probe I wrote showed that one oversized value made every later read of that account return 500. Text fields are trimmed and reject control characters (a NUL byte makes Postgres throw), and `description` is capped at 500 characters to match the column.

10. **Transfer legs are stored as `transfer:<ref>:debit|credit`** and ordinary references may not start with `transfer:`. Before that, a transfer reference and an ordinary one could collide, and reusing a transfer reference with a different destination crashed with a 500; both are covered now.

11. **Every error has the same `{error, message}` body**, including 404/405/400 and unexpected 500s (the 500 body never includes internals). Trailing-slash redirects are off, because a redirected POST can silently lose its body.

## Known limitations

- **SQLite stores money as floating point.** That's why the balance ceiling is 15 significant digits: beyond that SQLite can't keep cents exact, while Postgres `NUMERIC` could. Use Postgres for anything real.
- **SQLite serialises everything**, including reads, because of `BEGIN IMMEDIATE`. Fine for a take-home, not for real load.
- **No auth** — in production I'd add API key or JWT middleware.
- **Single currency** — no currency field. Would add it as a non-nullable column with a migration.
- **No Alembic** — using `create_all()` for simplicity. Real project would use migrations.
- **A transfer is two ledger rows, not its own record.** There's no `transfers` table. Both legs share one commit so a half-transfer can't be stored, but if someone repaired the data by hand and left only one leg, a retry would return a 422 conflict instead of fixing it. A real system would have a transfers table with its own unique key.

## What I'd add with more time

- Row locking / atomic conditional updates for concurrent safety
- Auth (API keys per client)
- Alembic migrations
- Docker + docker-compose with Postgres
- Rate limiting
- Structured logging (not print statements)
- Concurrency test with threading to prove the race condition fix works
- Double-entry bookkeeping (every transfer = 2 ledger lines that sum to zero)
