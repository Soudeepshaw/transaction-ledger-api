# Testing

```bash
pytest -q          # 71 tests, ~5s, in-memory SQLite (concurrency tests use a temp file DB)
```

Tests live in `tests/test_api.py` (the main flows) and `tests/test_scenarios.py` (edge cases, grouped by PRD section).

## What is covered

| PRD | Scenario | Expected |
|---|---|---|
| FR-1 | create account | 201, balance `0.00`, system `created_at`, unique id |
| FR-1 | missing / empty `name` | 422 |
| FR-1 | client sends `balance: 999` | ignored, balance stays `0.00` |
| FR-1 | two accounts, same name | different ids |
| FR-2 | credit, debit, mixed sequence (100 + 50 - 30) | 120.00 |
| FR-2 | debit exactly the balance | OK, balance 0.00 |
| FR-2 | debit more than balance / on empty account | 422, balance unchanged, **no transaction row written** |
| FR-2 | unknown account | 404 |
| FR-2 | amount `0`, `-5`, `abc`, empty | 422, balance unchanged |
| FR-2 | amount with 3 decimal places (`10.005`) | 422 |
| FR-2 | invalid `type`, missing `reference_id` | 422 |
| FR-2 | missing `description` | OK (optional) |
| FR-2 | 0.10 + 0.20 | exactly 0.30 (no float drift) |
| FR-2 | duplicate `reference_id` (credit and debit) | 200, `duplicate: true`, original returned, balance changes once, one row |
| FR-2 | duplicate with different description | still a replay, original description kept |
| FR-2 | same `reference_id` on a different account | allowed, 201 |
| FR-2 | same `reference_id`, different amount or type | 409, balance unchanged |
| FR-3 | balance of existing / unknown account | `{account_id, balance}` / 404 |
| FR-4 | no filter returns both types; filter by credit / debit | correct subsets |
| FR-4 | latest first; pages don't overlap and together cover everything | verified |
| FR-4 | pagination meta (page, size, total records, total pages) | verified, also when filtered |
| FR-4 | page past the end / empty account | empty `data`, not an error; `total_pages` 0 for empty |
| FR-4 | `page=0`, `page=-1`, `limit=0`, `limit=201`, `type=refund`, `page=abc` | 422 |
| FR-4 | unknown account; another account's transactions | 404; never leaks |
| Integrity | no PUT / PATCH / DELETE on transactions or accounts | 405, nothing changed |
| Integrity | brief's example 0 +100 +50 -30 -20 | 100.00 |
| Integrity | balance vs sum of the ledger after rejected and duplicate requests | equal |
| Errors | 404 (account, route), 405, 409, 422 (domain and validation), malformed JSON | all use `{error, message}` |
| Errors | validation error names the bad field | `details[0].field == "name"` |
| DB constraints | negative balance, zero amount, bad type, duplicate reference inserted directly | rejected by the database itself |
| Transfer | success, exact balance, unknown account, same account | 201 / 201 / 404 / 422 |
| Transfer | insufficient funds | 422, both balances and both ledgers untouched |
| Transfer | retry after the source was drained | 200 with the original result, not 422 |
| Transfer | `commit()` fails mid-transfer | both balances unchanged (atomic) |
| Concurrency | 20 threads post the same `reference_id` | exactly 1 transaction, credited once |
| Concurrency | 10 threads debit 30 each from a balance of 100 | at most 3 succeed, balance never negative, balance equals 100 minus recorded debits |

## A bug the tests found

The concurrent-debit test failed on the first run: all 10 debits succeeded and the balance ended at 70 while 10 debits were recorded. SQLite ignores `SELECT ... FOR UPDATE`, so two requests could read the same balance, both pass the check, and overwrite each other.

Fix: `database.py` now makes SQLite take its write lock when a transaction starts (`BEGIN IMMEDIATE`), so the read, check and write happen one request at a time. On Postgres the row lock in `crud.get_account(lock=True)` does the same job. The test was then run 15 more times with no failures.

The same test also caught that a retried transfer returned 201 instead of 200, which I fixed.

## Not covered

- **Postgres.** The row-lock path (`FOR UPDATE`) has never been run against a real Postgres. Only the SQLite path is tested.
- **Transfer with a reused `reference_id` but a different amount.** Plain transactions return 409, transfers currently treat it as a replay.
- **Load / performance.** Nothing measures throughput or deep-page speed.
- **Timezones.** SQLite drops tzinfo, so `created_at` has no `Z` in test output.
