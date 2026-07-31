# LotKit

LotKit is an invite-only workspace for independent dealership photographers:
VIN decoding, photo packaging, saved Runs, window stickers, draft Buyers
Guides, and controlled recipient delivery links.

## Setup

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 -m uvicorn api.main:app --reload
```

Existing Phase 5a databases keep their original `owner@local` primary key and
all owned dealerships, Runs, and delivery links. Claim that account once,
using interactive prompts so the password never enters shell history:

```bash
python -m api.manage_users claim-default
```

For a new empty database, create the first invite-only photographer:

```bash
python -m api.manage_users create
```

Other account operations are:

```bash
python -m api.manage_users list
python -m api.manage_users set-password
python -m api.manage_users disable
python -m api.manage_users enable
```

Each command that needs an account can also receive `--email`;
`claim-default` and `create` also accept `--display-name`. Passwords are
always entered twice through a hidden `getpass` prompt and stored only as
Argon2 hashes.

Run the tests:

```bash
python3 -m pytest
```

## Storage lifecycle

Photo packaging writes validated renamed files into a temporary Run directory,
creates the ZIP, reopens it, runs the ZIP CRC check, and verifies the exact
member names, count, uniqueness, and path safety. Only then does LotKit remove
the redundant loose copies. A successful current UUID Run therefore retains
the verified ZIP and `run_report.json`; source image bytes inside the ZIP are
unchanged.

Superseded Run outputs and unusable delivery snapshots enter an exact
database-backed cleanup queue. Jobs contain only validated paths relative to
the Runs root, never arbitrary or absolute paths. Cleanup is retryable and a
filesystem failure does not roll back an already committed output,
revocation, expiry, or retention decision. Unknown files and pre-UUID legacy
directories are reported but never automatically deleted.

Delivered Run artifacts default to 30 days of retention, measured from the
latest recorded delivery download start. A Run is eligible only after that
cutoff and when it has no active delivery link. Purging removes current
artifact references and files while preserving the Run, vehicle, dealership,
photo-order, lifecycle, and report metadata. Set
`LOTKIT_ARTIFACT_RETENTION_DAYS` to a positive integer from 1 through 3650 to
change the policy.

Storage work is explicit:

```bash
python -m api.cleanup_storage report
python -m api.cleanup_storage plan
python -m api.cleanup_storage apply
```

`report` and `plan` are read-only. `apply` expires links, retires eligible
current artifacts, reconciles already-revoked delivery manifests, and retries
pending exact-path jobs. There is no automatic scheduler yet; an operator or
future platform job must invoke `apply`.

Photographers must retain the original camera files. Photographs deleted by
LotKit are not recoverable from LotKit, and expired generated documents are
not guaranteed to regenerate identically.

## Authentication

Owner authentication and public delivery authentication are intentionally
separate:

- Photographers use invite-only local accounts. There is no public signup,
  email verification, OAuth, automated password reset, or JWT.
- `lotkit_owner_session` is an HttpOnly, SameSite=Strict cookie scoped to
  `/api`. `lotkit_owner_csrf` is a JavaScript-readable, SameSite=Strict cookie
  scoped to `/`. Both are Secure in production and expire after a fixed
  12-hour absolute lifetime.
- SQLite stores only SHA-256 hashes of the random owner session and CSRF
  credentials. Unsafe owner API requests require the session-bound
  `X-CSRF-Token` plus an exact `Origin` match.
- Owner login failures are throttled persistently by client IP: attempts one
  through seven in a 15-minute window retain the generic `401`; failure eight
  starts a 15-minute block and returns `429` with `Retry-After`. SQLite stores
  only a deterministic SHA-256 bucket key, never the raw IP, and a successful
  pre-threshold login clears that IP's failures. This is not an account or
  email lockout.
- Recipient links continue to use the separate `lotkit_delivery_session`
  cookie and fragment-secret exchange. Owner CSRF/authentication is never
  applied to `/d/*`.

For controlled-beta password recovery, an operator runs
`python -m api.manage_users set-password`. Changing a password or disabling
an account revokes all of that account's existing sessions. The login throttle
is SQLite-backed rather than process-local, and intentionally does not apply
to delivery-link secret exchange.

The application derives its throttle identity only from
`request.client.host`, after Uvicorn has applied its proxy-trust rules; it does
not parse forwarding headers. The container trusts loopback by default. Set
`LOTKIT_FORWARDED_ALLOW_IPS` to the reviewed direct proxy peers or networks at
deployment time—never a production wildcard—and perform a forwarding-header
spoof test before inviting pilot users. See [deployment notes](docs/deployment.md)
for the trust-boundary checklist.
