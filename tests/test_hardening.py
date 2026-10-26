"""Second round of edge cases: races on the conflict path, Unicode look-alikes, health check."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import crud
from app.errors import ReferenceConflict
from app.main import app
from tests.conftest import TestSession, run_parallel

client = TestClient(app, raise_server_exceptions=False)
API = "/api/v1"


def acct(name="acct"):
    return client.post(f"{API}/accounts", json={"name": name}).json()["id"]


def tx(aid, ref, amount="1", type_="credit", **extra):
    return client.post(
        f"{API}/accounts/{aid}/transactions",
        json={"type": type_, "amount": amount, "reference_id": ref, **extra},
    )


def raw_post(path, body: str):
    return client.post(API + path, content=body.encode("utf-8", "surrogatepass"),
                       headers={"Content-Type": "application/json"})


# ---- the "lost the race" branch ------------------------------------------

def _lose_the_race(monkeypatch):
    """Make the pre-insert duplicate check miss once, as if another request committed in between."""
    real = crud._find_existing_txn
    calls = {"n": 0}

    def flaky(db, account_id, reference_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, account_id, reference_id)

    monkeypatch.setattr(crud, "_find_existing_txn", flaky)


def test_race_loser_with_same_payload_gets_the_winner_as_a_replay(monkeypatch):
    aid = acct()
    db = TestSession()
    winner, _ = crud.add_transaction(db, aid, "credit", Decimal("10"), "", "r")
    _lose_the_race(monkeypatch)
    txn, dup = crud.add_transaction(db, aid, "credit", Decimal("10"), "", "r")
    assert dup is True and txn.id == winner.id
    db.close()
    assert client.get(f"{API}/accounts/{aid}/balance").json()["balance"] == "10.00"


@pytest.mark.parametrize("type_, amount", [("credit", "20"), ("debit", "10")])
def test_race_loser_with_different_payload_gets_a_conflict_not_a_fake_replay(monkeypatch, type_, amount):
    aid = acct()
    db = TestSession()
    crud.add_transaction(db, aid, "credit", Decimal("10"), "", "r")
    _lose_the_race(monkeypatch)
    with pytest.raises(ReferenceConflict):
        crud.add_transaction(db, aid, type_, Decimal(amount), "", "r")
    db.close()
    assert client.get(f"{API}/accounts/{aid}/balance").json()["balance"] == "10.00"


def test_parallel_same_reference_with_different_amounts(file_db):
    db = file_db()
    aid = crud.create_account(db, "race").id
    db.close()
    amounts = [Decimal("10") if i % 2 else Decimal("20") for i in range(20)]
    results = run_parallel(file_db, lambda s, i: crud.add_transaction(s, aid, "credit", amounts[i], "", "same"), range(20))

    ok = [(i, r) for i, r in enumerate(results) if isinstance(r, tuple)]
    others = [r for r in results if not isinstance(r, tuple)]
    assert all(isinstance(r, ReferenceConflict) for r in others), others
    # nobody was told "done" for an amount that wasn't actually recorded
    assert all(r[0].amount == amounts[i] for i, r in ok)
    winner_amounts = {r[0].amount for _, r in ok}
    assert len(winner_amounts) == 1
    db = file_db()
    assert crud.get_account(db, aid).balance == winner_amounts.pop()
    db.close()


def test_parallel_account_creation_gives_unique_ids(file_db):
    results = run_parallel(file_db, lambda s, i: crud.create_account(s, f"n{i}").id, range(30))
    assert all(isinstance(r, str) for r in results)
    assert len(set(results)) == 30


# ---- Unicode look-alikes -------------------------------------------------

def test_composed_and_decomposed_accents_are_the_same_reference():
    aid = acct()
    assert tx(aid, "café").status_code == 201        # é as one character
    r = tx(aid, "café")                              # e + combining acute accent
    assert r.status_code == 200 and r.json()["reference_id"] == "café"
    assert client.get(f"{API}/accounts/{aid}/balance").json()["balance"] == "1.00"


def test_names_are_stored_normalised():
    r = client.post(f"{API}/accounts", json={"name": "José"})
    assert r.json()["name"] == "José"


@pytest.mark.parametrize("invisible", ["​", "﻿", "‍", "⁠", "­"])
def test_invisible_characters_are_rejected_in_references(invisible):
    aid = acct()
    assert tx(aid, f"r{invisible}1").status_code == 422


@pytest.mark.parametrize("override", ["‮", "‭", "⁦", "⁧"])
@pytest.mark.parametrize("field", ["name", "description", "reference_id"])
def test_text_direction_overrides_are_rejected(override, field):
    aid = acct()
    spoof = f"invoice{override}fdp.exe"
    if field == "name":
        r = client.post(f"{API}/accounts", json={"name": spoof})
    elif field == "description":
        r = tx(aid, "r", description=spoof)
    else:
        r = tx(aid, spoof)
    assert r.status_code == 422


def test_emoji_with_zero_width_joiner_is_fine_in_a_name():
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    r = client.post(f"{API}/accounts", json={"name": f"The {family} account"})
    assert r.status_code == 201 and family in r.json()["name"]


@pytest.mark.parametrize(
    "path, template",
    [
        ("/accounts", '{"name": "a\\ud800b"}'),
        ("/accounts/{aid}/transactions", '{"type":"credit","amount":"1","reference_id":"a\\udfffb"}'),
        ("/accounts/{aid}/transactions", '{"type":"credit","amount":"1","reference_id":"r","description":"x\\ud83dy"}'),
    ],
)
def test_unpaired_surrogates_are_a_422_never_a_500(path, template):
    aid = acct()
    r = raw_post(path.format(aid=aid), template)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("amount", ["１０", "١", "१", "\U0001D7D9"])
def test_non_ascii_digits_are_not_money(amount):
    aid = acct()
    assert tx(aid, "r", amount=amount).status_code == 422


@pytest.mark.parametrize("amount, stored", [("007.50", "7.50"), ("0.50", "0.50"), ("1.10", "1.10")])
def test_leading_zeros_are_harmless(amount, stored):
    aid = acct()
    r = tx(aid, "r", amount=amount)
    assert r.status_code == 201 and r.json()["amount"] == stored


@pytest.mark.parametrize("amount", ["-0", "-0.00", "0.0", "00", ".", "-"])
def test_zero_in_any_spelling_is_rejected(amount):
    aid = acct()
    assert tx(aid, "r", amount=amount).status_code == 422


def test_whitespace_only_description_is_stored_empty():
    aid = acct()
    assert tx(aid, "r", description="   \n\t ").json()["description"] == ""


def test_duplicate_json_keys_use_the_last_value():
    # standard JSON parsing behaviour; documented so nobody is surprised by it
    aid = acct()
    r = raw_post(f"/accounts/{aid}/transactions", '{"type":"credit","amount":"1","amount":"5","reference_id":"k"}')
    assert r.status_code == 201 and r.json()["amount"] == "5.00"


# ---- health check --------------------------------------------------------

def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_reports_503_when_the_database_is_down(monkeypatch):
    def down(self, *a, **k):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(Session, "execute", down)
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "database_unavailable" and "refused" not in r.text


def test_health_only_allows_get():
    assert client.post("/health").status_code == 405
