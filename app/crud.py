"""Business logic — every write path commits in a single DB transaction.
Crash mid-op leaves no partial state."""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .errors import AccountNotFound, InsufficientBalance, InvalidPagination, ReferenceConflict


def create_account(db: Session, name: str) -> models.Account:
    acct = models.Account(name=name, balance=Decimal("0"))
    db.add(acct)
    db.commit()
    db.refresh(acct)
    return acct


def get_account(db: Session, account_id: str, lock: bool = False) -> models.Account:
    # lock=True -> SELECT ... FOR UPDATE on Postgres; SQLite ignores it (writes are serialised anyway)
    if lock:
        stmt = select(models.Account).where(models.Account.id == account_id).with_for_update()
        acct = db.execute(stmt).scalar_one_or_none()
    else:
        acct = db.get(models.Account, account_id)
    if acct is None:
        raise AccountNotFound(f"Account '{account_id}' does not exist.")
    return acct


def _find_existing_txn(db: Session, account_id: str, reference_id: str):
    stmt = select(models.Transaction).where(
        models.Transaction.account_id == account_id,
        models.Transaction.reference_id == reference_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def add_transaction(
    db: Session,
    account_id: str,
    type_: str,
    amount: Decimal,
    description: str,
    reference_id: str,
) -> tuple[models.Transaction, bool]:
    """Returns (txn, was_duplicate). Raises on 404 / insufficient funds."""
    # lock the row so two concurrent debits can't both pass the balance check
    acct = get_account(db, account_id, lock=True)

    # idempotency — return the original txn if ref already used
    existing = _find_existing_txn(db, account_id, reference_id)
    if existing is not None:
        if existing.type != type_ or existing.amount != amount:
            raise ReferenceConflict(
                f"reference_id '{reference_id}' was already used with a different type/amount."
            )
        return existing, True

    if type_ == "debit" and acct.balance < amount:
        raise InsufficientBalance(
            f"Account '{account_id}' balance {acct.balance}, cannot debit {amount}."
        )

    txn = models.Transaction(
        account_id=account_id,
        type=type_,
        amount=amount,
        description=description or "",
        reference_id=reference_id,
    )
    if type_ == "credit":
        acct.balance = acct.balance + amount
    else:
        acct.balance = acct.balance - amount

    db.add(txn)
    try:
        db.commit()
    except IntegrityError:
        # lost the race on the unique constraint — someone else inserted first
        db.rollback()
        winner = _find_existing_txn(db, account_id, reference_id)
        if winner is not None:
            return winner, True
        raise
    db.refresh(txn)
    db.refresh(acct)
    return txn, False


def get_balance(db: Session, account_id: str) -> models.Account:
    return get_account(db, account_id)


def list_transactions(
    db: Session,
    account_id: str,
    type_: str | None,
    page: int,
    limit: int,
):
    get_account(db, account_id)  # 404 early

    if page < 1 or limit < 1 or limit > 200:
        raise InvalidPagination("page >= 1, limit 1..200.")

    stmt = select(models.Transaction).where(
        models.Transaction.account_id == account_id
    )
    if type_ is not None:
        stmt = stmt.where(models.Transaction.type == type_)

    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    rows = (
        db.execute(
            stmt.order_by(
                models.Transaction.created_at.desc(),
                models.Transaction.id,  # stable when timestamps tie
            )
            .offset((page - 1) * limit)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    total_pages = (total + limit - 1) // limit if total else 0
    return rows, {
        "current_page": page,
        "page_size": limit,
        "total_records": total,
        "total_pages": total_pages,
    }


def transfer(
    db: Session,
    from_account_id: str,
    to_account_id: str,
    amount: Decimal,
    description: str,
    reference_id: str,
):
    """Atomic transfer — debit + credit in one commit."""
    # lock in id order so two opposite transfers can't deadlock each other
    locked = {a: get_account(db, a, lock=True) for a in sorted({from_account_id, to_account_id})}
    from_acct, to_acct = locked[from_account_id], locked[to_account_id]

    debit_ref = f"{reference_id}:debit"
    credit_ref = f"{reference_id}:credit"

    # FIX: check duplicates BEFORE balance so a retry after draining the
    # account returns the original result instead of wrongly 422-ing
    existing_debit = _find_existing_txn(db, from_account_id, debit_ref)
    existing_credit = _find_existing_txn(db, to_account_id, credit_ref)
    if existing_debit is not None and existing_credit is not None:
        return from_acct, to_acct, existing_debit, existing_credit, True

    if from_acct.balance < amount:
        raise InsufficientBalance(
            f"Account '{from_account_id}' balance {from_acct.balance}, "
            f"cannot transfer {amount}."
        )

    debit_txn = models.Transaction(
        account_id=from_account_id, type="debit", amount=amount,
        description=description, reference_id=debit_ref,
    )
    credit_txn = models.Transaction(
        account_id=to_account_id, type="credit", amount=amount,
        description=description, reference_id=credit_ref,
    )
    from_acct.balance = from_acct.balance - amount
    to_acct.balance = to_acct.balance + amount

    db.add_all([debit_txn, credit_txn])
    db.commit()
    db.refresh(debit_txn)
    db.refresh(credit_txn)
    db.refresh(from_acct)
    db.refresh(to_acct)
    return from_acct, to_acct, debit_txn, credit_txn, False
