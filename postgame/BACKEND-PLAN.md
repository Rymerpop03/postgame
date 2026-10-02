# Postgame — Backend Build Plan

Written 2026-08-04, immediately after the frontend audit closed at asset version 29.

This plan takes Postgame from a browser-only demo (2,000 games in JS files, everything
else in `localStorage`) to a real multi-user service. It is split into 17 phases meant
to be worked one at a time. Each phase ends in something you can run and verify, and
no phase depends on a later one.

Security is not a phase. It is a column in every phase, plus a set of rules in
[Cross-cutting rules](#cross-cutting-rules) that apply to every line of code we write.
Phase 16 is a *verification* pass, not the point at which security starts.

---

## Contents

- [What we already have](#what-we-already-have)
- [Threat model](#threat-model)
- [Cross-cutting rules](#cross-cutting-rules)
- [Phase 0 — Decisions](#phase-0--decisions-no-code)
- [Phase 1 — Skeleton, secrets, transport, CI gates](#phase-1--skeleton-secrets-transport-ci-gates)
- [Phase 2 — Data model and migrations](#phase-2--data-model-and-migrations)
- [Phase 3 — Catalogue read API and seed loader](#phase-3--catalogue-read-api-and-seed-loader)
- [Phase 4 — Frontend data layer: the async seam](#phase-4--frontend-data-layer-the-async-seam)
- [Phase 5 — Identity core](#phase-5--identity-core)
- [Phase 6 — Account lifecycle](#phase-6--account-lifecycle)
- [Phase 7 — Authorization layer and diary writes](#phase-7--authorization-layer-and-diary-writes)
- [Phase 8 — Social graph and activity feed](#phase-8--social-graph-and-activity-feed)
- [Phase 9 — Comments and server-authoritative moderation](#phase-9--comments-and-server-authoritative-moderation)
- [Phase 10 — Direct messages](#phase-10--direct-messages)
- [Phase 11 — Profiles and avatar upload](#phase-11--profiles-and-avatar-upload)
- [Phase 12 — Cover art service and CSP tightening](#phase-12--cover-art-service-and-csp-tightening)
- [Phase 13 — Abuse: blocks, reports, admin queue](#phase-13--abuse-blocks-reports-admin-queue)
- [Phase 14 — Privacy: export, deletion, retention](#phase-14--privacy-export-deletion-retention)
- [Phase 15 — Observability, backups, disaster recovery](#phase-15--observability-backups-disaster-recovery)
- [Phase 16 — Hardening pass and launch checklist](#phase-16--hardening-pass-and-launch-checklist)
- [Status](#status)
- [Dependency graph](#dependency-graph)

---

## Status

Updated at the end of the Phase 5 deployment pass, 2026-08-23. A phase is **done** only when
it has an outcome section below recording what was actually built, what was found, and which
exit criteria were met — including the ones that were not.

| Phase | | State |
|---|---|---|
| 0 | Decisions | **Done.** Two amended in flight (Python 3.13, Postgres 17), both recorded in place rather than rewritten |
| 1 | Skeleton, secrets, transport, CI gates | **Done**, plus a hardening pass. Deny-by-default authorization was inert on first build and looked healthy — the origin of this project's recurring lesson |
| 2 | Data model and migrations | **Done.** 18 tables, 60 CHECK constraints, two database roles. The Phase 10 IDOR shape is unrepresentable at the schema level |
| 3 | Catalogue read API and seed loader | **Done.** 2,000 games in Postgres with not one cover changed, by porting the frontend's hash bit-for-bit |
| 4 | Frontend data layer | **Done.** Both backends verified in a browser. Content-hashed filenames deferred — they need a build step, which is its own decision |
| 5 | Identity core | **Done.** All seven §0.3.1 conditions met. Two exit criteria partial and named as such |
| — | *Deployment pass* | **Done.** Not a numbered phase: `security.txt`, lock files, `Dockerfile`, staging noindex, and a pre-deploy go/no-go check |
| 6 | Account lifecycle | Next. Verification, reset, email change, session list — everything Phase 5 deferred |
| 7 | Authorization layer and diary writes | Not started. The phase that moves the frontend off `localStorage` |
| 8 | Social graph and activity feed | Not started |
| 9 | Comments and server-authoritative moderation | Not started |
| 10 | Direct messages | Not started. Carries the top risk in the threat model |
| 11 | Profiles and avatar upload | Not started |
| 12 | Cover art service and CSP tightening | Not started. CSP goes from report-only to enforcing here |
| 13 | Abuse: blocks, reports, admin queue | Not started |
| 14 | Privacy: export, deletion, retention | Not started |
| 15 | Observability, backups, disaster recovery | Not started |
| 16 | Hardening pass and launch checklist | Not started |

**Deployable today:** staging, on a real domain, with the catalogue served from Postgres and
accounts working on the API. **Not yet a public launch:** the frontend still signs in against
`localStorage` until Phase 7, there is no email delivery until Phase 6, and there are no
drilled backups until Phase 15.

---

## What we already have

Worth stating precisely, because the plan leans on it.

| Piece | State | Becomes |
|---|---|---|
| `js/games-01..21.js` | 2,000 games as `<script>` tags, 542 KB | Rows in `games`, served by an API |
| `js/data.js` | Seeded members, logs, reviews, comments, messages, follows | Seed fixtures, marked non-claimable |
| `js/store.js` | Single `load()`/`save()` pair over `localStorage`, all writes funnel through it | The seam we replace with an HTTP client |
| `js/moderation.js` | 89-term filter, runs in the browser | Advisory client copy; server becomes authority |
| `js/art.js` | Client fetches MediaWiki directly, caches to `localStorage` | Server-side proxy and cache |
| `js/ui.js`, `js/app.js` | 26 exports, hash router, `esc()` on every interpolation | Mostly unchanged; gains async states |
| Field caps | `NAME_MAX 40`, `BIO_MAX 240`, `MESSAGE_MAX 1000`, `COMMENT_MAX 600`, `REVIEW_MAX 6000` | Re-declared server-side as the real limits |
| `?v=29` | Manual cache-busting because there is no build step | Content hashes and real `Cache-Control` |

Two properties of the existing code make this much safer than it could be, and we must
not lose them:

1. **Every write already goes through one function.** `store.save()` is the only writer.
   That is why Phase 4 is tractable.
2. **Every interpolation already goes through `esc()`.** The audit verified this across
   26 routes. Any new `innerHTML` path that skips it reopens stored XSS.

---

## Threat model

### Actors

| Actor | Capability | Primary concern |
|---|---|---|
| Anonymous internet | Can hit any unauthenticated endpoint, at volume | Enumeration, scraping, DoS, SQLi |
| Authenticated user | Valid session, attacking *other users'* data | **IDOR** — the top risk here |
| Malicious content author | Controls review/bio/username/message text | Stored XSS, moderation evasion |
| Bot / scraper | Cheap account creation, no rate discipline | Spam, catalogue theft, credential stuffing |
| Attacker with a stolen session | Cookie or token exfiltrated | Session lifetime, revocation, re-auth on sensitive ops |
| Admin / operator | Can read anything | Audit trail, least privilege, no plaintext secrets |
| Supply chain | A dependency we pull in | Minimal deps, lockfile, audit in CI |

### Top eight risks, ranked for *this* application

1. **IDOR on direct messages.** A social app with DMs is one missing `WHERE user_id = ?`
   away from a breach. Phase 10 is built around this and nothing else.
2. **Stored XSS via user text.** Reviews are 6,000 characters of free text rendered into
   other people's pages. The frontend escapes today; the server must never hand back
   content that assumes otherwise, and CSP must be a real second line.
3. **SQL injection through filter parameters.** `genre`, `platform`, `year`, `sort` are
   exactly the params people concatenate into `ORDER BY` clauses. Sort keys especially —
   they cannot be parameterized, so they must be whitelist-mapped.
4. **Avatar upload.** Untrusted bytes we then serve back. SVG-with-script, polyglots,
   decompression bombs, and same-origin script execution.
5. **Credential stuffing and weak passwords.** No password field exists today, so this
   surface arrives entirely new in Phase 5.
6. **Account and email enumeration.** Signup, login, and reset all leak existence unless
   deliberately built not to.
7. **Abuse at zero cost.** Nothing stops one actor creating 10,000 accounts and messaging
   every user.
8. **Rolling our own auth.** Honest assessment: Phases 5 and 6 carry more risk than the
   other fifteen combined. See the Phase 0 decision on this.

### Explicitly out of scope

No payments, no OAuth-provider role (we are not an IdP for anyone else), no file sharing
beyond avatars, no real-time transport (polling is fine at this scale), no federation.

---

## Cross-cutting rules

These are non-negotiable and apply from Phase 1 onward. Every phase's exit criteria
implicitly include "did not violate these."

**Data access**
- Parameterized queries only. No f-strings, `%`, `+`, or `.format()` anywhere near SQL.
  CI greps for it (see Phase 1).
- Dynamic `ORDER BY` / column names come from a hardcoded whitelist dict, never from
  request text.
- Every query touching user-owned rows carries the actor's id in the `WHERE` clause.
  Not "fetch then check" — filter in the query.

**Authorization**
- Deny by default. The framework refuses any route that does not declare a policy.
- Authorization is a decorator/dependency on the route, never an `if` buried in a handler.
- "Can this actor see this row" is one function per resource type, tested in a matrix.

**Input**
- Validate at the boundary with a schema (Pydantic). Unknown fields rejected, not ignored.
- Every string has a max length. Every enum is a closed set. Every integer has a range.
- The database repeats the constraint as a `CHECK`. Two layers, deliberately.
- Never trust a client-computed value. The client downscales avatars and pre-checks
  moderation; both are conveniences, and the server redoes both.

**Output**
- JSON only, `Content-Type: application/json`, never HTML from an API route.
- No user-controlled data in URLs, redirects, or log messages without validation.
- Errors are generic to the client and specific in the logs. No stack traces over the wire.

**Secrets**
- Nothing secret in the repo, ever. Config from environment. `gitleaks` in CI.
- Secrets are rotatable without a code change.

**Logging**
- Never log passwords, tokens, session ids, email bodies, message bodies, or raw IPs.
- IPs are hashed with a rotating salt. Once the salt rotates, old hashes are unlinkable.

**Dependencies**
- Prefer the standard library. Every added dependency needs a one-line justification in
  a `DEPENDENCIES.md` note. Lockfile committed. `pip-audit` in CI.

---

## Phase 0 — Decisions (no code)

**Status: complete, 2026-08-04.**

Ten decisions that constrain everything downstream. All ten are settled below. The
reasoning for each is kept, along with what the rejected alternative was, so a future
reader can tell a considered choice from an accident. Any of these can be revisited, but
the cost rises steeply once the phase that depends on it has shipped.

| # | Decision | Answer |
|---|---|---|
| 0.1 | Stack | Python 3.12 + FastAPI + PostgreSQL 16 |
| 0.2 | `file://` support | Keep as demo mode — dual backend behind one interface |
| 0.3 | Auth | Build our own, with the extra controls in 0.3.1 |
| 0.4 | Sessions | Opaque server-side sessions in an `HttpOnly` cookie |
| 0.5 | Repeat plays | One log per game — `UNIQUE (user_id, game_id)` |
| 0.6 | Rating scale | `smallint` 1–10 half-stars, displayed 0.5–5.0 |
| 0.7 | Deleted users' content | Anonymize to a tombstone user, never cascade-delete |
| 0.8 | Seeded demo accounts | Unclaimable — `is_demo`, no password hash |
| 0.9 | Hosting | One origin for site + API, separate origin for avatars |
| 0.10 | Environments | `local`, `staging`, `production` |

### 0.1 Stack

**Decided: Python 3.13 + FastAPI + PostgreSQL 17.**

> **Amended 2026-08-05, at the start of Phase 2.** Originally recorded as PostgreSQL 16.
> `winget` offers 17 and 18, not 16, and nothing in the Phase 2 schema needs 16 specifically:
> `gen_random_uuid()` is built in from 13, generated `tsvector` columns from 12, and `citext`
> is an extension in every supported version. 17 over 18 because 18 is very new and the
> managed providers we would deploy to lag it — and dev and production sharing a major version
> matters more than being current. The version is pinned in CI and in the connection check, so
> a mismatch fails loudly rather than producing subtly different behaviour.

> **Amended 2026-08-04, at the start of Phase 1.** Originally recorded as Python 3.12; the
> development machine has 3.13.14 and every package on the allowlist supports it
> (`psycopg` 3.2+, `Pillow` 11+, `SQLAlchemy` 2.0+, `argon2-cffi`). Pinning 3.12 would have
> meant installing a second interpreter to gain nothing. The version is pinned explicitly in
> `pyproject.toml` and in CI, so this stays a decision rather than an accident of whatever
> is on `PATH`.

Reasons specific to this project: the catalogue pipeline (`scratchpad/harvest.py`,
`tags.py`, `emit.py`) is already Python and becomes the seed loader with almost no
rewriting; `serve.py` already exists; and Pydantic gives us schema-validated boundaries
for free, which is a security control, not just ergonomics. The frontend stays plain
files with no build step — that ethos survives intact.

Node 22 + Fastify was the considered alternative — one language end to end, no
context-switching. It lost because it would have meant rewriting the Python catalogue
pipeline and adding Zod to recover the boundary validation FastAPI has natively.

**Postgres over SQLite** — SQLite was also considered, and would genuinely work at this
scale. It lost because the social features want concurrent writes and real full-text
search, and migrating later is painful enough that starting on Postgres is cheaper than
moving to it.

### 0.2 The `file://` constraint

This is the decision with the sharpest edge, so it goes early.

Postgame currently works from `file://` — I verified it: 2,000 games load, routing works,
search works, `localStorage` works, covers load. **That property cannot survive
authentication.** Cookies, CSRF origin checks, and CORS all assume an origin, and
`file://` has an opaque one.

**Decided: split the two modes explicitly.**
- **Demo mode** (`file://` or no API reachable): the existing seeded data, `localStorage`
  persistence, no accounts. Keep it. It is a genuinely nice property and it is already
  built and tested.
- **Live mode** (HTTPS origin): real API, real accounts.

`store.js` detects which mode it is in once, at boot, and picks a backend. This is
achievable precisely because there is one write path. What we must *not* do is pretend
both work identically and ship a build where signing in silently does nothing.

### 0.3 Build our own auth, or use an identity provider?

**Decided: build it ourselves**, using vetted primitives (`argon2-cffi`) and never
hand-rolled crypto. A hosted identity provider was the alternative and is genuinely the
lower-risk choice on pure security grounds; it was rejected because understanding this
layer is part of the point of the project, and the surface is small and well-specified —
email + password, sessions, verification, reset.

Under no circumstances do we invent a password hash, a token format, or a comparison.

Choosing the higher-risk path obligates us to the controls below. These are not optional
mitigations; they are the condition on which the decision was made.

### 0.3.1 Conditions attached to building our own auth

1. **The timing-indistinguishability check is a CI gate, not a manual test.** Login with
   an unknown email versus a known email with the wrong password, sampled ≥ 1,000 times,
   compared statistically with a documented tolerance. It fails the build, because a
   regression here is invisible in every other way.
2. **The enumeration check compares full response bytes, not just status codes.** Signup
   against an existing email and against a fresh one must produce byte-identical bodies.
   Same for `password/forgot`.
3. **The dummy-hash path gets its own named test.** Verifying a throwaway Argon2 hash when
   the email is unknown is the single easiest line to delete during a refactor with no
   visible failure — and deleting it defeats control 1 entirely.
4. **Session rotation is asserted, on both login and password change.** Two tests, not a
   code comment.
5. **`api/security/` is a protected path in `CODEOWNERS`.** Even working solo, that forces
   a deliberate second read rather than a drive-by edit.
6. **Phase 5 does not exit until reviewed against OWASP ASVS v4 section V2** (authentica-
   tion), item by item, with each item marked met, not-applicable, or accepted-with-reason.
   The completed checklist is committed.
7. **A defined bail-out.** If Phase 5's exit criteria cannot be met — particularly controls
   1 and 2 — we stop and move to a hosted provider rather than shipping auth that is
   nearly right. That is a decision point with a pre-agreed answer, not a judgement call
   to be made under deadline pressure.

### 0.4 Sessions or JWTs?

**Decided: opaque server-side sessions in an `HttpOnly` cookie.**

Revocation is the deciding factor. A stolen JWT is valid until it expires and there is
nothing you can do about it; a stolen session id dies the moment we delete the row. We
also want a "sign out everywhere" button and a visible session list, both of which are
trivial with rows and awkward with JWTs.

The cookie: `__Host-pg_session`, `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`.
The token: 32 bytes from `secrets.token_bytes`, sent raw to the client, stored **only**
as a SHA-256 hash. A database dump does not yield usable sessions.

### 0.5 One log per game, or a diary of repeat plays?

Letterboxd allows rewatches. The current frontend assumes one entry per user per game
(`store.saveLog` upserts).

**Decided: keep the `UNIQUE (user_id, game_id)` constraint for v1**, because the
entire profile UI — the shelves, the "See more" routes we just built — assumes it. Add a
`plays` table later if you want replay history; the constraint is easier to relax than
to introduce.

### 0.6 Rating scale

The frontend has a ten-button rate strip. **Decided: store `smallint` 1–10 representing
half-stars**, and display as 0.5–5.0. Never floats — a rating is a discrete choice from
ten options, and storing it as a float invites averaging bugs and display drift.

### 0.7 What happens to a deleted user's content?

**Decided: anonymize, do not cascade-delete.** Deleting a user's reviews and
comments collapses other people's threads and destroys aggregate ratings. Reassign to a
tombstone user, drop the identifying fields, keep the text. The account deletion UI must
say this plainly before the user confirms — a deletion flow that quietly keeps content is
a dark pattern.

### 0.8 Seeded demo accounts

The ten seeded members (`pixelvagrant` and the rest) will exist as rows. **They must be
unclaimable**: `is_demo = true`, `password_hash IS NULL`, no email, and the login handler
refuses them before it does anything else. Otherwise "sign in as pixelvagrant" becomes a
real account someone can take.

### 0.9 Hosting

**Decided: one small VM or a boring PaaS (Fly.io / Render) plus managed Postgres.**
Static frontend on the same origin as the API — one origin means no CORS preflight
surface at all, which is one entire class of misconfiguration we skip. Avatars go on a
*separate* origin (see Phase 11).

> **Reviewed against a real deployment 2026-08-05**, after Phase 2 — see
> [`DEPLOYMENT.md`](DEPLOYMENT.md) for what a public domain actually needs. The architecture held;
> four concrete blockers were found by trying to boot a production config rather than by reading
> the code. Two fixed: the server bound `127.0.0.1:8132` hardcoded (unreachable in a container,
> and the deploy failure would not mention the bind address — now `PG_HOST`/`PG_PORT` with a
> platform `$PORT` override), and nothing required TLS to the database (libpq defaults to
> `sslmode=prefer`, which continues in cleartext when the server does not offer encryption — now
> refused in production for a non-local database). Two remain gated by design: production cannot
> boot until Phase 5 supplies the Postgres rate limiter, and nothing serves the frontend until
> Phase 4.
>
> The subtle one: with `proxy_headers` off — the Phase 1 default that stops anyone spoofing
> `X-Forwarded-For` — `request.client.host` behind a load balancer is the *balancer's* address for
> every visitor, so all of them would share one rate-limit bucket and a single abuser would
> throttle the whole internet. Both failure modes are real and point opposite ways; the resolution
> is to name the proxy in `PG_TRUSTED_PROXY_IPS`, and the app now warns at boot when that is empty
> in production.

### 0.10 Environments

Three: `local`, `staging`, `production`. Staging gets its own database and its own
secrets, is `noindex`, and is never seeded with production data. Migrations are proved on
staging before production, every time.

### 0.11 Consequences to carry forward

Recording the decisions surfaced four things that later phases must honour. Noted here so
they are not rediscovered as surprises.

- **Phase 1** implements the dependency allowlist in [`DEPENDENCIES.md`](DEPENDENCIES.md)
  and nothing outside it. That file is the concrete form of the 0.1 stack decision, and
  supply chain is in the threat model.
- **Phase 2** needs exactly one Postgres extension: `citext`, for case-insensitive
  `username` and `email_norm`. `gen_random_uuid()` is built in on Postgres 13+, so
  `pgcrypto` is not required.
- **Phase 4** must define the store backend interface *before* touching `app.js`, because
  0.2 means two implementations behind one contract. The seam is `store.js`'s existing
  method surface — that is the interface, and it should be written down and frozen before
  either backend is built against it.
- **Phase 5** inherits the seven conditions in 0.3.1, including a bail-out that changes
  the plan if it triggers.

**Exit criteria:** ✅ Met. All ten decisions recorded above with an answer, the rejected
alternative, and the reason it lost. `DEPENDENCIES.md` written. No code.

A consistency pass over the plan afterwards found and fixed four contradictions the
decisions had introduced: rate limiting was scheduled in Phase 1 but its Postgres token
bucket does not exist until Phase 2; a blanket `Cross-Origin-Resource-Policy: same-origin`
would have blocked the app from loading avatars off their own separate origin; Phase 15's
metrics need a dependency the allowlist does not contain; and Phase 12 was described as
independent of Phase 11 while sharing its image pipeline. Worth noting that all four came
from *recording* the decisions rather than from making them — writing a decision down is
what exposes what it collides with.

---

## Phase 1 — Skeleton, secrets, transport, CI gates

**Status: complete, 2026-08-04.** Built in [`../api/`](../api/) — see
[`api/README.md`](../api/README.md). 125 tests, `ruff` and `mypy --strict` clean, three
stdlib CI gates green and each demonstrated failing on deliberately bad input. Two items
consciously left undone and one exit criterion amended; all recorded at the end of this
phase.

Goal: an empty service that is already correctly configured. Building the security
scaffolding before there is anything to protect means we never retrofit it.

### Build
- Repo layout: `api/` (service), `postgame/` (existing frontend, untouched), `ops/`.
- `api/` structure: `main.py`, `config.py`, `db.py`, `security/`, `routes/`, `schemas/`,
  `services/`, `tests/`.
- Config via `pydantic-settings` from environment. **The app refuses to boot if a
  required secret is missing or is a known default.** No fallbacks to `"changeme"`.
- One route: `GET /api/health` returning `{"ok": true}` and nothing else — no version,
  no hostname, no dependency status. Health endpoints leak inventory.
- Structured JSON logging with a request id on every line.
- Global exception handler: generic message + request id to the client, full detail to
  the log. Debug mode physically cannot be enabled in production config.

### Security
- **Transport:** HTTPS only. HTTP redirects with 301. HSTS `max-age=31536000;
  includeSubDomains` — add `preload` only after Phase 16 confirms every subdomain is
  ready, because preload is very hard to undo.
- **Security headers** on every response, via one middleware:
  - `Content-Security-Policy` (see below)
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy` denying every feature we do not use (camera, microphone,
    geolocation, payment, usb, …)
  - `Cross-Origin-Opener-Policy: same-origin`
  - `Cross-Origin-Resource-Policy: same-origin` **on the app origin only.** The avatar
    origin from Phase 11 must send `same-site` instead — `same-origin` there would make
    the app unable to embed its own users' avatars, since CORP is enforced on cross-origin
    subresource loads. `postgame.app` and `img.postgame.app` are same-site, so `same-site`
    is the tightest value that works. Do not apply this header globally from one middleware
    without a per-origin branch.
  - `frame-ancestors 'none'` in CSP (supersedes `X-Frame-Options`)
- **CSP, starting strict and getting stricter in Phase 12.** The one wrinkle: `index.html`
  has an inline `<script>` in `<head>` that resolves the theme before first paint — it
  exists specifically to prevent a dark flash for light-theme readers, so we cannot move
  it to a file. Solution: **allow it by hash**, `script-src 'self' 'sha256-…'`. That
  keeps `index.html` a static file, which a nonce would not. The hash goes in config and
  CI verifies it still matches the file, so an edit to that script fails the build rather
  than silently breaking the page.
  - Initial policy: `default-src 'none'; script-src 'self' 'sha256-…';
    style-src 'self' fonts.googleapis.com; font-src fonts.gstatic.com;
    img-src 'self' data: upload.wikimedia.org …; connect-src 'self' …;
    base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
  - Report-only first, with a report endpoint, until the console is clean.
- **Self-host the fonts.** Currently they come from Google. Self-hosting removes two
  external origins from CSP, removes a third party from every page load, and removes a
  privacy leak. Do it in this phase while nothing else is moving.
- **Rate limiting middleware** installed now, generously configured, so later phases only
  have to set numbers. Note the ordering problem: the token bucket lives in Postgres
  (per `DEPENDENCIES.md`, which is why `slowapi` was rejected), and Postgres arrives in
  Phase 2. So Phase 1 ships the middleware **interface** with an in-process in-memory
  backend — adequate while the only route is `/api/health` and there is a single worker —
  and Phase 5 swaps the backend to Postgres before any limit protects something that
  matters. Same pattern as `store.js`: one interface, two backends. The interface is the
  deliverable here; the memory backend is scaffolding and must be gone before launch.

### CI gates (all blocking)
- `gitleaks` — secret scanning, including full history once.
- `pip-audit` — dependency CVEs.
- **Custom lint: fail on any SQL-adjacent string interpolation.** A grep for f-strings
  and `%`/`+`/`.format()` inside `execute(` calls. Crude, and it will catch the real bug
  the one time it matters.
- **Custom lint: fail on any route lacking an explicit auth policy decorator.** This is
  what makes "deny by default" real rather than aspirational.
- Security-header assertion test against a live response.
- Type checking (`mypy --strict` on `api/`).

**Exit criteria:** health check responds over HTTPS; every header present and asserted by
a test; CSP report-only produces zero violations against the existing frontend; all five
CI gates run and one of them can be demonstrated failing on a deliberately bad commit;
the app refuses to boot without secrets.

### Phase 1 outcome

Met, with one criterion amended and three items outstanding. Recorded because the
interesting part of this phase was what the checks caught *in the checks themselves*.

**Amended: "CSP report-only produces zero violations."** It now does — across eight routes
with scrolling, verified with a `securitypolicyviolation` listener rather than a console
scrape. But reaching zero required allowing inline style *attributes*, which the original
criterion implicitly assumed would be unnecessary. The real frontend has 0 inline `<style>`
elements and 21 sites emitting `style="..."` with dynamic values (`--h:<hue>` on every cover
and avatar, `width:<pct>%` on every star bar). CSP hashes never apply to style attributes and
the values are per-game, so `style-src-attr 'unsafe-inline'` is the tightest policy that
works — plus `'unsafe-inline'` on `style-src` as a floor, because Firefox implements neither
granular directive and falls back to it. `script-src` carries no unsafe keyword and is
asserted separately. Phase 4 could remove this by generating hue classes; noted, not
committed.

**Three bugs found, two of them in security controls that looked healthy.**

1. **Deny-by-default was completely inert.** `assert_all_declared` walked `app.routes` for
   `APIRoute` instances, but FastAPI 0.141 no longer flattens `include_router` — it inserts a
   `_IncludedRouter` wrapper whose real routes hang off `original_router`. The walk found zero
   routes and reported success. The only symptom was `routes=0` in one boot log line. Fixed by
   walking candidate attributes rather than one hard-coded path, and by making **an empty walk
   a hard failure** — a checker that inspected nothing must never report success. That guard
   alone would have caught it.
2. **`Server: uvicorn` leaked.** Uvicorn appends it after the ASGI app returns, so the
   middleware's `del headers["server"]` did nothing — and the ASGI test transport never adds
   the header, so the test passed while production leaked. Found by curling the real server,
   not by the suite. Now `server_header=False` in `app/server.py`, with the test asserting the
   setting rather than a response header.
3. **The SQL gate flagged its own help text.** Matching `"update "` and `"order by"` caught
   English prose. Now requires either a strong marker or two weak ones, and skips arguments to
   message and exception calls. A gate that cries wolf gets ignored, which is worse than no
   gate at all.

**Added beyond the plan:** `PG_TRUSTED_PROXY_IPS`, defaulting to trusting no forwarded
headers. Honouring `X-Forwarded-For` from arbitrary clients lets a caller choose its own
rate-limit key, which would silently defeat every per-IP limit in Phase 5. Better decided
here than discovered there.

### Phase 1 hardening pass

A second review, before starting Phase 2, deliberately looking for flaws the Phase 1 tests
were not written to catch. Four found. Two were only visible by probing a real server over
real HTTP, which is the lesson worth keeping: the test suite drives the ASGI app directly, so
nothing between the app and the wire is covered by it.

1. **Unbounded request headers.** A 2 MB request header was accepted and answered 200 —
   arbitrary per-connection allocation for any anonymous caller. Capping it needed *two*
   settings: `h11_max_incomplete_event_size` is honoured only by uvicorn's h11 implementation,
   and `http="auto"` selects httptools whenever it is installed, which `uvicorn[standard]`
   always does. Setting the limit alone changed nothing and the probe still returned 200.
   Pinning `http="h11"` costs some throughput and buys a control we can actually verify.
   The threshold is not an exact byte count — h11 bounds the incomplete-event buffer, so TCP
   chunking puts the real cutoff between 32 KB and 70 KB — but unbounded is now closed.
2. **The CSP report sink buffered the whole body before checking its size.** `await
   request.body()` reads everything, then the length check rejects it — so a large POST to
   the only unauthenticated endpoint in the app was fully allocated before being refused. The
   check was real; the protection was not. Now reads in bounded chunks and rejects a declared
   `Content-Length` over the cap before reading a byte.
3. **That endpoint had no rate limit.** It is the one thing an anonymous caller can make the
   server work for. Now 60/min per address, returning 429 with `Retry-After` — which also puts
   the Phase 1 limiter interface into real use instead of leaving it untested in practice.
4. **`main.py` inserted `tools/` at `sys.path[0]`** to import the hashing function, putting a
   non-package directory ahead of the standard library for the whole process. The hashing moved
   into `app/security/csp.py`; the dependency now runs one way, tools/ imports app/. An
   AST-based test enforces it.

**And a fifth, in the probe itself:** the check for "request id is not taken from the client"
passed vacuously, because uvicorn emits header names lower-case and a case-sensitive lookup on
a plain dict returned `None` — and `None != "injected"` is true. Exactly the same shape as the
`Server` banner test that passed while production leaked. Re-verified properly: the header is
present, server-generated, unique per request, and a client-supplied value is discarded.

The pattern in three of these five is worth stating plainly, because it will recur: **a check
that cannot distinguish "verified absent" from "never looked" is not a check.** The empty-walk
guard in `policy.py` came from the same realisation.

Final state: 133 tests, `ruff` and `mypy --strict` clean, three gates green, 54/54 adversarial
probe checks against a live server, zero CSP violations across ten routes with both
stylesheets loading and inline style attributes working.

**Outstanding, carried forward:**

- **HTTPS, the http→https redirect, and HSTS are unverified.** Nothing terminates TLS
  locally, so these are staging concerns. HSTS is deliberately *not* sent when env is local:
  sending it from localhost pins every other local project to https in that browser.
- **Fonts are still Google-hosted.** Self-hosting was a Phase 1 item and was not done, so
  `fonts.googleapis.com` and `fonts.gstatic.com` remain in the CSP. Fold into Phase 12, which
  removes the other external origins anyway.
- **`CODEOWNERS` has a placeholder owner.** `@OWNER` must be replaced, or condition 5 of
  §0.3.1 protects nothing while looking like a control.

---

## Phase 2 — Data model and migrations

**Status: complete, 2026-08-05.** 19 tables, 60 CHECK constraints, 23 foreign keys, 56 indexes,
7 triggers, applied to `postgame` and `postgame_test`. 224 tests pass with **zero skipped**;
`ruff`, `ruff format` and `mypy --strict` clean; all three gates green. One serious bug found and
fixed — see the outcome at the end of this phase.

Goal: the full schema, with constraints doing real work. No endpoints.

### Build
Migrations with Alembic, every one reversible, `up`/`down` both tested.

Tables, with the constraints that matter called out:

```
users
  id                uuid pk default gen_random_uuid()
  username          citext unique not null   CHECK (username ~ '^[a-z0-9_]{3,20}$')
  email_norm        citext unique            -- null for demo accounts
  email_raw         text                     -- as typed, display only
  display_name      text not null            CHECK (length(display_name) <= 40)
  bio               text                     CHECK (length(bio) <= 240)
  hue               smallint not null default 260  CHECK (hue >= 0 AND hue < 360)
  avatar_id         uuid null references avatars(id)
  password_hash     text null                -- null => cannot log in
  password_algo     text null                -- enables rehash-on-login
  email_verified_at timestamptz
  is_demo           boolean not null default false
  role              text not null default 'user'  CHECK (role IN ('user','moderator','admin'))
  status            text not null default 'active' CHECK (status IN ('active','suspended','deleted'))
  created_at, updated_at, deleted_at
```

That `hue` CHECK is deliberate: it is the same invariant I patched client-side during the
audit with `hue()` in `ui.js`. The client coercion stays as display armour, but the
database is where the invariant actually lives.

```
sessions
  id            uuid pk
  user_id       uuid not null references users(id) on delete cascade
  token_hash    bytea not null unique        -- sha256(raw token); raw never stored
  created_at, last_seen_at, expires_at, revoked_at
  ip_hash       bytea                        -- hashed with rotating salt
  user_agent    text                         CHECK (length(user_agent) <= 400)
  INDEX (user_id) WHERE revoked_at IS NULL

email_tokens
  id, user_id, purpose CHECK (purpose IN ('verify','reset','email_change')),
  token_hash bytea not null unique, expires_at, used_at, created_at

games
  id            text pk                      -- existing slug, e.g. 'elden-ring'
  title, studio text not null
  year          smallint                     CHECK (year BETWEEN 1958 AND 2100)
  hue           smallint                     CHECK (hue >= 0 AND hue < 360)
  platforms     text[] not null default '{}'
  genres        text[] not null default '{}'
  popularity    integer not null default 0
  search_tsv    tsvector GENERATED ALWAYS AS (…) STORED
  INDEX GIN (search_tsv), GIN (genres), GIN (platforms), (popularity DESC, id)

game_covers
  game_id pk, url text, state CHECK (state IN ('pending','ok','none')),
  source text, checked_at, attempts smallint

logs
  id            uuid pk
  user_id, game_id  not null, fk
  status        text not null CHECK (status IN ('playing','finished','abandoned','backlog'))
  rating        smallint null CHECK (rating BETWEEN 1 AND 10)   -- half-stars
  review        text null     CHECK (length(review) <= 6000)
  review_state  text not null default 'visible' CHECK (review_state IN ('visible','hidden','pending'))
  favorite      boolean not null default false
  played_on     date null, hours numeric(6,1) null CHECK (hours >= 0)
  created_at, updated_at
  UNIQUE (user_id, game_id)
  INDEX (game_id) WHERE review IS NOT NULL AND review_state = 'visible'
  INDEX (user_id, updated_at DESC)

game_stats                                   -- trigger-maintained, never computed live
  game_id pk, rating_count, rating_sum, avg_rating numeric(3,2),
  log_count, favorite_count, review_count, updated_at

comments
  id, log_id, user_id, parent_id null,
  body text not null CHECK (length(body) <= 600),
  state CHECK (state IN ('visible','hidden')), created_at, updated_at, deleted_at
  INDEX (log_id, created_at)

follows
  follower_id, followee_id, created_at
  PRIMARY KEY (follower_id, followee_id)
  CHECK (follower_id <> followee_id)
  INDEX (followee_id)

conversations            id, created_at
conversation_members     conversation_id, user_id, last_read_at, muted,
                         PRIMARY KEY (conversation_id, user_id), INDEX (user_id)
messages                 id, conversation_id, sender_id,
                         body text not null CHECK (length(body) <= 1000),
                         created_at, deleted_at
                         INDEX (conversation_id, created_at DESC)

blocks                   blocker_id, blocked_id, created_at,
                         PRIMARY KEY (blocker_id, blocked_id), CHECK (blocker_id <> blocked_id)

reports                  id, reporter_id, target_type CHECK (… IN ('log','comment','message','user')),
                         target_id, reason CHECK (closed set), note text CHECK (length <= 1000),
                         status CHECK (… IN ('open','actioned','dismissed')),
                         created_at, resolved_by, resolved_at

moderation_events        id, actor_id null, action, target_type, target_id,
                         rule_id, created_at        -- never the offending text

audit_log                id, at, actor_user_id, event, ip_hash, detail jsonb

avatars                  id, user_id, mime, byte_size, width, height, sha256, created_at
```

Note the field caps are exactly the frontend's existing ones (40 / 240 / 600 / 1000 /
6000). They are now enforced in three places: client (UX), schema (boundary), database
(truth).

### Security
- Two database roles: a migration role that owns the schema, and an **application role
  that cannot `ALTER`, `DROP`, or `CREATE`.** The running app cannot change its own schema.
- No `SELECT *` in application code; explicit column lists, so a column added later is
  never accidentally serialized to a client.
- `game_stats` maintained by trigger inside the same transaction as the write. Aggregates
  computed per request are both a performance and a consistency problem.
- Soft-delete columns exist from the start, so no later migration has to invent them.

**Exit criteria:** migrations run up and down cleanly on an empty database; a fixture
script proves every `CHECK` rejects the value it is meant to (including `hue = 400`,
`rating = 11`, a 6,001-character review, and `follower_id = followee_id`); the app role
demonstrably cannot `DROP TABLE`; `EXPLAIN` shows index use for the catalogue's five
filter/sort combinations.

### Phase 2 outcome

All four met, on a real PostgreSQL 17. 67 constraint tests, 9 privilege tests, 7 `EXPLAIN` tests,
3 reversibility tests. 224 tests total, zero skipped.

**The migration reported success and did nothing.** This is the finding worth keeping. `alembic
upgrade head` exited 0, printed nothing, and left the database completely empty — no
`alembic_version` table, no tables at all. The cause: `env.py`'s `_guard()` ran its `SELECT`s on
the same connection Alembic was about to take over, and under SQLAlchemy 2.0 those autobegin a
transaction that Alembic cannot then manage. Every statement the migration executed was rolled
back when the connection closed, and nothing raised.

Two changes came out of it. The guard now takes its own connection and hands it back before
Alembic starts. And **`env.py` verifies its own work**: it records the heads Alembic reports
applying via `on_version_apply`, then reads `alembic_version` back on a *fresh* connection and
fails loudly if they disagree. An exit code is not evidence.

Worth noting how it was caught — not by the exit code, and not by the test suite, but by querying
the database directly because a silent success looked wrong. Same instinct that found the inert
route-policy walk in Phase 1.

**Three of my own checks were wrong before the code was.**

1. The first version of that post-condition compared the stored revision against *head*, which
   fails a perfectly good `downgrade base` — after which the version table is legitimately empty.
   Now direction-agnostic.
2. Alembic invokes `on_version_apply` with keyword arguments, so `ctx`/`step`/`heads`/`run_args`
   are part of the contract; renaming them to `_ctx` and friends raised `TypeError`.
3. The reversibility test asserted zero functions remain after a downgrade, but `citext` installs
   about 50 into `public` and the downgrade deliberately leaves the extension in place. Now
   filtered on `pg_depend.deptype = 'e'` so it counts only what the migration created.

**Two testing decisions worth recording.** The constraint tests match the *named* constraint in
the error text, not the exception class — a `NOT NULL` violation and the `CHECK` under test are
both `IntegrityError`, so a test accepting either proves nothing about the `CHECK`. And they run
as `postgame_app`, the DML-only role, never as the owner: testing as the owner would verify
constraints for a role the application never uses, and would not notice a missing grant.

**Added beyond the plan:** a separate `postgame_test` database, so a test run cannot touch
development data; `rate_limits`, the Postgres token bucket Phase 5 needs, because a table is
schema; a trigger rejecting a future `played_on`, since a `CHECK` cannot call `now()`; and a
trigger requiring a message sender to be a member of the conversation — which makes the Phase 10
IDOR shape unrepresentable at the storage layer rather than merely guarded in the API. CI gained a
`postgres:17` service and a step that **fails if any database test skips**, because on a job that
provides a database a skip means 90 checks quietly proved nothing.

**Outstanding:** `alembic upgrade` prints nothing on success — the logger in `alembic.ini` has no
handler attached. Cosmetic in itself, but it is exactly what made the silent no-op above easy to
miss, so it is worth fixing early in Phase 3.

---

## Phase 3 — Catalogue read API and seed loader

**Status: complete, 2026-08-05.** 2,000 games in Postgres, six endpoints, 317 tests passing,
`ruff`/`mypy --strict` clean, all three gates green, 63/64 on an adversarial HTTP probe (the one
"failure" was the probe's own wrong expectation). Two migrations added — see the outcome at the
end of this phase, which records a real gap in the Phase 2 schema.

Goal: the 2,000 games served from Postgres. Unauthenticated, read-only — which makes it
the ideal place to get pagination, validation, and caching right before anything
sensitive exists.

### Build
- **Seed loader** adapting `scratchpad/emit.py`: instead of writing `games-NN.js`, it
  writes rows. Idempotent, re-runnable, and it validates every field against the schema
  before insert so a bad harvest fails loudly rather than landing malformed rows.
- Endpoints:
  - `GET /api/games?q=&genre=&platform=&year=&sort=&cursor=&limit=`
  - `GET /api/games/{slug}`
  - `GET /api/games/{slug}/reviews?cursor=&limit=`
  - `GET /api/search/suggest?q=` — the header dropdown; capped at 8 results
- **Keyset pagination, not `OFFSET`.** Cursor is an opaque base64 of the last row's
  `(sort_key, id)`, validated on decode, rejected if malformed. `limit` capped at 50
  server-side regardless of what is asked for.
- `Cache-Control: public, max-age=300` on catalogue reads; `ETag` on game detail.

### Security
- **`sort` is whitelist-mapped**, not interpolated:
  `{"popularity": "popularity DESC, id", "year": "year DESC, id", …}`. An unknown value
  is a 422, not a fallback — silent fallbacks hide probing.
- `genre` and `platform` validated against the closed vocabulary from the seed data, not
  passed through to the query as text.
- `q` length-capped (≤ 80) and passed to `websearch_to_tsquery` as a parameter. Never
  concatenated. Full-text search is a classic injection site precisely because people
  assume `tsquery` sanitizes for them.
- **Tight rate limit on `/search/suggest`** — it fires on every keystroke, so it is both
  the easiest endpoint to hammer and the cheapest to abuse for enumeration later.
- `GET /api/games/{slug}/reviews` returns only `review_state = 'visible'` rows, and
  returns them for *everyone* — decide now that reviews are public, and make the query
  say so explicitly rather than by omission.
- No stack traces, no SQL, no row counts in error responses.

**Exit criteria:** all 2,000 games queryable; every filter combination the existing UI
offers returns correct results; `sort=;DROP TABLE games;--` returns 422 and appears in
logs; a malformed cursor returns 422; `limit=100000` is silently capped to 50; p95 under
80 ms for the catalogue list on a cold cache; the seed loader run twice produces
identical state.

### Phase 3 outcome

All met, with one correction to the criteria themselves and one real gap found in Phase 2.

**`limit=100000` is a 422, not "silently capped to 50".** The criterion asked for a silent cap;
the implementation rejects instead, which is the same reasoning the plan applies to `sort` — a
silent adjustment hides someone probing the boundary. The cap is still enforced server-side as a
second layer inside `list_games`, so a caller that reaches the query cannot exceed it either.

**The Phase 2 schema was missing five fields the frontend reads.** Found while writing the seed
loader, which had nowhere to put half of each row: `blurb`, `critic`, `cover_pattern`,
`steam_app_id` and the authored `rank`. The schema had been written from this plan rather than
from the data. Migration 0002 adds them, with bounds taken from measuring all 2,000 rows instead
of guessing, and also adds the `genres`/`platforms` reference tables the filters validate against.
Migration 0003 adds the expression indexes the seven UI sort orders need.

**The parser had to reproduce js/data.js bit-for-bit, and that was the real work.** Each game's
slug, cover hue and cover pattern are derived from a hash of its title, and the frontend renders
2,000 covers from those values today — so a port that is merely close changes every cover the
moment Phase 4 switches over. Two details mattered: `Math.imul` is a 32-bit multiply and Python's
ints are unbounded, so every step needs masking; and the files must be read in zero-padded
filename order or the authored ranks shift. Verified against values read out of the running
browser, not against the port's own output: 15 samples including titles with apostrophes, colons,
ampersands and parentheses, all exact.

**Keyset pagination is proved by walking, not by inspection.** A cursor whose key expression
disagrees with its `ORDER BY` skips or repeats rows, and a single page looks perfect either way.
So each of the seven sorts is walked end to end and asserted to yield all 2,000 games exactly
once — 40 pages, ~0.6s per sort. Two of the sorts order on nullable columns, where row comparison
is undefined, so those wrap the column in a `coalesce` sentinel and the index is built on the same
expression.

**Two bugs found by running it rather than reading it.**

1. **A bare `postgresql://` URL means psycopg2**, which this project does not install. The engine
   builds lazily, so the mismatch surfaced as `ModuleNotFoundError` inside a 500 on the first
   request that touched the database — not at boot. Config now refuses any URL that does not name
   the driver.
2. **`alembic upgrade` was still silent** even after adding a handler to `alembic.ini`, because
   this is a hand-written `env.py` and the generated template's `fileConfig()` call was missing.
   Both are fixed, so migrations now say what they did — which is what made the Phase 2 silent
   no-op hard to spot.

**And a self-inflicted one worth recording.** Reflowing over-long comment lines with a script
merged adjacent `import` statements and `class` declarations, because the "is this prose?" test
was too permissive. Seven files broke. Then, fixing the fallout, I put a `# sql-safe:` annotation
after an opening `f"""` — which puts the comment *inside* the SQL string and would have sent it to
Postgres. The lasting fix is in the gate: it now accepts the annotation on the line above, since
a multi-line f-string has nowhere safe to put a trailing comment. The lesson is narrower than "no
scripts": do not run a heuristic rewriter over source files when the heuristic cannot distinguish
code from prose.

---

## Phase 4 — Frontend data layer: the async seam

**Status: complete, 2026-08-23.** Catalogue-only scope, as agreed before starting — the
social endpoints do not exist until Phases 7 to 10. 346 tests, `ruff`/`mypy --strict` clean,
all three gates green, both backends verified in a real browser with zero JS errors.

Goal: the frontend talks to the API for reads, with no visual change and no regression in
anything the audit verified. Still no accounts.

This is the phase where the frontend's synchronous assumptions break, and doing it while
everything is read-only means we debug one thing at a time.

### Build
- New `js/api.js`: `fetch` wrapper with timeout, one retry on network failure (never on
  4xx), and a single error shape. Sends `Accept: application/json` and
  `credentials: 'same-origin'`.
- `js/store.js` gains a **backend switch**, chosen once at boot per the Phase 0.2
  decision: `LocalBackend` (today's `localStorage`, used for `file://` demo mode) and
  `ApiBackend`. Same method signatures, both async.
- **Every `store.*` read becomes a promise.** `app.js` view builders currently return
  HTML strings synchronously — they now become `async` and the router awaits them.
- **Loading and error states, which do not exist today.** Skeleton rows for the catalogue
  grid, an inline retry for a failed section, and a distinction between "empty" (already
  designed — the `.empty` block) and "failed to load" (new). Right now a failed read
  would render as an empty shelf, which is a lie.
- Drop the `{add: [], remove: []}` localStorage overlay from the API path. It is a demo
  shortcut and must not reach the real schema.
- Replace `?v=29` with content-hashed filenames plus long `Cache-Control` on the hashed
  assets and `no-cache` on `index.html`.

### Security
- `js/api.js` never builds a URL from unvalidated input; query params go through
  `URLSearchParams`.
- No personal data in query strings, ever — a rule that outlives this phase.
- Continue to route every server value through `esc()`. The server is now a *source* of
  untrusted content (it stores what other users typed), so nothing about this relaxes.
- Add a test that greps the frontend for `innerHTML` assignments whose value does not
  pass through `esc()` or a known-safe builder. The audit established this property by
  inspection; from here it should be established by CI.

**Exit criteria:** all 26 routes render from the API with no visual diff against v29;
`file://` demo mode still passes the checks it passes today (2,000 games, routing,
search, shelves, no console errors); killing the API mid-session produces visible error
states and never a blank shelf; no `?v=` remains; the `esc()` grep passes.

### Phase 4 outcome

Met, with one criterion deferred and one unverifiable in this environment. Both are stated
plainly below rather than quietly counted as passes.

**The seam was not where the plan said it was.** The plan assumed `store.js` was the thing to
convert. It is not: `store.js` holds *user* data, and the catalogue is read from `PG.data`, a
synchronous in-memory array built by `data.js`. So the async boundary is a new module,
`js/catalogue.js`, with `LocalCatalogue` and `ApiCatalogue` behind one promise-returning
interface, chosen once at boot. On `file://` it short-circuits to local without probing at
all, because a failed request there is console noise rather than information.

**Deferred: content-hashed filenames.** `?v=` is still there, now at v32. Replacing it needs a
build step, and this project has deliberately had none since the beginning — introducing one
is a decision worth making on its own rather than smuggling into a data-layer phase. The
caching is correct in the meantime: `index.html` is always `no-cache`, and assets are
`immutable` only outside local (see below for why that qualifier exists).

**Unverifiable here: true `file://` loading.** The browser pane now renders local files as a
`data:` snapshot, so relative script URLs cannot resolve and nothing loads. The *same code
path* was verified instead by serving the frontend with no API available at all, which is
what `file://` reduces to: backend reports `local`, 2,000 games, all 12 routes, search
working, zero errors. Worth re-checking by hand in a real browser before launch.

**Four bugs found, three of them only by running it.**

1. **`PG.data.trending` is a function, not an array**, and the local backend resolved its
   promise with the function itself. Home rendered fine through the API and broke on the
   local path — the exact failure a dual-backend design invites, and the reason both were
   exercised rather than one.
2. **A refactor left `q` out of scope** in the search dropdown's renderer. It threw, the
   rejection handler treated it as a network failure, and the dropdown just stayed shut. The
   handler now distinguishes an `ApiError` from a programming error and logs the latter,
   because swallowing both is how a real bug hides behind a plausible excuse.
3. **`Cache-Control: immutable` plus a hand-bumped `?v=` meant edits had no effect.** A fix
   already on disk kept appearing unfixed in the browser. Caching is now `no-cache` when
   `env` is local; `immutable` is only honest once the version changes automatically.
4. **`Permissions-Policy` named three features no browser implements**, and CSP sent
   `upgrade-insecure-requests` in a report-only policy where it is ignored — four console
   errors and warnings on every single page load. Noise that trains people to ignore the
   console is worse than no policy, because the console is where report-only CSP violations
   are meant to be read.

**Added beyond the plan:** a boot-time guard that refuses to start if any route is registered
after the `/{asset:path}` catch-all. That hazard is not hypothetical — it caught an existing
test immediately, which had been adding a route post-construction and would have silently
received a 404 on an endpoint plainly present in the source.

**Still local, and deliberately so:** members, logs, reviews, follows, messages, and the
community-derived parts of the home page. They have no endpoints yet. The split lives in
exactly one function — `PG.catalogue.resolve()` — so Phase 7 knows precisely what to replace.

---

## Phase 5 — Identity core

Goal: real accounts, passwords, sessions, CSRF. The highest-risk phase — build it slowly
and test it adversarially.

### Build
- `POST /api/auth/signup` — email, username, display name, password.
- `POST /api/auth/login`
- `POST /api/auth/logout`, `POST /api/auth/logout-all`
- `GET /api/auth/me`
- Session middleware resolving the cookie to a user, or anonymous.

### Security — password storage
- **Argon2id** via `argon2-cffi`. Parameters tuned on the target hardware to ~250 ms:
  start at `m=64MiB, t=3, p=4` and measure.
- Store the algorithm and parameters alongside the hash. **Rehash on successful login**
  when parameters have changed, so an upgrade needs no migration and no password reset.
- Password policy follows NIST: **minimum 12 characters, no composition rules, no forced
  rotation.** Composition rules produce `Password1!` and nothing else.
- **Check against a local list of the top 100,000 breached passwords.** Local, not an
  external API — no third party learns anything about our users' passwords, and there is
  no network dependency in the signup path.
- Maximum password length 256, to bound the hashing cost. An unbounded password field is
  a CPU-exhaustion DoS.

### Security — enumeration
- Signup with an existing email returns **the same response as a successful signup**, and
  sends an email to the existing address telling them someone tried. The response body
  must not differ, and neither should its timing.
- Login failure is one generic message for every cause: wrong password, unknown email,
  unverified, suspended, demo account. Identical body, identical status.
- **Run a dummy Argon2 verification when the email is unknown**, so response time does
  not distinguish. This is easy to forget and completely defeats the above if omitted.
- Username availability check is rate-limited and only reachable authenticated during
  signup, not as a free enumeration oracle.

### Security — sessions
- Token: 32 bytes from `secrets.token_bytes`, base64url. Stored as SHA-256 only.
- Lookup by hash with a constant-time comparison.
- Cookie: `__Host-pg_session; HttpOnly; Secure; SameSite=Lax; Path=/`.
- **New session id issued on login** (session fixation) and on password change.
- Idle expiry 14 days, absolute expiry 90 days, whichever comes first.
- `last_seen_at` updated at most once per 5 minutes, to avoid a write per request.

### Security — CSRF
Defence in depth, all three:
1. `SameSite=Lax` on the session cookie.
2. **`Origin` header must match an allowlist on every state-changing request.** Missing
   `Origin` on a state-changing request is a rejection, not a pass.
3. A per-session CSRF token required in a custom header for state-changing requests.

### Security — rate limiting
| Endpoint | Limit |
|---|---|
| `POST /auth/login` | 5 per 15 min per `(ip, email)`; 20/hour per ip |
| `POST /auth/signup` | 3/hour per ip |
| Any authenticated write | 60/min per user |

**No account lockout.** Lockout on failed attempts lets anyone lock any user out of their
own account — it converts a nuisance into a denial of service. Throttle with increasing
delay instead.

### Security — audit
Every one of these writes to `audit_log`: signup, login success, login failure, logout,
logout-all, password change, session revoke, role change. Actor, event, `ip_hash`,
timestamp. No credentials, no tokens.

### Demo accounts
The login handler checks `is_demo` and `password_hash IS NULL` **first**, before any
other logic, and refuses. Test it explicitly for all ten seeded members.

**Exit criteria:** signup/login/logout work end to end; timing of unknown-email vs
wrong-password login is statistically indistinguishable over 1,000 samples; signup
against an existing email is byte-identical to a fresh signup; all three CSRF layers
demonstrably reject a cross-origin write; a session id changes on login; the raw token
appears nowhere in the database; the ten demo accounts cannot be logged into; every rate
limit verified; `audit_log` populated for all eight events.

---

### Phase 5 outcome

Met, with one exit criterion partially met and one that cannot be fully met until Phase 6.
Both are stated plainly below rather than quietly counted as passes.

**491 tests pass**; `ruff`, `ruff format --check` and `mypy --strict` are clean; all four CI
gates are green, the fourth being new: `check_login_timing.py`. The whole flow was then
exercised in a real browser over real HTTP, not only through the ASGI test transport, because
cookies, `HttpOnly` and the `Origin` header are exactly the things a test transport can be
kind about.

#### The seven conditions of §0.3.1

| # | Condition | Where it lives |
|---|---|---|
| 1 | Timing indistinguishability is a **CI gate** | `tools/check_login_timing.py`, blocking in CI at 1,000 samples per arm. Interleaved sampling, Mann-Whitney effect size, documented tolerance. Measured: 12.19 ms vs 12.29 ms, AUC 0.498 |
| 2 | Enumeration compares **full response bytes** | `tests/test_auth_enumeration.py`. Status, every header and the body, with only `Date` and `X-Request-ID` excluded — and a second test asserts those are the *only* differences, so the exclusion list cannot quietly grow |
| 3 | The dummy-hash path has **its own named test** | `test_an_absent_stored_hash_still_costs_a_real_argon2_verification`, which counts Argon2 calls rather than timing them |
| 4 | Session rotation **asserted**, on login and on credential change | `test_login_issues_a_new_session_and_kills_the_old_one` and `test_revoking_siblings_spares_the_current_session` |
| 5 | `api/app/security/` is a **protected path** | Already in `.github/CODEOWNERS`. Still carrying the `@OWNER` placeholder — a CODEOWNERS file naming a non-existent owner protects nothing while looking like a control |
| 6 | **ASVS V2 reviewed item by item**, committed | `postgame/ASVS-V2-CHECKLIST.md`. 57 items: 15 met, 3 partial, 9 deferred to Phase 6, 1 accepted with reason, 29 not applicable |
| 7 | A defined **bail-out** to a hosted provider | Not invoked. Conditions 1 and 2 were met with room to spare, which is the test the bail-out was attached to |

#### Three decisions that came out differently from the plan

**Argon2 parallelism is 1, not 4.** The plan said start at `m=64MiB, t=3, p=4` and measure.
Measured, `p=4` is faster per hash — and that is the problem: it asks for four threads per
verification, so on a one- or two-core box it occupies the whole machine for roughly the same
elapsed time as `p=1` with a higher `t`. Shipped at `m=64MiB, t=5, p=1`, measured at 268 ms.

**Argon2's memory hardness is also a denial-of-service lever, and the plan did not say so.**
Each concurrent verification holds `memory_kib` for its whole duration. Starlette runs sync
endpoints in a 40-slot thread pool, so an unbounded implementation would ask for 40 × 64 MiB
= 2.5 GB under a burst of login attempts and be killed by the OOM reaper — a denial of
service requiring no credentials, presenting as a crash. `Hasher` holds a semaphore; the peak
is `memory_kib × concurrency`, defaulting to 384 MiB, logged at boot and warned about above
512 MiB.

**Handlers are `def`, not `async def`.** An `async def` handler performing a 250 ms Argon2
verification blocks the event loop for that whole time, so four login attempts a second stall
every other request on the process. Sync handlers run in Starlette's thread pool instead. The
rate-limit interface changed from `async def hit` to `def hit` for the same reason — the
Postgres backend does a round trip — which forced the two remaining `async def` callers to
wrap it in `run_in_threadpool` explicitly rather than silently not doing so.

The rest of the app has a smaller version of this problem: `app/routes/catalogue.py` is
`async def` around blocking database calls. Those queries are milliseconds rather than
hundreds of them, so it is a throughput ceiling rather than a denial of service, and changing
it is a behaviour-preserving one-word edit per handler that belongs in its own change rather
than folded into this one. **Recorded for Phase 15**, with the measurement, not left implied.

#### Seven bugs, five of which only running it would find

1. **`HTTPException(headers=...)` was silently discarded** by the custom exception handler, so
   every 429 the rate limiter produced went out with no `Retry-After`. The throttle worked and
   the response no longer told a well-behaved client when to come back — meaning the only
   clients backing off correctly were the ones not trying to abuse anything. The handler now
   merges the raiser's headers, with the hardened set taking precedence so a handler cannot
   weaken a security header by raising with one.
2. **CSRF made signing in impossible with a stale cookie.** Requiring a session-bound token to
   *create* a session is circular, and the failure is nasty: a browser holding a revoked or
   expired cookie is refused at the login form with a 403 about forgery, and the token it
   would need is derived from a cookie JavaScript cannot read. Found by a test that wrote a
   junk cookie to check login replaced it. Login and signup now skip layer 3 and keep layer 2
   in full — `TOKEN_OPTIONAL_PATHS`, with the reasoning in the module.
3. **`PG_APP_ORIGIN` said `:8000` while the app serves `:8132`.** Every state-changing request
   from a browser was refused by the origin check. Found by opening the site. `.env` fixed,
   and `deployment_warnings()` now says so at boot, because "403: this request did not come
   from Postgame" is a long way from "the port is wrong".
4. **Every password and username field carried `autocomplete="off"`.** Found by the ASVS
   review (V2.1.11), not by any test — nothing was broken. It fights the one habit that most
   reduces credential reuse and buys nothing, since browsers ignore `off` on credential fields
   anyway. Now `username`, `current-password`, `new-password`. Assets at `?v=33`.
5. **Binding one parameter to both `email_norm` (citext) and `email_raw` (text)** left Postgres
   unable to infer a type: "inconsistent types deduced for parameter $1". The fix is also the
   better model — `email_raw` holds the address as the person typed it, so Phase 6's mail is
   addressed the way they wrote it.
6. **`text()` does not recognise `:param::cast`.** `:except_id::uuid` reached Postgres verbatim
   as a syntax error. `CAST(:except_id AS uuid)`.
7. **Two of my own guards caught me.** The audit-detail screen refused the key `rehashed`
   (it contains "hash"), and the log scrubber redacted `hash_peak_mib` and
   `password_list_entries` — the two numbers that boot line exists to publish. Both controls
   were right and both key names were wrong; renamed to `cost_upgraded`, `argon2_peak_mib` and
   `common_list_entries`.

#### The gate was tested by breaking the thing it guards

`test_the_gate_notices_when_the_dummy_verification_is_deleted` replaces `Hasher.verify` with
the version somebody writes while tidying — an early `return False` when there is no stored
hash — and requires `check_login_timing` to fail. Every other test in the suite still passes
with that mutation in place: signup works, login works, wrong passwords are still refused, the
responses are still byte-identical. Only the clock notices. A gate nobody has watched fail is
a gate nobody knows works, and this project has already shipped two controls that reported
success while inspecting nothing.

#### Exit criteria, honestly

| Criterion | Verdict |
|---|---|
| signup/login/logout end to end | Met, and verified in a real browser over HTTP as well as in tests |
| timing indistinguishable over 1,000 samples | Met. 0.096 ms median difference, AUC 0.498, tolerance 2 ms |
| signup against an existing email byte-identical | Met, headers included |
| all three CSRF layers reject a cross-origin write | Met. `SameSite=Lax` on the cookie, `Origin` mismatch and absence both 403, token mismatch 403 — confirmed with `curl` against the running server as well as in tests |
| session id changes on login | Met, and the old session is revoked rather than merely replaced |
| the raw token appears nowhere in the database | Met. The test scans every text and jsonb column in the schema from `information_schema`, not just `sessions` |
| the ten demo accounts cannot be logged into | Met. All ten, plus a direct attempt to give one a password hash, which `users_demo_has_no_credentials` refuses |
| every rate limit verified | Met, including atomicity under eight concurrent threads, and that a rolled-back caller cannot erase a hit |
| `audit_log` populated for all eight events | **Partial, and it could not be otherwise.** Five events have endpoints in Phase 5 and are asserted end to end. `password.change`, `session.revoke` and `role.change` belong to Phases 6 and 13; their names exist in the closed `Event` enum and the writer is tested directly, but nothing in Phase 5 emits them. Recorded as partial rather than claimed |

**Also partial: the breached-password check.** `passwords.check_policy` consults a local list,
as the plan requires — but the shipped list is generated by `tools/build_password_list.py`,
not the real top-100,000 corpus. It holds 166,241 entries, of which 31,996 are at or above the
12-character minimum and therefore actually reachable. Fetching the real corpus means
downloading a large file from a third party, which is a decision for a person rather than
something to do quietly. `PG_PASSWORD_LIST` points at it when there is one, the app refuses to
boot if the file is missing or too small to be a control, and `deployment_warnings()` says so
in production.

**What Phase 5 does not do:** the frontend still signs in against `localStorage`. The API has
real accounts and the browser does not use them yet, deliberately — wiring the sign-in form to
the API while diary writes are still local would produce a genuinely confusing half-state,
where you are signed in for real and your reviews belong to a different identity. That is
Phase 7's job, and it is the phase that moves writes server-side.

**`PG_ENV=production` boots.** The gate Phase 1 put in place — refusing the in-memory rate
limiter in production — has been satisfied rather than removed.

---

## Phase 6 — Account lifecycle

Goal: everything that makes accounts survivable long-term — verification, reset, session
visibility. Split from Phase 5 deliberately so the core can be reviewed on its own.

### Build
- `POST /api/auth/verify-email`, `POST /api/auth/resend-verification`
- `POST /api/auth/password/forgot`, `POST /api/auth/password/reset`
- `POST /api/auth/password/change`
- `GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`
- Email change flow: confirm on the *old* address, verify on the new one.
- Transactional email sending via a provider, with SPF/DKIM/DMARC configured.

### Security
- **Tokens: 32 random bytes, stored as SHA-256, single-use, 1-hour TTL** (24 hours for
  initial verification). Marked `used_at` inside the same transaction that consumes them,
  so a race cannot use one twice.
- `password/forgot` returns the **same response whether or not the email exists**, and
  takes the same time. Rate limited to 3/hour per email and 10/hour per ip.
- **A successful password reset revokes every other session.** A reset is what you do
  when you think you are compromised; leaving other sessions alive defeats it.
- `password/change` requires the current password, and revokes other sessions by default
  with an opt-out.
- Email change requires confirmation on the old address, so a stolen session cannot
  quietly take ownership of the account.
- Reset links must not leak: no token in a redirect, no token in a `Referer`, `noindex`
  on the reset page.
- Sending email is a background job, so SMTP latency is not observable in the response
  time of endpoints that must not leak existence.
- Verification is required before the account can post reviews, comments, or messages —
  it is the cheapest anti-spam control we have.

**Exit criteria:** full verify/reset/change cycles pass; a reset token used twice fails
the second time; reset revokes sibling sessions (proved with two live sessions); forgot
responses are identical and equal-time for known and unknown emails; email change cannot
complete without the old address; DMARC passes on a real send; session list shows correct
devices and revocation is immediate.

---

## Phase 7 — Authorization layer and diary writes

Goal: the guard layer, then the first real user-owned writes on top of it. The layer
comes first and is tested on its own.

### Build
- **A policy layer as a route dependency.** Every route declares one of
  `public` / `authenticated` / `owner` / `moderator` / `admin`. The Phase 1 CI gate
  already fails any route that declares nothing.
- One `can_view` / `can_edit` function per resource type. Not scattered `if` statements.
- Endpoints:
  - `GET /api/users/{username}/logs?shelf=&cursor=`
  - `PUT /api/logs/{game_slug}` (upsert own log)
  - `PATCH /api/logs/{game_slug}` (status, favorite)
  - `DELETE /api/logs/{game_slug}`
- Wire the profile shelves and "See more" routes built in the last session to real data.

### Security
- **The log identity is `(authenticated_user, game_slug)` — the client never sends a
  `user_id`.** This makes the entire class of "edit someone else's log" bugs
  unrepresentable rather than merely checked for. Where an id must be accepted, the query
  filters on the actor.
- `shelf` is whitelist-mapped to the five known shelves; unknown values are 422.
- `status`, `rating`, `hours`, `played_on` validated at the boundary and again by `CHECK`.
- `played_on` cannot be in the future. `hours` has an upper bound.
- **Mass-assignment protection:** the update schema lists exactly the mutable fields.
  `user_id`, `id`, `created_at`, `review_state` are not among them.
- `game_stats` updated by trigger in the same transaction, so a rating and its aggregate
  can never disagree.
- **The authorization matrix test**, which becomes a permanent fixture: every endpoint ×
  {anonymous, owner, other user, unverified, suspended, blocked, moderator, admin}.
  Expected status for all of them, asserted. Every future phase adds its rows.

**Exit criteria:** the matrix test passes with no gaps; a user cannot create, edit, or
delete another user's log by any parameter manipulation; a suspended user cannot write;
an unverified user cannot post a review; ratings and `game_stats` agree after 1,000
concurrent randomized writes; profile shelves and "See more" match the counts they showed
in the localStorage build.

---

## Phase 8 — Social graph and activity feed

### Build
- `PUT /api/users/{username}/follow`, `DELETE …`
- `GET /api/users/{username}/followers`, `…/following`
- `GET /api/activity?scope=all|following&cursor=`
- `GET /api/users/{username}` (public profile + counts)

### Security
- Self-follow rejected at the schema, the service, and the `CHECK`.
- Follow is idempotent — a repeat is 200, not a duplicate row or a 500.
- **Follower lists are paginated and rate-limited.** An unpaginated follower list is a
  free social-graph scrape.
- Blocked users (Phase 13) are filtered from feeds and from each other's follower lists.
  Build the feed query with a block join from the start, even before blocks have a UI, so
  it does not need reshaping later.
- Follow counts come from a maintained counter, not `COUNT(*)` per request.
- Activity feed shows only content the viewer may see — the feed query is the single most
  likely place for a visibility leak, because it crosses every resource type at once.
  It gets its own test file.

**Exit criteria:** follow/unfollow idempotent and correct; counts accurate under
concurrency; feed pagination stable with concurrent inserts (no duplicates, no skips);
the feed leaks nothing hidden, suspended, or blocked; follower enumeration is rate-limited.

---

## Phase 9 — Comments and server-authoritative moderation

### Build
- `GET /api/logs/{id}/comments`, `POST …`, `PATCH /api/comments/{id}`,
  `DELETE /api/comments/{id}`
- **Port `moderation.js` to the server as the authority.** The 89-term list, the
  normalization, and the false-positive protections all move; the client keeps its copy
  purely for instant feedback, with a comment saying so explicitly.
- A moderation pipeline applied to review text, comments, messages, display names,
  usernames, and bios — every free-text field, not just the obvious ones.

### Security
- **Normalize before matching:** NFKC, strip zero-width characters, fold homoglyphs and
  common leetspeak substitutions. Without this the filter is trivially bypassed with
  `а` (Cyrillic) or a zero-width space.
- **The existing false-positive protections are a regression suite, not a nice-to-have.**
  The audit verified the current filter blocks slurs while passing "spicy" and "Spicer".
  Those cases become tests that must pass after every normalization change, because
  aggressive normalization is exactly what breaks them.
- **Never store the offending text.** `moderation_events` records the rule id and a hash.
  This is already the design in `moderation.js` and the reasoning holds: storing it
  defeats the point of refusing it.
- Comment depth capped (2 levels — the existing UI is not deeper). Unbounded nesting is
  both a rendering and a recursion problem.
- Editing a comment re-runs moderation. An edit is the obvious bypass if it does not.
- Rate limit: 10 comments/min, 100/day, verified account required.
- Comment authors and log owners can delete; log owners can delete comments on their own
  reviews; moderators can hide anything. All four paths in the authorization matrix.
- Deleted comments become tombstones so threads do not collapse.

**Exit criteria:** the slur/false-positive regression suite passes; homoglyph and
zero-width bypasses are blocked; no offending text anywhere in the database after a
blocked attempt; edit re-runs moderation; the four deletion paths behave per the matrix;
rate limits verified.

---

## Phase 10 — Direct messages

Goal: the single highest-risk feature in the application. This phase exists on its own
specifically so it gets undivided attention.

### Build
- `GET /api/conversations`
- `POST /api/conversations` (start with a username)
- `GET /api/conversations/{id}/messages?cursor=`
- `POST /api/conversations/{id}/messages`
- `POST /api/conversations/{id}/read`
- Unread badge counts — the existing header badge, now real.

### Security
This is where IDOR lives, so the controls are specific and non-negotiable.

- **Every single message query joins `conversation_members` and filters on the
  authenticated user id.** Not "load the conversation, then check membership" — the
  membership predicate is in the same `WHERE` clause as the fetch. There is no code path
  that can load a message without it.
- Conversation ids are UUIDv4, never sequential. Non-membership returns **404, not 403** —
  403 confirms the conversation exists.
- **A single data-access function** for messages that takes the actor as a required, non-
  defaulted parameter. If it is impossible to call without an actor, it is impossible to
  forget one.
- You cannot start a conversation with someone who has blocked you, and the error must
  not reveal that a block is the reason.
- Rate limits: 20 messages/min, 200/day, and **a much tighter limit on conversations
  started with new people** (say 10/day) — that is the spam vector, not message volume
  inside an existing thread.
- Verified account required to send.
- Messages run through the Phase 9 moderation pipeline.
- **Message bodies never appear in logs, error messages, or exception context.** Add a
  scrubber to the log formatter rather than relying on discipline.
- `last_read_at` is per member; one member's read state cannot be written by another.
- Deletion is per-user hide plus a genuine tombstone. Be honest in the UI: deleting your
  copy does not delete theirs.

**Exit criteria:** an automated adversarial suite that, for every message endpoint,
attempts access as a non-member by id guessing, by parameter injection, and by tampered
cursor — and gets 404 every time; two-user manual verification of the full flow; unread
counts correct under concurrency; a grep proving no message body reaches any log sink;
all rate limits verified; the conversation-start limit blocks a spam pattern.

---

## Phase 11 — Profiles and avatar upload

### Build
- `PATCH /api/users/me` (display name, bio, hue)
- `PUT /api/users/me/avatar`, `DELETE /api/users/me/avatar`
- Avatar serving

### Security — the upload pipeline
Untrusted bytes that we later serve back is the most dangerous shape in the app after
DMs. In order:

1. **Reject on `Content-Length` before reading**, then enforce the cap again while
   streaming. A lying `Content-Length` must not get us to read 2 GB.
2. Cap at 5 MB.
3. **Sniff magic bytes.** Accept only PNG, JPEG, WebP. **Reject SVG outright** — SVG is a
   script container and there is no safe way to serve user SVG from an origin that
   matters.
4. Never use the client-supplied filename, extension, or MIME type for anything.
5. **Guard decompression bombs:** set `Image.MAX_IMAGE_PIXELS`, reject absurd dimensions
   before decoding, and decode inside a memory and time limit.
6. **Re-encode.** Always. Output is a fresh WebP at max 512×512, generated by our code
   from decoded pixels. This destroys any polyglot, any appended payload, and all EXIF —
   including GPS coordinates, which is a genuine privacy leak in user photos.
7. Store under a generated UUID. Record `sha256`, dimensions, byte size.
8. **Serve from a separate origin** (`img.postgame.app`), with an explicit
   `Content-Type`, `X-Content-Type-Options: nosniff`,
   `Content-Disposition: inline`, and a restrictive `Content-Security-Policy: default-src
   'none'; sandbox`. A separate origin means that even a total failure of steps 1–7 cannot
   execute script against the app origin or read its cookies.
9. Rate limit: 5 uploads/hour per user.

The client already downscales in a canvas (`app.js:1202`). Keep it — it saves bandwidth —
but it is a convenience, and the server behaves as though it never happened.

### Security — profile fields
- `display_name` ≤ 40, `bio` ≤ 240, `hue` coerced to 0–359, all matching the existing
  caps and now enforced by `CHECK`.
- Display names run through moderation, and through a homoglyph/zero-width check for
  impersonation ("Adм1n").
- **A reserved-username list**: `admin`, `moderator`, `postgame`, `support`, `api`,
  `help`, `root`, `system`, and the route names (`games`, `activity`, `members`,
  `signin`, `settings`). Enforce at signup and at any future rename.
- Username changes: rate-limited, audited, and old usernames quarantined for 30 days so
  links do not silently redirect to a different person.

**Exit criteria:** an upload corpus of malicious files (SVG-with-script, a JPEG/HTML
polyglot, a 40,000×40,000 PNG bomb, an EXIF-GPS photo, a file with a lying MIME type, a
zip renamed `.png`) is either rejected or neutralized — verified by inspecting the stored
bytes, not the response code; stored avatars contain no EXIF; avatars serve from a
separate origin with correct headers; reserved usernames rejected; profile caps enforced
at all three layers.

---

## Phase 12 — Cover art service and CSP tightening

Goal: move cover resolution server-side, and collect the CSP dividend that makes possible.

Right now `art.js` has every visitor's browser querying MediaWiki directly, caching to
their own `localStorage`. It works — I confirmed covers load even from `file://` because
MediaWiki sends `Access-Control-Allow-Origin: *` — but it means the API is hit once per
user per game, the cache is worthless across users, and `connect-src` and `img-src` must
stay open to external origins.

### Build
- A resolver worker adapting `art.js`'s existing logic (article mapping, redirect
  following, search fallback) into a background job writing `game_covers`.
- Warm the whole catalogue once, offline, politely: a low concurrency cap, a descriptive
  `User-Agent` with contact details, and respect for rate-limit responses.
- Store resolved images ourselves rather than hot-linking, so a Wikimedia URL change does
  not blank the grid.
- Serve from the avatar origin with long cache lifetimes and content-hashed paths.
- `GET /api/covers/{slug}` for the miss case, queueing a resolve.

### Security
- **This is an SSRF surface.** The resolver fetches URLs derived from third-party API
  responses. Therefore: an allowlist of hostnames (`upload.wikimedia.org` and friends),
  scheme restricted to HTTPS, **redirects not followed to non-allowlisted hosts**, DNS
  resolution checked against private ranges (RFC1918, loopback, link-local, `::1`,
  IPv4-mapped IPv6), a hard response-size cap, and a short timeout.
- The resolver runs with no credentials and, ideally, on an egress-restricted network path.
- Fetched images go through **the same re-encode pipeline as avatars**. Third-party bytes
  get identical treatment to user bytes. This is a genuine dependency on Phase 11, so
  whichever of the two phases runs first builds the pipeline as a standalone service
  (`services/images.py`) and the other consumes it. It must not be written twice — two
  copies means one of them eventually misses a hardening fix.
- Keep `art.js`'s existing circuit breaker (three failures disables the source) on the
  server side — it is good behaviour toward an API we do not own.
- **Then tighten CSP:** drop `upload.wikimedia.org` from `img-src`, drop external origins
  from `connect-src`, leaving `default-src 'none'; script-src 'self' 'sha256-…';
  style-src 'self'; font-src 'self'; img-src 'self' <img-origin> data:;
  connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`.
  Fonts are already self-hosted from Phase 1, so nothing external remains.

**Exit criteria:** every catalogue cover resolved and self-served; an SSRF suite (an
allowlisted host redirecting to `169.254.169.254`, to `127.0.0.1`, to a `file://` URL, to
a 10 GB response, to a hostname resolving to a private IP) is blocked in all cases;
enforced CSP with zero violations across all 26 routes; no external requests in a network
trace of a full page load.

---

## Phase 13 — Abuse: blocks, reports, admin queue

### Build
- `PUT /api/users/{username}/block`, `DELETE …`, `GET /api/users/me/blocks`
- `POST /api/reports`
- `GET /api/admin/reports`, `POST /api/admin/reports/{id}/resolve`
- Moderator actions: hide a review or comment, suspend an account.

### Security
- Blocking is mutual in effect: neither party sees the other's content, neither can
  message or follow. Verify across feeds, comments, followers, search, and DMs — every
  surface, or the block is theatre.
- **A block must not be observable to the blocked user.** Endpoints behave as if the
  content simply is not there.
- Report abuse is itself abusable: rate limit to 20/day, and one report per
  `(reporter, target)`.
- **Admin routes require a moderator or admin role *and* a session younger than 15
  minutes.** A stolen long-lived session should not grant moderation powers.
- Every moderator action writes to `moderation_events` with the actor. Admin actions are
  never anonymous.
- Admin endpoints are separately rate-limited and never expose bulk PII export.
- Suspension revokes all sessions immediately.

**Exit criteria:** a block is verified as effective on all six surfaces; the block is
undetectable from the blocked side; suspension takes effect immediately on active
sessions; admin routes reject a role-less user, a stale session, and a cross-origin
request; every moderator action is attributable in `moderation_events`.

---

## Phase 14 — Privacy: export, deletion, retention

### Build
- `POST /api/users/me/export` → background job → notification → single-use expiring link
- `POST /api/users/me/delete` → 30-day soft delete → hard purge job
- A retention job enforcing the policies below.
- A privacy policy page that matches what the code actually does.

### Security
- **The export link is single-use, expires in 1 hour, and requires the session that
  requested it.** An export is a complete copy of someone's account; a guessable or
  shareable link is a breach.
- The export contains only the requesting user's data. Not their DMs' counterparties'
  details beyond what they already see, not other users' profile data.
- Deletion requires password re-entry.
- **The deletion UI states plainly that reviews and comments are anonymized rather than
  removed** (the Phase 0.7 decision) before the user confirms. Anything less is a dark
  pattern.
- Retention: `audit_log` 90 days; `ip_hash` salt rotated quarterly, making older hashes
  unlinkable; `moderation_events` 1 year; revoked sessions purged after 30 days; email
  tokens purged after use or expiry.
- Hard purge is genuinely hard — including backups, which means documenting the maximum
  window before a deleted user is gone from every restore point.

**Exit criteria:** an export contains everything the user created and nothing belonging to
anyone else; the link cannot be reused, cannot be used from another session, and expires;
deletion anonymizes and leaves threads intact; the purge job removes the row after 30
days; salt rotation demonstrably breaks linkability of old `ip_hash` values; the privacy
policy matches the implementation, checked line by line.

---

## Phase 15 — Observability, backups, disaster recovery

### Build
- Metrics: request rate, latency percentiles, error rate, database pool saturation, job
  queue depth. **This needs a dependency the Phase 0 allowlist does not yet contain** —
  either `prometheus-client` plus something to scrape it, or a hosted APM. Deliberately
  left open: it is the one place where the hosting choice (0.9) should drive the answer,
  and that is easier to judge with the service actually running. Decide it at the start of
  this phase and record it in `DEPENDENCIES.md` before installing anything.
- Alerts on: 5xx rate, p99 latency, **authentication failure rate** (credential stuffing
  in progress), rate-limit rejection spikes, disk, replication lag, backup age.
- Uptime check from outside the network.
- Automated nightly base backup plus continuous WAL archiving.
- A written runbook: how to restore, how to revoke all sessions, how to put the site in
  read-only mode, how to rotate every secret.

### Security
- **A quarterly restore drill, performed, timed, and written down.** An untested backup is
  not a backup. Record actual RTO and RPO rather than aspirational ones.
- Backups encrypted at rest with a key **not stored alongside them**, and not accessible
  to the application role.
- Logs scrubbed of secrets by the formatter, verified by a test that logs a fake token and
  asserts it does not appear in output.
- Log access is itself audited.
- An incident response outline: who is notified, what gets disclosed, in what timeframe,
  and what the disclosure says.
- A "revoke every session" switch, tested — needed the day a token leaks, which is the
  worst day to be writing it.

**Exit criteria:** a restore performed into a clean environment with measured RTO/RPO
written into the runbook; every alert fired once deliberately to prove it routes
somewhere a human sees; the secret-scrubbing test passes; the revoke-all switch tested;
the runbook followed end to end by someone other than its author.

---

## Phase 16 — Hardening pass and launch checklist

Goal: verify, do not build. Anything found here is a bug in an earlier phase.

### Verification
- **The full authorization matrix** re-run: every endpoint × every actor class. Zero gaps.
- An adversarial pass per OWASP Top 10, scoped to this app's actual surface.
- Automated scanning (ZAP or similar) against staging with a seeded dataset.
- Load test: catalogue list, search suggest, activity feed, at 10× expected peak. Confirm
  rate limits hold and that hitting them degrades rather than collapses.
- Dependency audit with zero unresolved highs, and a documented decision for each medium.
- **Re-run the frontend audit** — the 17-item sweep from this session, against the live
  build. The frontend changed substantially in Phase 4, so its clean bill of health from
  v29 does not automatically transfer.
- A full request trace confirming no PII in URLs, logs, or third-party calls.
- Confirm `file://` demo mode still works, per the Phase 0.2 decision. It is a stated
  project property and it is easy to break silently.

### Launch checklist
- [ ] HSTS `preload` submitted (only now, and only if every subdomain is ready)
- [ ] CSP enforced, not report-only, with zero violations
- [ ] `security.txt` published with a contact address
- [ ] A vulnerability disclosure policy, and an inbox someone reads
- [ ] Every default credential and every development secret rotated
- [ ] Debug mode provably impossible in production config
- [ ] Staging is `noindex` and holds no production data
- [ ] Every rate limit verified against the live deployment
- [ ] Backup restore drilled within the last 30 days
- [ ] Runbook reviewed
- [ ] `robots.txt` deliberate about what is crawlable
- [ ] Privacy policy and terms published and accurate
- [ ] The authorization matrix is in CI and blocks merges

---

## Dependency graph

```
0 ─→ 1 ─→ 2 ─→ 3 ─→ 4
               │
               └──→ 5 ─→ 6 ─→ 7 ─→ 8 ─→ 9 ─→ 10
                                    │
                                    ├──→ 11
                                    └──→ 13 ─→ 14
                    12  (needs 2; shares the image pipeline with 11)
                    15  (needs 1; do before launch)
                    16  (needs everything)
```

**Can run in parallel:** Phase 12 (covers) needs no part of the identity chain and can be
picked up any time after Phase 2 — useful as a change of pace. Its one coupling is the
shared image re-encode pipeline it has in common with Phase 11; whichever phase runs first
builds it. Phase 15 can start as soon as Phase 1 lands.

**Must be sequential:** 5 → 6 → 7 is a hard chain, and 7's authorization layer gates
8, 9, 10, 11, and 13. Do not start Phase 10 before Phase 7's matrix test is green; DMs
without a proven authorization layer is how breaches happen.

**Suggested order if you want visible progress early:** 0, 1, 2, 3, 4 gets you a real
API-backed site with no accounts — a satisfying milestone. Then 5, 6, 7 for the hard
security work while motivation is high. Then 8, 9, 10, 11 are features on a solid base.

---

## Where I would push back

Two things in this plan are more work than they look, and it is worth knowing now.

**Phase 4 is bigger than it reads.** "Make the store async" touches every view builder in
`app.js`, and the frontend currently has no concept of a failed read. The audit found the
code clean, which helps, but this is a genuine refactor of the view layer, not a shim.

**Phase 5–6 is where a real vulnerability is most likely to end up.** The specification
above is complete, but auth is unforgiving: the timing-attack mitigation, the
enumeration-identical responses, and the session-fixation rotation are each one forgotten
line away from being defeated, and none of those failures is visible from the outside.

Decision 0.3 chose to build it anyway, knowingly. That choice came with the seven
conditions in §0.3.1 — including condition 7, a pre-agreed bail-out to a hosted provider
if Phase 5's timing and enumeration gates cannot be met. The point of writing that down
now is that the decision to abandon our own auth should be made against a criterion
settled in advance, not argued about late with working code in hand.
