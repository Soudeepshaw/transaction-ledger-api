# Testing

## How to run

```bash
pip install -r requirements-dev.txt
pytest -q                                   # 339 tests, in-memory SQLite, ~20s

# same tests against Postgres: a THROWAWAY database, every row is deleted between tests
TEST_DATABASE_URL="postgresql://user:pass@host/db" pytest -q

# black-box: start the server, then hit it with curl (139 checks, including parallel requests)
uvicorn app.main:app --port 8000
bash scripts/curl_smoke.sh http://127.0.0.1:8000
```

## Results (26-27 Oct 2026, latest dependency versions, Python 3.13)

| | SQLite | Postgres 18.6 (Neon) |
|---|---|---|
| pytest | 339 / 339 | 178 passed, 0 failed (see below) |
| curl script | 139 / 139 | an earlier version of the script ran against Postgres: all checks passed except requests hit by dropped connections from this machine (see "Not covered") |

Postgres pytest detail: the suite is slow against a remote database from here (a few tests a minute), so it was run in two parts. The first part got through 161 of 317 selected tests with 0 failures before it was interrupted. The second part ran the concurrency and failure tests (parallel credits, mixed credits and debits, identical and draining transfers, a dropped commit): all 17 passed with 0 failures (about 11 minutes). The remaining Postgres tests were not run, and the final curl script (139 checks) was not run against Postgres. Everything was run in full on SQLite.

The Docker image and `compose.yaml` were written and the compose file validated (`docker compose config`), but the image has not been built or run here.

## Test files

| File | What it covers |
|---|---|
| `tests/test_api.py` | the main flows from the brief |
| `tests/test_scenarios.py` | every rule and example in the brief, section by section |
| `tests/test_edge_cases.py` | hostile input, boundaries, idempotency, pagination, transfers, failures, concurrency, random histories |
| `tests/test_hardening.py` | race on the conflict path, Unicode look-alikes, health check |
| `tests/test_config.py` | database URL handling |
| `scripts/curl_smoke.sh` | the same ground, black-box, against a running server |

## Where the edge cases came from

I listed the ways money APIs usually break, from open-source ledger projects and write-ups on idempotency, decimal handling and pagination, then wrote a probe that fired about 100 odd requests at the API and recorded every non-4xx answer. Each finding became a test. The main themes:

- **Double posting**: retries, duplicate references, parallel duplicates, a retry after the balance changed.
- **Lost updates**: parallel debits, credits and transfers on the same account; opposite-direction transfers (deadlock).
- **Money format**: floats, `NaN`/`Infinity`, exponents, signs, separators, non-ASCII digits, more than 2 decimals, overflow.
- **Text**: empty or whitespace, control and NUL characters, invisible characters, text-direction overrides, unpaired surrogates, Unicode normalisation, very long values, SQL-looking strings.
- **Pagination**: bounds, huge values, rows with identical timestamps, every row exactly once.
- **Failure**: the database dropping mid-commit, unexpected exceptions, an unreachable database for the health check.

## What is covered

| Area | Scenarios | Expected |
|---|---|---|
| Accounts | missing / empty / whitespace / NUL / 201-char name; client sends `id`, `balance`, `created_at`; unicode names; parallel creation | 422 for bad names; server-owned fields ignored; unique ids |
| Amounts | `1`, `10.5`, `10.000`, `0.01`, `1000000000000` accepted; `0`, `-1`, `NaN`, `Infinity`, `1e2`, `+10`, `" 10 "`, `1,000`, `0x10`, `1.005`, `١٢٣`, `１０`, `true`, `null`, `[1]`, 400 digits, more than 1 trillion rejected | 201 / 422, balance unchanged |
| Exactness | 0.10 + 0.20; 100 x 0.01; debit 0.31 from 0.30 | 0.30; 1.00; 422 |
| Ceiling | credit past 9,999,999,999,999.99; transfer into a full account | 422 `balance_limit_exceeded`, nothing moves |
| References | 200 chars, unicode, emoji, SQL-looking, `../`, `<script>`; 201 chars, control chars, zero-width, BOM, `transfer:` prefix; padded `" r1 "`; `café` typed two ways; case | accepted / 422; padding and NFC map to the same reference; case-sensitive |
| Idempotency | replay; replay after the balance changed; `10` vs `10.00`; different description; different amount or type; rejected and invalid requests don't use up the reference; same reference on another account | 200 with original; 422 conflict; reference reusable |
| Brief's examples | 100+50=150, 100-30=70, 150 from 100 rejected, 0+100+50-30-20=100, A100/B50 transfer 30 -> 70/80 | exact |
| History | default both types; filter; latest first; 250 rows across pages; 30 rows with the same timestamp; page math; `page` 0 / -1 / 1.5 / 1,000,001 / 2^63; `limit` 0 / 201; `type=CREDIT` | each row exactly once, stable order; 422 for bad input, never 500 |
| Transfers | success; retry; retry after source drained; reuse with another destination or amount; opposite direction; collision with `x:debit`; 150/151-char reference; bad amounts; unknown, empty, NUL or 65-char account id; self-transfer | 201 / 200 / 422 / 404 as documented; failed transfers change nothing |
| Errors | 404 account and route, 405 on every wrong method, 400 for invalid UTF-8, malformed JSON, wrong content type, deep nesting, 2 MB body | one `{error, message}` shape everywhere |
| Failures | `commit()` raises mid-transaction and mid-transfer; unexpected exception; database down for `/health` | JSON 500 / 503 without internals; nothing saved; reference still usable |
| Concurrency | 20 identical requests; 10 debits of 30 from 100; 40 parallel credits; 60 mixed credits/debits; 15 identical transfers; 10 transfers draining one account; 40 opposite-direction transfers; same reference with different amounts | one row / exactly 3 succeed / no lost update / ledger equals balance / money conserved / no deadlock / no fake replay |
| Invariants | 400 random credits, debits, transfers and replays over 4 accounts | balance never negative, equals the ledger, and total money = deposits - withdrawals |

## Bugs the tests found (all fixed)

1. Concurrent debits overdrew an account on SQLite (`FOR UPDATE` is ignored there). Fixed with `BEGIN IMMEDIATE`.
2. A duplicate `reference_id` returned 201 instead of 200; a retried transfer did the same.
3. Amounts like `1e30` or a 400-digit number were accepted, after which every read of that account returned 500.
4. `1e2`, `+10`, `" 10 "` and Arabic-Indic digits were accepted as money.
5. A balance could grow past what the column holds; near 10^16 SQLite also loses the cents (it stores floats), so the ceiling is 15 significant digits.
6. Whitespace-only names and references were accepted; NUL bytes (which crash Postgres) and over-long descriptions reached the database.
7. Reusing a transfer reference with another destination crashed with 500; a 200-character transfer reference overflowed the stored leg; an ordinary reference could collide with a transfer leg.
8. `page=9223372036854775807` overflowed the SQL offset.
9. Trailing-slash requests were redirected (307), which can drop a POST body.
10. `description: null` returned 500.
11. Validation, 404 and 405 errors used a different body shape from domain errors.
12. Two requests racing with the same reference but different amounts: the loser was told "already exists" for an amount that was never recorded.
13. `café` typed as one character or as `e` + accent counted as two references; zero-width characters allowed look-alike references.
14. After upgrading to SQLAlchemy 2.1, plain `postgresql://` URLs failed because 2.1 switched its default driver; only a real Postgres run showed it.

## Not covered

- **Load and latency.** Nothing measures throughput.
- **Network faults beyond a dropped commit.** Against the remote Neon database, bursts of parallel requests sometimes hit DNS failures or dropped connections from this machine. The API answered those with a JSON 500, saved nothing and conserved money, and because every write carries a `reference_id`, clients can retry safely. There is no automatic retry inside the server.
- **Offset pagination under concurrent writes.** New rows can shift pages while a client is paging; cursor pagination would fix it.
- **The Docker image** has not been built or run.
