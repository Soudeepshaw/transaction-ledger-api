"""Domain-level exceptions, mapped to HTTP responses in main.py's exception handlers."""


class LedgerError(Exception):
    status_code = 400
    error_code = "bad_request"

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class AccountNotFound(LedgerError):
    status_code = 404
    error_code = "account_not_found"


class InsufficientBalance(LedgerError):
    status_code = 422
    error_code = "insufficient_balance"


class ReferenceConflict(LedgerError):
    status_code = 422  # IETF Idempotency-Key draft §2.7: same key, different payload
    error_code = "reference_conflict"


class ValidationFailed(LedgerError):
    status_code = 422
    error_code = "validation_error"


class InvalidPagination(LedgerError):
    status_code = 422
    error_code = "invalid_pagination"
