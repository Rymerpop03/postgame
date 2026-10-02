"""Indexes for the seven sort orders the catalogue UI offers.

Revision ID: 0003
Revises: 0002
Created: 2026-08-05

The filter bar in js/app.js offers: popular, rating, logged, critic, newest, oldest, title.
Phase 3 serves all seven with keyset pagination, and keyset needs the index to match the ORDER
BY exactly — expression and direction included — or every page after the first degrades to a
sort over the whole catalogue.

Two of the seven sort on nullable columns (`critic`, `year`). Row comparison `(a, b) < (:x, :y)`
is undefined once NULLs are involved, so those sorts wrap the column in a `coalesce` that pushes
unknown values to the end, and the index is built on the same expression. A sentinel is used
rather than `NULLS LAST` because mixed sort directions defeat row comparison, and row comparison
is what makes a keyset cursor a single indexed seek instead of an offset scan.

`rating` and `logged` sort on game_stats and are deliberately left unindexed here — see the note
at the bottom.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# 9999 for the ascending sort and -1 for the descending ones: in both cases an unknown year or
# critic score sorts last rather than pretending to be the oldest game ever made.
INDEXES = (
    ("games_critic_idx", "games", "(coalesce(critic, -1) DESC, id DESC)"),
    ("games_year_desc_idx", "games", "(coalesce(year, -1) DESC, id DESC)"),
    ("games_year_asc_idx", "games", "(coalesce(year, 9999) ASC, id ASC)"),
)


def upgrade() -> None:
    for name, table, expression in INDEXES:
        op.execute(f"CREATE INDEX {name} ON {table} {expression}")  # noqa: S608  # sql-safe: literals

    # The `popular` sort reuses games_popularity_idx and `title` reuses games_title_idx, both
    # from 0001 — but games_title_idx is (title, id) ascending, which is exactly what the title
    # sort needs, so nothing to add.
    #
    # `rating` and `logged` sort on game_stats.avg_rating and game_stats.log_count. Those live
    # in a different table, so no single index can serve the join and the ordering together;
    # Postgres joins 2,000 rows and sorts, which measures in fractions of a millisecond.
    # Indexing them here would not be used. The moment to revisit is when the catalogue is large
    # enough that the sort shows up in a query plan — at which point the fix is a materialised
    # view or denormalising the two aggregates onto games, and both are worth doing only with a
    # measurement in hand.


def downgrade() -> None:
    for name, _table, _expression in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")  # noqa: S608  # sql-safe: literal
