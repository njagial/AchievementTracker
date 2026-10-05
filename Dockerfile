# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Stage 1 - resolve dependencies and build the project into a virtualenv.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependency metadata first so this layer is cached across source changes.
COPY pyproject.toml uv.lock .python-version README.md ./
COPY backend/ backend/
COPY client/ client/

RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------
# Stage 2 - minimal runtime: virtualenv + migration scripts.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic/ alembic/
COPY README.md ./

USER app

EXPOSE 8000

# Default: the API. docker-compose overrides this for the worker/migrator services.
CMD ["uvicorn", "trophycase.main:app", "--host", "0.0.0.0", "--port", "8000"]
