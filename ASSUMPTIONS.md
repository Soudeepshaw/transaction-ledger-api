# Assumptions

1. **reference_id is unique per account, not globally.** Different clients/accounts might reuse their own reference IDs. The unique constraint is `(account_id, reference_id)`.

2. **Duplicate reference_id with same amount** returns 200 with the original transaction and a `duplicate: true` flag. Balance is not touched. I went with Stripe's pattern here because the client doesn't need to branch on error codes — it just gets back the expected data.

3. **Duplicate reference_id with different amount** — honestly, this is a client bug. Strictly the right response is 409 or 422 saying "reference_id reused with different payload." Implemented: it returns 409 `reference_conflict`.

4. **Single currency (INR implied).** No currency column. If I needed multi-currency, I'd add a non-nullable `currency` field on both Account and Transaction, and reject transfers across different currencies.

5. **No authentication.** This is a take-home, not a production system. In prod I'd use API keys per client validated in middleware, or JWTs for user-facing flows.

6. **SQLite for local dev.** It serialises writes, which hides the concurrent debit race condition. I'm aware the bug exists on Postgres and have documented the fix (SELECT FOR UPDATE or atomic conditional UPDATE) even though I haven't added it. Swap to Postgres by setting `DATABASE_URL`.

7. **Amounts limited to 2 decimal places.** The DB column is Numeric(18,2). Anything with more precision (like 10.005) would get silently rounded by the DB, which is not ok for money. The API validates this upfront and returns 422.

8. **422 for business rule violations** (insufficient balance, bad pagination params, same-account transfer). The request body is valid JSON — it just fails a domain rule. 400 would also be defensible but I find 422 more precise.

9. **Offset pagination.** Works fine for this scope. Cursor pagination (keyset) would be the move for large datasets — I'd paginate on `(created_at, id)` to keep ordering stable when timestamps tie.

10. **Account deletion not supported.** Deleting accounts with existing transactions would break the ledger. In production, I'd add a soft-delete / frozen status instead.
