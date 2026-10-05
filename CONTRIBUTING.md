# Contributing to TrophyCase

Thanks for helping out. This document is the short path from a clean checkout to a
reviewable pull request.

---

## 1. Setup

Prerequisites: **Python 3.12+** (3.13 recommended), [uv](https://docs.astral.sh/uv/),
Docker (only needed for the integration suite).

```bash
git clone <repo-url> trophycase
cd trophycase
uv sync                      # runtime + dev dependencies into .venv
cp .env.example .env         # then set TROPHYCASE_SECRET_KEY
```

Useful entry points:

```bash
uv run trophycase-api        # uvicorn with reload semantics
uv run trophycase-worker     # rule engine
uv run trophycase-client     # Flet desktop client
uv run flet run client/main.py --web   # same client in a browser
```

---

## 2. Everyday workflow

```bash
# 1. infrastructure (only the first time / after docker compose down)
docker compose up -d db redis
docker compose run --rm migrate

# 2. run everything locally
uv run uvicorn trophycase.main:app --reload
uv run trophycase-worker

# 3. quality gate - run this before every commit
uv run ruff check --fix .
uv run ruff format .
uv run mypy
uv run pytest -q -m "not integration"
uv run pytest -q -m integration     # needs Docker
```

Nothing is "done" until all four of `ruff check`, `ruff format --check`, `mypy` and the
test suite are green. The CI workflow runs exactly these commands.

---

## 3. Project layout

```
backend/trophycase/
├── domain/            pure Python - entities, rule engine, errors, UUIDv7
├── application/       use cases and orchestration
├── infrastructure/    repositories (SQLModel), Redis event bus, security
├── interfaces/        FastAPI routers/schemas + WebSocket gateway
└── worker/            Redis Stream consumer

client/trophycase_client/
├── offline/           SQLite outbox + sync engine (the offline-first core)
├── ui/                Flet views, theme, cards
├── api.py  realtime.py  model.py  app.py

tests/{unit,api,client,integration}
alembic/               migrations
```

**Dependency rule:** `interfaces → application → domain`; `infrastructure` implements
ports declared inward. Domain code never imports I/O - if you need the clock, a database
or a socket, the value comes in as an argument.

---

## 4. Coding standards

* **Types**: `mypy --strict` passes with no suppressions outside the documented SQLModel
  override. Public functions have full signatures; no `Any` leaking out of helpers.
* **Style**: `ruff` (line length 100). Format with `ruff format` rather than by hand.
* **Docstrings**: module docstrings explain *why*, not *what*. Public classes and
  functions get a one-line summary; complex invariants (sync, idempotency) get a
  `Delivery contract`-style section.
* **Comments**: only where the code would mislead without them. No narrating comments.
* **Errors**: raise domain errors (`trophycase.domain.errors`) instead of returning
  sentinels; routers map them to HTTP in `main.py`.
* **No secrets in code**: never hard-code keys, and never log a token or password hash.

### Testing

| Change | Where to add coverage |
| --- | --- |
| Rule / progress logic | `tests/unit/test_rules.py` |
| Use case or conflict resolution | `tests/api/test_sync.py` (or a new `tests/unit/...`) |
| Offline store / sync engine | `tests/client/...` |
| Endpoint contract | `tests/api/...` |
| Schema change | `tests/integration/test_postgres.py` + a new Alembic revision |

Prefer deterministic tests: pass timestamps in, use `fakeredis`, use `tmp_path` for
files. A test that needs real Docker must be marked `@pytest.mark.integration` and skip
gracefully when the daemon is unavailable.

### Database changes

1. Edit the SQLModel model.
2. `uv run alembic revision --autogenerate -m "what changed"`
3. Review the generated SQL by hand - autogenerate misses server defaults and CHECKs.
4. `uv run alembic upgrade head` then `uv run alembic downgrade -1 && uv run alembic upgrade head`
   (both directions must work) and `uv run alembic check`.

---

## 5. Pull requests

* Keep one concern per PR; a refactor PR should not change behaviour.
* Describe *why* the change is needed and how it was verified (commands + results).
* Update `README.md` / `ARCHITECTURE.md` / `API_SPEC.md` when behaviour or contracts
  change, and add a `CHANGELOG.md` entry under **Unreleased**.
* Never commit `.env`, `*.db`, credentials, or editor cruft (see `.gitignore`).
* Do not weaken the quality gate to make CI pass (`# type: ignore` without a reason,
  `verify=False`, skipped tests). If something genuinely cannot be typed, document it in
  the PR and in the code.

---

## 6. Reporting bugs

Open an issue with: what you did, what you expected, what happened, and the output of

```bash
uv run python scripts/check_tls.py   # for anything TLS / connectivity related
uv run pytest -q -x                  # to confirm a clean baseline
```
