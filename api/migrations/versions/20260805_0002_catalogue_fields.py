"""Catalogue fields the frontend actually uses, plus filter vocabularies.

Revision ID: 0002
Revises: 0001
Created: 2026-08-05

The Phase 2 schema was written from BACKEND-PLAN.md rather than from the data, and it turns out
to be missing five fields the existing frontend reads on every game page: `blurb`, `critic`,
`pattern`, `steam` and the authored `rank`. Discovered while writing the Phase 3 seed loader —
the loader had nowhere to put half of each row.

Bounds below come from measuring all 2,000 rows rather than guessing:

    title    max  64   studio  max 48   blurb  max 96
    year     1972..2026            critic  43..99  (present in 1482)
    steam id 10..3241660  (845)   user %  16..99  (878)
    genres   70 distinct           platforms 38 distinct
    slugs    2000 unique, none empty, longest 59

Every row has either 7 or 9 pipe-separated fields; the 9-field ones are the Steam-sourced rows
carrying an app id and a positive-review percentage.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# js/data.js COVER_PATTERNS, in order — the index into this list is derived from the title hash,
# so the order is part of the data and must not be rearranged.
COVER_PATTERNS = ("grid", "rays", "orbs", "stripes", "arcs", "noise")


def upgrade() -> None:
    patterns = ", ".join(f"'{p}'" for p in COVER_PATTERNS)

    # sql-safe: patterns is built from the module-level literal tuple above
    add_columns = f"""
        ALTER TABLE games
            ADD COLUMN blurb text NOT NULL DEFAULT ''
                CONSTRAINT games_blurb_len CHECK (char_length(blurb) <= 300),
            ADD COLUMN critic smallint
                CONSTRAINT games_critic_range CHECK (critic IS NULL OR (critic BETWEEN 0 AND
                100)),
            ADD COLUMN user_score smallint
                CONSTRAINT games_user_score_range CHECK (user_score IS NULL OR (user_score
                BETWEEN 0 AND 100)),
            ADD COLUMN steam_app_id integer
                CONSTRAINT games_steam_app_id_positive CHECK (steam_app_id IS NULL OR
                steam_app_id > 0),
            ADD COLUMN cover_pattern text NOT NULL DEFAULT 'grid'
                CONSTRAINT games_cover_pattern_known CHECK (cover_pattern IN ({patterns}))
    """  # noqa: S608
    op.execute(add_columns)

    # Rebuild the search vector with weighting. The 0001 version concatenated title and studio
    # flat, so a studio match ranked identically to a title match; searching "Nintendo" buried
    # the game called Nintendo World among everything Nintendo published. Weight A/B lets
    # ts_rank prefer titles.
    #
    # The regconfig stays an explicit literal cast: the one-argument to_tsvector() depends on
    # default_text_search_config and is therefore not IMMUTABLE, which a generated column
    # requires. Dropping the column drops games_search_idx with it, so that is recreated too.
    op.execute("ALTER TABLE games DROP COLUMN search_tsv")
    op.execute("""
        ALTER TABLE games ADD COLUMN search_tsv tsvector GENERATED ALWAYS AS (
            setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english'::regconfig, coalesce(studio, '')), 'B')
        ) STORED
    """)
    op.execute("CREATE INDEX games_search_idx ON games USING GIN (search_tsv)")

    # Filter vocabularies. The plan requires genre and platform filters to be validated against
    # a closed set rather than passed through to the query as text, and the frontend needs the
    # same lists for its dropdowns. Reference tables give both from one source, maintained by
    # the seed loader — so an unknown filter value is a 422 rather than a query that returns
    # nothing and looks like an empty catalogue.
    for table in ("genres", "platforms"):
        # sql-safe: table name comes from the literal tuple in the loop above
        create = f"""
            CREATE TABLE {table} (
                name       text PRIMARY KEY
                           CONSTRAINT {table}_name_len
                           CHECK (char_length(name) BETWEEN 1 AND 40),
                game_count integer NOT NULL DEFAULT 0
                           CONSTRAINT {table}_count_nonneg CHECK (game_count >= 0)
            )
        """  # noqa: S608
        op.execute(create)
        # sql-safe: same literal table name
        index = f"CREATE INDEX {table}_count_idx ON {table} (game_count DESC, name)"  # noqa: S608
        op.execute(index)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platforms")
    op.execute("DROP TABLE IF EXISTS genres")

    op.execute("ALTER TABLE games DROP COLUMN IF EXISTS search_tsv")
    op.execute("""
        ALTER TABLE games ADD COLUMN search_tsv tsvector GENERATED ALWAYS AS (
            to_tsvector('english'::regconfig,
                coalesce(title, '') || ' ' || coalesce(studio, ''))
        ) STORED
    """)
    op.execute("CREATE INDEX games_search_idx ON games USING GIN (search_tsv)")

    op.execute("""
        ALTER TABLE games
            DROP COLUMN IF EXISTS cover_pattern, DROP COLUMN IF EXISTS steam_app_id, DROP COLUMN
            IF EXISTS user_score, DROP COLUMN IF EXISTS critic, DROP COLUMN IF EXISTS blurb
    """)
