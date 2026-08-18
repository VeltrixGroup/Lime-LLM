# storeguard cloud — multi-tenant control plane

The GPU-free SaaS backend that lets storeguard be sold to many stores. It owns
**accounts, tenants, and login**; detection stays on each store's edge PC (see
[`../dashboard`](../dashboard) / [`../pipeline.py`](../pipeline.py)) because
cameras are only reachable on the store LAN. The control plane never imports
torch/ultralytics.

> **Status: Phases 0–2.**
> - **Phase 0 (foundation):** database + `Tenant`/`User`/`Membership` models +
>   password auth (signed-cookie sessions) + tenant-scoped queries.
> - **Phase 1 (auth-gated UI + roles):** a login/signup web UI at `/` that gates
>   on the session, owner/staff roles, member management (add / change role /
>   remove, with a last-owner guard), and self-service password change.
> - **Phase 2 (cameras + zones):** per-tenant camera CRUD and polygon zones
>   (stored in the DB, replacing the per-deployment YAML), managed in the UI.
>   Owners write; staff read. Camera sources hide credentials in labels.
> - **Phase 3 (edge-agent enrollment):** per-tenant agent tokens; the edge
>   agent pulls its camera config and pushes events + short alarm clips up.
>   Events carry a `person_id` (cross-camera identity) so the cabinet can show
>   every camera that saw one person — filter with `GET /api/events?person_id=`.
> - **Telegram delivery:** each tenant configures a bot in the cabinet
>   (`/api/settings/telegram`); when an event's clip lands, the cloud pushes it
>   to that tenant's bot as a background task. Bot tokens live only in the cloud
>   (never shipped to store PCs) and are never returned by the API.
>
> Edge AI (theft/idle/cashier detection, cross-camera re-ID, POS check) runs on
> the edge and is a separate track; the cloud is its config + event backend.

## Architecture (why hybrid, not "all cloud")

```
  Store LAN (edge PC)                         Cloud control plane (this package)
  ┌───────────────────────────┐              ┌─────────────────────────────────┐
  │ cameras → storeguard edge  │  events/     │ accounts · tenants · config      │
  │ (YOLO detection, local)    │ ── thumbs ─▶ │ dashboards · event storage       │
  └───────────────────────────┘  (HTTPS up)   └─────────────────────────────────┘
       video never leaves the store                 no GPU required
```

## Tenancy model

- **User** — one global login identity (unique email).
- **Tenant** — one store account/organization.
- **Membership** — links a user to a tenant with a `role` (`owner`/`staff`).

Every tenant-owned query is gated by `current_membership` (in [`auth.py`](auth.py)):
the active tenant id comes from the signed session, but access is granted only
if a matching membership row exists — a forged/stale tenant id can't reach
another store's data.

## Run it (local dev)

```bash
# 1. create the schema (production path)
STOREGUARD_DATABASE_URL="sqlite:///./storeguard_cloud.db" uv run --extra cloud alembic upgrade head

# 2. serve
STOREGUARD_SECRET_KEY="$(openssl rand -hex 32)" uv run --extra cloud storeguard cloud

# …or a one-shot dev run that creates tables directly (skips Alembic):
uv run --extra cloud storeguard cloud --dev
```

Interactive API docs: `http://127.0.0.1:8000/api/docs`.

## Configuration (env vars, prefix `STOREGUARD_`)

| Variable                     | Default                          | Notes                                  |
| ---------------------------- | -------------------------------- | -------------------------------------- |
| `STOREGUARD_DATABASE_URL`    | `sqlite:///./storeguard_cloud.db`| Use Postgres in prod (`postgresql+psycopg://…`). |
| `STOREGUARD_SECRET_KEY`      | `dev-insecure-change-me`         | **Must** be set in prod — signs sessions. |
| `STOREGUARD_SESSION_COOKIE`  | `storeguard_session`             | Session cookie name.                   |
| `STOREGUARD_SESSION_MAX_AGE` | `1209600` (14 days)              | Session lifetime, seconds.             |
| `STOREGUARD_SECURE_COOKIES`  | `false`                          | Set `true` behind HTTPS.               |

## Web UI

`GET /` serves a single-page UI (static assets under [`static/`](static)): it
calls `/api/me` on load, shows login/signup when logged out, and the tenant
dashboard (team roster + role controls) when logged in. Owners get the
add-member / change-role / remove controls; staff see a read-only roster.

## API

| Method | Path                          | Auth    | Purpose                                     |
| ------ | ----------------------------- | ------- | ------------------------------------------- |
| GET    | `/api/health`                 | —       | Liveness.                                   |
| POST   | `/api/auth/signup`            | —       | Create org + owner user, log in.            |
| POST   | `/api/auth/login`             | —       | Log in (starts session on first tenant).    |
| POST   | `/api/auth/logout`            | —       | Clear session.                              |
| POST   | `/api/auth/change-password`   | session | Change your own password.                   |
| GET    | `/api/me`                     | session | Current user + active tenant + role.        |
| GET    | `/api/org/members`            | session | Members of the caller's tenant only.        |
| POST   | `/api/org/members`            | owner   | Add a new user to the tenant.               |
| PATCH  | `/api/org/members/{user_id}`  | owner   | Change a member's role (last-owner guard).  |
| DELETE | `/api/org/members/{user_id}`  | owner   | Remove a member (last-owner guard).         |
| GET    | `/api/cameras`                | member  | List the tenant's cameras (+ zones).        |
| POST   | `/api/cameras`                | owner   | Create a camera (optionally with zones).    |
| GET    | `/api/cameras/{id}`           | member  | One camera (+ zones).                       |
| PATCH  | `/api/cameras/{id}`           | owner   | Update camera fields.                       |
| DELETE | `/api/cameras/{id}`           | owner   | Delete a camera (and its zones).            |
| PUT    | `/api/cameras/{id}/zones`     | owner   | Replace the camera's full zone set.         |
| GET    | `/api/agent-keys`             | member  | List edge-agent tokens (prefix only).       |
| POST   | `/api/agent-keys`             | owner   | Mint a token (plaintext returned once).     |
| DELETE | `/api/agent-keys/{id}`        | owner   | Revoke a token.                             |
| GET    | `/api/events`                 | member  | Recent events; `?person_id=` for a trail.   |
| GET    | `/api/events/{id}/clip`       | member  | Download an event's alarm clip.             |
| GET    | `/api/settings/telegram`      | owner   | Telegram config (token never returned).     |
| PUT    | `/api/settings/telegram`      | owner   | Set bot token / chat id / enabled.          |
| POST   | `/api/settings/telegram/test` | owner   | Send a test message to the bot.             |
| GET    | `/api/agent/config`           | agent   | Edge pulls its cameras + zones + sources.   |
| POST   | `/api/agent/heartbeat`        | agent   | Liveness (stamps last-seen).                |
| POST   | `/api/agent/events`           | agent   | Push a detection event (metadata).          |
| POST   | `/api/agent/events/{id}/clip` | agent   | Attach a short alarm clip to an event.      |

**Auth column:** *member* = any logged-in user of the tenant, *owner* = owner
role, *agent* = an edge-agent token via `Authorization: Bearer <token>`.

## Edge agent

The store PC runs the edge agent, which talks to the cloud with a token minted
in the cabinet. A thin HTTP client ([agent_client.py](agent_client.py)) wraps
the calls; two CLI modes:

```bash
# Check mode — verify enrollment and print the pulled config (no detection):
storeguard agent --server https://cloud.example.com --key sga_… --test-event

# Detection mode — pull cameras, run the tracker + scenarios, push events + clips:
storeguard agent --server https://cloud.example.com --key sga_… --run
```

`--run` ([edge_agent.py](../edge_agent.py)) enrolls, pulls this tenant's cameras
+ zones, drives the per-camera pipeline ([runner.py](../runner.py) + `scenarios/`
via a cloud-pushing `CloudAlertSink`), and sends each scenario event — with a
short evidence clip — up through the agent API. Only events + clips leave the
store, never the raw video.

## Migrations

Alembic lives in [`migrations/`](migrations); the DB URL is read from settings
(not hard-coded in `alembic.ini`). To change the schema: edit
[`models.py`](models.py), then:

```bash
STOREGUARD_DATABASE_URL="sqlite:///./dev.db" uv run --extra cloud alembic revision --autogenerate -m "describe change"
STOREGUARD_DATABASE_URL="sqlite:///./dev.db" uv run --extra cloud alembic upgrade head
```

## Tests

```bash
uv run --extra cloud pytest tests/test_cloud_auth.py -q
```

Covers signup/login/logout, hashing, validation, **cross-tenant isolation**,
and that the real Alembic migrations build the schema.
