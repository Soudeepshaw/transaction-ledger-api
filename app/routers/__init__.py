from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import ValidationFailed
from .. import crud, schemas

router = APIRouter(prefix="/api/v1")


# ── accounts ─────────────────────────────────────────────────────────

@router.post("/accounts", response_model=schemas.AccountOut, status_code=201)
def create_account(body: schemas.AccountCreate, db: Session = Depends(get_db)):
    return crud.create_account(db, body.name)


@router.get("/accounts/{account_id}", response_model=schemas.AccountOut)
def get_account(account_id: str, db: Session = Depends(get_db)):
    return crud.get_account(db, account_id)


@router.get("/accounts/{account_id}/balance", response_model=schemas.BalanceOut)
def get_balance(account_id: str, db: Session = Depends(get_db)):
    acct = crud.get_balance(db, account_id)
    return schemas.BalanceOut(account_id=acct.id, balance=acct.balance)


# ── transactions ─────────────────────────────────────────────────────

@router.post(
    "/accounts/{account_id}/transactions",
    response_model=schemas.TransactionOut,
    status_code=201,
)
def create_transaction(
    account_id: str,
    body: schemas.TransactionCreate,
    response: Response,
    db: Session = Depends(get_db),
):
    txn, dup = crud.add_transaction(
        db,
        account_id=account_id,
        type_=body.type,
        amount=body.amount,
        description=body.description or "",
        reference_id=body.reference_id,
    )
    if dup:
        response.status_code = 200  # replay: nothing new was created
        response.headers["Idempotent-Replayed"] = "true"
    out = schemas.TransactionOut.model_validate(txn)
    out.duplicate = dup
    if dup:
        out.message = "Transaction already exists for this reference_id; nothing was changed."
    return out


@router.get(
    "/accounts/{account_id}/transactions",
    response_model=schemas.TransactionHistoryOut,
)
def list_transactions(
    account_id: str,
    type: str | None = Query(None, pattern="^(credit|debit)$"),
    page: int = Query(1, ge=1, le=1_000_000),
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    rows, meta = crud.list_transactions(db, account_id, type, page, limit)
    return schemas.TransactionHistoryOut(
        data=[schemas.TransactionOut.model_validate(r) for r in rows],
        pagination=meta,
    )


# ── transfers ────────────────────────────────────────────────────────

@router.post("/transfers", response_model=schemas.TransferOut, status_code=201)
def create_transfer(
    body: schemas.TransferCreate,
    response: Response,
    db: Session = Depends(get_db),
):
    if body.from_account_id == body.to_account_id:
        raise ValidationFailed("Cannot transfer to the same account.")


    from_acc, to_acc, debit_txn, credit_txn, dup = crud.transfer(
        db,
        from_account_id=body.from_account_id,
        to_account_id=body.to_account_id,
        amount=body.amount,
        description=body.description or "",
        reference_id=body.reference_id,
    )
    resp = schemas.TransferOut(
        from_account=schemas.AccountOut.model_validate(from_acc),
        to_account=schemas.AccountOut.model_validate(to_acc),
        debit_transaction=schemas.TransactionOut.model_validate(debit_txn),
        credit_transaction=schemas.TransactionOut.model_validate(credit_txn),
    )
    if dup:
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
        resp.debit_transaction.duplicate = True
        resp.credit_transaction.duplicate = True
    return resp
