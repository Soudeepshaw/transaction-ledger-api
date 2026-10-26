# syntax=docker/dockerfile:1

# ---- build: install dependencies into a virtualenv with uv ----------------
FROM python:3.15-slim-trixie AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

# compile .pyc at build time (faster start), copy instead of hardlink (cache mount is
# another filesystem), and never download a Python: use the one in this image
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN uv venv /opt/venv --python /usr/local/bin/python3

# dependencies first, so this layer is reused until requirements.txt changes
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    VIRTUAL_ENV=/opt/venv uv pip install -r requirements.txt


# ---- runtime: just Python, the virtualenv and the app ---------------------
FROM python:3.15-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATABASE_URL="sqlite:////data/ledger.db"

# unprivileged user; /data holds the SQLite file when no DATABASE_URL is given
RUN groupadd --system app \
 && useradd --system --gid app --home-dir /app --no-create-home app \
 && mkdir /data && chown app:app /data

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app app ./app

USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

# exec form so uvicorn is PID 1 and gets SIGTERM directly for a clean shutdown
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
