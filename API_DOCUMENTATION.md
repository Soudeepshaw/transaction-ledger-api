# API documentation

Base URL: `http://127.0.0.1:8000/api/v1` (interactive docs at `/docs`).
All bodies are JSON (UTF-8). Amounts are returned as strings (e.g. `"100.00"`). Timestamps are UTC, e.g. `2026-10-26T17:48:46.231670Z`.

Errors always look like `{"error": "<code>", "message": "<text>"}`.
Malformed input (missing fields, wrong types, bad `type` filter, more than 2 decimals, invalid JSON) is rejected with 422 `validation_error` and adds a `details` list naming each bad field:
```json
{"error":"validation_error","message":"Invalid request data.","details":[{"field":"name","message":"Field required"}]}
```
Unknown routes return 404 `not_found` and wrong methods (e.g. DELETE on a transaction) return 405 `method_not_allowed`, in the same shape. Transactions and accounts cannot be edited or deleted.

| Status | `error` code | When |
|---|---|---|
| 404 | `account_not_found` | account id doesn't exist |
| 404 | `not_found` | unknown route |
| 405 | `method_not_allowed` | e.g. PUT/DELETE on a transaction |
| 422 | `reference_conflict` | `reference_id` already used on this account with a different type/amount (or a transfer with a different amount) |
| 422 | `insufficient_balance` | debit or transfer larger than the balance |
| 422 | `validation_error` | bad body or query (see the rules below), or a transfer to the same account |
| 422 | `balance_limit_exceeded` | the credit would take an account above 9,999,999,999,999.99 |
| 400 | `bad_request` | body isn't valid UTF-8 |
| 500 | `internal_error` | unexpected failure; nothing was changed, and no internal details are returned |

## Validation rules

| Field | Rule |
|---|---|
| `amount` | plain decimal (`10`, `10.5`, `10.50`; `10.000` is fine), more than 0, at most 1,000,000,000,000.00, at most 2 decimal places. Rejected: `NaN`, `Infinity`, exponents (`1e2`), signs (`+10`), spaces, thousands separators, non-ASCII digits, booleans, `null`, arrays |
| `reference_id` | 1-200 characters (1-150 for transfers) after trimming; no control characters, invisible characters (zero-width space, BOM, soft hyphen...) or text-direction overrides; must not start with `transfer:` (reserved). Case-sensitive. Unicode is fine and is normalised (NFC), so `café` typed either way is the same reference |
| `description` | optional, up to 500 characters; `null` is treated as empty; no control characters except newline and tab |
| `name` | 1-200 characters after trimming, NFC-normalised; no control characters or text-direction overrides (emoji, including ones joined with zero-width joiners, are fine) |
| `type` | exactly `credit` or `debit` (lowercase) |
| `page` / `limit` | `page` 1-1,000,000; `limit` 1-200 |
| account ids | an id that is not a 36-character UUID-style value simply doesn't exist (404) |

Fields the server owns (`id`, `created_at`, `balance`, a transaction's `account_id`) are ignored if a client sends them.
Leading and trailing spaces in text fields are trimmed, so `" r1 "` and `"r1"` are the same reference.

## GET /health

`200 {"status": "ok"}` when the app can reach its database, otherwise `503 {"error": "database_unavailable", ...}`. Used by the Docker health check. Not under `/api/v1`.

## POST /accounts

Create an account. Balance always starts at 0; any `balance` sent by the client is ignored.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/accounts \
  -H "Content-Type: application/json" -d '{"name":"Alice"}'
```
`201`
```json
{"id":"95d3def2-99ff-48cd-9d93-c52606d25dbd","name":"Alice","balance":"0.00","created_at":"2026-10-26T16:44:40.751113"}
```

## GET /accounts/{account_id}

Returns the same shape as above. `404 account_not_found` if it doesn't exist.

## GET /accounts/{account_id}/balance

```bash
curl http://127.0.0.1:8000/api/v1/accounts/$ID/balance
```
`200`
```json
{"account_id":"95d3def2-99ff-48cd-9d93-c52606d25dbd","balance":"100.00"}
```

## POST /accounts/{account_id}/transactions

| Field | Type | Notes |
|---|---|---|
| `type` | `"credit"` or `"debit"` | required |
| `amount` | decimal > 0, max 2 places | required |
| `description` | string | optional |
| `reference_id` | string, 1-200 chars | required, unique per account |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/accounts/$ID/transactions \
  -H "Content-Type: application/json" \
  -d '{"type":"credit","amount":"100.00","description":"salary","reference_id":"r1"}'
```
`201` (new transaction)
```json
{"id":"c0b5aee7-f068-462d-91b0-74183f8f3446","account_id":"95d3def2-...","type":"credit","amount":"100.00","description":"salary","reference_id":"r1","created_at":"2026-10-26T16:44:40.981408","duplicate":false}
```

Sending the same request again returns **`200`** with the `Idempotent-Replayed: true` header and the original transaction, `"duplicate": true` and a `message` saying it already exists (`"Transaction already exists for this reference_id; nothing was changed."`). No new row is created and the balance doesn't change.

Debit larger than the balance: `422`, balance unchanged.
```json
{"error":"insufficient_balance","message":"Account '95d3def2-...' balance 100.00, cannot debit 150."}
```

Same `reference_id` with a different type or amount: `422` (IETF Idempotency-Key draft §2.7).
```json
{"error":"reference_conflict","message":"reference_id 'r1' was already used with a different type/amount."}
```

## GET /accounts/{account_id}/transactions

| Query param | Default | Notes |
|---|---|---|
| `type` | both | `credit` or `debit` |
| `page` | 1 | >= 1 |
| `limit` | 20 | 1-200 |

Newest first (`created_at` descending, `id` as tie-break).

```bash
curl "http://127.0.0.1:8000/api/v1/accounts/$ID/transactions?type=credit&page=1&limit=10"
```
`200`
```json
{
  "data": [ { "id": "...", "type": "credit", "amount": "100.00", "reference_id": "r1", "...": "..." } ],
  "pagination": { "current_page": 1, "page_size": 10, "total_records": 1, "total_pages": 1 }
}
```

## POST /transfers (extra, not in the brief)

Moves money between two accounts. Both legs commit together or not at all. Safe to retry with the same `reference_id` (the two legs are stored as `<ref>:debit` and `<ref>:credit`).

```bash
curl -X POST http://127.0.0.1:8000/api/v1/transfers \
  -H "Content-Type: application/json" \
  -d '{"from_account_id":"<A>","to_account_id":"<B>","amount":"200","reference_id":"xfer-1"}'
```
`201` with `from_account`, `to_account`, `debit_transaction`, `credit_transaction`. Errors: 404 unknown account, 422 insufficient balance or same account.
