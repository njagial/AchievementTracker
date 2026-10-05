# TrophyCase Architecture

How the pieces fit together, why they were chosen, and where the invariants live.

---

## 1. System context (C4 level 1)

```
                     ┌──────────────────────────┐
      HTTPS / WSS    │                          │     XADD / XREADGROUP
 ┌──────────┐ ──────▶│   FastAPI  (stateless)   │ ─────────────────────┐
 │  Flet    │        │  auth · catalogue ·      │                      ▼
 │  client  │ ◀──────│  events · sync · ws      │               ┌─────────────┐
 │ (desktop │  push  └───────────┬──────────────┘               │   Redis     │
 │  / web)  │                   │ pub/sub  user:{id}           │ streams +   │
 └────┬─────┘                   │                              │ pub/sub +   │
      │                         │                              │ rate limits │
      │ local SQLite            ▼                              └──────┬──────┘
      │ (outbox + cache)   ┌──────────────┐                            │
      ▼                    │ PostgreSQL   │ ◀──────────────────────────┤
 ┌──────────────┐          │ source of    │        worker writes       │
 │ offline store│          │ truth        │ ◀────────────────────┐     │
 └──────────────┘          └──────────────┘                      │     │
                                                         ┌───────┴─────┴──┐
                                                         │ asyncio worker │
                                                         │ rule engine +  │
                                                         │ leaderboards   │
                                                         └────────────────┘
```

| Component | Responsibility | State |
| --- | --- | --- |
| **Flet client** | Rendering, optimistic UI, offline outbox, realtime subscriptions | local SQLite |
| **FastAPI tier** | Auth, validation, idempotent ingest, sync protocol, WS fan-out | stateless |
| **Redis** | Event stream, consumer group, pub/sub channels, rate-limit buckets | ephemeral |
| **Worker** | Rule evaluation, unlock + leaderboard writes, notifications | none (consumes) |
| **PostgreSQL** | Users, sessions, achievements, progress, events, friendships | durable |

---

## 2. Container view (C4 level 2)

```
client/trophycase_client/
├── app.py                 Flet shell: navigation, chrome, background loops
├── model.py               AppState - observers, use cases, toasts
├── api.py                 typed HTTP client (401 → refresh → retry)
├── realtime.py            reconnecting WebSocket with backoff
├── config.py              endpoints, device identity, TLS verification
├── offline/
│   ├── store.py           SQLite: outbox · catalogue mirror · activity feed
│   └── sync.py            push → reconcile → pull (the sync protocol)
└── ui/                    views, theme, cards

backend/trophycase/
├── domain/                entities, rule engine, errors, UUIDv7
├── application/           auth, event, achievement, sync, leaderboard use cases
├── infrastructure/        SQLAlchemy/SQLModel repositories, Redis bus, security
├── interfaces/api/        routers, schemas, middleware, dependency wiring
├── interfaces/ws/         connection registry (per-process)
├── worker/                stream consumer
└── main.py                app factory, error mapping, lifespan
```

**Dependency rule:** `interfaces → application → domain`, with `infrastructure`
implementing ports declared inward. Nothing in `domain/` imports I/O, so the rule engine
is exercised directly by unit tests and reused verbatim by the client.

---

## 3. Event pipeline

```
client log ──▶ POST /v1/events ──▶ user_events row (unique on event_id)
                                        │
                                        ▼ XADD trophycase:events
                                   Redis Stream
                                        │ XREADGROUP (worker group)
                                        ▼
                        ┌───────────────────────────────────────┐
                        │ for each active achievement:          │
                        │   state' = apply_event(rule, state, e)│
                        │   upsert progress_logs                │
                        │   on completion → user_achievements   │
                        │   recompute score → leaderboards      │
                        └───────────────────────────────────────┘
                                        │ after COMMIT
                                        ▼ PUBLISH user:{id}
                                   progress.updated
                                   achievement.unlocked
                                   leaderboard.changed
                                        │
                                        ▼
                              every connected device
```

**Delivery contract**

* *At-least-once* from stream to worker: a message is `XACK`ed only after the event row is
  marked `processed_at`. A crash in between replays it; `is_processed` short-circuits the
  second evaluation, so nothing is double counted.
* *Idempotent ingestion*: `event_id` is unique per user, so an outbox replay returns
  `duplicate=true` instead of creating a second row.
* *Row before stream*: the API commits the event row (live ingest or outbox replay) before
  `XADD`ing its id, because the worker looks the row up the moment the message arrives. If
  a row is ever still invisible the message is left un-acknowledged, reclaimed after the
  idle timeout and retried - never acknowledged-and-dropped.
* *Notifications after commit*: the worker publishes only once the transaction that wrote
  progress has committed, so a client can never see an unlock that did not happen.

---

## 4. The rule engine

A rule is declarative JSON stored in `achievements.rule_spec`:

| Kind | Spec | Progress metric |
| --- | --- | --- |
| `counter` | `{"kind":"counter","event_type":"task.completed","count":5}` | number of matching events |
| `milestone` | `{"kind":"milestone","event_type":"points.earned","threshold":100,"value_field":"value"}` | summed payload value |
| `boolean` | `{"kind":"boolean","event_type":"profile.photo_set"}` | `1` on first match |
| `streak` | `{"kind":"streak","event_type":"app.opened","days":7}` | consecutive UTC days |

`domain/rules.py` is pure: `apply_event(rule, state, event_type, payload, occurred_at)` →
`RuleEvaluation {current, target, progress, completed, state}`. Because there is no I/O
and no clock read beyond the supplied timestamp, **the client imports the same module**
to fold events optimistically while offline. Server and client therefore compute
identical numbers, and any drift is repaired on the next catalogue refresh.

---

## 5. Offline-first sync

### 5.1 Outbox

Every local write lands in a SQLite `outbox` row first (`op_id`, monotonic `seq`,
`kind ∈ {event, profile}`, payload, `occurred_at`, `base_updated_at`), *then* the local
mirror is updated optimistically, *then* a background flush runs. The UI is never blocked
on the network and never loses an action: killing the app leaves the row on disk.

### 5.2 The round trip (`SyncEngine.flush`)

```
1. POST /v1/sync/batch   ← everything in the outbox, ordered by seq
2. apply results per op:
     applied | duplicate | rejected  → delete the row (done forever)
     conflict                        → counted, row deleted (server state wins)
     unknown / no answer             → keep row, attempts += 1 (retry later)
3. GET /v1/achievements  → reconcile the catalogue mirror
4. GET /v1/sync/changes?since=<cursor> → activity feed + new cursor
```

### 5.3 Conflict rules

| Kind | Rule | Why |
| --- | --- | --- |
| `event` | idempotent on `op_id` **and** `event_id` | replaying an offline queue must not double count |
| `profile` | last-writer-wins on `base_updated_at` | only the newest field set survives; superseded ops are collapsed client-side before the push |
| progress | server is monotonic | counters never go backwards; the client's optimistic guess is merged with `max()` |

**Catalogue merge** (step 3): if the server's `current` is lower than the local optimistic
value, the worker has not evaluated the batch yet → keep the merged (`max`) view. If the
server is ahead, or any op was *rejected*, the server snapshot replaces the mirror
outright, which is what heals drift.

### 5.4 Realtime

`/v1/ws?token=<access>` authenticates against a live device session, then three tasks run:
Redis pub/sub → socket, client frames → `ping`/`ack`, and a heartbeat. The client
reconnects forever with exponential backoff (1s → 30s) and re-primes state from the
catalogue on the next flush.

---

## 6. Security

* **Passwords**: Argon2id (`pwdlib`), per-password salt, constant-time verify.
* **Tokens**: JWT access (15 min) + rotating refresh (30 days), each bound to a
  `device_sessions` row. Revoking a session invalidates both tokens for that device.
* **Rate limits**: Redis fixed-window buckets injected as route dependencies
  (`auth` scope is stricter than the global scope).
* **TLS**: outbound verification is always on. The only knob is *which* CA bundle to trust
  (`TROPHYCASE_CA_BUNDLE`); `verify=False` appears nowhere in the codebase.
* **Transport**: every response is an explicit schema - password hashes and token digests
  never cross the wire by accident (`extra="forbid"` on all models).

---

## 7. Scaling & resilience

| Concern | Approach |
| --- | --- |
| API tier | stateless; horizontal scale behind a load balancer |
| Fan-out | Redis pub/sub is cross-instance; the in-process registry only tracks local sockets |
| Backpressure | stream consumer group with `XAUTOCLAIM` for stale messages, batch size 32 |
| Slow/absent Redis | health check reports `error`, worker retries on its next tick, API stays up |
| DB migrations | Alembic runs as its own one-shot `migrate` service; `api`/`worker` wait for it |
| Idempotency | every replay path is a no-op (unique `event_id`, unique `(user, achievement)`) |

---

## 8. Testing strategy

| Layer | Where | Notes |
| --- | --- | --- |
| Pure domain | `tests/unit/test_rules.py` | rule evaluation, streaks, milestones |
| Conflict logic | `tests/unit/test_sync_conflicts.py` | LWW + idempotency at the service level |
| API contract | `tests/api/*` | SQLite + fakeredis, full ASGI stack |
| Offline client | `tests/client/*` | outbox, merge, optimistic fold, toasts |
| UI construction | `tests/client/test_ui_smoke.py` | every view builds (flet validates kwargs) |
| Real database | `tests/integration/test_postgres.py` | testcontainers + Alembic, skips without Docker |

Lint/type gate: `ruff check`, `ruff format --check`, `mypy --strict` (77 files).

---

## 9. Decision log

| Decision | Alternative | Why this one |
| --- | --- | --- |
| Custom asyncio worker on Redis Streams | Celery / Dramatiq | one less broker/beat/flower surface; consumer groups give retries and reclaim for free |
| SQLModel + SQLAlchemy 2.0 async + Alembic | Tortoise, Prisma | async-native, typed models, mature migrations |
| SQLite (`sqlite3`) in the client | aiosqlite | sub-ms local writes, no extra dependency, runs synchronously in a single-threaded UI |
| Shared pure rule engine on both sides | re-implement progress in the client | identical numbers offline, one source of truth, unit-tested once |
| Flet 1.0 for the client | React Native, Tkinter | one Python codebase for desktop, web and mobile, native rendering |
| Redis fixed-window limiter | slowapi | no extra middleware stack, testable against fakeredis |
| UUIDv7 primary keys | serial ints, UUIDv4 | time-sortable, non-guessable, index-friendly inserts |
