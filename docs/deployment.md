# LotKit deployment notes

## Deployment is blocked

Do not deploy this build publicly.

Two separate blockers remain:

1. Phase 5b must replace the implicit local-owner dependency with real
   authentication before any public deployment.
2. The current public delivery URL contains its raw bearer credential in
   the path: `/d/{token}`. Render's platform-edge HTTP request logs can record
   the requested URL before traffic reaches LotKit, so the application's
   Uvicorn path redaction cannot protect that platform log. This is a hard
   deployment blocker for Phase 5a.1.

Phase 5a.1 must move the raw credential out of both the URL path and query
string before Phase 5c deployment. The intended design is a fragment-based
share URL. Browser code will deliberately exchange the fragment token in a
POST body or authorization header, then remove the fragment from the browser
URL. That transport redesign is deliberately not implemented in Phase 5a.
The existing application-level Uvicorn redaction remains enabled and useful
for local and application logs; it is not a defense against Render's
platform-edge request logging.

The committed `render.yaml` has automatic deploys disabled. Do not apply the
Blueprint until both blockers above are closed.

## Production configuration

Set these environment variables:

| Variable | Required | Secret? | Production value |
| --- | --- | --- | --- |
| `LOTKIT_ENV` | Yes | No | `production` |
| `LOTKIT_DATA_DIR` | Yes | No | `/var/data` on Render |
| `LOTKIT_PUBLIC_BASE_URL` | Yes | No | The canonical public HTTPS origin, with no path or trailing slash |
| `LOTKIT_TRUSTED_HOSTS` | Yes | No | Comma-separated hostnames accepted by the service; include the Render/custom hostname, without schemes or paths |
| `PORT` | Supplied by Render | No | The platform-assigned HTTP port |
| `LOTKIT_DOCS_ENABLED` | Optional | No | Leave unset to keep production API docs disabled; enable only for a deliberate, controlled diagnostic |

`LOTKIT_PUBLIC_BASE_URL` and `LOTKIT_TRUSTED_HOSTS` use `sync: false` in the
Blueprint because they are deployment-specific and must be entered in the
Render Dashboard. They are configuration rather than credentials. No
session, Stripe, delivery, or other secret value belongs in `render.yaml` or
an image build argument. Future authentication secrets introduced in Phase
5b must be Dashboard-managed secrets and must never be logged.

Render terminates HTTPS at its edge and forwards the original request scheme
to the container. The production Uvicorn command enables proxy-header
handling for that trusted platform boundary. Canonical public delivery URLs
are built from `LOTKIT_PUBLIC_BASE_URL`, not an incoming `Host` header.

`requirements.txt` remains the developer/CI dependency source and includes
pytest. `requirements-runtime.txt` mirrors its application dependencies and
declared versions while excluding only pytest, so the production image does
not install test tooling. `python-dotenv` remains in the runtime set because
the application imports it during startup; the image still contains no
`.env` file.

## Persistent disk and initial startup

Attach exactly one persistent disk at `/var/data`. The application keeps all
durable state under that mount:

```text
/var/data/
├── lotkit.db
├── runs/
│   └── <run-id>/
│       ├── processed photos and photo ZIPs
│       ├── generated sticker and draft Buyers Guide PDFs
│       └── immutable delivery-manifest copies
└── storage/
    └── logos/
        └── saved dealership logos
```

On an empty disk, application startup creates the required directories,
initialises the SQLite schema, and seeds the temporary implicit owner used by
the current Phase 4 workflow. It does not create a real authenticated
account. Phase 5b will introduce the first-account flow and replace only the
current-owner dependency; it will not require changing profile ownership or
CRUD schema.

The Blueprint intentionally omits `plan`. Before any future deployment,
select and verify a paid web-service instance in the Render Dashboard.
Persistent disks are unavailable to free web services. Keep `numInstances`
at `1`: this SQLite-and-disk beta cannot scale horizontally.

The configured endpoints are:

- `GET /health` — lightweight process liveness, also used by the container
  and Render Blueprint health checks.
- `GET /ready` — storage and database readiness; use this for an operator
  check after startup.

## Local container verification

Build the production image:

```bash
docker build --tag lotkit:phase5a .
```

Create an isolated data directory and start the container:

```bash
LOTKIT_DOCKER_DATA="$(mktemp -d)"
chmod 770 "$LOTKIT_DOCKER_DATA"

docker run --detach \
  --name lotkit-phase5a \
  --group-add "$(id -g)" \
  --publish 8000:8000 \
  --env LOTKIT_ENV=production \
  --env LOTKIT_DATA_DIR=/var/data \
  --env LOTKIT_PUBLIC_BASE_URL=https://lotkit.invalid \
  --env LOTKIT_TRUSTED_HOSTS=lotkit.invalid,localhost,127.0.0.1 \
  --env PORT=8000 \
  --mount "type=bind,src=$LOTKIT_DOCKER_DATA,dst=/var/data" \
  lotkit:phase5a
```

The supplemental host group grants the non-root container user access to the
temporary `0770` bind mount without making it world-writable.

Verify the application and container identity:

```bash
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/ready
curl --fail http://localhost:8000/
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  http://localhost:8000/docs)" = 404
docker exec lotkit-phase5a id
find "$LOTKIT_DOCKER_DATA" -maxdepth 4 -print
```

The docs request should return `404`, and `id` should report UID/GID `10001`
rather than root.

To verify persistence, create a disposable dealership through the normal API,
restart with the same mount, and list it:

```bash
curl --fail --request POST http://localhost:8000/api/dealerships \
  --form nickname=restart-check

docker stop lotkit-phase5a
docker rm lotkit-phase5a

docker run --detach \
  --name lotkit-phase5a \
  --group-add "$(id -g)" \
  --publish 8000:8000 \
  --env LOTKIT_ENV=production \
  --env LOTKIT_DATA_DIR=/var/data \
  --env LOTKIT_PUBLIC_BASE_URL=https://lotkit.invalid \
  --env LOTKIT_TRUSTED_HOSTS=lotkit.invalid,localhost,127.0.0.1 \
  --env PORT=8000 \
  --mount "type=bind,src=$LOTKIT_DOCKER_DATA,dst=/var/data" \
  lotkit:phase5a

curl --fail http://localhost:8000/api/dealerships
```

Use a disposable Run through the normal UI/API for the equivalent artifact
restart check. Confirm its folder remains under
`$LOTKIT_DOCKER_DATA/runs/` after the restart. Never mount the repository
root or the development database into this production check.

Stop and remove the test container when finished:

```bash
docker stop lotkit-phase5a
docker rm lotkit-phase5a
```

The temporary data directory can then be removed after its contents have
been inspected.

## Logs and backups

Inspect application logs with:

```bash
docker logs lotkit-phase5a
```

Do not paste or export request-log lines that might contain delivery URLs.
The application-level Uvicorn filter redacts current `/d/{token}` paths, but
always inspect output before sharing it. Render edge request logs remain
unsafe for the current raw-token URL shape, as described in the deployment
blocker above.

Render disk snapshots are useful for operational recovery. Before any wider
launch, add and rehearse a database-consistent SQLite backup/export
procedure; a filesystem snapshot alone is not the documented database
backup process.

This beta is intentionally limited to one application instance. A
disk-backed redeploy has brief downtime, and the persistent disk is not a
horizontal-scaling architecture. After beta, migrate relational state to
managed Postgres and files to object storage so multiple stateless
application instances can be used.

Render references used for this configuration:

- [Blueprint YAML reference](https://render.com/docs/blueprint-spec)
- [Persistent disk behavior and limitations](https://render.com/docs/disks)
- [Docker services on Render](https://render.com/docs/docker)
- [Health checks](https://render.com/docs/health-checks)
