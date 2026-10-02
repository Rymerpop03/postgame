"""Liveness check.

Returns `{"ok": true}` and nothing else. No version, no hostname, no dependency status,
no build id: a health endpoint that reports its inventory is free reconnaissance, and it
is reachable unauthenticated by definition.

Anything richer — "is the database reachable", "which migration is applied" — belongs
behind the admin policy in Phase 13, not here.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.security.policy import Policy, policy

router = APIRouter()


@router.get("/api/health")
@policy(Policy.PUBLIC)
async def health() -> dict[str, bool]:
    return {"ok": True}
