# API documentation

Base URL: `http://127.0.0.1:8000/api/v1` (interactive docs at `/docs`).
All bodies are JSON. Amounts are strings or numbers with at most 2 decimal places and are returned as strings (e.g. `"100.00"`).

Errors always look like `{"error": "<code>", "message": "<text>"}`.
Malformed input (missing fields, wrong types, bad `type` filter, more than 2 decimals) is rejected with 422 by request validation and uses FastAPI's standard `detail` format instead.

| Status | `error` code | When |
|---|---|---|
| 404 | `account_not_found` | account id doesn't exist |
| 409 | `reference_conflict` | `reference_id` already used on this account with a different type/amount |
| 422 | `insufficient_balance` | debit or transfer larger than the balance |
| 422 | `validation_error` | e.g. transfer to the same account |
| 422 | `invalid_pagination` | `page` < 1 or `limit` outside 1-200 |

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

Sending the same request again returns **`200`** with the original transaction and `"duplicate": true`. No new row is created and the balance doesn't change.

Debit larger than the balance: `422`, balance unchanged.
```json
{"error":"insufficient_balance","message":"Account '95d3def2-...' balance 100.00, cannot debit 150."}
```

Same `reference_id` with a different type or amount: `409`.
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
