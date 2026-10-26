"""Edge-case and scenario tests, grouped by PRD section. See TESTING.md for the matrix."""
import tempfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import crud, models
from app.database import Base, configure_sqlite, get_db
from app.errors import InsufficientBalance
from app.main import app

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestSession = sessionmaker(bind=engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def fresh_db():
    app.dependency_overrides[get_db] = override_get_db
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


client = TestClient(app)
API = "/api/v1"


def acct(name="acct"):
    r = client.post(f"{API}/accounts", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def txn(aid, type_, amount, ref, **extra):
    return client.post(
        f"{API}/accounts/{aid}/transactions",
        json={"type": type_, "amount": str(amount), "reference_id": ref, **extra},
    )


def bal(aid):
    return client.get(f"{API}/accounts/{aid}/balance").json()["balance"]


def count_txns(aid):
    db = TestSession()
    try:
        return db.scalar(
            select(func.count()).select_from(models.Transaction).where(models.Transaction.account_id == aid)
        )
    finally:
        db.close()


# ---- FR-1 create account -------------------------------------------------

def test_create_account_missing_name_422():
    assert client.post(f"{API}/accounts", json={}).status_code == 422


def test_create_account_empty_name_422():
    assert client.post(f"{API}/accounts", json={"name": ""}).status_code == 422


def test_client_cannot_set_initial_balance():
    r = client.post(f"{API}/accounts", json={"name": "Bob", "balance": 999})
    assert r.status_code == 201
    assert r.json()["balance"] == "0.00"


def test_account_ids_are_unique_and_created_at_is_set():
    a = client.post(f"{API}/accounts", json={"name": "same name"}).json()
    b = client.post(f"{API}/accounts", json={"name": "same name"}).json()
    assert a["id"] != b["id"]
    assert a["created_at"] and b["created_at"]


# ---- FR-2 transactions ---------------------------------------------------

def test_credit_then_debit_sequence_math():
    aid = acct()
    txn(aid, "credit", 100, "a")
    txn(aid, "credit", 50, "b")
    txn(aid, "debit", 30, "c")
    assert bal(aid) == "120.00"


def test_debit_exact_balance_leaves_zero():
    aid = acct()
    txn(aid, "credit", 100, "seed")
    assert txn(aid, "debit", 100, "all").status_code == 201
    assert bal(aid) == "0.00"


def test_debit_on_empty_account_rejected():
    aid = acct()
    assert txn(aid, "debit", 1, "x").status_code == 422


def test_rejected_debit_creates_no_transaction_row():
    aid = acct()
    txn(aid, "credit", 100, "seed")
    assert txn(aid, "debit", 150, "big").status_code == 422
    assert bal(aid) == "100.00"
    assert count_txns(aid) == 1


def test_transaction_on_unknown_account_404():
    assert txn("nope", "credit", 10, "x").status_code == 404


@pytest.mark.parametrize("amount", ["0", "-5", "abc", ""])
def test_bad_amount_422(amount):
    aid = acct()
    assert txn(aid, "credit", amount, "x").status_code == 422
    assert bal(aid) == "0.00"


def test_invalid_type_422():
    aid = acct()
    assert txn(aid, "refund", 10, "x").status_code == 422


def test_missing_reference_id_422():
    aid = acct()
    r = client.post(f"{API}/accounts/{aid}/transactions", json={"type": "credit", "amount": "5"})
    assert r.status_code == 422


def test_description_is_optional():
    aid = acct()
    assert txn(aid, "credit", 5, "x").status_code == 201


def test_two_decimal_places_accepted_and_exact():
    aid = acct()
    txn(aid, "credit", "0.10", "a")
    txn(aid, "credit", "0.20", "b")
    assert bal(aid) == "0.30"  # would be 0.30000000000000004 with floats


def test_duplicate_debit_does_not_debit_twice():
    aid = acct()
    txn(aid, "credit", 100, "seed")
    first = txn(aid, "debit", 40, "d1")
    again = txn(aid, "debit", 40, "d1")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["duplicate"] is True
    assert bal(aid) == "60.00"
    assert count_txns(aid) == 2


def test_duplicate_response_says_the_transaction_already_exists():
    aid = acct()
    first = txn(aid, "credit", 10, "r").json()
    again = txn(aid, "credit", 10, "r").json()
    assert first["message"] is None
    assert "already exists" in again["message"]
    assert again["duplicate"] is True


def test_duplicate_with_different_description_still_a_replay():
    aid = acct()
    a = txn(aid, "credit", 10, "r", description="one")
    b = txn(aid, "credit", 10, "r", description="two")
    assert b.status_code == 200 and b.json()["id"] == a.json()["id"]
    assert b.json()["description"] == "one"  # original is returned untouched


def test_same_reference_id_allowed_on_different_accounts():
    a, b = acct("a"), acct("b")
    assert txn(a, "credit", 10, "shared").status_code == 201
    r = txn(b, "credit", 10, "shared")
    assert r.status_code == 201 and r.json()["duplicate"] is False
    assert bal(a) == bal(b) == "10.00"


def test_same_reference_id_different_amount_409_and_balance_unchanged():
    aid = acct()
    txn(aid, "credit", 50, "r")
    assert txn(aid, "credit", 75, "r").status_code == 409
    assert bal(aid) == "50.00"


def test_same_reference_id_different_type_409():
    aid = acct()
    txn(aid, "credit", 50, "r")
    assert txn(aid, "debit", 50, "r").status_code == 409


def test_error_body_shape():
    body = txn("nope", "credit", 10, "x").json()
    assert set(body) == {"error", "message"} and body["error"] == "account_not_found"


# ---- FR-3 balance --------------------------------------------------------

def test_balance_unknown_account_404():
    r = client.get(f"{API}/accounts/nope/balance")
    assert r.status_code == 404 and r.json()["error"] == "account_not_found"


def test_balance_response_has_account_id_and_balance():
    aid = acct()
    assert client.get(f"{API}/accounts/{aid}/balance").json() == {"account_id": aid, "balance": "0.00"}


# ---- FR-4 history --------------------------------------------------------

def test_history_default_returns_both_types():
    aid = acct()
    txn(aid, "credit", 100, "c")
    txn(aid, "debit", 10, "d")
    r = client.get(f"{API}/accounts/{aid}/transactions").json()
    assert {t["type"] for t in r["data"]} == {"credit", "debit"}


def test_history_is_latest_first():
    aid = acct()
    for i in range(5):
        txn(aid, "credit", 1, f"r{i}")
    refs = [t["reference_id"] for t in client.get(f"{API}/accounts/{aid}/transactions").json()["data"]]
    assert refs == ["r4", "r3", "r2", "r1", "r0"]


def test_history_pages_do_not_overlap_and_cover_everything():
    aid = acct()
    for i in range(5):
        txn(aid, "credit", 1, f"r{i}")
    seen = []
    for page in (1, 2, 3):
        d = client.get(f"{API}/accounts/{aid}/transactions?page={page}&limit=2").json()
        seen += [t["reference_id"] for t in d["data"]]
    assert seen == ["r4", "r3", "r2", "r1", "r0"]


def test_history_page_beyond_last_is_empty_not_error():
    aid = acct()
    txn(aid, "credit", 1, "r")
    d = client.get(f"{API}/accounts/{aid}/transactions?page=9&limit=10").json()
    assert d["data"] == [] and d["pagination"]["total_records"] == 1


def test_history_empty_account():
    aid = acct()
    d = client.get(f"{API}/accounts/{aid}/transactions").json()
    assert d["data"] == [] and d["pagination"]["total_pages"] == 0


def test_history_pagination_meta_fields():
    aid = acct()
    for i in range(3):
        txn(aid, "credit", 1, f"r{i}")
    p = client.get(f"{API}/accounts/{aid}/transactions?page=2&limit=2").json()["pagination"]
    assert p == {"current_page": 2, "page_size": 2, "total_records": 3, "total_pages": 2}


def test_filter_plus_pagination_counts_only_filtered():
    aid = acct()
    for i in range(3):
        txn(aid, "credit", 10, f"c{i}")
    txn(aid, "debit", 5, "d")
    p = client.get(f"{API}/accounts/{aid}/transactions?type=credit&limit=2").json()["pagination"]
    assert p["total_records"] == 3 and p["total_pages"] == 2


@pytest.mark.parametrize("query", ["page=0", "page=-1", "limit=0", "limit=201", "type=refund", "page=abc"])
def test_history_bad_query_422(query):
    aid = acct()
    assert client.get(f"{API}/accounts/{aid}/transactions?{query}").status_code == 422


def test_history_unknown_account_404():
    assert client.get(f"{API}/accounts/nope/transactions").status_code == 404


def test_history_only_shows_own_account():
    a, b = acct("a"), acct("b")
    txn(a, "credit", 10, "r")
    assert client.get(f"{API}/accounts/{b}/transactions").json()["data"] == []


# ---- transfers (extra) ---------------------------------------------------

def transfer(a, b, amount, ref):
    return client.post(
        f"{API}/transfers",
        json={"from_account_id": a, "to_account_id": b, "amount": str(amount), "reference_id": ref},
    )


def test_transfer_unknown_account_404():
    a = acct()
    assert transfer(a, "nope", 1, "x").status_code == 404
    assert transfer("nope", a, 1, "x").status_code == 404


def test_transfer_exact_balance_ok():
    a, b = acct(), acct()
    txn(a, "credit", 100, "seed")
    assert transfer(a, b, 100, "all").status_code == 201
    assert (bal(a), bal(b)) == ("0.00", "100.00")


def test_failed_transfer_changes_nothing():
    a, b = acct(), acct()
    txn(a, "credit", 10, "seed")
    assert transfer(a, b, 50, "x").status_code == 422
    assert (bal(a), bal(b)) == ("10.00", "0.00")
    assert count_txns(a) == 1 and count_txns(b) == 0


def test_transfer_retry_after_draining_returns_original_not_422():
    a, b = acct(), acct()
    txn(a, "credit", 100, "seed")
    first = transfer(a, b, 100, "x")
    again = transfer(a, b, 100, "x")  # source is now empty
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["debit_transaction"]["duplicate"] is True
    assert (bal(a), bal(b)) == ("0.00", "100.00")


def test_transfer_records_both_legs_in_history():
    a, b = acct(), acct()
    txn(a, "credit", 100, "seed")
    transfer(a, b, 30, "x")
    assert client.get(f"{API}/accounts/{a}/transactions?type=debit").json()["pagination"]["total_records"] == 1
    assert client.get(f"{API}/accounts/{b}/transactions?type=credit").json()["pagination"]["total_records"] == 1


def test_transfer_is_atomic_when_commit_fails(monkeypatch):
    a, b = acct(), acct()
    txn(a, "credit", 100, "seed")

    def boom(self):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(Session, "commit", boom)
    with pytest.raises(RuntimeError):
        transfer(a, b, 40, "x")
    monkeypatch.undo()
    assert (bal(a), bal(b)) == ("100.00", "0.00")


# ---- concurrency ---------------------------------------------------------

@pytest.fixture
def file_db():
    """Real file-backed SQLite with one connection per thread, unlike the in-memory test DB."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        eng = create_engine(
            f"sqlite:///{d}/race.db", connect_args={"check_same_thread": False, "timeout": 30}
        )
        configure_sqlite(eng)
        Base.metadata.create_all(eng)
        yield sessionmaker(bind=eng)
        eng.dispose()


def _run_parallel(session_factory, fn, jobs, workers=10):
    def wrap(job):
        db = session_factory()
        try:
            return fn(db, job)
        except Exception as exc:  # noqa: BLE001 - we want to count every outcome
            return exc
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(wrap, jobs))


def test_concurrent_duplicate_reference_creates_one_transaction(file_db):
    db = file_db()
    aid = crud.create_account(db, "race").id
    db.close()

    results = _run_parallel(
        file_db,
        lambda s, _: crud.add_transaction(s, aid, "credit", Decimal("10"), "", "same-ref"),
        range(20),
    )

    db = file_db()
    n = db.scalar(select(func.count()).select_from(models.Transaction))
    final = crud.get_account(db, aid).balance
    db.close()
    assert n == 1, results
    assert final == Decimal("10.00")


def test_concurrent_debits_never_overdraw(file_db):
    db = file_db()
    aid = crud.create_account(db, "race").id
    crud.add_transaction(db, aid, "credit", Decimal("100"), "", "seed")
    db.close()

    results = _run_parallel(
        file_db,
        lambda s, i: crud.add_transaction(s, aid, "debit", Decimal("30"), "", f"d{i}"),
        range(10),
    )

    ok = [r for r in results if isinstance(r, tuple)]
    db = file_db()
    final = crud.get_account(db, aid).balance
    n_debits = db.scalar(
        select(func.count()).select_from(models.Transaction).where(models.Transaction.type == "debit")
    )
    db.close()
    assert final >= 0
    assert len(ok) <= 3  # 100 // 30
    assert final == Decimal("100") - Decimal("30") * n_debits  # ledger and balance agree
    assert n_debits == len(ok)


# ---- section 5: data integrity -------------------------------------------

def test_balance_equals_result_of_all_valid_transactions():
    """The example from the brief: 0 +100 +50 -30 -20 = 100."""
    aid = acct()
    for type_, amt, ref in [("credit", 100, "1"), ("credit", 50, "2"), ("debit", 30, "3"), ("debit", 20, "4")]:
        assert txn(aid, type_, amt, ref).status_code == 201
    assert bal(aid) == "100.00"


def test_balance_always_equals_sum_of_ledger():
    aid = acct()
    txn(aid, "credit", 100, "a")
    txn(aid, "debit", 500, "too-big")  # rejected, must not count
    txn(aid, "debit", 40, "b")
    txn(aid, "credit", 5, "a")  # duplicate, must not count
    rows = client.get(f"{API}/accounts/{aid}/transactions").json()["data"]
    ledger = sum(Decimal(t["amount"]) * (1 if t["type"] == "credit" else -1) for t in rows)
    assert Decimal(bal(aid)) == ledger == Decimal("60")


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_transactions_cannot_be_edited_or_deleted(method):
    aid = acct()
    t = txn(aid, "credit", 10, "r").json()
    for url in (f"{API}/accounts/{aid}/transactions/{t['id']}", f"{API}/accounts/{aid}/transactions"):
        r = getattr(client, method)(url, json={"amount": "999"}) if method != "delete" else client.delete(url)
        assert r.status_code in (404, 405)
    assert bal(aid) == "10.00"
    stored = client.get(f"{API}/accounts/{aid}/transactions").json()["data"][0]
    for field in ("id", "account_id", "type", "amount", "description", "reference_id", "created_at"):
        assert stored[field] == t[field], field


def test_accounts_cannot_be_overwritten_or_deleted_via_api():
    aid = acct()
    assert client.put(f"{API}/accounts/{aid}", json={"name": "x", "balance": 1}).status_code == 405
    assert client.delete(f"{API}/accounts/{aid}").status_code == 405
    assert client.patch(f"{API}/accounts/{aid}/balance", json={"balance": 1}).status_code == 405


# ---- section 8: consistent errors ----------------------------------------

def _is_standard_error(resp, code):
    body = resp.json()
    return {"error", "message"} <= set(body) and body["error"] == code and isinstance(body["message"], str)


def test_every_error_kind_uses_the_same_body_shape():
    aid = acct()
    txn(aid, "credit", 10, "r")
    cases = [
        (client.get(f"{API}/accounts/nope"), 404, "account_not_found"),
        (client.get(f"{API}/nothing-here"), 404, "not_found"),
        (client.delete(f"{API}/accounts/{aid}"), 405, "method_not_allowed"),
        (txn(aid, "debit", 999, "big"), 422, "insufficient_balance"),
        (txn(aid, "credit", 99, "r"), 409, "reference_conflict"),
        (txn(aid, "credit", -1, "neg"), 422, "validation_error"),
        (txn(aid, "weird", 1, "t"), 422, "validation_error"),
        (client.post(f"{API}/accounts", json={}), 422, "validation_error"),
        (client.post(f"{API}/accounts", content="not json", headers={"Content-Type": "application/json"}), 422, "validation_error"),
        (client.get(f"{API}/accounts/{aid}/transactions?limit=0"), 422, "validation_error"),
    ]
    for resp, status, code in cases:
        assert resp.status_code == status, resp.text
        assert _is_standard_error(resp, code), resp.text


def test_validation_error_names_the_bad_field():
    body = client.post(f"{API}/accounts", json={}).json()
    assert body["details"][0]["field"] == "name"


# ---- section 10: database constraints ------------------------------------

def _raw_insert(obj):
    from sqlalchemy.exc import IntegrityError
    db = TestSession()
    try:
        db.add(obj)
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_db_rejects_negative_balance():
    _raw_insert(models.Account(name="x", balance=Decimal("-1")))


def test_db_rejects_zero_amount_and_bad_type_and_duplicate_ref():
    aid = acct()
    _raw_insert(models.Transaction(account_id=aid, type="credit", amount=Decimal("0"), reference_id="z"))
    _raw_insert(models.Transaction(account_id=aid, type="refund", amount=Decimal("1"), reference_id="t"))
    txn(aid, "credit", 1, "dup")
    _raw_insert(models.Transaction(account_id=aid, type="credit", amount=Decimal("1"), reference_id="dup"))
