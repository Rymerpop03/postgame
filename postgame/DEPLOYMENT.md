# Putting Postgame on a public domain

Written 2026-08-05, after Phase 2, in answer to: *is what we have built compatible with a public
internet deployment?*

**Short answer: the architecture is compatible, and the app cannot be deployed as a live site
yet.** Nothing in the design needs revisiting. Four concrete blockers were found by testing rather
than reasoning, two are now fixed, and two are gated on later phases by deliberate design.

---

## What was actually wrong, and what changed

These were found by trying to boot a production configuration, not by reading the code.

### Fixed

**The server bound to `127.0.0.1:8132`, hardcoded.** In a container that is unreachable — the
platform health-checks from outside and gets nothing, and the deploy fails without mentioning the
bind address. `PG_HOST` and `PG_PORT` are now configuration, and a platform-supplied `$PORT`
overrides both, which is the convention Fly, Render, Railway and Heroku all use. The default stays
`127.0.0.1`, because binding every interface on a laptop by accident exposes the dev server to the
local network.

**Nothing required TLS to the database.** libpq's default `sslmode` is `prefer`: it encrypts when
the server offers encryption and *silently continues in cleartext* when it does not. For a managed
Postgres reached across the internet that means the password and every row can cross unprotected
with nothing reporting a problem. Production now refuses to boot unless a non-local database URL
carries `sslmode=require`, `verify-ca` or `verify-full`. Use `verify-full`: it authenticates the
server too, so interception fails instead of succeeding quietly.

### Gated on later phases, by design

> **Both of the blockers below cleared in Phase 5 (Phase 4 for the second). Kept for the
> record, because the reasoning is what makes the current state trustworthy.**

**~~Production cannot boot at all right now~~ — it can, as of Phase 5.** Config refused
`PG_RATE_LIMIT_BACKEND=memory` in production, because a per-process counter that resets on restart
does not throttle a login endpoint — it only looks like it does. The Postgres backend it demanded
instead landed in Phase 5, and `PG_ENV=production` now constructs. The test that asserted the
refusal now asserts the other side of it (`test_production_now_boots`), which is the point at which
a gate has done its job.

**~~Nothing serves the frontend.~~** Phase 4 added static serving on the app origin, with an
extension allowlist that refuses source files, documents and dotfiles, and a containment check
after path resolution. Decision 0.9's single origin is real.

### Newly understood, and worth knowing before you deploy

**Rate limits behind a load balancer would have bucketed the entire internet together.** With
`proxy_headers` off — the Phase 1 default, chosen so nobody can spoof `X-Forwarded-For` and pick
their own rate-limit key — `request.client.host` is the *balancer's* address for every visitor.
They would all share one bucket, and one abuser would throttle everybody.

Both failure modes are real and they point opposite ways: trust forwarded headers from anyone and
limits become spoofable; trust nobody behind a proxy and limits become collective. The resolution
is to name the proxy explicitly in `PG_TRUSTED_PROXY_IPS`, and the app now warns at boot when that
is empty in production. It warns rather than refuses because a directly-exposed process is a
legitimate setup.

---

## What a live deployment needs

### Domain and DNS

Two names, because decision 0.9 separates user-uploaded images onto their own origin so that an
upload defeating every check in Phase 11 still cannot touch app cookies:

| Name | Serves |
|---|---|
| `postgame.app` (or whatever you register) | Site and API, one origin, so there is no CORS surface at all |
| `img.postgame.app` | Avatars and cached cover art, `Cross-Origin-Resource-Policy: same-site` |

Both need certificates. Any PaaS or Caddy will do this automatically; with nginx use certbot.

**One commitment to make deliberately:** HSTS is sent with `includeSubDomains`, so *every*
subdomain of the apex must be HTTPS from then on, including ones that do not exist yet. That is
fine and it is also hard to walk back. `preload` is deliberately **not** sent yet — Phase 16's
checklist submits it only once every subdomain is confirmed ready, because preload is very
difficult to undo.

### Hosting

A small VM or a boring PaaS plus managed Postgres (decision 0.9). Concretely:

- **The app** — one container running `python -m app.server`, with `PG_HOST=0.0.0.0`.
  `Dockerfile` at the repository root builds it. Two stages so the compiler toolchain never
  reaches the running image, a non-root user, and `pip install --no-deps -r
  requirements.lock` so pip has nothing left to resolve. Build context is the repository
  root, not `api/`, because one origin means one image carrying both.
- **Postgres 17** — managed. Match the major version; decision 0.1 pins 17 and dev/prod agreeing
  matters more than being current.
- **TLS termination** — the platform's router, or nginx/Caddy in front.

### The one command to run before you deploy

```bash
python tools/check_deploy_ready.py
```

Run it **inside the target container with the target environment loaded**. Exit 0 is go, 1 is
no-go, and 2 means the check could not run — which is not a pass. It checks the configuration,
the CSP hash against the real `index.html`, `security.txt` and its expiry, the lock file, the
common-password list, the database version and migration state, the ten demo members, and it
measures Argon2 on that machine and refuses parameters that were plainly tuned on different
hardware. CI runs it too, with `--skip-cost`, because a timing measured on a build runner is a
number about the wrong computer.

### Release order

```bash
docker build -t postgame .
docker run --rm --env-file .env.production postgame alembic upgrade head   # release
docker run --rm --env-file .env.production postgame python tools/seed_members.py  # once
docker run -d --env-file .env.production -p 8132:8132 postgame            # then serve
```

Migrations run as `postgame_migrate`; the application connects as `postgame_app`, which holds
no DDL privilege at all. That split is the reason a compromised application cannot alter the
schema, so do not collapse it into one URL for convenience.

### Environment

Everything below is required; the app refuses to start if a secret is missing, a placeholder, too
short, or entropy-free.

```
PG_ENV=production
PG_DEBUG=false
PG_SECRET_KEY=<32+ chars, generated>
PG_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/postgame?sslmode=verify-full
PG_APP_ORIGIN=https://postgame.app
PG_IMAGE_ORIGIN=https://img.postgame.app
PG_HOST=0.0.0.0
PG_TRUSTED_PROXY_IPS=<your proxy's address>
PG_RATE_LIMIT_BACKEND=postgres
PG_CSP_INLINE_SCRIPT_SHA256=sha256-wUSIVEqO+PH+odmhgSQM3zn2YwO5xKTlSifxtLg2E9o=
PG_CSP_REPORT_ONLY=false              # Phase 12
PG_LOG_LEVEL=INFO

# Phase 5. Sessions: idle expiry or absolute expiry, whichever comes first.
PG_SESSION_IDLE_DAYS=14
PG_SESSION_ABSOLUTE_DAYS=90

# Phase 5. Argon2id. MEASURE THESE ON THE MACHINE YOU DEPLOY TO — a laptop and a shared
# vCPU differ by several times, and parameters copied between them are either painfully
# slow or quietly weaker than intended:
#
#     python tools/tune_argon2.py --target-ms 250
#
# Raising them later is a deploy, not a migration: each stored hash is upgraded at its
# owner's next login.
PG_PASSWORD_HASH_MEMORY_KIB=65536
PG_PASSWORD_HASH_TIME_COST=5
PG_PASSWORD_HASH_PARALLELISM=1

# A memory bound, not a throughput knob. Peak is MEMORY_KIB x this — 384 MiB at these
# values. Without it, Starlette's 40-slot thread pool would ask for 40 x 64 MiB under a
# burst of logins and meet the OOM killer, which is a denial of service needing no
# credentials. The app logs the peak at boot and warns above 512 MiB.
PG_PASSWORD_HASH_CONCURRENCY=6

# Empty uses the bundled common-password list, which is NOT the real top-100k breach
# corpus — see api/app/security/breached.py. Point it at the real one when there is one.
PG_PASSWORD_LIST=
```

**Two things to get right before the first sign-in on a real domain**, because both fail in
ways that do not mention themselves:

- **`PG_APP_ORIGIN` must be exactly the origin the browser loads.** The CSRF middleware
  requires `Origin` to match it, so a stray `www.`, a wrong port, or `http` where the browser
  uses `https` refuses every sign-in with a 403 about forgery. The app warns at boot when a
  local origin disagrees with the port it serves on.
- **The session cookie is `__Host-pg_session` outside local**, and that prefix is only
  honoured with `Secure`, `Path=/` and no `Domain`. Over plain http the browser drops the
  cookie silently — no error, no cookie, an endless sign-in loop. TLS has to be working
  before accounts will.

Generate the secret with:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Never commit it. `.env` is gitignored and `gitleaks` runs in CI over full history.

### Database setup on the host

`ops/bootstrap_01_cluster.sql` and `ops/bootstrap_02_schema.sql`, run once as a superuser, then
`alembic upgrade head` as `postgame_migrate`. The two-role split matters more in production than
locally: `postgame_app` holds no DDL privilege, so an SQL injection bug that reaches the database
leaks rows instead of dropping tables.

The application must **never** be given the migration credentials. `migrations/env.py` refuses to
run as the app role for the same reason in reverse.

### Health check

Point the platform at `GET /api/health`. It returns `{"ok": true}` and deliberately nothing else —
no version, no hostname, no dependency status, because a health endpoint that reports its inventory
is free reconnaissance on an unauthenticated path.

### Files to exclude from the deployed bundle

- `serve.py` — the development static server
- `scratchpad/*.py` — the catalogue pipeline (`harvest` → `tags` → `emit`); they carry relative
  paths into `js/` and belong in the repo, not on a web root
- `api/.venv/`, `api/.env`, `BACKEND-PLAN.md`, `DEPENDENCIES.md`, `ASVS-V2-CHECKLIST.md`, this file
- `postgame/test/filter-test.html` — a development page for the moderation filter. `.html` is
  on the servable extension list, so before Phase 5's deployment pass it was reachable at
  `/test/filter-test.html` on any deployment serving the repository directory
- `logo-mockup.html` is already deleted

`.dockerignore` enforces all of this, and `site.py` refuses `test/`, `tests/`, `scratchpad/`
and any dot-directory other than `.well-known` regardless of what is on disk. Two layers,
because "remember to prune the bundle" is exactly the step that gets skipped.

`postgame/robots.txt` disallows `/api/`. It protects nothing — a scraper that ignores it faces no
obstacle — it is only crawler etiquette and index hygiene.

**Staging is handled, and not by remembering to swap a file.** When `PG_ENV=staging` the server
serves `robots-staging.txt` (`Disallow: /`) in place of `robots.txt`, and sends
`X-Robots-Tag: noindex, nofollow` on every response. The header is the half that works: robots.txt
asks a crawler not to *fetch* a page, which does nothing about a URL already indexed or reached
through a link. A staging copy of a social site in search results is a privacy problem, not just an
embarrassing one.

---

## Still to decide

**~~`security.txt`~~ — written.** `postgame/.well-known/security.txt`, contact
`security@postgame.app`, which is a retireable alias rather than a personal address on an
unauthenticated public path. **You need to make that alias forward somewhere you read**, or the
file points at nothing, which is worse than not having one: somebody with a genuine finding will
believe they have reported it.

Its `Expires` date is 2027-08-01, and RFC 9116 makes an expired file invalid. Renewing it is a
test (`TestSecurityTxt`, which fails a month early, on purpose) rather than a note in a document.

**A sitemap.** Not useful yet. The frontend is hash-routed, so every URL serves the same
`index.html` and there is nothing per-game to index. Worth revisiting after Phase 3, when game data
comes from the API and individual pages become worth crawling.

---

## The honest sequence

You can put the **demo** online today — the static frontend works from any web root, and I verified
it runs from `file://` too. It has no accounts and stores everything in the visitor's browser.

For a **real multi-user site** the order is forced, and it is the plan's order:

1. **Phase 3** — catalogue API, so the 2,000 games come from Postgres rather than 21 JS files.
2. **Phase 4** — the async data layer and static serving, which is what makes one origin real.
3. ~~**Phase 5**~~ — done. Identity: accounts, passwords, sessions, CSRF, real rate limiting,
   and the audit log. `PG_ENV=production` boots. There are accounts on the API, but the
   frontend still signs in against `localStorage` — moving it over is Phase 7, when writes
   become server-side. See `ASVS-V2-CHECKLIST.md` for what is and is not met.
4. **Phase 12** — the cover proxy, after which CSP goes from report-only to enforcing.
5. **Phase 15/16** — backups with a drilled restore, then the launch checklist.

Deploying to staging is worth doing now. HTTPS, HSTS, the `__Host-` cookie prefix and the proxy
header handling are all things that cannot be verified locally, and the cookie prefix in particular
fails silently — the browser simply does not store the cookie, and the symptom is a sign-in that
appears to succeed and then does not. Much better to meet that on staging than at launch.
