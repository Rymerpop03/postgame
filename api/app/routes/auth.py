"""Identity endpoints: signup, login, logout, logout-all, me.

BACKEND-PLAN.md Phase 5, the phase §0.3 calls "the highest-risk phase — build it slowly and
test it adversarially". Most of what follows is ordinary; the parts that are not are here.

**Handlers are `def`, not `async def`.** Starlette runs a sync endpoint in a thread pool.
An `async def` handler doing a 250 ms Argon2 verification would block the event loop for
that whole time, so four login attempts a second would stall every other request on the
process — a denial of service anyone can perform with a laptop and no credentials. The
rest of the app has the same shape for its database calls, which is a smaller version of
the same problem and is noted in the Phase 5 outcome.

**Nothing distinguishes accounts that exist from accounts that do not.**
  * signup returns the same status, the same headers and byte-identical content whether
    the address is new or already registered, and performs the same statements in both
    cases (the INSERT carries ON CONFLICT DO NOTHING, so both paths run it);
  * login has exactly one failure response for every cause — unknown address, wrong
    password, demo account, suspended account — and the unknown-address path still performs
    a real Argon2 verification, inside `Hasher.verify`, so it costs the same;
  * the reason is recorded in `audit_log` for our own detection work and never returned.

Usernames are treated differently on purpose: a taken username is a 409. Usernames are
public — they are in profile URLs — so refusing to say a username is taken would cost every
person signing up a guessing game to protect information anyone can read off the site.

**No session is created by signup.** Doing so would put a `Set-Cookie` on the fresh-address
response and not on the already-registered one, which is the enumeration oracle rebuilt in
a header after being carefully kept out of the body. Sign up, then sign in.

**No account lockout.** Locking an account after failed attempts lets anyone lock anyone
out of their own account, converting a nuisance into a denial of service. The controls are
the per-address and per-address-and-account throttles instead.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.catalogue.parse import hash32
from app.config import Settings
from app.db import connect
from app.logging import logger
from app.security import audit, emails, passwords, ratelimit, sessions
from app.security.deps import (
    ClientIp,
    CurrentActor,
    HasherDep,
    LimiterDep,
    RequiredActor,
    SettingsDep,
)
from app.security.passwords import PasswordPolicyError
from app.security.policy import Policy, policy

router = APIRouter(prefix="/api/auth")
log = logger("postgame.auth")

# The one thing a failed login ever says. Identical for every cause — see the module
# docstring. Phrased so it is also true and useful for the person who simply mistyped.
LOGIN_FAILED = "That email and password combination does not match an account."

# Byte-identical for a new address and for one that is already registered. It has to be
# true in both cases, which is why it is conditional rather than congratulatory.
SIGNUP_ACCEPTED: dict[str, Any] = {
    "status": "accepted",
    "message": (
        "If that address is new to Postgame, the account is ready — you can sign in now."
    ),
}

# Matches the users_username_shape CHECK. Applied to the lower-cased form, because that is
# what gets stored: `username` is citext, so `ada` and `Ada` are the same account, and
# storing whichever case was typed first would make the constraint depend on who signed up.
USERNAME = re.compile(r"^[a-z0-9_]{3,20}$")

# Names that must not become accounts. Two reasons, both concrete: profile URLs are
# `#/u/<username>`, so a name that collides with a route would be unreachable or would
# shadow one; and an account called `support` or `moderator` is an impersonation tool that
# costs nothing to prevent now and is very awkward to take away later.
RESERVED_USERNAMES = frozenset(
    # A word list, kept as a word list. Hand-quoting 85 names one per line is how one
    # of them ends up misspelled and nobody notices.
    """
    about account accounts admin administrator api assets auth backlog blog contact css
    dashboard delete developer developers diary docs edit explore faq favicon feed feeds
    followers following games games_api help home img index js legal list lists log login
    logout mail me media mod moderator moderators new news notifications null official
    postgame press privacy profile profiles register reports reset review reviews robots
    root rss search security session settings signin signup site sitemap staff static status
    support system team terms test tos undefined update user users www
    """.split()  # noqa: SIM905 - deliberate, see above
)

NO_STORE = {"Cache-Control": "no-store"}


class SignupIn(BaseModel):
    # extra="forbid" so a client sending `role` or `is_admin` gets a 422 rather than having
    # it silently ignored — the ignoring is safe, the silence is what hides a bug.
    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, Field(min_length=3, max_length=emails.MAX_LENGTH)]
    username: Annotated[str, Field(min_length=1, max_length=40)]
    display_name: Annotated[str, Field(min_length=1, max_length=40)]
    # Bounds the request body. The real limit is passwords.MAX_LENGTH, enforced by
    # check_policy so the caller gets an explanation rather than a field-name error.
    password: Annotated[str, Field(min_length=1, max_length=4096)]


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, Field(min_length=3, max_length=emails.MAX_LENGTH)]
    password: Annotated[str, Field(min_length=1, max_length=4096)]


def _too_many(decision: ratelimit.Decision) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many attempts. Please wait a little and try again.",
        headers={"Retry-After": str(decision.retry_after), **NO_STORE},
    )


def _check(limiter: ratelimit.Backend, key: str, name: str) -> None:
    decision = limiter.hit(key, ratelimit.LIMITS[name])
    if not decision.allowed:
        raise _too_many(decision)


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _session_payload(actor: sessions.Actor) -> dict[str, Any]:
    return {"user": actor.public_json(), "csrfToken": actor.csrf_token}


# ------------------------------------------------------------------------------- signup


@router.post("/signup", status_code=202)
@policy(Policy.PUBLIC)
def signup(
    request: Request,
    body: SignupIn,
    settings: SettingsDep,
    hasher: HasherDep,
    limiter: LimiterDep,
    ip: ClientIp,
) -> JSONResponse:
    _check(limiter, ratelimit.client_key(request, "signup"), "signup.per_ip")

    try:
        email = emails.normalise(body.email)
    except emails.EmailFormatError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    username = body.username.strip().casefold()
    if not USERNAME.match(username):
        raise HTTPException(
            status_code=422,
            detail="Usernames are 3–20 characters, using lowercase letters, numbers and "
            "underscores.",
        )
    if username in RESERVED_USERNAMES:
        raise HTTPException(status_code=409, detail="That username is not available.")

    display_name = body.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="Enter a display name.")

    try:
        passwords.check_policy(body.password, username=username, email=email)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    # Hashed before any lookup, and always, so the work done is the same whether or not the
    # address is already registered.
    password_hash = hasher.hash(body.password)
    ip_hash = audit.ip_hash_bytes(settings, ip)

    with connect(settings) as conn:
        if _username_taken(conn, username):
            taken = True
        else:
            taken = False
            created = _insert_user(
                conn,
                email_norm=email,
                email_raw=body.email.strip(),
                username=username,
                display_name=display_name,
                password_hash=password_hash,
            )
            # `created` is None when the address is already registered. Both branches have
            # already run the same INSERT statement; only the audit row differs, and it
            # never leaves the database.
            audit.record(
                conn,
                audit.Event.SIGNUP,
                actor_user_id=created,
                ip_hash=ip_hash,
                created=created is not None,
            )

    if taken:
        raise HTTPException(status_code=409, detail="That username is already taken.")

    log.info("signup_accepted")
    return JSONResponse(status_code=202, content=SIGNUP_ACCEPTED, headers=NO_STORE)


def _username_taken(conn: Connection, username: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM users WHERE username = :username"), {"username": username}
    ).one_or_none()
    return row is not None


def _insert_user(
    conn: Connection,
    *,
    email_norm: str,
    email_raw: str,
    username: str,
    display_name: str,
    password_hash: str,
) -> Any:
    """Insert, or do nothing if the address is taken. Returns the new id, or None.

    ON CONFLICT rather than a SELECT-then-INSERT: two simultaneous signups for the same
    address would both find nothing and both insert, and one would get a constraint error
    that differs from the other's response — an enumeration oracle assembled out of a race.

    The two email parameters are bound separately even though one is derived from the
    other. `email_norm` is citext and `email_raw` is text, and binding one parameter to both
    leaves Postgres unable to infer a type for it — "inconsistent types deduced for
    parameter $1", which is a confusing error for what is really a modelling point:
    `email_raw` is meant to hold the address as the person typed it, so that mail we send
    them is addressed the way they wrote it, and `email_norm` is what uniqueness and lookup
    use.

    `hue` is chosen here rather than defaulted in the schema so that a new account looks
    like the seeded members, which are spread across the wheel rather than all sharing the
    column default. It uses the same FNV-1a the frontend has always used for derived
    colours (app/catalogue/parse.hash32), so a username has one colour everywhere.
    """
    row = conn.execute(
        text(
            "INSERT INTO users (email_norm, email_raw, username, display_name, "
            "                   password_hash, password_algo, hue) "
            "VALUES (:email_norm, :email_raw, :username, :display_name, :password_hash, "
            ":algo, :hue) "
            "ON CONFLICT (email_norm) DO NOTHING "
            "RETURNING id"
        ),
        {
            "email_norm": email_norm,
            "email_raw": email_raw,
            "username": username,
            "display_name": display_name,
            "password_hash": password_hash,
            "algo": passwords.ALGORITHM,
            "hue": hash32(username) % 360,
        },
    ).one_or_none()
    return row.id if row is not None else None


# -------------------------------------------------------------------------------- login


@router.post("/login")
@policy(Policy.PUBLIC)
def login(
    request: Request,
    body: LoginIn,
    settings: SettingsDep,
    hasher: HasherDep,
    limiter: LimiterDep,
    ip: ClientIp,
) -> JSONResponse:
    _check(limiter, ratelimit.client_key(request, "login"), "login.per_ip")
    _check(
        limiter,
        ratelimit.secret_key(settings, "login", ip or "unknown", body.email),
        "login.per_ip_email",
    )

    # A malformed address cannot match any row, and saying so is not an oracle — it is a
    # statement about the string that was typed, not about who has an account.
    try:
        email = emails.normalise(body.email)
    except emails.EmailFormatError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    ip_hash = audit.ip_hash_bytes(settings, ip)

    # Held only for the lookup. The Argon2 verification below takes a quarter of a second,
    # and holding a pooled connection across it would let a burst of failed logins exhaust
    # the pool — a login throttle that starves the rest of the site is not a good trade.
    with connect(settings) as conn:
        row = conn.execute(
            text(
                "SELECT id, password_hash, is_demo, is_tombstone, status "
                "FROM users WHERE email_norm = :email"
            ),
            {"email": email},
        ).one_or_none()

    reason = _refusal_reason(row)
    stored = row.password_hash if (row is not None and reason is None) else None

    # One call, both cases. `stored=None` runs a real verification against a throwaway hash
    # — see Hasher.verify, and BACKEND-PLAN.md §0.3.1 conditions 1 and 3.
    ok = hasher.verify(stored, body.password)
    if not ok and reason is None:
        reason = audit.Reason.BAD_PASSWORD

    if reason is not None:
        with connect(settings) as conn:
            audit.record(
                conn,
                audit.Event.LOGIN_FAILURE,
                actor_user_id=row.id if row is not None else None,
                ip_hash=ip_hash,
                reason=str(reason),
            )
        # Raised after the block, so the audit row is committed. Raising inside would roll
        # it back and the record of the attempt — the entire point of auditing failures —
        # would be lost exactly when someone is trying addresses one after another.
        raise HTTPException(status_code=401, detail=LOGIN_FAILED, headers=NO_STORE)

    # `reason is None` already implies both of these; stating it narrows the types and
    # documents the invariant in the one place where getting it wrong would sign somebody in.
    assert row is not None and stored is not None  # noqa: S101
    user_id = row.id
    # Computed outside the transaction below for the same reason as the lookup above.
    upgraded = hasher.hash(body.password) if hasher.needs_rehash(stored) else None
    presented = request.cookies.get(sessions.cookie_name(settings))

    with connect(settings) as conn:
        # Session fixation: whatever session the caller arrived with does not survive
        # authentication, whether or not it was valid. §0.3.1 condition 4.
        if presented:
            conn.execute(
                text(
                    "UPDATE sessions SET revoked_at = now() "
                    "WHERE token_hash = :token_hash AND revoked_at IS NULL"
                ),
                {"token_hash": sessions.token_digest(presented)},
            )

        if upgraded is not None:
            conn.execute(
                text(
                    "UPDATE users SET password_hash = :hash, password_algo = :algo "
                    "WHERE id = :id"
                ),
                {"hash": upgraded, "algo": passwords.ALGORITHM, "id": user_id},
            )

        token, session_id = sessions.create(
            conn,
            settings,
            user_id=user_id,
            ip_hash=ip_hash,
            user_agent=_user_agent(request),
        )
        audit.record(
            conn,
            audit.Event.LOGIN_SUCCESS,
            actor_user_id=user_id,
            ip_hash=ip_hash,
            session_id=str(session_id),
            # Not `rehashed`: the audit screen refuses any key containing "hash", and it was
            # right to — the rule cannot tell a boolean about hashing from a hash.
            cost_upgraded=upgraded is not None,
        )
        actor = sessions.resolve(conn, settings, token)

    if actor is None:  # pragma: no cover - a session we just created must resolve
        raise HTTPException(status_code=500, detail="Could not start a session.")

    response = JSONResponse(content=_session_payload(actor), headers=NO_STORE)
    response.set_cookie(
        sessions.cookie_name(settings), token, **sessions.cookie_kwargs(settings)
    )
    return response


def _refusal_reason(row: Any) -> audit.Reason | None:
    """Why this account may not be signed into, or None if it may.

    The demo check is first and is deliberately independent of the password: decision 0.8
    makes the ten seeded members unclaimable, and `users_demo_has_no_credentials` already
    guarantees they have no hash to match. This is the second of those two layers, and the
    one that would still hold if the constraint were ever relaxed.
    """
    if row is None:
        return audit.Reason.UNKNOWN_ACCOUNT
    if row.is_demo or row.is_tombstone:
        return audit.Reason.DEMO_ACCOUNT
    if row.status != "active":
        return audit.Reason.NOT_ACTIVE
    if row.password_hash is None:
        return audit.Reason.DEMO_ACCOUNT
    return None


# ------------------------------------------------------------------------------- logout


@router.post("/logout", status_code=204)
@policy(Policy.AUTHENTICATED)
def logout(actor: RequiredActor, settings: SettingsDep, ip: ClientIp) -> Response:
    with connect(settings) as conn:
        sessions.revoke(conn, actor.session_id)
        audit.record(
            conn,
            audit.Event.LOGOUT,
            actor_user_id=actor.user_id,
            ip_hash=audit.ip_hash_bytes(settings, ip),
            session_id=str(actor.session_id),
        )
    return _cleared(settings)


@router.post("/logout-all", status_code=204)
@policy(Policy.AUTHENTICATED)
def logout_all(actor: RequiredActor, settings: SettingsDep, ip: ClientIp) -> Response:
    """Sign out everywhere, this device included.

    Including this device is the point: someone reaching for this has decided their account
    is compromised, and leaving the session they are holding alive would be the one they
    least want left alive if the device they are on is the problem.
    """
    with connect(settings) as conn:
        revoked = sessions.revoke_all(conn, actor.user_id)
        audit.record(
            conn,
            audit.Event.LOGOUT_ALL,
            actor_user_id=actor.user_id,
            ip_hash=audit.ip_hash_bytes(settings, ip),
            sessions_revoked=revoked,
        )
    return _cleared(settings)


def _cleared(settings: Settings) -> Response:
    """A 204 that also removes the cookie.

    `delete_cookie` has to be given the same path and flags the cookie was set with, or the
    browser keeps the original and the user stays signed in — with a server-side session
    that no longer exists, so the symptom is a confusing half-signed-in state rather than an
    obvious failure.
    """
    # A bare Response, not a JSONResponse: a 204 must not carry a body, and rendering
    # `null` into one is the kind of protocol violation proxies handle inconsistently.
    response = Response(status_code=204, headers=NO_STORE)
    kwargs = sessions.cookie_kwargs(settings)
    response.delete_cookie(
        sessions.cookie_name(settings),
        path=kwargs["path"],
        httponly=kwargs["httponly"],
        secure=kwargs["secure"],
        samesite=kwargs["samesite"],
    )
    return response


# ----------------------------------------------------------------------------------- me


@router.get("/me")
@policy(Policy.PUBLIC)
def me(actor: CurrentActor, settings: SettingsDep) -> JSONResponse:
    """Who is signed in, and the CSRF token for their session.

    PUBLIC rather than AUTHENTICATED, and it answers 200 with `user: null` for an anonymous
    caller. That is the whole point of it: the frontend calls this once at boot to decide
    what to render, and a 401 there would mean treating the ordinary state of a public site
    as an error.

    This is also where the client gets its CSRF token, which is safe because a cross-origin
    page cannot read the response — there is no CORS configuration anywhere in this app, by
    design (decision 0.9: one origin).
    """
    if actor is None:
        return JSONResponse(content={"user": None, "csrfToken": None}, headers=NO_STORE)
    return JSONResponse(content=_session_payload(actor), headers=NO_STORE)
