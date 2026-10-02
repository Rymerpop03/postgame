# Postgame API

Phase 1 of [`../postgame/BACKEND-PLAN.md`](../postgame/BACKEND-PLAN.md): an empty service
that is already correctly configured. One route, and a lot of scaffolding that later phases
get built inside.

Python 3.13 · FastAPI · PostgreSQL 16 (from Phase 2). Every dependency is justified in
[`../postgame/DEPENDENCIES.md`](../postgame/DEPENDENCIES.md); nothing outside that list.

## Running it

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into PG_SECRET_KEY
.venv/Scripts/python -m app.server
```

Use `python -m app.server`. The entrypoint sets things that cannot be fixed in middleware —
see [Server-layer hardening](#server-layer-hardening) — and it is now the only way in:
`app.main` exposes a factory and no `app` instance, so `uvicorn app.main:app`, which would
skip every one of those settings, fails immediately instead of starting a weaker server.

## Checks

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy
.venv/Scripts/python tools/check_sql_interpolation.py app tools tests
.venv/Scripts/python tools/check_route_policies.py app
.venv/Scripts/python tools/csp_hash.py ../postgame/index.html --check "$PG_CSP_INLINE_SCRIPT_SHA256"
.venv/Scripts/python tools/check_login_timing.py --samples 1000
.venv/Scripts/python tools/check_codeowners.py
.venv/Scripts/python tools/freeze_lock.py --check
.venv/Scripts/python tools/check_deploy_ready.py --skip-cost
```

`check_codeowners.py` fails if CODEOWNERS ever names a placeholder instead of a real
account. §0.3.1 condition 5 makes `app/security/` a protected path, and a CODEOWNERS file
naming an account that does not exist protects nothing while looking like it does.

The first three `tools/` gates are stdlib-only and run without installing anything. The
fourth needs the test database, and exits **2** rather than 0 when it cannot reach one — a
gate that did not run has not passed.

Two other tools are not gates:

```bash
.venv/Scripts/python tools/tune_argon2.py --target-ms 250   # measure on THIS machine
.venv/Scripts/python tools/seed_members.py                  # the ten demo members
.venv/Scripts/python tools/build_password_list.py           # regenerate the common list
.venv/Scripts/python tools/freeze_lock.py                   # rewrite the lock files
```

`tune_argon2.py` matters more than it looks. Argon2 parameters copied from a laptop to a
shared vCPU are either painfully slow or quietly weaker than intended, and neither shows up
as anything but a vague feeling that signing in is sluggish.

## What refuses to start

The app fails at boot rather than serving in a weakened state:

| Condition | Where |
|---|---|
| A required secret is missing, a known placeholder, too short, or entropy-free | `config.py` |
| `PG_DEBUG=true` or `LOG_LEVEL=DEBUG` in production | `config.py` |
| The memory rate limiter in production (Phase 1 scaffolding; per-process, resets on restart) | `config.py` |
| A non-https or localhost origin in production | `config.py` |
| `image_origin == app_origin` in production (removes the upload containment boundary) | `config.py` |
| Any route without an `@policy(...)` declaration | `security/policy.py` |
| The route-policy walk finding **no** routes at all | `security/policy.py` |
| The configured CSP hash not matching the inline script in `index.html` | `main.py` |
| The common-password list missing, or too small to refuse anything anyone picks | `security/breached.py` |
| A route registered after the `/{asset:path}` catch-all, where it can never match | `security/policy.py` |

That second-to-last one is not paranoia. See below.

## Three things worth knowing

### The route-policy check was inert, and looked healthy

`assert_all_declared` originally walked `app.routes` for `APIRoute` instances. FastAPI
0.141 no longer flattens `include_router` into `app.routes` — it inserts a
`_IncludedRouter` wrapper whose real routes hang off `original_router`. The walk therefore
found **zero** routes and reported success. Deny-by-default was completely switched off, and
the only visible symptom was `routes=0` in one boot log line.

Two changes came out of it. The traversal now descends through a list of candidate
attributes rather than one hard-coded path, so a future structural change degrades into
"found fewer routes" instead of "found none". And **an empty walk is now a hard failure** —
a checker that inspected nothing must never report success. That guard alone would have
caught the original bug.

### `Server: uvicorn` cannot be removed in middleware

Uvicorn appends the banner at the protocol layer, *after* the ASGI app has produced its
headers, so `del headers["server"]` in middleware silently does nothing. Worse, the ASGI
test transport never adds the header at all, so a test asserting its absence passed while
the real server leaked it — found by curling the running server, not by the suite.

It is suppressed with `server_header=False` in `app/server.py`, and the test now asserts
that setting rather than a response header.

### Inline style attributes are allowed, deliberately and narrowly

`style-src-attr 'unsafe-inline'`, plus `'unsafe-inline'` on `style-src` as a floor for
Firefox, which implements neither granular directive.

Measured against the real frontend: **0** inline `<style>` elements, **21** sites emitting
`style="..."` attributes with dynamic values — `--h:<hue>` on every cover and avatar,
`width:<pct>%` on every star bar. CSP hashes never apply to style attributes, and the values
are per-game and per-rating, so they cannot move into the stylesheet without a redesign.
`script-src` carries no unsafe keyword and a test enforces that separately.

Phase 4 could remove this by replacing `--h` with a generated set of hue classes. Noted, not
committed.

### The header cap needed two settings, not one

Probing the running server found a 2 MB request header accepted with a 200 — arbitrary
per-connection allocation for any anonymous caller. `h11_max_incomplete_event_size` alone did
nothing, because it is honoured only by uvicorn's h11 implementation and `http="auto"` selects
httptools whenever it is installed, which `uvicorn[standard]` always did. The extra has since
been dropped, so httptools is not installed at all; the pin stays as the guarantee.

Both are now set. The threshold is not an exact byte count — h11 bounds the *incomplete-event*
buffer, so TCP chunking puts the real cutoff between 32 KB and 70 KB — but 2 MB is refused
where it was previously served. Pinning h11 costs some throughput against httptools' C parser;
that is the trade for a limit this repo can verify rather than a proxy default nothing tests.

## Server-layer hardening

`app/server.py` holds settings that must be applied where they take effect:

- `server_header=False` — the banner, per above.
- `http="h11"` + `h11_max_incomplete_event_size` — the request-header cap, per above. Both
  are required; neither works alone.
- `timeout_keep_alive`, `limit_concurrency` — bounded idle connections and in-flight requests.
- `access_log=False` — `RequestIdMiddleware` already logs one structured line per request.
- `log_config=None` — structlog owns logging; uvicorn's dictConfig would override it.
- `proxy_headers` / `forwarded_allow_ips` — **off unless `PG_TRUSTED_PROXY_IPS` names a
  proxy.** Honouring `X-Forwarded-For` from anyone lets a client choose its own rate-limit
  key, which would quietly defeat every per-IP limit added in Phase 5.

## The recurring failure mode

Three separate bugs here shared one shape: **a check that cannot distinguish "verified absent"
from "never looked" is not a check.**

- The route-policy walk found zero routes and called it success.
- The `Server`-banner test asserted absence against a transport that never sends the header.
- The probe's request-id check compared a case-sensitively-missing value to the injected one
  and passed because `None != "injected"`.

Hence the empty-walk guard in `policy.py`, the settings-level assertions in
`test_security_headers.py`, and presence-before-inequality in the probe. Expect this shape
again in later phases — an authorization test that passes because the fixture returned nothing
is the same bug wearing different clothes.

## Verifying the CSP against the real frontend

`tools/serve_with_headers.py` serves `../postgame` with the production header set, importing
the same `build_headers` the API uses — verifying a hand-copied policy string would prove
nothing. Phase 1 does not serve static files; the real deployment puts the frontend on the
app origin (decision 0.9), which lands with Phase 4.

```bash
.venv/Scripts/python tools/serve_with_headers.py 8131
```

## Not done yet

*Updated at the end of Phase 5. Three entries here were about Phases 2 to 5 and have gone.*

- **HTTPS and HSTS are unverified.** Nothing terminates TLS locally, so the redirect and the
  header are staging concerns. HSTS is deliberately not sent when `PG_ENV=local`: sending it
  from localhost pins every other local project to https in that browser. The `__Host-`
  cookie prefix is in the same category and fails more quietly — over plain http the browser
  simply does not store the cookie, so a sign-in appears to succeed and then has not.
- **The frontend still signs in against `localStorage`.** The API has real accounts; the
  browser does not use them yet, deliberately. Wiring the form to the API while diary writes
  are still local would leave you signed in for real with your reviews under a different
  identity. Phase 7.
- **The common-password list is generated, not the real corpus.** 166,241 entries, of which
  31,996 are long enough to be submittable. `PG_PASSWORD_LIST` points at a real one.
- **Every database-touching route outside `auth.py` is `async def` around a blocking
  driver**, so each query occupies the event loop for its duration. Milliseconds rather than
  the hundreds that made this a denial of service on the login path, so it is a throughput
  ceiling rather than a hole — but it is a one-word fix per handler and it should be measured
  and done. Phase 15.
- **The lock files pin versions, not contents.** No per-artefact hashes, so a package
  replaced on the index after the fact would install. Generating hashes needs the network at
  generation time; Phase 16.
- **Fonts are still Google-hosted.** Self-hosting is a Phase 1 item in the plan that has not
  been done; it stays in the CSP until then.
