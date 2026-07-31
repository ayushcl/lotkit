# LotKit deployment notes

## Controlled-beta status

Phase 5b adds invite-only photographer authentication. This implementation
does not deploy the application, and the committed `render.yaml` keeps
automatic deploys disabled. Treat deployment as a separate reviewed
operation.

There is no public signup, automated password reset, email verification,
OAuth, or JWT authentication. Owner login throttling is persistent in SQLite
and keyed only by a SHA-256 derivative of the canonical client IP. It is not a
process-local limiter and never locks an email address or account.

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
| `LOTKIT_PUBLIC_BASE_URL` | Conditional | Explicit canonical HTTPS origin override; required in non-Render production |
| `LOTKIT_TRUSTED_HOSTS` | Conditional | Explicit comma-separated hostname override; required in non-Render production |
| `LOTKIT_FORWARDED_ALLOW_IPS` | Yes | Reviewed direct proxy IPs/networks Uvicorn may trust; never use a production wildcard |
| `RENDER` | Supplied by Render | Exact platform indicator value `true` |
| `RENDER_EXTERNAL_URL` | Supplied by Render | Fallback canonical public URL when the LotKit override is empty |
| `RENDER_EXTERNAL_HOSTNAME` | Supplied by Render | Fallback trusted hostname when the LotKit override is empty |
| `LOTKIT_ARTIFACT_RETENTION_DAYS` | Optional | Positive integer from 1 through 3650; defaults to `30` |
| `PORT` | Supplied by Render | Platform-assigned port |
| `LOTKIT_DOCS_ENABLED` | Optional | Leave unset to disable production docs |

`LOTKIT_PUBLIC_BASE_URL` builds delivery URLs and is the exact expected
`Origin` for owner login and unsafe owner requests. Explicit non-empty LotKit
URL and trusted-host values always win. When `RENDER` is exactly `true` and an
override is empty, LotKit uses only Render's official `RENDER_EXTERNAL_URL`
and `RENDER_EXTERNAL_HOSTNAME`. The Blueprint therefore does not prompt for
the two overrides. Production outside Render continues to require explicit
valid LotKit values. Missing or malformed production fallback values stop
startup; incoming `Host` or forwarding headers never fill configuration.

Passwords, session values, CSRF values, and delivery secrets must never appear
in `render.yaml`, image arguments, or logs.

Render terminates HTTPS at its edge. Configure
`LOTKIT_FORWARDED_ALLOW_IPS` in the Render Dashboard with only the verified
direct proxy peers or networks. Uvicorn applies that trust decision before
the application sees the request; the login throttle then uses only
`request.client.host`. Application code deliberately does not read
`X-Forwarded-For`, `Forwarded`, `X-Real-IP`, or provider-specific equivalents.
The container default is loopback-only (`127.0.0.1`), not `*`. Public URLs
still come only from `LOTKIT_PUBLIC_BASE_URL`, not the incoming `Host` header.
Uvicorn access logs remain enabled. Owner-authentication application logs stay
generic and never include email, passwords, request bodies, cookies, session
or CSRF credentials, throttle keys, or forwarding-header contents.

Keep the initial deployment private. Before inviting pilot users, run a live
spoof test from outside the trusted proxy boundary: changing a supplied
forwarding header must not change the observed throttle identity. Also confirm
requests through Render resolve to the real client identity rather than one
shared proxy IP. A wrong boundary can either let callers rotate spoofed IPs or
make unrelated users share a bucket. `LOTKIT_FORWARDED_ALLOW_IPS` is a
separate manual security boundary and must never be `*` in production.

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
│       ├── run_report.json
│       ├── verified current artifacts
│       └── immutable active delivery snapshots
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

The Blueprint's current `sizeGB: 5` is a pilot placeholder, not a
production capacity recommendation. Select production capacity only after
representative full vehicle shoots have been measured after ZIP verification,
loose-photo removal, snapshot cleanup, and retention processing.

## Storage lifecycle and explicit maintenance

Photo packaging stages validated renamed photographs, creates the ZIP, then
reopens the completed archive. LotKit requires a successful CRC check, the
exact expected member-name sequence and count, no duplicates, and no absolute,
parent-traversal, or directory members. Only after those checks pass are the
redundant loose staged photographs removed. Images are not resized,
recompressed, rotated, or otherwise changed; the bytes extracted from the ZIP
match the accepted uploads.

SQLite's `storage_cleanup_jobs` table stores exact paths relative to
`LOTKIT_DATA_DIR/runs`. A partial unique index permits only one pending job per
path. Jobs reject absolute paths, traversal, empty components, symlinks,
directories, non-UUID Run directories, and paths outside the Runs root.
Deletion revalidates current output references and active delivery manifests,
then uses non-following directory descriptors. Missing files complete
successfully. Failures retain a bounded error and attempt count for retry.

Cleanup is queued in the same SQLite transaction that:

- replaces a current photo ZIP, sticker, or Buyers Guide reference;
- revokes a delivery link because it was replaced, manually withdrawn,
  reopened, changed, transport-migrated, or explicitly expired; or
- retires eligible delivered Run artifacts.

The filesystem attempt happens only after commit. Delivery rows and immutable
manifests remain as audit history after their snapshot files are gone.
Historical Run rows and `run_report.json` also remain after retention.

`LOTKIT_ARTIFACT_RETENTION_DAYS` defaults to 30. A Run is eligible only when it
is `delivered`, its latest non-null `first_download_started_utc` is at least
that old, it has no unrevoked/unexpired link, and it still has a current
artifact reference. Retention removes those references transactionally,
records `artifacts_purged_utc`, and queues only their exact files. It never
purges `in_progress` or `ready` Runs.

Photographers are responsible for retaining original camera files.
Photographs removed by LotKit cannot be recovered from LotKit. Expired PDFs
and other generated documents are not guaranteed to regenerate identically.

Run maintenance explicitly as the same non-root application user and with the
same environment:

```bash
python -m api.cleanup_storage report
python -m api.cleanup_storage plan
python -m api.cleanup_storage apply
```

`report` measures the Run root, referenced artifacts, active and inactive
snapshots, pending work, loose photos, unmanaged data, legacy data, and the
largest current Runs. `plan` uses the same selection logic as `apply` but
makes no filesystem or database changes. Running the module with no
subcommand only prints help.

There is no in-process scheduler, cron configuration, or background worker in
this phase. A future deployment should invoke `python -m api.cleanup_storage
apply` explicitly from a reviewed platform job, after first reviewing
`report` and `plan`. The command keeps individual unlink failures pending and
returns a failure status only for a real command or database failure.

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

`POST /api/auth/login` permits seven failed credential checks per canonical
client IP in a 15-minute window. Failure eight starts a 15-minute block and
returns the generic `429` body `{"detail":"Too many login attempts. Try again
later."}` with a numeric, rounded-up `Retry-After` of at least one second.
Blocked requests are rejected before Argon2. A successful login before the
threshold clears the IP bucket; expired partial windows and expired blocks
restart cleanly. Stale rows are removed opportunistically.

The bucket key is a deterministic SHA-256 value derived from Python's
canonical IPv4/IPv6 representation. No raw IP or key is logged. If ASGI peer
data is missing or malformed, requests use one deterministic shared
unavailable-peer bucket; this conservative fallback cannot be used to create
unbounded identities. The route still requires the exact Origin and remains
exempt from session-bound CSRF exactly as before. Phase 5b.1 does not throttle
the separate delivery fragment-secret exchange under `/d/*`.

Owner authentication is never applied to `/d/*`; recipient routes continue
using the separate fragment-secret and delivery-session architecture.

## Local production-container verification

Build:

```bash
docker build --tag lotkit:phase5b1 .
```

Start with isolated storage:

```bash
LOTKIT_DOCKER_DATA="$(mktemp -d)"
chmod 770 "$LOTKIT_DOCKER_DATA"

docker run --detach \
  --name lotkit-phase5b1 \
  --group-add "$(id -g)" \
  --publish 8000:8000 \
  --env LOTKIT_ENV=production \
  --env LOTKIT_DATA_DIR=/var/data \
  --env RENDER=true \
  --env RENDER_EXTERNAL_URL=https://lotkit.invalid \
  --env RENDER_EXTERNAL_HOSTNAME=lotkit.invalid \
  --env LOTKIT_FORWARDED_ALLOW_IPS=127.0.0.1 \
  --env PORT=8000 \
  --mount "type=bind,src=$LOTKIT_DOCKER_DATA,dst=/var/data" \
  lotkit:phase5b1
```

Verify:

```bash
curl --fail --header 'Host: lotkit.invalid' http://localhost:8000/health
curl --fail --header 'Host: lotkit.invalid' http://localhost:8000/ready
curl --fail --header 'Host: lotkit.invalid' http://localhost:8000/
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header 'Host: lotkit.invalid' \
  http://localhost:8000/api/runs)" = 401
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header 'Host: lotkit.invalid' \
  http://localhost:8000/docs)" = 404
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header 'Host: lotkit.invalid' \
  http://localhost:8000/openapi.json)" = 404
docker exec lotkit-phase5b1 id
docker exec lotkit-phase5b1 python -m api.cleanup_storage report
docker exec lotkit-phase5b1 python -m api.cleanup_storage plan
docker logs lotkit-phase5b1
```

`id` must report UID/GID `10001`. The root page must contain the login-capable
UI. Logs must contain no password, raw photographer session value, raw CSRF
value, or delivery credential. Public recipient routes should still return
their generic bootstrap/unavailable behavior without an owner session.

Stop and remove the disposable container:

```bash
docker stop lotkit-phase5b1
docker rm lotkit-phase5b1
```

Use an authenticated browser and disposable records for persistence checks.
Never mount the repository root or development database into this test.

## Backups and scaling

Before wider use, document and rehearse a database-consistent SQLite backup;
a filesystem snapshot alone is not that procedure. After beta, move
relational state to managed Postgres and files to object storage before
running multiple stateless instances.

Unknown orphan files are intentionally not inferred from filename patterns or
deleted. Use the report to investigate them. Pre-UUID timestamp-named
directories remain legacy data and must be handled only through a separately
reviewed migration or backup process.

Render references:

- [Blueprint YAML reference](https://render.com/docs/blueprint-spec)
- [Persistent disk behavior and limitations](https://render.com/docs/disks)
- [Docker services on Render](https://render.com/docs/docker)
- [Health checks](https://render.com/docs/health-checks)
