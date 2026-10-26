"""Pydantic request/response schemas."""
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

MAX_AMOUNT = Decimal("1000000000000")  # 1 trillion per transaction
TRANSFER_PREFIX = "transfer:"  # reserved: transfer legs are stored as transfer:<ref>:debit / :credit

_PLAIN_DECIMAL = re.compile(r"^[0-9]{1,13}(\.[0-9]+)?$")
_TWO_PLACES = Decimal("0.01")


def _parse_amount(v):
    """Plain decimal only: no NaN/Infinity, exponents, signs, spaces or non-ASCII digits."""
    if isinstance(v, bool) or v is None:
        raise ValueError("amount must be a number")
    if isinstance(v, (int, float)):
        v = str(v)
    if isinstance(v, Decimal):
        v = format(v, "f")
    if not isinstance(v, str) or not _PLAIN_DECIMAL.match(v):
        raise ValueError("amount must be a plain decimal number such as 10 or 10.50")
    try:
        d = Decimal(v)
        if d != d.quantize(_TWO_PLACES):
            raise ValueError("amount must have at most 2 decimal places")
        return d.quantize(_TWO_PLACES)
    except InvalidOperation:
        raise ValueError("amount must have at most 2 decimal places")


# right-to-left/left-to-right overrides and isolates: they make text display differently from what it is
_BIDI_CONTROLS = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _check_chars(v: str, allow: str = "") -> None:
    for ch in v:
        if ch in allow:
            continue
        cat = unicodedata.category(ch)
        if cat == "Cc":
            raise ValueError("must not contain control characters")
        if cat == "Cs":
            raise ValueError("must not contain unpaired surrogate characters")
        if ch in _BIDI_CONTROLS:
            raise ValueError("must not contain text-direction override characters")


def _clean_text(v):
    if not isinstance(v, str):
        return v  # type error is reported by pydantic itself
    _check_chars(v)
    # NFC: "e" + combining accent and the single character "\u00e9" become the same string
    return unicodedata.normalize("NFC", v).strip()


def _clean_reference(v):
    v = _clean_text(v)
    # invisible characters (zero-width space, joiners, BOM...) would allow look-alike references
    if isinstance(v, str) and any(unicodedata.category(ch) == "Cf" for ch in v):
        raise ValueError("must not contain invisible formatting characters")
    return v


def _clean_description(v):
    if v is None:
        return ""
    if not isinstance(v, str):
        return v
    _check_chars(v, allow="\n\r\t")
    return unicodedata.normalize("NFC", v).strip()


Money = Annotated[Decimal, BeforeValidator(_parse_amount), Field(gt=0, le=MAX_AMOUNT)]
Text = Annotated[str, BeforeValidator(_clean_text)]
Reference = Annotated[str, BeforeValidator(_clean_reference)]
TxnType = Literal["credit", "debit"]


class _UtcOut(BaseModel):
    """SQLite drops tzinfo; every stored timestamp is UTC, so say so."""

    @field_validator("created_at", mode="after", check_fields=False)
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v


class AccountCreate(BaseModel):
    name: Annotated[Text, Field(min_length=1, max_length=200)]


class AccountOut(_UtcOut):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    balance: Decimal
    created_at: datetime


class TransactionCreate(BaseModel):
    type: TxnType
    amount: Money
    description: Annotated[str, BeforeValidator(_clean_description), Field(max_length=500)] = ""
    reference_id: Annotated[Reference, Field(min_length=1, max_length=200)]

    @field_validator("reference_id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v.lower().startswith(TRANSFER_PREFIX):
            raise ValueError(f"reference_id must not start with '{TRANSFER_PREFIX}' (reserved for transfers)")
        return v


class TransactionOut(_UtcOut):
    model_config = ConfigDict(from_attributes=True)
    id: str
    account_id: str
    type: str
    amount: Decimal
    description: Optional[str]
    reference_id: str
    created_at: datetime
    duplicate: bool = False  # true when this reference_id already existed
    message: Optional[str] = None  # set on a replay: says the transaction already exists


class BalanceOut(BaseModel):
    account_id: str
    balance: Decimal


class PaginationMeta(BaseModel):
    current_page: int
    page_size: int
    total_records: int
    total_pages: int


class TransactionHistoryOut(BaseModel):
    data: list[TransactionOut]
    pagination: PaginationMeta


class TransferCreate(BaseModel):
    from_account_id: Annotated[Text, Field(min_length=1, max_length=64)]
    to_account_id: Annotated[Text, Field(min_length=1, max_length=64)]
    amount: Money
    description: Annotated[str, BeforeValidator(_clean_description), Field(max_length=500)] = ""
    # shorter than a transaction reference: the stored legs add "transfer:" and ":credit"
    reference_id: Annotated[Reference, Field(min_length=1, max_length=150)]

    @field_validator("reference_id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v.lower().startswith(TRANSFER_PREFIX):
            raise ValueError(f"reference_id must not start with '{TRANSFER_PREFIX}'")
        return v


class TransferOut(BaseModel):
    from_account: AccountOut
    to_account: AccountOut
    debit_transaction: TransactionOut
    credit_transaction: TransactionOut


class ErrorResponse(BaseModel):
    error: str
    message: str
