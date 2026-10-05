# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Event ingestion now commits the row **before** the stream message naming it is written.
  The worker reads ids immediately, so publishing inside the request's own transaction let
  it race the commit, find nothing and drop the event - progress silently stopped moving.
  The same ordering is applied to offline replay (`POST /v1/sync/batch`).
- The worker leaves a not-yet-visible event un-acknowledged instead of acknowledging it,
  so it is reclaimed and retried rather than lost, and it marks an evaluation processed
  before updating the leaderboard cache or fanning out notifications.

### Added

- `scripts/smoke.py`: end-to-end check against a running stack (register, realtime unlock,
  idempotent sync replay, leaderboard, logout).
- Regression tests for the publish-after-commit ordering (single ingest + offline replay)
  and for the worker's acknowledge/retry contract.
- CI: client suite added to the test job, container image build job, Python pinned to 3.13.
- README: client quickstart (`flet run`, `--web`, `trophycase-client`, `uv sync --extra
  client`), `TROPHYCASE_API_PORT` for machines where `8000` is already taken, and the
  client configuration variables.

## [0.1.0] - 2026-10-04

First complete release: API, worker, offline client, migrations, tests and CI.

### Added

**Backend**
- FastAPI service with versioned `/v1` routes: auth (register, login, rotating refresh,
  per-device sessions, logout, OAuth Google scaffold), events (single + batch idempotent
  ingest), achievement catalogue, sync push/pull, leaderboards and friends.
- Declarative achievement rule engine (`counter`, `milestone`, `boolean`, `streak`) as a
  pure domain module, evaluated by the worker and reused by the client for optimistic UI.
- Redis Stream + custom asyncio worker: at-least-once delivery, consumer group with
  stale-message reclaim, unlock/score/leaderboard fan-out over Redis pub/sub.
- WebSocket gateway `/v1/ws` bridging pub/sub to every connected device, with heartbeat
  and ping/pong keep-alive.
- Offline sync protocol: `POST /v1/sync/batch` (idempotent `op_id`, LWW profile writes)
  and `GET /v1/sync/changes` (cursor-based pull); catalogue responses now carry `rule`
  and `state` so a reconnecting client resumes evaluation exactly.
- JWT access (15 min) + rotating refresh (30 days) bound to device sessions, Argon2id
  password hashing, per-IP Redis rate limiting with `Retry-After`.
- Alembic migrations (8 tables) with an async env, `compare_type` and batch mode.
- Structured error envelope (`code`/`message`/`details`), request validation mapping and
  `/healthz` probing database + Redis.

**Client**
- Flet 1.0 desktop/web client: adaptive dark/light theme, sign-in/sign-up, achievement
  catalogue with one-tap logging, activity feed, leaderboard and profile screens.
- Offline-first core: SQLite outbox (FIFO, per-op attempts), catalogue mirror with
  optimistic rule evaluation, activity feed mirror, and a sync engine that pushes,
  reconciles (`max()` merge, server wins on rejection) and pulls behind a cursor.
- Reconnecting WebSocket client with exponential backoff, plus periodic background sync.
- Unlock toasts and offline/queued status indicators in the chrome.

**Tooling**
- `uv` project with `trophycase-api`, `trophycase-worker` and `trophycase-client` entry
  points, hatchling packaging of `backend/` + `client/`.
- `mypy --strict`, `ruff` (lint + format), pytest suites for unit, API, client offline
  logic, UI construction smoke tests and testcontainers-backed Postgres integration.
- Multi-stage `Dockerfile` (non-root, frozen lockfile) and `docker-compose.yml` with
  healthchecked `db`, `redis`, one-shot `migrate`, `api` and `worker`.
- GitHub Actions workflow: lint → typecheck → test with coverage → build → integration.
- `scripts/check_tls.py` certificate-chain diagnostics and README troubleshooting for
  `localhost` → `::1` black-holed loopback sockets.
