from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ── helpers ──────────────────────────────────────────────────────────

def _make_account(name="test acct"):
    resp = client.post("/api/v1/accounts", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


def _credit(acct_id, amount, ref):
    return client.post(
        f"/api/v1/accounts/{acct_id}/transactions",
        json={"type": "credit", "amount": str(amount), "reference_id": ref},
    )


def _debit(acct_id, amount, ref):
    return client.post(
        f"/api/v1/accounts/{acct_id}/transactions",
        json={"type": "debit", "amount": str(amount), "reference_id": ref},
    )


# ── accounts ─────────────────────────────────────────────────────────

def test_create_and_get_account():
    resp = client.post("/api/v1/accounts", json={"name": "Alice"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Alice"
    assert data["balance"] == "0.00"  # Numeric(18,2) always has 2dp

    # fetch the same account
    resp2 = client.get(f"/api/v1/accounts/{data['id']}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == data["id"]


def test_get_nonexistent_account_404():
    resp = client.get("/api/v1/accounts/does-not-exist")
    assert resp.status_code == 404


# ── credit / debit ───────────────────────────────────────────────────

def test_credit_increases_balance():
    aid = _make_account()
    resp = _credit(aid, 100, "ref-1")
    assert resp.status_code == 201

    bal = client.get(f"/api/v1/accounts/{aid}/balance").json()
    assert bal["balance"] == "100.00"


def test_debit_decreases_balance():
    aid = _make_account()
    _credit(aid, 200, "seed")
    resp = _debit(aid, 75, "ref-2")
    assert resp.status_code == 201

    bal = client.get(f"/api/v1/accounts/{aid}/balance").json()
    assert bal["balance"] == "125.00"


def test_debit_more_than_balance_422():
    aid = _make_account()
    _credit(aid, 50, "seed")
    resp = _debit(aid, 100, "too-much")
    assert resp.status_code == 422
    assert "insufficient" in resp.json()["error"]


# ── idempotency ──────────────────────────────────────────────────────

def test_duplicate_ref_returns_original_txn():
    """Second POST with same ref_id should return the first txn, not create a new one."""
    aid = _make_account()
    r1 = _credit(aid, 50, "dup-ref")
    r2 = _credit(aid, 50, "dup-ref")

    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["duplicate"] is True

    # balance should only reflect one credit
    bal = client.get(f"/api/v1/accounts/{aid}/balance").json()
    assert bal["balance"] == "50.00"


# ── transaction history ──────────────────────────────────────────────

def test_history_pagination_meta():
    aid = _make_account()
    for i in range(5):
        _credit(aid, 10, f"pg-{i}")

    resp = client.get(f"/api/v1/accounts/{aid}/transactions?page=1&limit=2")
    data = resp.json()
    assert len(data["data"]) == 2
    assert data["pagination"]["total_records"] == 5
    assert data["pagination"]["total_pages"] == 3


def test_history_filter_by_type():
    aid = _make_account()
    _credit(aid, 100, "c1")
    _credit(aid, 50, "c2")
    _debit(aid, 30, "d1")

    credits = client.get(f"/api/v1/accounts/{aid}/transactions?type=credit").json()
    assert len(credits["data"]) == 2

    debits = client.get(f"/api/v1/accounts/{aid}/transactions?type=debit").json()
    assert len(debits["data"]) == 1


# ── transfers ────────────────────────────────────────────────────────

def test_transfer_moves_money_atomically():
    a = _make_account("sender")
    b = _make_account("receiver")
    _credit(a, 500, "fund-a")

    resp = client.post("/api/v1/transfers", json={
        "from_account_id": a,
        "to_account_id": b,
        "amount": "200",
        "reference_id": "xfer-1",
    })
    assert resp.status_code == 201

    assert client.get(f"/api/v1/accounts/{a}/balance").json()["balance"] == "300.00"
    assert client.get(f"/api/v1/accounts/{b}/balance").json()["balance"] == "200.00"


def test_transfer_to_same_account_rejected():
    a = _make_account()
    _credit(a, 100, "seed")

    resp = client.post("/api/v1/transfers", json={
        "from_account_id": a,
        "to_account_id": a,
        "amount": "50",
        "reference_id": "self-xfer",
    })
    assert resp.status_code == 422


def test_transfer_insufficient_funds():
    a = _make_account("broke")
    b = _make_account("receiver")
    _credit(a, 10, "seed")

    resp = client.post("/api/v1/transfers", json={
        "from_account_id": a,
        "to_account_id": b,
        "amount": "100",
        "reference_id": "no-money",
    })
    assert resp.status_code == 422


def test_amount_rejects_3_decimal_places():
    """DB is Numeric(18,2) — silently rounding 10.005 would be a bug.
    We catch it before it hits the DB."""
    aid = _make_account()
    resp = _credit(aid, "10.005", "bad-precision")
    assert resp.status_code == 422


def test_reusing_ref_with_different_amount_422():
    aid = _make_account()
    _credit(aid, 50, "same-ref")
    resp = _credit(aid, 75, "same-ref")
    assert resp.status_code == 422
    assert resp.json()["error"] == "reference_conflict"
    assert client.get(f"/api/v1/accounts/{aid}/balance").json()["balance"] == "50.00"
