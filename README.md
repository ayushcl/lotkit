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
- Recipient links continue to use the separate `lotkit_delivery_session`
  cookie and fragment-secret exchange. Owner CSRF/authentication is never
  applied to `/d/*`.

For controlled-beta password recovery, an operator runs
`python -m api.manage_users set-password`. Changing a password or disabling
an account revokes all of that account's existing sessions. Robust persistent
login throttling remains required before a public launch; LotKit deliberately
does not use a process-local limiter or fixed account lockout.
