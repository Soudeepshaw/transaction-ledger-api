# Ledger API

Take-home for Confluencr — a wallet/ledger service in FastAPI + SQLite.

## What it does

- Create accounts with UUID-based IDs
- Credit and debit with idempotent `reference_id` (duplicate = 200 with original txn, balance unchanged)
- Transfer between accounts atomically (both legs in one DB commit)
- Transaction history with pagination and type filtering

## Setup

```bash
python -m venv venv
venv\Scripts\activate    # or source venv/bin/activate on mac/linux
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for the interactive API.

## Test

```bash
pytest -q
```

73 tests. What each one covers, and the bug they found, is in [TESTING.md](TESTING.md).

## API

Full request/response examples are in [API_DOCUMENTATION.md](API_DOCUMENTATION.md). The requirements I worked from are in [PRD.md](PRD.md), and my assumptions in [ASSUMPTIONS.md](ASSUMPTIONS.md).

## Design decisions

1. **Idempotency via reference_id** — I went with Stripe's approach: duplicate POST returns 200 + the original transaction with a `duplicate: true` flag, an "already exists" message and an `Idempotent-Replayed: true` header, instead of an error. That's what the IETF Idempotency-Key draft (§2.6) and Stripe do for retries. The client doesn't need special error handling, it just gets back what it expects. The DB unique constraint `(account_id, reference_id)` is the real guardrail; the SELECT before INSERT is just a fast path.

2. **Balance column vs SUM(ledger)** — storing balance directly is O(1) per read instead of scanning the whole ledger. The tradeoff is that the balance and the ledger can drift if writes aren't atomic. I keep them in sync by updating both in a single `db.commit()`.

3. **422 for insufficient balance** — the request is valid JSON, just breaks a business rule. Some teams use 400 or 409, I don't think it matters much as long as you pick one.

4. **SQLite for dev, Postgres for prod** — SQLite means the reviewer can run this with zero setup. Just set `DATABASE_URL` to switch. Concurrent requests can't both pass the balance check: Postgres uses a row lock (`SELECT ... FOR UPDATE`), and SQLite takes its write lock at the start of each transaction (`BEGIN IMMEDIATE`). A threaded test caught this bug on SQLite before I added that, see [TESTING.md](TESTING.md).

5. **UUID IDs** — not guessable, no info leakage about how many accounts exist, safe to generate without coordinating across services.

6. **Decimal, not float** — `0.1 + 0.2 != 0.3` in IEEE 754. Money columns are `Numeric(18,2)`, Python side uses `Decimal`, and the API rejects amounts with more than 2 decimal places so nothing gets silently rounded.

7. **Offset pagination** — simple and good enough for this scope. I'd switch to cursor/keyset (`WHERE created_at < :cursor ORDER BY created_at DESC, id`) for production since offset gets slow on deep pages.

8. **Reusing a `reference_id` with a different type or amount returns 422** (IETF Idempotency-Key draft §2.7). A replay of the same request is harmless (200), but the same reference with different values is almost certainly a client bug and shouldn't silently succeed.

## Known limitations

- **Postgres path is untested** — the concurrency tests run on SQLite only. The `FOR UPDATE` path hasn't been run against a real Postgres.
- **SQLite serialises everything**, including reads, because of `BEGIN IMMEDIATE`. Fine for a take-home, not for real load.
- **Timestamps come back without a timezone** — they're stored as UTC but SQLite drops the tzinfo, so `created_at` has no `Z`. Postgres keeps it.
- **No auth** — in production I'd add API key or JWT middleware.
- **Single currency** — no currency field. Would add it as a non-nullable column with a migration.
- **No Alembic** — using `create_all()` for simplicity. Real project would use migrations.
- **Transfer idempotency is two-legged** — uses `{ref}:debit` and `{ref}:credit` as sub-references. If only one leg made it (shouldn't happen since they share a commit, but still), a retry would see a partial match. Edge case I'd want to handle more carefully in production.

## What I'd add with more time

- Row locking / atomic conditional updates for concurrent safety
- Auth (API keys per client)
- Alembic migrations
- Docker + docker-compose with Postgres
- Rate limiting
- Structured logging (not print statements)
- Concurrency test with threading to prove the race condition fix works
- Double-entry bookkeeping (every transfer = 2 ledger lines that sum to zero)
