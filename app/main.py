import logging

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from .database import Base, engine, get_db
from .errors import LedgerError
from .routers import router

Base.metadata.create_all(bind=engine)

log = logging.getLogger("ledger")

# no trailing-slash redirects: a redirected POST can silently lose its body
app = FastAPI(title="Ledger API", version="0.1.0", redirect_slashes=False)


@app.exception_handler(LedgerError)
async def ledger_error_handler(request: Request, exc: LedgerError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error_code, "message": exc.message},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # same {error, message} shape as every other error, plus which fields failed
    details = [
        {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "message": "Invalid request data.", "details": details},
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    codes = {400: "bad_request", 404: "not_found", 405: "method_not_allowed"}
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": codes.get(exc.status_code, "http_error"), "message": str(exc.detail)},
    )


app.include_router(router)


@app.get("/health", tags=["health"])
def health(db: Session = Depends(get_db)):
    """Liveness + database check, used by the Docker HEALTHCHECK."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        log.exception("health check: database unreachable")
        return JSONResponse(
            status_code=503,
            content={"error": "database_unavailable", "message": "The database is not reachable."},
        )
    return {"status": "ok"}


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": "Something went wrong on our side."},
    )
