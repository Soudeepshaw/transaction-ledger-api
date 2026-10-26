"""Edge cases for a money ledger: hostile input, boundaries, races and invariants.
Grouped the way TESTING.md lists them."""
import random
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import crud, models
from app.errors import InsufficientBalance, LedgerError
from app.main import app
from tests.conftest import TestSession, run_parallel

client = TestClient(app, raise_server_exceptions=False)
API = "/api/v1"


def acct(name="acct"):
    r = client.post(f"{API}/accounts", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def tx(aid, type_="credit", amount="1", ref="r", **extra):
    return client.post(
        f"{API}/accounts/{aid}/transactions",
        json={"type": type_, "amount": amount, "reference_id": ref, **extra},
    )


def bal(aid):
    return client.get(f"{API}/accounts/{aid}/balance").json()["balance"]


def xfer(a, b, amount="1", ref="x", **extra):
    return client.post(
        f"{API}/transfers",
        json={"from_account_id": a, "to_account_id": b, "amount": amount, "reference_id": ref, **extra},
    )


def set_balance(aid, value):
    db = TestSession()
    try:
        db.execute(update(models.Account).where(models.Account.id == aid).values(balance=Decimal(value)))
        db.commit()
    finally:
        db.close()


def standard_error(resp, code=None):
    body = resp.json()
    assert {"error", "message"} <= set(body), body
    assert code is None or body["error"] == code, body
    return body


# ---- amounts -------------------------------------------------------------

@pytest.mark.parametrize(
    "sent, stored",
    [
        ("1", "1.00"), ("10.5", "10.50"), ("0.01", "0.01"), ("10.000", "10.00"),
        (100, "100.00"), (0.1, "0.10"), ("999999999999", "999999999999.00"),
        ("1000000000000", "1000000000000.00"),
    ],
)
def test_valid_amounts_are_stored_exactly(sent, stored):
    aid = acct()
    r = tx(aid, amount=sent, ref="a")
    assert r.status_code == 201, r.text
    assert r.json()["amount"] == stored
    assert bal(aid) == stored


@pytest.mark.parametrize(
    "bad",
    [
        "0", "0.00", "-1", "-0.01", "NaN", "nan", "Infinity", "-Infinity", "1e2", "1E+2", "1e-2", "+10",
        " 10 ", "1,000", "0x10", "1.005", "0.001", "١٢٣", "", "abc", "1_000", "1000000000000.01",
        "1e30", "9" * 400, "0." + "0" * 400 + "1", "10 USD", "$10", "--1", "1.2.3", ".5", "5.",
    ],
)
def test_invalid_amount_strings_rejected_with_422(bad):
    aid = acct()
    r = tx(aid, amount=bad, ref="a")
    assert r.status_code == 422, (bad, r.text)
    standard_error(r, "validation_error")
    assert bal(aid) == "0.00"


@pytest.mark.parametrize("bad", [True, False, None, [1], {"a": 1}, 0, -5, 1e20, 1e-9, 0.001])
def test_invalid_amount_json_types_rejected(bad):
    aid = acct()
    assert tx(aid, amount=bad, ref="a").status_code == 422
    assert bal(aid) == "0.00"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_non_json_number_literals_rejected(literal):
    aid = acct()
    r = client.post(
        f"{API}/accounts/{aid}/transactions",
        content=f'{{"type":"credit","amount":{literal},"reference_id":"a"}}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_float_amounts_do_not_drift():
    aid = acct()
    tx(aid, amount=0.1, ref="a")
    tx(aid, amount=0.2, ref="b")
    assert bal(aid) == "0.30"


def test_hundred_cents_make_exactly_one_dollar():
    aid = acct()
    for i in range(100):
        tx(aid, amount="0.01", ref=f"c{i}")
    assert bal(aid) == "1.00"


def test_debit_of_exact_cent_over_balance_rejected():
    aid = acct()
    tx(aid, amount="0.30", ref="seed")
    assert tx(aid, "debit", "0.31", "d1").status_code == 422
    assert tx(aid, "debit", "0.30", "d2").status_code == 201
    assert bal(aid) == "0.00"


def test_balance_ceiling_is_enforced_not_overflowed():
    aid = acct()
    set_balance(aid, "9999999999999.50")
    r = tx(aid, amount="1", ref="over")
    assert r.status_code == 422
    standard_error(r, "balance_limit_exceeded")
    assert bal(aid) == "9999999999999.50"
    assert tx(aid, amount="0.49", ref="fits").status_code == 201
    assert bal(aid) == "9999999999999.99"


def test_transfer_into_full_account_rejected_and_nothing_moves():
    a, b = acct(), acct()
    tx(a, amount="10", ref="seed")
    set_balance(b, "9999999999999.99")
    r = xfer(a, b, "1", "x")
    assert r.status_code == 422
    standard_error(r, "balance_limit_exceeded")
    assert (bal(a), bal(b)) == ("10.00", "9999999999999.99")


# ---- text fields ---------------------------------------------------------

@pytest.mark.parametrize(
    "ref",
    ["r" * 200, "订单-🙂", "'; DROP TABLE accounts;--", "ORD:123/ab", "a b", "../../etc/passwd", "<script>alert(1)</script>", "0", "null"],
)
def test_odd_but_legal_reference_ids_work_and_replay(ref):
    aid = acct()
    assert tx(aid, ref=ref).status_code == 201
    again = tx(aid, ref=ref)
    assert again.status_code == 200 and again.json()["reference_id"] == ref
    assert bal(aid) == "1.00"


@pytest.mark.parametrize("ref", ["", "   ", "\t", "a\x00b", "a\nb", "a\rb", "a\x1fb", "a\x7fb", "r" * 201, "transfer:x", "TRANSFER:x", "Transfer:x"])
def test_illegal_reference_ids_rejected(ref):
    aid = acct()
    r = tx(aid, ref=ref)
    assert r.status_code == 422, repr(ref)
    standard_error(r, "validation_error")


def test_reference_id_is_trimmed_so_padding_cannot_dodge_idempotency():
    aid = acct()
    assert tx(aid, ref="r1").status_code == 201
    r = tx(aid, ref="  r1  ")
    assert r.status_code == 200 and r.json()["reference_id"] == "r1"
    assert bal(aid) == "1.00"


def test_reference_ids_are_case_sensitive():
    aid = acct()
    assert tx(aid, ref="abc").status_code == 201
    assert tx(aid, ref="ABC").status_code == 201
    assert bal(aid) == "2.00"


@pytest.mark.parametrize("name", ["", "   ", "a\x00b", "n" * 201, "\n"])
def test_illegal_account_names_rejected(name):
    assert client.post(f"{API}/accounts", json={"name": name}).status_code == 422


@pytest.mark.parametrize("name", ["José 🙂 李", "n" * 200, "O'Brien; DROP TABLE x;--", "  padded  "])
def test_legal_account_names_round_trip(name):
    r = client.post(f"{API}/accounts", json={"name": name})
    assert r.status_code == 201
    assert r.json()["name"] == name.strip()
    assert client.get(f"{API}/accounts/{r.json()['id']}").json()["name"] == name.strip()


def test_description_limits_and_null():
    aid = acct()
    assert tx(aid, ref="a", description="d" * 500).status_code == 201
    assert tx(aid, ref="b", description="d" * 501).status_code == 422
    assert tx(aid, ref="c", description="a\x00b").status_code == 422
    r = tx(aid, ref="d", description=None)
    assert r.status_code == 201 and r.json()["description"] == ""
    r = tx(aid, ref="e", description="line1\nline2\tx")
    assert r.status_code == 201 and r.json()["description"] == "line1\nline2\tx"
    assert tx(aid, ref="f", description=123).status_code == 422


def test_client_cannot_choose_server_owned_fields():
    r = client.post(
        f"{API}/accounts",
        json={"name": "A", "id": "mine", "balance": 5, "created_at": "2001-01-01T00:00:00Z"},
    )
    body = r.json()
    assert r.status_code == 201
    assert body["id"] != "mine" and body["balance"] == "0.00" and not body["created_at"].startswith("2001")
    other = acct("other")
    r = tx(body["id"], ref="a", id="mine", account_id=other, created_at="2001-01-01T00:00:00Z")
    assert r.status_code == 201
    t = r.json()
    assert t["id"] != "mine" and t["account_id"] == body["id"] and not t["created_at"].startswith("2001")
    assert bal(other) == "0.00"


# ---- ids, routes, methods ------------------------------------------------

@pytest.mark.parametrize(
    "bad_id", ["a%00b", "x" * 5000, "1'%20OR%20'1'='1", "%E6%9D%8E", "..%2F..%2Fetc%2Fpasswd", "%20", "null", "0"]
)
def test_hostile_account_ids_are_404_never_500(bad_id):
    for method, path, kw in [
        ("get", f"/accounts/{bad_id}", {}),
        ("get", f"/accounts/{bad_id}/balance", {}),
        ("get", f"/accounts/{bad_id}/transactions", {}),
        ("post", f"/accounts/{bad_id}/transactions", {"json": {"type": "credit", "amount": "1", "reference_id": "r"}}),
    ]:
        r = getattr(client, method)(API + path, **kw)
        assert r.status_code in (404, 422), (method, path, r.status_code)
        standard_error(r)


def test_trailing_slash_is_a_plain_404_not_a_redirect():
    r = client.post(f"{API}/accounts/", json={"name": "x"}, follow_redirects=False)
    assert r.status_code == 404
    standard_error(r, "not_found")


@pytest.mark.parametrize(
    "method, path",
    [
        ("put", "/accounts"), ("delete", "/accounts"), ("get", "/accounts"), ("patch", "/accounts/{a}"),
        ("put", "/accounts/{a}"), ("delete", "/accounts/{a}"), ("post", "/accounts/{a}/balance"),
        ("put", "/accounts/{a}/balance"), ("delete", "/accounts/{a}/transactions"),
        ("put", "/accounts/{a}/transactions"), ("patch", "/accounts/{a}/transactions"),
        ("get", "/transfers"), ("put", "/transfers"), ("delete", "/transfers"),
    ],
)
def test_wrong_methods_are_405_with_standard_body(method, path):
    aid = acct()
    r = getattr(client, method)(API + path.format(a=aid))
    assert r.status_code == 405
    standard_error(r, "method_not_allowed")


@pytest.mark.parametrize("sub", ["", "/balance", "/transactions"])
def test_no_transaction_or_account_item_routes_exist(sub):
    aid = acct()
    t = tx(aid, ref="a").json()
    for path in (f"/accounts/{aid}/transactions/{t['id']}", f"/transactions/{t['id']}"):
        for method in ("get", "put", "patch", "delete"):
            assert getattr(client, method)(API + path).status_code in (404, 405)


@pytest.mark.parametrize(
    "content, ctype",
    [
        ("", "application/json"), ("[]", "application/json"), ("null", "application/json"), ("{", "application/json"),
        ('{"name": "x"}', "text/plain"), ("name=x", "application/x-www-form-urlencoded"), ('"just a string"', "application/json"),
        ("{" * 5000, "application/json"),
    ],
)
def test_malformed_bodies_are_422(content, ctype):
    r = client.post(f"{API}/accounts", content=content, headers={"Content-Type": ctype})
    assert r.status_code == 422
    standard_error(r, "validation_error")


def test_body_that_is_not_valid_utf8_is_a_clean_400():
    bad = bytes([0x7B, 0x22, 0x6E, 0x61, 0x6D, 0x65, 0x22, 0x3A, 0x22, 0xE9, 0xFF, 0x22, 0x7D])  # {"name":"<invalid utf-8>"}
    r = client.post(f"{API}/accounts", content=bad, headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    standard_error(r, "bad_request")


def test_large_body_does_not_break_anything():
    r = client.post(f"{API}/accounts", json={"name": "x", "junk": "j" * 2_000_000})
    assert r.status_code == 201


@pytest.mark.parametrize("type_", ["CREDIT", "Credit", " credit", "credit ", "refund", "", None, 1, True, ["credit"]])
def test_type_must_be_exactly_credit_or_debit(type_):
    aid = acct()
    assert tx(aid, type_=type_, ref="a").status_code == 422


def test_timestamps_are_utc_with_z():
    aid = acct()
    t = tx(aid, ref="a").json()
    a = client.get(f"{API}/accounts/{aid}").json()
    for stamp in (a["created_at"], t["created_at"]):
        assert stamp.endswith("Z"), stamp
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        assert parsed.utcoffset() == timedelta(0)
        assert abs(datetime.now(parsed.tzinfo) - parsed) < timedelta(minutes=5)


# ---- idempotency ---------------------------------------------------------

def test_replay_after_the_balance_changed_still_returns_the_original():
    aid = acct()
    tx(aid, amount="100", ref="seed")
    first = tx(aid, "debit", "100", "d")
    assert first.status_code == 201 and bal(aid) == "0.00"
    again = tx(aid, "debit", "100", "d")  # would be 'insufficient' if re-evaluated
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert bal(aid) == "0.00"


@pytest.mark.parametrize("a, b", [("10", "10.00"), ("10.0", "10"), (10, "10.00"), ("10.000", "10")])
def test_same_amount_written_differently_is_still_a_replay(a, b):
    aid = acct()
    assert tx(aid, amount=a, ref="r").status_code == 201
    assert tx(aid, amount=b, ref="r").status_code == 200
    assert bal(aid) == "10.00"


def test_same_reference_on_different_accounts_and_types_is_independent():
    a, b = acct(), acct()
    tx(b, amount="5", ref="seed")
    assert tx(a, "credit", "10", "shared").status_code == 201
    assert tx(b, "debit", "5", "shared").status_code == 201
    assert (bal(a), bal(b)) == ("10.00", "0.00")


def test_rejected_requests_do_not_burn_the_reference_id():
    aid = acct()
    assert tx(aid, "debit", "5", "r").status_code == 422  # insufficient
    tx(aid, amount="10", ref="seed")
    r = tx(aid, "debit", "5", "r")  # same reference, now fundable
    assert r.status_code == 201 and bal(aid) == "5.00"


def test_invalid_requests_do_not_burn_the_reference_id():
    aid = acct()
    assert tx(aid, amount="-1", ref="r").status_code == 422
    assert tx(aid, amount="1", ref="r").status_code == 201


def test_replay_does_not_overwrite_the_original():
    aid = acct()
    first = tx(aid, ref="r", description="original").json()
    again = tx(aid, ref="r", description="changed").json()
    assert again["id"] == first["id"] and again["description"] == "original"
    assert again["created_at"] == first["created_at"]


# ---- history / pagination ------------------------------------------------

def test_history_pages_cover_250_rows_exactly_once():
    aid = acct()
    for i in range(250):
        crud_db = TestSession()
        try:
            crud.add_transaction(crud_db, aid, "credit", Decimal("1"), "", f"r{i:03d}")
        finally:
            crud_db.close()
    seen = []
    for page in (1, 2, 3):
        r = client.get(f"{API}/accounts/{aid}/transactions?page={page}&limit=200").json()
        seen += [t["reference_id"] for t in r["data"]]
    assert len(seen) == 250 and len(set(seen)) == 250
    assert seen[0] == "r249" and seen[-1] == "r000"  # latest first


def test_rows_with_identical_timestamps_page_without_gaps_or_repeats():
    aid = acct()
    stamp = datetime(2026, 1, 1, 12, 0, 0)
    db = TestSession()
    try:
        for i in range(30):
            db.add(models.Transaction(account_id=aid, type="credit", amount=Decimal("1"), reference_id=f"t{i}", created_at=stamp))
        db.commit()
    finally:
        db.close()

    def walk():
        out = []
        for page in range(1, 6):
            out += [t["reference_id"] for t in client.get(f"{API}/accounts/{aid}/transactions?page={page}&limit=7").json()["data"]]
        return out

    first = walk()
    assert len(first) == 30 and len(set(first)) == 30
    assert walk() == first  # deterministic order


@pytest.mark.parametrize(
    "query, status",
    [
        ("page=1&limit=1", 200), ("page=1&limit=200", 200), ("page=1&limit=201", 422), ("page=0", 422), ("page=-1", 422),
        ("limit=0", 422), ("limit=-5", 422), ("page=1000000&limit=200", 200), ("page=1000001", 422),
        ("page=9223372036854775807", 422), ("page=99999999999999999999", 422), ("limit=99999999999999999999", 422),
        ("page=1.5", 422), ("limit=2.5", 422), ("page=abc", 422), ("page=", 422), ("page=%20", 422), ("page=1e3", 422),
        ("limit=%00", 422), ("type=", 422), ("type=CREDIT", 422), ("type=refund", 422), ("type=%00", 422),
        ("type=credit&type=debit", 200), ("page=1&page=2", 200), ("unknown=1", 200),
    ],
)
def test_history_query_matrix_never_500(query, status):
    aid = acct()
    tx(aid, ref="a")
    r = client.get(f"{API}/accounts/{aid}/transactions?{query}")
    assert r.status_code == status, (query, r.status_code, r.text[:200])
    if status == 422:
        standard_error(r, "validation_error")


def test_pagination_math_for_awkward_sizes():
    aid = acct()
    for i in range(3):
        tx(aid, ref=f"r{i}")
    p = client.get(f"{API}/accounts/{aid}/transactions?limit=1&page=3").json()["pagination"]
    assert p == {"current_page": 3, "page_size": 1, "total_records": 3, "total_pages": 3}
    p = client.get(f"{API}/accounts/{aid}/transactions?limit=2").json()["pagination"]
    assert p["total_pages"] == 2
    p = client.get(f"{API}/accounts/{aid}/transactions?limit=3").json()["pagination"]
    assert p["total_pages"] == 1


# ---- transfers -----------------------------------------------------------

def test_reusing_a_transfer_reference_with_another_destination_is_a_conflict_not_a_crash():
    a, b, c = acct(), acct(), acct()
    tx(a, amount="100", ref="seed")
    assert xfer(a, b, "10", "x").status_code == 201
    r = xfer(a, c, "10", "x")
    assert r.status_code == 422
    standard_error(r, "reference_conflict")
    assert (bal(a), bal(b), bal(c)) == ("90.00", "10.00", "0.00")


def test_reusing_a_transfer_reference_with_another_amount_is_a_conflict():
    a, b = acct(), acct()
    tx(a, amount="100", ref="seed")
    xfer(a, b, "10", "x")
    assert xfer(a, b, "20", "x").status_code == 422
    assert (bal(a), bal(b)) == ("90.00", "10.00")


def test_same_transfer_reference_in_the_opposite_direction_is_a_separate_transfer():
    a, b = acct(), acct()
    tx(a, amount="100", ref="seed")
    tx(b, amount="100", ref="seed")
    assert xfer(a, b, "10", "x").status_code == 201
    assert xfer(b, a, "10", "x").status_code == 201
    assert (bal(a), bal(b)) == ("100.00", "100.00")


def test_transfer_replay_returns_the_same_transactions_and_moves_nothing():
    a, b = acct(), acct()
    tx(a, amount="100", ref="seed")
    first = xfer(a, b, "30", "x").json()
    again = xfer(a, b, "30", "x")
    assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["debit_transaction"]["id"] == first["debit_transaction"]["id"]
    assert again.json()["credit_transaction"]["id"] == first["credit_transaction"]["id"]
    assert (bal(a), bal(b)) == ("70.00", "30.00")


def test_transfer_legs_cannot_collide_with_ordinary_references():
    a, b = acct(), acct()
    tx(a, amount="100", ref="seed")
    assert xfer(a, b, "10", "x").status_code == 201
    assert tx(a, "debit", "1", "x:debit").status_code == 201  # distinct from the stored leg
    assert tx(a, "debit", "1", "x").status_code == 201
    assert xfer(a, b, "1", "y:credit").status_code == 201
    assert (bal(a), bal(b)) == ("87.00", "11.00")


@pytest.mark.parametrize("ref, status", [("y" * 150, 201), ("y" * 151, 422), ("transfer:x", 422), ("", 422), ("  ", 422)])
def test_transfer_reference_limits(ref, status):
    a, b = acct(), acct()
    tx(a, amount="10", ref="seed")
    assert xfer(a, b, "1", ref).status_code == status


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "1e2", "1.005", "", "abc", "1000000000000.01"])
def test_transfer_amount_validation(amount):
    a, b = acct(), acct()
    tx(a, amount="10", ref="seed")
    assert xfer(a, b, amount, "x").status_code == 422
    assert (bal(a), bal(b)) == ("10.00", "0.00")


@pytest.mark.parametrize(
    "from_id, to_id, status",
    [("nope", "B", 404), ("A", "nope", 404), ("A", "A", 422), ("", "B", 422), ("A", "", 422), ("a\x00b", "B", 422), ("x" * 65, "B", 422)],
)
def test_transfer_account_validation(from_id, to_id, status):
    a, b = acct(), acct()
    ids = {"A": a, "B": b}
    r = xfer(ids.get(from_id, from_id), ids.get(to_id, to_id), "1", "x")
    assert r.status_code == status, r.text
    standard_error(r)


def test_transfer_with_missing_fields_or_bad_json():
    assert client.post(f"{API}/transfers", json={}).status_code == 422
    assert client.post(f"{API}/transfers", json={"from_account_id": "a"}).status_code == 422
    assert client.post(f"{API}/transfers", content="nope", headers={"Content-Type": "application/json"}).status_code == 422


def test_failed_transfer_leaves_both_ledgers_untouched():
    a, b = acct(), acct()
    tx(a, amount="5", ref="seed")
    n_before = [len(client.get(f"{API}/accounts/{x}/transactions").json()["data"]) for x in (a, b)]
    for amount, ref in [("50", "x1"), ("0", "x2")]:
        assert xfer(a, b, amount, ref).status_code == 422
    n_after = [len(client.get(f"{API}/accounts/{x}/transactions").json()["data"]) for x in (a, b)]
    assert n_before == n_after and (bal(a), bal(b)) == ("5.00", "0.00")


# ---- failures the server must survive ------------------------------------

def test_unexpected_exception_returns_json_500_without_leaking_details(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail /etc/passwd")

    monkeypatch.setattr(crud, "get_account", boom)
    r = client.get(f"{API}/accounts/anything/balance")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    assert standard_error(r, "internal_error")
    assert "secret" not in r.text and "passwd" not in r.text and "Traceback" not in r.text


def test_database_failure_mid_commit_is_a_clean_500_and_changes_nothing(monkeypatch):
    aid = acct()
    tx(aid, amount="10", ref="seed")

    def dead(self):
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(Session, "commit", dead)
    r = tx(aid, amount="5", ref="doomed")
    assert r.status_code == 500
    standard_error(r, "internal_error")
    assert "connection lost" not in r.text
    monkeypatch.undo()
    assert bal(aid) == "10.00"
    assert tx(aid, amount="5", ref="doomed").status_code == 201  # the reference was not burned


def test_database_failure_during_transfer_changes_nothing(monkeypatch):
    a, b = acct(), acct()
    tx(a, amount="10", ref="seed")

    def dead(self):
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(Session, "commit", dead)
    assert xfer(a, b, "5", "x").status_code == 500
    monkeypatch.undo()
    assert (bal(a), bal(b)) == ("10.00", "0.00")
    assert xfer(a, b, "5", "x").status_code == 201


# ---- concurrency ---------------------------------------------------------

def _fresh(file_db, name="p", balance=None):
    db = file_db()
    aid = crud.create_account(db, name).id
    if balance:
        crud.add_transaction(db, aid, "credit", Decimal(balance), "", "seed")
    db.close()
    return aid


def _balance(file_db, aid):
    db = file_db()
    try:
        return crud.get_account(db, aid).balance
    finally:
        db.close()


def _count(file_db, aid):
    db = file_db()
    try:
        return db.scalar(select(func.count()).select_from(models.Transaction).where(models.Transaction.account_id == aid))
    finally:
        db.close()


def test_parallel_credits_with_distinct_references_lose_no_updates(file_db):
    aid = _fresh(file_db)
    results = run_parallel(file_db, lambda s, i: crud.add_transaction(s, aid, "credit", Decimal("7"), "", f"c{i}"), range(40), workers=10)
    assert all(isinstance(r, tuple) for r in results), [r for r in results if not isinstance(r, tuple)]
    assert _balance(file_db, aid) == Decimal("280") and _count(file_db, aid) == 40


def test_parallel_mixed_credits_and_debits_keep_ledger_and_balance_equal(file_db):
    aid = _fresh(file_db, balance="50")

    def op(s, i):
        kind = "credit" if i % 3 else "debit"
        return crud.add_transaction(s, aid, kind, Decimal("10"), "", f"m{i}")

    results = run_parallel(file_db, op, range(60), workers=10)
    bad = [r for r in results if isinstance(r, Exception) and not isinstance(r, InsufficientBalance)]
    assert not bad, bad
    db = file_db()
    rows = db.execute(select(models.Transaction).where(models.Transaction.account_id == aid)).scalars().all()
    ledger = sum((t.amount if t.type == "credit" else -t.amount) for t in rows)
    db.close()
    final = _balance(file_db, aid)
    assert final == ledger and final >= 0


def test_parallel_identical_transfers_move_money_once(file_db):
    a, b = _fresh(file_db, "a", "100"), _fresh(file_db, "b")
    results = run_parallel(file_db, lambda s, i: crud.transfer(s, a, b, Decimal("30"), "", "same"), range(15), workers=10)
    assert all(isinstance(r, tuple) for r in results), [r for r in results if not isinstance(r, tuple)]
    assert sum(1 for r in results if r[4] is False) == 1  # exactly one real transfer, the rest replays
    assert (_balance(file_db, a), _balance(file_db, b)) == (Decimal("70"), Decimal("30"))
    assert _count(file_db, a) == 2 and _count(file_db, b) == 1


def test_parallel_transfers_from_one_account_never_overdraw_and_conserve_money(file_db):
    src = _fresh(file_db, "src", "100")
    dests = [_fresh(file_db, f"d{i}") for i in range(10)]
    results = run_parallel(file_db, lambda s, i: crud.transfer(s, src, dests[i], Decimal("30"), "", f"t{i}"), range(10), workers=10)
    ok = [r for r in results if isinstance(r, tuple)]
    assert not [r for r in results if isinstance(r, Exception) and not isinstance(r, InsufficientBalance)]
    assert len(ok) == 3
    assert _balance(file_db, src) == Decimal("10")
    assert sum(_balance(file_db, d) for d in dests) == Decimal("90")


def test_opposite_direction_transfers_do_not_deadlock(file_db):
    a, b = _fresh(file_db, "a", "1000"), _fresh(file_db, "b", "1000")

    def op(s, i):
        src, dst = (a, b) if i % 2 == 0 else (b, a)
        return crud.transfer(s, src, dst, Decimal("1"), "", f"ab{i}")

    results = run_parallel(file_db, op, range(40), workers=10)
    assert all(isinstance(r, tuple) for r in results), [r for r in results if not isinstance(r, tuple)]
    assert _balance(file_db, a) + _balance(file_db, b) == Decimal("2000")
    assert _balance(file_db, a) == Decimal("1000")  # 20 each way


def test_parallel_duplicate_debits_only_charge_once(file_db):
    aid = _fresh(file_db, balance="100")
    results = run_parallel(file_db, lambda s, i: crud.add_transaction(s, aid, "debit", Decimal("40"), "", "d"), range(12), workers=10)
    assert all(isinstance(r, tuple) for r in results), [r for r in results if not isinstance(r, tuple)]
    assert _balance(file_db, aid) == Decimal("60")


# ---- invariants over random histories ------------------------------------

def test_random_operation_sequences_never_break_the_ledger(file_db):
    rng = random.Random(20261026)
    db = file_db()
    ids = [crud.create_account(db, f"acct{i}").id for i in range(4)]
    model = {i: Decimal("0") for i in ids}
    deposited = Decimal("0")
    withdrawn = Decimal("0")
    used = {}
    for n in range(400):
        op = rng.choice(["credit", "debit", "transfer", "replay"])
        amt = Decimal(rng.randint(1, 5000)) / 100
        a, b = rng.sample(ids, 2)
        if op == "replay" and used:
            (acc, ref), (typ, val) = rng.choice(list(used.items()))
            got, dup = crud.add_transaction(db, acc, typ, val, "", ref)
            assert dup is True
        elif op == "credit":
            ref = f"c{n}"
            crud.add_transaction(db, a, "credit", amt, "", ref)
            model[a] += amt; deposited += amt; used[(a, ref)] = ("credit", amt)
        elif op == "debit":
            ref = f"d{n}"
            try:
                crud.add_transaction(db, a, "debit", amt, "", ref)
                model[a] -= amt; withdrawn += amt; used[(a, ref)] = ("debit", amt)
            except InsufficientBalance:
                assert model[a] < amt
        elif op == "transfer":
            try:
                crud.transfer(db, a, b, amt, "", f"t{n}")
                model[a] -= amt; model[b] += amt
            except InsufficientBalance:
                assert model[a] < amt
        for acc in ids:
            assert crud.get_account(db, acc).balance == model[acc] >= 0
    for acc in ids:
        rows = db.execute(select(models.Transaction).where(models.Transaction.account_id == acc)).scalars().all()
        assert sum((t.amount if t.type == "credit" else -t.amount) for t in rows) == model[acc]
    assert sum(model.values()) == deposited - withdrawn  # transfers neither create nor destroy money
    db.close()
