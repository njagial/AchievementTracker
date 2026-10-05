# TrophyCase API Specification

Base URL: `http://127.0.0.1:8000` · Version prefix: `/v1` · Content type:
`application/json; charset=utf-8`. Interactive docs: `/docs` (Swagger), `/redoc`.

All timestamps are RFC 3339 with an explicit offset (`2026-10-04T09:30:00Z`). All ids are
UUIDv7 strings unless stated otherwise.

---

## 1. Authentication

Send `Authorization: Bearer <access_token>` on every call except registration, login,
refresh and the OAuth dance.

```bash
curl -X POST http://127.0.0.1:8000/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ada@example.com","password":"correct horse battery","device_id":"d-1","device_name":"laptop"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 900,
  "session_id": "018f4a1b-...",
  "user": {
    "id": "018f4a1b-...",
    "email": "ada@example.com",
    "display_name": "Ada",
    "avatar_url": null,
    "total_score": 40,
    "created_at": "2026-10-01T08:00:00Z",
    "updated_at": "2026-10-04T09:00:00Z"
  }
}
```

* **Access token**: 15 minutes. On `401`, refresh once and retry the original request.
* **Refresh token**: 30 days, rotating; `POST /v1/auth/refresh` issues a new pair.
* **Devices**: every token is bound to a `device_sessions` row (`device_id`/`device_name`),
  listed by `GET /v1/auth/sessions` and revocable with `DELETE /v1/auth/sessions/{id}`.
* `user.updated_at` is the value a client must echo as `base_updated_at` when pushing a
  profile write (last-writer-wins).

---

## 2. Errors

Every failure uses the same envelope:

```json
{"code": "validation_error", "message": "request body failed validation", "details": {}}
```

| HTTP | `code` | Meaning |
| --- | --- | --- |
| 401 | `not_authenticated` / `session_expired` | missing, invalid or expired token |
| 403 | `forbidden` | token valid, resource not yours |
| 404 | `not_found` | unknown id |
| 409 | `conflict` | duplicate slug / already-finished state |
| 422 | `validation_error` | body failed Pydantic validation (`details.errors`) |
| 429 | `rate_limited` | budget exhausted; `Retry-After` header + `details.retry_after` |
| 500 | `internal_error` | unexpected (never contains stack traces) |

**Rate limits** (per client IP, fixed window per minute):

| Scope | Default | Env |
| --- | --- | --- |
| global | 120 | `TROPHYCASE_RATE_LIMIT_PER_MINUTE` |
| `login` (`/register`, `/login`, `/refresh`) | 10 | `TROPHYCASE_RATE_LIMIT_LOGIN_PER_MINUTE` |

---

## 3. Endpoints

### 3.1 Auth

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/auth/register` | `201` → `AuthOut` |
| `POST` | `/v1/auth/login` | `200` → `AuthOut` |
| `POST` | `/v1/auth/refresh` | `RefreshIn {refresh_token, device_id, device_name}` → `AuthOut` |
| `GET` | `/v1/auth/me` | → `UserOut` |
| `GET` | `/v1/auth/sessions` | → `SessionOut[]` |
| `POST` | `/v1/auth/logout` | revokes the current session → `MessageOut` |
| `DELETE` | `/v1/auth/sessions/{session_id}` | revoke another device |
| `GET` | `/v1/auth/oauth/{provider}/authorize` | start the OAuth dance (`google`) |
| `GET` | `/v1/auth/oauth/{provider}/callback` | → `AuthOut` |

```jsonc
// POST /v1/auth/register  ·  LoginIn-style body
{"email": "ada@example.com", "password": "min 10 characters", "display_name": "Ada",
 "device_id": "d-1", "device_name": "laptop"}
```

### 3.2 Events

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/events` | `EventIn` | `202` `EventOut {event_id, duplicate, accepted}` |
| `POST` | `/v1/events/batch` | `EventBatchIn {events:[EventIn]}` (1-200) | `202` `EventBatchOut {results, accepted}` |

```jsonc
// EventIn
{
  "event_type": "task.completed",      // ^[a-z][a-z0-9_.]{0,63}$
  "payload": {"target_id": "task-7", "value": 10},
  "occurred_at": "2026-10-04T09:00:00Z",   // optional, max 24h in the future
  "client_id": "d-1",                       // optional
  "seq": 12,                                // optional, monotonic per client
  "event_id": "018f4a1b-..."                // optional: idempotency key
}
```

Ingestion is **idempotent on `event_id`**: replaying the same id returns
`duplicate=true` and does not create a second row. Payloads are limited to 8 KiB.

### 3.3 Achievements

| Method | Path | Response |
| --- | --- | --- |
| `GET` | `/v1/achievements` | `CatalogueOut {achievements, unlocked, score, generated_at}` |
| `GET` | `/v1/achievements/{id}` | `AchievementOut` |
| `POST` | `/v1/achievements` | `201` `AchievementOut` (needs a valid `rule`) |
| `GET` | `/v1/me/unlocks` | `AchievementOut[]` newest unlock first |

```jsonc
// AchievementOut
{
  "id": "018f4a1b-...",
  "slug": "first-steps",
  "title": "First steps",
  "description": "Complete five tasks",
  "icon": "trophy",
  "points": 10,
  "current": 3.0, "target": 5.0, "progress": 0.6,
  "unlocked": false, "unlocked_at": null,
  "rule": {"kind": "counter", "event_type": "task.completed", "count": 5},
  "state": {"count": 3, "value": 0.0, "streak": 0, "last_day": null}
}
```

`rule` and `state` are what let an offline client evaluate progress with the *same* rule
engine the server runs, and resume from the exact accumulator the server last persisted.

**Rule kinds**

```jsonc
{"kind": "counter",    "event_type": "task.completed", "count": 5, "comparison": "at_least"}
{"kind": "milestone",  "event_type": "points.earned",  "threshold": 100, "value_field": "value"}
{"kind": "boolean",    "event_type": "profile.photo_set"}
{"kind": "streak",     "event_type": "app.opened",     "days": 7}
```

### 3.4 Sync (offline)

| Method | Path | Body / query | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/sync/batch` | `SyncPushIn {client_id, ops:[SyncOpIn]}` (1-500) | `SyncPushOut` |
| `GET` | `/v1/sync/changes` | `?since=<ISO8601>&limit=1..500` | `SyncPullOut` |

```jsonc
// SyncOpIn - exactly what the client outbox stores
{
  "op_id": "018f4a1b-...",       // client-generated, idempotency key
  "seq": 12,                      // monotonic per client, replay order
  "kind": "event",                // "event" | "profile"
  "event_type": "task.completed",
  "payload": {"value": 1},
  "occurred_at": "2026-10-04T09:00:00Z",
  "fields": {},                    // profile writes: {"display_name": "Ada"}
  "base_updated_at": null          // profile writes: user.updated_at you last saw
}

// SyncPushOut
{
  "results": [{"op_id":"...", "status":"applied", "conflict":false, "detail":{}}],
  "applied": 1, "duplicates": 0, "conflicts": 0,
  "server_time": "2026-10-04T09:00:05Z"
}

// SyncPullOut
{"cursor": "2026-10-04T09:00:05Z", "server_time": "...", "has_more": false,
 "events": [{"id":"...", "event_type":"task.completed", "payload":{},
             "occurred_at":"...", "received_at":"..."}]}
```

**Status per op**

| `status` | Meaning | Client action |
| --- | --- | --- |
| `applied` | stored | delete from outbox |
| `duplicate` | replayed (`op_id`/`event_id` known) | delete from outbox |
| `rejected` | violates a rule (bad payload, expired timestamp) | delete; re-fetch catalogue to repair drift |
| `conflict: true` | LWW lost for a `profile` op | delete; adopt server state |
| *(absent)* | server did not answer for that op | keep, retry later |

### 3.5 Leaderboards & social

| Method | Path | Response |
| --- | --- | --- |
| `GET` | `/v1/leaderboards/{scope}` | `LeaderboardOut` (`scope` = `global` \| `friends`) |
| `GET` | `/v1/friends` | `FriendOut[]` |
| `POST` | `/v1/friends/request` | `201` `FriendOut` (`{email}`) |
| `POST` | `/v1/friends/{friendship_id}/accept` | `FriendOut` |

```jsonc
{"scope": "global",
 "entries": [{"rank": 1, "user_id": "...", "display_name": "Ada",
              "score": 120, "avatar_url": null, "you": true}],
 "generated_at": "2026-10-04T09:00:00Z"}
```

### 3.6 Operations

| Method | Path | Response |
| --- | --- | --- |
| `GET` | `/healthz` | `{"api":"ok","version":"0.1.0","database":"ok","redis":"ok"}` |

---

## 4. WebSocket - `GET /v1/ws?token=<access_token>`

Authenticates against a live device session; a bad token closes with `1008`.

**Server → client**

```jsonc
{"type": "connected", "user_id": "...", "connections": 1}
{"type": "heartbeat"}                                              // every 30s
{"type": "progress.updated", "achievement_id": "...", "slug": "first-steps",
 "title": "First steps", "icon": "trophy", "current": 4.0, "target": 5.0,
 "progress": 0.8, "completed": false}
{"type": "achievement.unlocked", "achievement_id": "...", "slug": "first-steps",
 "title": "First steps", "description": "Five tasks", "icon": "trophy",
 "points": 10, "unlocked_at": "2026-10-04T09:00:00Z"}
{"type": "leaderboard.changed", "user_id": "...", "score": 30}
```

**Client → server**

```jsonc
"ping"                       // keep-alive; answered with {"type":"pong"}
```

Unknown frames are acknowledged (`{"type":"ack","frame":"..."}`) but ignored, so a
misbehaving client can never break the channel. Reconnect with exponential backoff and
re-prime state with `GET /v1/achievements`.
