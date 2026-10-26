from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .database import Base, engine
from .errors import LedgerError
from .routers import router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Ledger API", version="0.1.0")


@app.exception_handler(LedgerError)
async def ledger_error_handler(request: Request, exc: LedgerError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error_code, "message": exc.message},
    )


app.include_router(router)
