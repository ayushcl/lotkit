# LotKit deployment notes

## Controlled-beta status

Phase 5b adds invite-only photographer authentication. This implementation
does not deploy the application, and the committed `render.yaml` keeps
automatic deploys disabled. Treat deployment as a separate reviewed
operation.

There is no public signup, automated password reset, email verification,
OAuth, or JWT authentication. Before public launch, add robust persistent
login throttling. Do not substitute a process-local in-memory limiter or a
fixed per-account lockout: either design is unsuitable for this recovery
model.

Recipient share URLs retain the Phase 5a.1 design:

```text
https://example.com/d/{public_id}#{delivery_secret}
```

The fragment secret is not sent in an HTTP request path. The browser exchanges
it in a bounded same-origin POST body for a short-lived
`lotkit_delivery_session` HttpOnly cookie, then removes the fragment. Verify
that the deployment platform does not independently record request bodies or
cookies.

## Production configuration

Set these environment variables:

| Variable | Required | Production value |
| --- | --- | --- |
| `LOTKIT_ENV` | Yes | `production` |
| `LOTKIT_DATA_DIR` | Yes | An absolute persistent path, `/var/data` on Render |
| `LOTKIT_PUBLIC_BASE_URL` | Yes | Canonical public HTTPS origin, with no path or trailing slash |
| `LOTKIT_TRUSTED_HOSTS` | Yes | Comma-separated accepted hostnames, without schemes or paths |
| `PORT` | Supplied by Render | Platform-assigned port |
| `LOTKIT_DOCS_ENABLED` | Optional | Leave unset to disable production docs |

`LOTKIT_PUBLIC_BASE_URL` builds delivery URLs and is the exact expected
`Origin` for owner login and unsafe owner requests. It and
`LOTKIT_TRUSTED_HOSTS` use `sync: false` in the Blueprint because they are
deployment-specific. Passwords, session values, CSRF values, and delivery
secrets must never appear in `render.yaml`, image arguments, or logs.

Render terminates HTTPS at its edge. The production Uvicorn command trusts
that proxy boundary, while public URLs come only from
`LOTKIT_PUBLIC_BASE_URL`, not the incoming `Host` header.

The runtime image installs `requirements-runtime.txt`; its application
versions stay aligned with `requirements.txt` while pytest remains
development-only.

## Persistent disk and first account

Attach exactly one persistent disk at `/var/data`. Durable state remains:

```text
/var/data/
├── lotkit.db
├── runs/
│   └── <run-id>/
│       ├── generated artifacts
│       └── immutable delivery-manifest copies
└── storage/
    └── logos/
```

Startup creates directories and migrates the SQLite schema, but never creates
or falls back to an account.

For a new empty database, create an invite-only account from a controlled
shell:

```bash
python -m api.manage_users create
```

For an existing Phase 5a database, claim `owner@local` in place:

```bash
python -m api.manage_users claim-default
```

The command prompts for the real email, display name, password, and password
confirmation. It retains the original primary-key ID, preserving ownership of
all dealerships, Runs, and delivery links. Until claimed, `owner@local` has
no password and cannot authenticate.

Administrative commands are:

```bash
python -m api.manage_users list
python -m api.manage_users set-password
python -m api.manage_users disable
python -m api.manage_users enable
```

Account arguments can be supplied with `--email`, and create/claim also accept
`--display-name`. Passwords never have a command-line option; hidden
interactive prompts request them twice.

`set-password` is the controlled-beta password-recovery procedure. Password
changes and disabling revoke all sessions for that user. Enabling does not
invent or change a password.

The SQLite-and-disk beta must run as one application instance. Persistent
disks are unavailable to free Render web services, so select an appropriate
paid instance before any deployment.

## Photographer session and CSRF architecture

Photographer sessions have a fixed 12-hour absolute lifetime. Authenticated
activity updates `last_seen_utc` without extending `expires_utc`. Multiple
sessions can exist deliberately; logout revokes only the current session.

| Cookie | HttpOnly | Secure | SameSite | Path | Max-Age |
| --- | --- | --- | --- | --- | --- |
| `lotkit_owner_session` | Yes | Production only | Strict | `/api` | 43200 |
| `lotkit_owner_csrf` | No | Production only | Strict | `/` | 43200 |

Both credentials use cryptographic randomness. SQLite stores only SHA-256
hashes. Every authenticated `POST`, `PUT`, `PATCH`, and `DELETE` requires the
CSRF cookie value in `X-CSRF-Token` and an `Origin` exactly equal to
`LOTKIT_PUBLIC_BASE_URL`. Login has no session-bound token yet, so it still
requires the exact Origin. Authentication responses use `Cache-Control:
no-store`.

Owner authentication is never applied to `/d/*`; recipient routes continue
using the separate fragment-secret and delivery-session architecture.

## Local production-container verification

Build:

```bash
docker build --tag lotkit:phase5b .
```

Start with isolated storage:

```bash
LOTKIT_DOCKER_DATA="$(mktemp -d)"
chmod 770 "$LOTKIT_DOCKER_DATA"

docker run --detach \
  --name lotkit-phase5b \
  --group-add "$(id -g)" \
  --publish 8000:8000 \
  --env LOTKIT_ENV=production \
  --env LOTKIT_DATA_DIR=/var/data \
  --env LOTKIT_PUBLIC_BASE_URL=https://lotkit.invalid \
  --env LOTKIT_TRUSTED_HOSTS=lotkit.invalid,localhost,127.0.0.1 \
  --env PORT=8000 \
  --mount "type=bind,src=$LOTKIT_DOCKER_DATA,dst=/var/data" \
  lotkit:phase5b
```

Verify:

```bash
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/ready
curl --fail http://localhost:8000/
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  http://localhost:8000/api/runs)" = 401
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  http://localhost:8000/docs)" = 404
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  http://localhost:8000/openapi.json)" = 404
docker exec lotkit-phase5b id
docker logs lotkit-phase5b
```

`id` must report UID/GID `10001`. The root page must contain the login-capable
UI. Logs must contain no password, raw photographer session value, raw CSRF
value, or delivery credential. Public recipient routes should still return
their generic bootstrap/unavailable behavior without an owner session.

Stop and remove the disposable container:

```bash
docker stop lotkit-phase5b
docker rm lotkit-phase5b
```

Use an authenticated browser and disposable records for persistence checks.
Never mount the repository root or development database into this test.

## Backups, scaling, and deferred storage work

Before wider use, document and rehearse a database-consistent SQLite backup;
a filesystem snapshot alone is not that procedure. After beta, move
relational state to managed Postgres and files to object storage before
running multiple stateless instances.

Phase 5b deliberately does not change loose-photo duplication, old re-package
ZIPs, delivery snapshot cleanup, orphan cleanup, retention, resizing, or
recompression. Those remain Phase 5c.0 work.

Render references:

- [Blueprint YAML reference](https://render.com/docs/blueprint-spec)
- [Persistent disk behavior and limitations](https://render.com/docs/disks)
- [Docker services on Render](https://render.com/docs/docker)
- [Health checks](https://render.com/docs/health-checks)
