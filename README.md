# TrophyCase

**Real-time, offline-first, cross-device achievement tracker.**

TrophyCase lets a user unlock achievements (badges, streaks, milestones) from any device —
phone, desktop, browser — and see every other connected device update *instantly* through a
WebSocket. When a device goes offline it queues events in a local SQLite outbox and replays
them, conflict-free, as soon as connectivity returns.

| Pillar | How it is delivered |
| --- | --- |
| **Scalability** | Stateless FastAPI tier, decoupled Redis Streams worker, pub/sub fan-out |
| **Reliability** | Eventual consistency, idempotent event ingestion, LWW + op-id conflict resolution |
| **Security** | JWT access/refresh rotation, per-device sessions, Argon2id, Redis token-bucket rate limits, zero `verify=False` TLS |
| **Maintainability** | Clean Architecture / DDD layering, `mypy --strict`, `ruff`, full test suite |
| **UX** | Low-latency realtime pushes, adaptive dark/light Flet UI, animated unlock toasts |

---

## 1. Architecture at a glance

```
┌─────────────┐   HTTPS/JSON    ┌──────────────┐    XADD     ┌───────────────┐
│ Flet client │ ───────────────▶│  FastAPI API │────────────▶│ Redis Streams │
│ (desktop/   │◀─────────────── │  (auth, sync,│             │  trophycase:  │
│  web/mobile)│   WebSocket     │   events)    │             │    events     │
└──────┬──────┘                 └──────┬───────┘             └──────┬────────┘
       │                               │ pub/sub                    │ XREADGROUP
       │ SQLite outbox                 │ PUBLISH user:{id}          ▼
       ▼                               ▼                     ┌───────────────┐
┌─────────────┐                 ┌──────────────┐              │ Worker        │
│ local DB    │                 │    Redis     │◀─────────────│ (rule engine, │
└─────────────┘                 │ cache+pubsub │  notifications│  leaderboards)│
                                └──────────────┘              └──────┬────────┘
                                                                     │
                                                              ┌──────▼────────┐
                                                              │  PostgreSQL   │
                                                              └───────────────┘
```

Full diagrams (C4, ERD, event flows) live in [`ARCHITECTURE.md`](ARCHITECTURE.md).

### Layering (Clean Architecture)

```
backend/trophycase/
├── domain/          pure Python: entities, achievement rule specs, domain errors
├── application/     use cases: auth, event evaluation, sync, leaderboards
├── infrastructure/  persistence (SQLModel/SQLAlchemy), Redis, security, event bus
└── interfaces/      FastAPI routers + WebSocket gateway (transport concerns only)
```

Dependencies point inwards only: `interfaces → application → domain`, with
`infrastructure` implementing ports declared by the application layer.

---

## 2. Quickstart

### Prerequisites

* Python **3.12+** (3.13 recommended)
* [uv](https://docs.astral.sh/uv/) — `pip install uv` or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
* Docker + Docker Compose (PostgreSQL, Redis)

### 2.1 Clone and install

```bash
git clone <repo-url> trophycase
cd trophycase
uv sync                 # creates .venv and installs runtime + dev dependencies
cp .env.example .env    # then edit TROPHYCASE_SECRET_KEY
```

### 2.2 Start infrastructure

```bash
docker compose up -d db redis
docker compose run --rm migrate          # alembic upgrade head
```

### 2.3 Run the API + worker

```bash
uv run uvicorn trophycase.main:app --reload          # API  → http://127.0.0.1:8000/docs
uv run trophycase-worker                             # achievement rule engine
```

### 2.4 Run the client (Flet)

```bash
uv run flet run client/main.py                       # desktop
uv run flet run client/main.py --web                 # browser
uv run trophycase-client                             # same app via the console entry point
```

The client is offline-first: events queue in a local SQLite outbox and flush in the
background, so it keeps working (and unlocking achievements) with the API down. It talks to
`http://127.0.0.1:8000` by default — point it somewhere else with
`TROPHYCASE_API_BASE_URL` / `TROPHYCASE_WS_URL`, and isolate a test install with
`TROPHYCASE_CLIENT_DATA_DIR`. For a production (non-dev) install of just the client:

```bash
uv sync --extra client
```

### 2.5 Verify the stack end to end

```bash
uv run python scripts/smoke.py http://127.0.0.1:8000
```

Registers a throwaway user, unlocks an achievement over the WebSocket, replays a sync op to
prove idempotency, and reads the leaderboard.

### 2.6 One-shot: everything

```bash
docker compose up --build
```

| Service | URL |
| --- | --- |
| API + Swagger | http://127.0.0.1:8000/docs |
| Health | http://127.0.0.1:8000/healthz |
| Postgres | `127.0.0.1:5432` (`trophycase` / `trophycase`) |
| Redis | `127.0.0.1:6379` |

---

## 3. Environment variables

All settings are read by `trophycase.config.Settings` (pydantic-settings) and prefixed with
`TROPHYCASE_`. See [`.env.example`](.env.example) for the complete list.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TROPHYCASE_SECRET_KEY` | *(required in production)* | HMAC key for JWT signing |
| `TROPHYCASE_DATABASE_URL` | local PG on `127.0.0.1` | Async SQLAlchemy URL |
| `TROPHYCASE_REDIS_URL` | `redis://127.0.0.1:6379/0` | Cache, pub/sub, streams |
| `TROPHYCASE_CA_BUNDLE` | *(empty)* | Custom PEM bundle for outbound TLS |
| `TROPHYCASE_RATE_LIMIT_PER_MINUTE` | `120` | Global API budget per client IP |
| `TROPHYCASE_API_BASE_URL` | `http://127.0.0.1:8000` | Where the Flet client finds the API |
| `TROPHYCASE_WS_URL` | `ws://127.0.0.1:8000/v1/ws` | Realtime endpoint used by the client |
| `TROPHYCASE_CLIENT_DATA_DIR` | per-OS app-data dir | Offline SQLite + client logs |

---

## 4. Troubleshooting

### `SSL: CERTIFICATE_VERIFY_FAILED` / "unknown certificate verification error"

TrophyCase **never disables certificate verification**. If your network intercepts TLS
(a corporate proxy, Zscaler, or a private PyPI mirror with a custom CA), point the process
at your organisation's PEM bundle instead:

```bash
# 1. Diagnose what is failing and which CA store is in use
uv run python scripts/check_tls.py

# 2. Tell every TrophyCase process which CA bundle to trust
export TROPHYCASE_CA_BUNDLE=/etc/ssl/certs/company-ca.pem   # Linux/macOS
$env:TROPHYCASE_CA_BUNDLE="C:\certs\company-ca.pem"         # PowerShell

# 3. Python-wide (pip, httpx, websockets)
export SSL_CERT_FILE=/etc/ssl/certs/company-ca.pem
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/company-ca.pem
```

Package installs:

```bash
uv pip install --index-url https://simple.example.internal/ --allow-insecure-host pypi.org
# or, for pip only, if the mirror's CA is not yet trusted:
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org <pkg>
```

Docker (bake the CA into the image rather than turning verification off):

```dockerfile
COPY company-ca.pem /usr/local/share/ca-certificates/company-ca.pem
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
```

Git:

```bash
git config --global http.sslCAInfo /etc/ssl/certs/company-ca.pem
```

`verify=False`, `--trusted-host` on production containers and `GIT_SSL_NO_VERIFY=1` are
**not** acceptable fixes — they trade a working build for a silent man-in-the-middle hole.

### Connection hangs on `localhost` (Windows)

`localhost` resolves to `::1` first on many Windows machines, and some local COM services
bind that IPv6 loopback socket for *every* port Docker publishes — accepting the connection
and then never answering. The symptom is a hang or an opaque TLS failure against a URL that
definitely works with `127.0.0.1`. All defaults in this repository therefore use
`127.0.0.1`:

```bash
uv run python scripts/check_tls.py http://127.0.0.1:8000/healthz
```

If you must keep the name `localhost`, force IPv4 in your own tooling and expect the same
behaviour from anything that resolves names itself (browsers, `psql`, `redis-cli`).

### Ports already in use

Something on your machine already holds the published port (a local PostgreSQL on `8000`, a
proxy, a previous stack). Publish elsewhere instead of killing it:

```bash
TROPHYCASE_API_PORT=18000 docker compose up -d api     # Linux/macOS
$env:TROPHYCASE_API_PORT="18000"; docker compose up -d api   # PowerShell
```

Then point the client at the new port (`TROPHYCASE_API_BASE_URL`,
`TROPHYCASE_WS_URL`) and run `scripts/smoke.py` against it. To move Postgres/Redis as well,
set `TROPHYCASE_DB_PORT` and `TROPHYCASE_REDIS_PORT`.

```bash
docker compose down          # stop the stack
docker compose up -d         # restart
```

### Database schema out of date

```bash
uv run alembic upgrade head
uv run alembic current
```

---

## 5. Development

```bash
uv run ruff check --fix .      # lint
uv run ruff format .           # format
uv run mypy                    # strict type check
uv run pytest                  # unit + API tests (SQLite + fakeredis)
uv run pytest -m integration    # needs Docker (testcontainers → real Postgres)
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and coding standards,
[`API_SPEC.md`](API_SPEC.md) for REST/WebSocket contracts, and
[`CHANGELOG.md`](CHANGELOG.md) for release history.

---

## 6. Repository layout

```
.
├── backend/trophycase/        API + worker source (Clean Architecture)
├── client/                    Flet cross-platform app (offline outbox, realtime UI)
├── tests/                     unit / api / integration suites
├── alembic/                   DB migrations
├── scripts/check_tls.py       certificate-chain diagnostics
├── docker-compose.yml         postgres + redis + api + worker
├── .github/workflows/ci.yml   lint → typecheck → test → docker build
└── README.md ARCHITECTURE.md API_SPEC.md CONTRIBUTING.md CHANGELOG.md
```

## 7. License

MIT — see [`LICENSE`](LICENSE).
