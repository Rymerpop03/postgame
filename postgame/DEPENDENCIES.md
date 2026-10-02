# Dependency allowlist

Decided in Phase 0, 2026-08-04. See [BACKEND-PLAN.md](BACKEND-PLAN.md) §0.1.

Supply chain is in the threat model, so this file is a control, not documentation.
**Nothing outside this list gets installed.** Adding an entry requires a one-line
justification here in the same commit that adds it to the lockfile.

Platform: **Python 3.12**, **PostgreSQL 16**.
Postgres extensions: `citext` only.

The frontend keeps **zero** dependencies. That does not change.

---

## Runtime

| Package | Why |
|---|---|
| `fastapi` | The framework. Brings `starlette` and `pydantic` transitively. |
| `uvicorn[standard]` | ASGI server, behind a reverse proxy in production. |
| `pydantic-settings` | Environment config with validation — this is what makes "refuse to boot without a secret" enforceable rather than hoped for. |
| `psycopg[binary,pool]` | Postgres 3 driver. Native connection pooling, so no separate pool package. |
| `sqlalchemy` | **Core only — the ORM layer is not used.** See rejections below. Alembic requires it regardless. |
| `alembic` | Reversible migrations. |
| `argon2-cffi` | Password hashing. Binding to the reference Argon2 implementation. |
| `python-multipart` | Multipart parsing. Required by FastAPI for the Phase 11 avatar upload. |
| `Pillow` | Image decode and re-encode for avatars (Phase 11) and covers (Phase 12). |
| `httpx` | Outbound HTTP: cover resolver, email provider API. |
| `structlog` | Structured JSON logging, and the host for the secret-scrubbing processor. |

Two of these deserve a note.

**`Pillow` is the highest-risk dependency in the project.** It parses untrusted bytes from
strangers, which is exactly the shape that produces memory-corruption CVEs. It gets
priority patching, `Image.MAX_IMAGE_PIXELS` set explicitly, and dimension checks *before*
decode — see Phase 11.

**`httpx` was chosen over `requests` for a security reason**, not preference: `httpx`
defaults `follow_redirects` to `False`, which is the SSRF-safe default. `requests` follows
redirects unless told not to, so the safe behaviour depends on remembering a keyword
argument at every call site. Phase 12's resolver fetches URLs derived from third-party API
responses, so this matters.

## Development and CI

| Package | Why |
|---|---|
| `pytest`, `pytest-asyncio` | Tests. |
| `mypy` | `--strict` over `api/`. |
| `ruff` | Lint *and* format — one dependency instead of `black` + `flake8` + `isort`. |
| `pip-audit` | Dependency CVE scan, blocking in CI. |
| `hypothesis` | Approved, not yet required. Earmarked for the moderation normalizer (Phase 9) and the pagination cursor codec (Phase 3) — both are pure functions with adversarial inputs, which is where property testing actually pays. |

CI binaries, not Python packages: `gitleaks` (secret scanning, Phase 1), OWASP ZAP
(Phase 16 scan only).

---

## Rejected, and why

The reasoning matters more than the list — these are the things someone will reach for.

**Redis** — not in v1. Rate limits go in Postgres as an `UPSERT` token bucket; background
jobs (email, exports, cover resolution) go in a `jobs` table with a worker loop. One fewer
service to run, secure, patch, monitor, and back up. Revisit only if measured contention
demands it, not preemptively.

**The SQLAlchemy ORM layer** — Core only. Lazy loading makes it hard to see which query
actually runs, and the ORM's natural style is fetch-the-object-then-check-permission. That
is precisely the IDOR pattern Phase 10 is built to make impossible. Explicit parameterized
Core queries keep "the actor id is in the `WHERE` clause" verifiable by reading the code.

**`slowapi`** — a thin wrapper that does not cover what we need. Phase 5 wants per-`(ip,
email)` and per-user buckets on different windows, plus a much tighter bucket on
conversation creation in Phase 10. Clearer written directly against the token-bucket table.

**`passlib`** — no release since 1.7.4 in October 2020, and it adds an abstraction layer
over Argon2 we have no use for. `argon2-cffi` directly means fewer layers between our code
and the KDF, and the parameters we tune in Phase 5 are visible rather than configured
through a wrapper.

**`pyjwt` / `python-jose`** — not needed. Decision 0.4 chose opaque server-side sessions,
so there is no token to sign or verify.

**`celery`** — overkill for a handful of background jobs, and it wants a broker, which
means Redis, which we just rejected.

**An email SDK** — the provider's HTTP API over `httpx`. A vendor SDK for what is one POST
request is a dependency we would be adding for autocomplete.

**A CAPTCHA service** — rejected for v1 on two grounds: a third-party script directly
contradicts the strict CSP that Phase 12 works toward, and it leaks every visitor to
another company. Anti-abuse comes from throttling (Phase 5), required email verification
(Phase 6), and rate-limited account creation. Reconsider only if real abuse appears, and
prefer a self-hosted proof-of-work challenge over a hosted CAPTCHA.

---

## Phase 5 added nothing

Worth recording, because identity is the phase where dependencies usually arrive. Everything
Phase 5 needed was already on the list or already in the standard library:

| Need | What it used | What it avoided |
|---|---|---|
| Password hashing | `argon2-cffi` (already listed) | `passlib`, rejected above |
| Session tokens | `secrets`, `hashlib`, `base64` | any token library |
| CSRF tokens | `hmac` — the token is `HMAC(secret, "csrf:" \|\| session_token)`, so it needs no storage and no column | `fastapi-csrf-protect`, `itsdangerous` |
| Email validation | one regex in `app/security/emails.py` | `pydantic[email]`, which brings `dnspython` — a DNS client in the signup path, for a check that proves nothing, since a domain that resolves may still bounce. The real verification is Phase 6: send a message and see whether anyone reads it |
| Rate limiting | one SQL upsert in `app/security/ratelimit.py` | Redis and `slowapi`, both rejected above |
| Unicode normalisation | `unicodedata.normalize("NFKC", ...)` | — |
| Common-password list | a generated text file and a `frozenset` | the Pwned Passwords range API, which puts a prefix of the hash of a password somebody is about to use onto somebody else's server, and puts a network call where a timeout becomes an outage |

The one place this cost something is the common-password list: `tools/build_password_list.py`
generates the shipped list rather than downloading a real corpus, and
`app/security/breached.py` says so out loud rather than implying otherwise. That is a
smaller, more visible debt than a network dependency in the signup path.

---

## Rules

- **Prefer the standard library.** `secrets`, `hashlib`, `hmac`, `base64`, `unicodedata`,
  and `smtplib` cover more of this project than people expect.
- Every dependency is pinned in a committed lockfile. No floating versions, including
  transitive ones. **Done in the Phase 5 deployment pass:** `api/requirements.lock` (39 pins,
  what ships) and `api/requirements-dev.lock` (test tooling, never in the image), generated by
  `tools/freeze_lock.py` and checked in CI. They pin *versions*, not contents — per-artefact
  hashes need the network at generation time and are a Phase 16 item, stated in the header of
  the generated file rather than implied by the word "lock".
- `pip-audit` is blocking. Zero unresolved highs. Every medium gets a written decision.
- **Priority patching**, in order: `Pillow`, `psycopg`, `fastapi`/`starlette`,
  `argon2-cffi`. The first parses hostile bytes; the rest sit on the auth and data paths.
- Reviewed quarterly: is each entry still used, still maintained, still the best option?
  An unused dependency is pure attack surface.
