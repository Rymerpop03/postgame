"""Parse the authored catalogue rows, reproducing js/data.js exactly.

Phase 3 of BACKEND-PLAN.md: the seed loader "adapting scratchpad/emit.py: instead of
writing games-NN.js, it writes rows."

The important constraint is not parsing — it is *fidelity*. js/data.js derives each game's
id, cover hue and cover pattern from a hash of its title, and the frontend renders 2,000
covers from those values today. If this module computes them differently, every cover in
the catalogue changes colour and pattern the moment Phase 4 switches the frontend to the
API. So the hash, the PRNG and the slug rules below are deliberate ports, checked against
values read out of the running frontend rather than against this code.

Row format, from the header comment in games-01.js:

    Title|Year|Developer|Genres|Platforms|Critic|Blurb[|SteamAppId|UserScore]

1,079 rows carry 7 fields and 921 carry 9; the 9-field rows are Steam-sourced and add an
app id and a positive-review percentage.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# js/data.js COVER_PATTERNS. The index is derived from the title hash, so the order is data
# and must not be rearranged.
COVER_PATTERNS = ("grid", "rays", "orbs", "stripes", "arcs", "noise")

# A fixed base rather than the row count, so a game's sort key does not shift every time the
# catalogue grows. Ordering by popularity DESC reproduces the authored order; `rank`, where
# 0 is the biggest game, is what the API exposes because that is what the frontend reads.
RANK_BASE = 1_000_000

# Matches one quoted row inside the concat([...]) literal. Deliberately line-based: the
# files are generated one row per line by scratchpad/emit.py, and a real JS parser here
# would be a dependency and an attack surface for no benefit.
ROW_RE = re.compile(r'^\s*"(.*?)",?\s*$')

UINT32 = 0xFFFFFFFF


class CatalogueError(ValueError):
    """A row that cannot be trusted. Never silently skipped — see load_games."""


def hash32(text: str) -> int:
    """FNV-1a as js/data.js implements it.

    Math.imul is a 32-bit multiply, so every step is masked back to 32 bits. Python's
    unbounded ints would otherwise diverge after the first multiplication and every hue in
    the catalogue would change.
    """
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & UINT32
    return h


def _imul(a: int, b: int) -> int:
    """Math.imul: multiply as 32-bit signed, return the low 32 bits unsigned."""
    a &= UINT32
    b &= UINT32
    # Interpret both operands as signed before multiplying, exactly as imul does.
    if a >= 0x80000000:
        a -= 0x100000000
    if b >= 0x80000000:
        b -= 0x100000000
    return (a * b) & UINT32


def rng(seed: int) -> Callable[[], float]:
    """The mulberry32 variant in js/data.js. Yields floats in [0, 1)."""
    state = seed & UINT32

    def nxt() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & UINT32
        t = _imul(state ^ (state >> 15), 1 | state)
        t = ((t + _imul(t ^ (t >> 7), 61 | t)) & UINT32) ^ t
        return ((t ^ (t >> 14)) & UINT32) / 4294967296

    return nxt


def slugify(title: str) -> str:
    """js/data.js slugify: lowercase, & becomes ' and ', other runs become '-'."""
    s = title.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


@dataclass(frozen=True, slots=True)
class Game:
    id: str
    title: str
    studio: str
    year: int | None
    genres: tuple[str, ...]
    platforms: tuple[str, ...]
    critic: int | None
    blurb: str
    steam_app_id: int | None
    user_score: int | None
    hue: int
    cover_pattern: str
    rank: int

    @property
    def popularity(self) -> int:
        return RANK_BASE - self.rank


def _optional_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.isdigit() else None


def parse_row(row: str, rank: int) -> Game:
    parts = row.split("|")
    if len(parts) < 7:
        raise CatalogueError(f"row {rank}: expected at least 7 fields, found {len(parts)}")

    title = parts[0].strip()
    if not title:
        raise CatalogueError(f"row {rank}: empty title")

    slug = slugify(title)
    if not slug:
        raise CatalogueError(f"row {rank}: title {title!r} produces an empty slug")

    seed = hash32(title)
    random = rng(seed)

    # dict.fromkeys de-duplicates while preserving order, which matters because the arrays
    # are rendered in the order they were authored.
    genres = tuple(dict.fromkeys(g.strip() for g in parts[3].split(",") if g.strip()))
    platforms = tuple(dict.fromkeys(p.strip() for p in parts[4].split(",") if p.strip()))

    return Game(
        id=slug,
        title=title,
        studio=parts[2].strip(),
        year=_optional_int(parts[1]),
        genres=genres,
        platforms=platforms,
        critic=_optional_int(parts[5]),
        blurb=parts[6].strip(),
        steam_app_id=_optional_int(parts[7]) if len(parts) > 7 else None,
        user_score=_optional_int(parts[8]) if len(parts) > 8 else None,
        hue=int(random() * 360),
        cover_pattern=COVER_PATTERNS[seed % len(COVER_PATTERNS)],
        rank=rank,
    )


def read_rows(js_dir: Path) -> list[str]:
    """Every catalogue row, in authored order across games-01.js … games-NN.js.

    Sorted by filename, which is why the generator zero-pads them: games-2.js would sort
    before games-10.js and silently reorder the catalogue, changing every game's rank.
    """
    files = sorted(js_dir.glob("games-*.js"))
    if not files:
        raise CatalogueError(f"no games-*.js found in {js_dir}")

    rows: list[str] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = ROW_RE.match(line)
            if match and "|" in match.group(1):
                rows.append(match.group(1))
    if not rows:
        raise CatalogueError(f"found {len(files)} catalogue files but parsed no rows")
    return rows


def load_games(js_dir: Path) -> list[Game]:
    """Parse every row, refusing the whole catalogue if any row is bad.

    All-or-nothing on purpose. A loader that skips malformed rows produces a catalogue that
    is quietly short, and nobody notices 1,997 games where there should be 2,000.
    """
    rows = read_rows(js_dir)
    games = [parse_row(row, rank) for rank, row in enumerate(rows)]

    seen: dict[str, str] = {}
    for game in games:
        if game.id in seen:
            raise CatalogueError(
                f"duplicate slug {game.id!r} from titles {seen[game.id]!r} and "
                f"{game.title!r}. Two games cannot share a URL; rename one in the source."
            )
        seen[game.id] = game.title
    return games
