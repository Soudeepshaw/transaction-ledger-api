# Assumptions

1. **reference_id is unique per account, not globally.** Different clients/accounts might reuse their own reference IDs. The unique constraint is `(account_id, reference_id)`.

2. **Duplicate reference_id with same amount** returns 200 with the original transaction and a `duplicate: true` flag. Balance is not touched. I went with Stripe's pattern here because the client doesn't need to branch on error codes — it just gets back the expected data.

3. **Duplicate reference_id with different amount** — honestly, this is a client bug. It returns 422 `reference_conflict`, which is what the IETF Idempotency-Key draft (§2.7) recommends for a reused key with a different payload. 409 there is reserved for a request that is still being processed.

4. **Single currency (INR implied).** No currency column. If I needed multi-currency, I'd add a non-nullable `currency` field on both Account and Transaction, and reject transfers across different currencies.

5. **No authentication.** This is a take-home, not a production system. In prod I'd use API keys per client validated in middleware, or JWTs for user-facing flows.

6. **Database: the brief allows any, so I used SQLite.** It needs no setup for the reviewer. It ignores `SELECT ... FOR UPDATE`, which let concurrent debits overdraw until I made it take the write lock up front (`BEGIN IMMEDIATE`); a threaded test covers that. On Postgres the row lock is used instead, but I haven't run that path. Swap to Postgres by setting `DATABASE_URL`.

7. **Amounts limited to 2 decimal places.** The DB column is Numeric(18,2). Anything with more precision (like 10.005) would get silently rounded by the DB, which is not ok for money. The API validates this upfront and returns 422.

8. **422 for business rule violations** (insufficient balance, bad pagination params, same-account transfer). The request body is valid JSON — it just fails a domain rule. 400 would also be defensible but I find 422 more precise.

9. **Offset pagination.** Works fine for this scope. Cursor pagination (keyset) would be the move for large datasets — I'd paginate on `(created_at, id)` to keep ordering stable when timestamps tie.

10. **Account deletion not supported.** Deleting accounts with existing transactions would break the ledger. In production, I'd add a soft-delete / frozen status instead.

11. **Money limits.** One transaction is at most 1,000,000,000,000.00 and a balance can't exceed 9,999,999,999,999.99. The brief doesn't set limits; I added them because an unbounded amount made the column overflow on Postgres and broke later reads of that account. The ceiling is 15 significant digits so cents stay exact even on SQLite, which stores numbers as floats.

12. **Amounts are plain decimals.** `1e2`, `+10`, ` 10 `, `NaN`, `1,000` and non-ASCII digits are rejected rather than guessed at. `10.000` is accepted because it is exactly 10.00; `10.005` is rejected because it would need rounding.

13. **Text is trimmed and must not contain control characters.** `" r1 "` and `"r1"` are the same `reference_id` (otherwise padding would dodge idempotency). Names and references are case-sensitive and may be any Unicode.

14. **`transfer:` is a reserved reference prefix.** Transfer legs are stored as `transfer:<ref>:debit` and `transfer:<ref>:credit`, so an ordinary reference can never collide with one. Transfer references are limited to 150 characters so the stored leg still fits in 200.

15. **Reusing a transfer reference.** Same pair and amount is a replay (200). A different destination or amount is a 422 `reference_conflict`. The same reference in the opposite direction is a separate transfer, because references are unique per account.

16. **No trailing-slash redirects.** `POST /accounts/` is a 404, not a 307, because clients often drop the body when following a redirect.

17. **Unexpected failures return a generic 500** in the usual `{error, message}` shape, with nothing internal in the message. A failed commit rolls back, so nothing is half-saved and the `reference_id` is not burned.

18. **Unicode is normalised (NFC) and look-alikes are refused.** `café` typed as one character or as `e` plus an accent is the same reference. Zero-width and other invisible characters are rejected in references (they'd let two different-looking-identical references exist), and text-direction override characters are rejected everywhere (they make a name display as something else).

19. **When two requests race with the same reference but different amounts**, one wins and the other gets the 422 conflict. Nobody is told "already exists" for an amount that wasn't recorded.

20. **Duplicate keys in a JSON body**: the last one wins (standard JSON parsing). Clients shouldn't send them.
