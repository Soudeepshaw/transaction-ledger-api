"""Pydantic request/response schemas."""
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, ConfigDict


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    balance: Decimal
    created_at: datetime


class TransactionCreate(BaseModel):
    type: Literal["credit", "debit"]
    amount: Decimal = Field(gt=0)
    description: Optional[str] = ""
    reference_id: str = Field(min_length=1, max_length=200)


class TransactionOut(BaseModel):
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
    from_account_id: str
    to_account_id: str
    amount: Decimal = Field(gt=0)
    description: Optional[str] = ""
    reference_id: str = Field(min_length=1, max_length=200)


class TransferOut(BaseModel):
    from_account: AccountOut
    to_account: AccountOut
    debit_transaction: TransactionOut
    credit_transaction: TransactionOut


class ErrorResponse(BaseModel):
    error: str
    message: str
