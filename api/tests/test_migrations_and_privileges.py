"""Migration reversibility, role privileges, and index use.

The three Phase 2 exit criteria that need a live database and are not about CHECK constraints:

  * migrations run up and down cleanly
  * the app role demonstrably cannot DROP TABLE
  * EXPLAIN shows index use for the catalogue's filter and sort combinations

The privilege test is the one that matters most. "The running app cannot change its own schema"
is a claim about a GRANT, and a GRANT nobody exercised is indistinguishable from one that was
mis-typed. If an SQL injection bug ever reaches the database, this grant is what decides whether
the consequence is a leaked row or a dropped table.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import ProgrammingError

from tests.conftest import migrate_database_url

pytestmark = pytest.mark.db

API_DIR = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "alembic_version",
    "audit_log",
    "avatars",
    "blocks",
    "comments",
    "conversation_members",
    "conversations",
    "email_tokens",
    "follows",
    "game_covers",
    "game_stats",
    "games",
    # Filter vocabularies, added by migration 0002 and maintained by the seed loader.
    "genres",
    "platforms",
    "logs",
    "messages",
    "moderation_events",
    "rate_limits",
    "reports",
    "sessions",
    "users",
}


def alembic(*args: str, url: str | None = None) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    env["PG_MIGRATE_DATABASE_URL"] = url or migrate_database_url()
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


class TestSchemaShape:
    def test_every_expected_table_exists(self, db: Connection) -> None:
        found = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
        }
        assert found >= EXPECTED_TABLES, EXPECTED_TABLES - found

    def test_no_unexpected_tables(self, db: Connection) -> None:
        # Catches a stray table left behind by a hand-run experiment, which would then diverge
        # from the migration and only surface on a fresh deploy.
        found = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
        }
        assert found <= EXPECTED_TABLES, found - EXPECTED_TABLES

    def test_citext_extension_is_installed(self, db: Connection) -> None:
        assert db.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'citext'")
        ).scalar_one_or_none()

    def test_at_one_migration_head(self, db_engine: object) -> None:
        # db_engine requested purely for its reachability skip — these shell out to alembic
        # rather than using the connection, and without it they fail hard on a machine with no
        # database instead of skipping like the rest of the suite.
        result = alembic("current")
        assert result.returncode == 0, result.stderr
        # Exactly one head. Two means a branch nobody merged, and the next upgrade picks one
        # arbitrarily.
        heads = alembic("heads")
        assert heads.stdout.count("(head)") == 1, heads.stdout


class TestAppRoleCannotChangeSchema:
    """The two-role split, exercised rather than assumed."""

    def test_cannot_create_table(self, db: Connection) -> None:
        with pytest.raises(ProgrammingError) as exc:
            db.execute(text("CREATE TABLE should_not_exist (id int)"))
        assert "permission denied" in str(exc.value).lower()

    def test_cannot_drop_table(self, db: Connection) -> None:
        with pytest.raises(ProgrammingError) as exc:
            db.execute(text("DROP TABLE users"))
        assert (
            "must be owner" in str(exc.value).lower() or "permission" in str(exc.value).lower()
        )

    def test_cannot_alter_table(self, db: Connection) -> None:
        with pytest.raises(ProgrammingError) as exc:
            db.execute(text("ALTER TABLE users ADD COLUMN backdoor text"))
        assert (
            "must be owner" in str(exc.value).lower() or "permission" in str(exc.value).lower()
        )

    def test_cannot_drop_a_constraint(self, db: Connection) -> None:
        # The specific escalation this guards: dropping the CHECK that keeps hue in range, or
        # the one that keeps demo accounts unclaimable, would be far more useful to an attacker
        # than dropping a table.
        with pytest.raises(ProgrammingError):
            db.execute(text("ALTER TABLE users DROP CONSTRAINT users_hue_range"))

    def test_cannot_disable_a_trigger(self, db: Connection) -> None:
        # Disabling the membership trigger would re-open the Phase 10 IDOR shape.
        with pytest.raises(ProgrammingError):
            db.execute(text("ALTER TABLE messages DISABLE TRIGGER messages_sender_is_member"))

    def test_cannot_create_a_function(self, db: Connection) -> None:
        with pytest.raises(ProgrammingError):
            db.execute(
                text("CREATE FUNCTION evil() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql")
            )

    def test_can_still_read_and_write_rows(self, db: Connection) -> None:
        # The grant must not be so tight that the application cannot work — a test that only
        # proves things are forbidden would pass on a role with no privileges at all.
        new_id = uuid.uuid4()
        db.execute(
            text("INSERT INTO users (id, username, display_name) VALUES (:i, :u, 'DML')"),
            {"i": new_id, "u": f"dml{new_id.hex[:10]}"},
        )
        assert (
            db.execute(
                text("SELECT count(*) FROM users WHERE id = :i"), {"i": new_id}
            ).scalar_one()
            == 1
        )
        db.execute(text("UPDATE users SET bio = 'ok' WHERE id = :i"), {"i": new_id})
        db.execute(text("DELETE FROM users WHERE id = :i"), {"i": new_id})

    def test_role_is_the_application_role(self, db: Connection) -> None:
        # If this suite ever runs as the owner, every test above becomes meaningless.
        assert db.execute(text("SELECT current_user")).scalar_one() == "postgame_app"

    def test_statement_timeout_is_set(self, db: Connection) -> None:
        value = db.execute(text("SHOW statement_timeout")).scalar_one()
        assert value in ("30s", "30000ms"), value


class TestIndexUse:
    """EXPLAIN for the five filter and sort combinations the catalogue UI offers.

    A sequential scan over 2,000 games is fast enough that a missing index would never be
    noticed in development, and would be the first thing to fall over in production.
    """

    def plan(self, db: Connection, sql: str, params: dict[str, object] | None = None) -> str:
        rows = db.execute(text(f"EXPLAIN {sql}"), params or {})  # noqa: S608  # sql-safe: literal
        return "\n".join(str(r[0]) for r in rows)

    def test_full_text_search_uses_the_gin_index(self, db: Connection) -> None:
        db.execute(text("SET enable_seqscan = off"))
        plan = self.plan(
            db,
            "SELECT id FROM games WHERE search_tsv @@ websearch_to_tsquery('english', :q) "
            "LIMIT 20",
            {"q": "elden ring"},
        )
        assert "games_search_idx" in plan, plan

    def test_genre_filter_uses_the_gin_index(self, db: Connection) -> None:
        db.execute(text("SET enable_seqscan = off"))
        plan = self.plan(
            db, "SELECT id FROM games WHERE genres @> ARRAY[:g]::text[] LIMIT 20", {"g": "RPG"}
        )
        assert "games_genres_idx" in plan, plan

    def test_platform_filter_uses_the_gin_index(self, db: Connection) -> None:
        db.execute(text("SET enable_seqscan = off"))
        plan = self.plan(
            db,
            "SELECT id FROM games WHERE platforms @> ARRAY[:p]::text[] LIMIT 20",
            {"p": "PC"},
        )
        assert "games_platforms_idx" in plan, plan

    def test_popularity_keyset_pagination_uses_its_index(self, db: Connection) -> None:
        db.execute(text("SET enable_seqscan = off"))
        # The shape Phase 3's cursor pagination will emit: sort key plus id tiebreaker.
        plan = self.plan(
            db,
            "SELECT id FROM games WHERE (popularity, id) < (:p, :i) "
            "ORDER BY popularity DESC, id DESC LIMIT 20",
            {"p": 500, "i": "zzz"},
        )
        assert "games_popularity_idx" in plan, plan

    def test_year_sort_uses_its_index(self, db: Connection) -> None:
        db.execute(text("SET enable_seqscan = off"))
        plan = self.plan(db, "SELECT id FROM games ORDER BY year DESC NULLS LAST, id LIMIT 20")
        assert "games_year_idx" in plan, plan

    def test_member_shelf_uses_its_index(self, db: Connection) -> None:
        db.execute(text("SET enable_seqscan = off"))
        plan = self.plan(
            db,
            "SELECT game_id FROM logs WHERE user_id = :u ORDER BY updated_at DESC LIMIT 20",
            {"u": uuid.uuid4()},
        )
        assert "logs_user_recent_idx" in plan, plan

    def test_game_reviews_use_the_partial_index(self, db: Connection) -> None:
        db.execute(text("SET enable_seqscan = off"))
        plan = self.plan(
            db,
            "SELECT id FROM logs WHERE game_id = :g AND review IS NOT NULL "
            "AND review_state = 'visible' ORDER BY created_at DESC LIMIT 20",
            {"g": "elden-ring"},
        )
        assert "logs_game_reviews_idx" in plan, plan


class TestReversibility:
    """Down and back up, on a scratch database.

    Never against postgame_test itself: dropping the schema the rest of the suite depends on,
    mid-run, would make failures baffling. A fresh database per run also means this proves a
    migration works from nothing, which is the case that matters on first deploy.
    """

    @pytest.fixture
    def scratch_url(self, db_engine: object) -> object:
        owner = create_engine(migrate_database_url(), isolation_level="AUTOCOMMIT")
        name = f"postgame_rev_{uuid.uuid4().hex[:8]}"
        try:
            with owner.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{name}"'))  # noqa: S608  # sql-safe: uuid hex
        except ProgrammingError as exc:
            owner.dispose()
            pytest.skip(f"cannot create a scratch database (needs CREATEDB): {exc}")

        url = migrate_database_url().rsplit("/", 1)[0] + "/" + name
        try:
            yield url
        finally:
            with owner.connect() as conn:
                drop = f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'  # sql-safe: uuid hex
                conn.execute(text(drop))
            owner.dispose()

    def test_up_down_up_leaves_the_same_schema(self, scratch_url: str) -> None:
        up = alembic("upgrade", "head", url=scratch_url)
        assert up.returncode == 0, up.stderr

        engine = create_engine(scratch_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
                conn.commit()
                after_up = self._fingerprint(conn)
            assert EXPECTED_TABLES - {"alembic_version"} <= after_up["tables"]

            down = alembic("downgrade", "base", url=scratch_url)
            assert down.returncode == 0, down.stderr

            with engine.connect() as conn:
                after_down = self._fingerprint(conn)
            # alembic_version survives a downgrade to base by design; everything else must go.
            assert after_down["tables"] <= {"alembic_version"}, after_down["tables"]
            assert not after_down["functions"], after_down["functions"]
            assert not after_down["triggers"], after_down["triggers"]

            again = alembic("upgrade", "head", url=scratch_url)
            assert again.returncode == 0, again.stderr

            with engine.connect() as conn:
                after_again = self._fingerprint(conn)
            assert after_again == after_up
        finally:
            engine.dispose()

    @staticmethod
    def _fingerprint(conn: Connection) -> dict[str, set[str]]:
        tables = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
        }
        constraints = {
            f"{r[0]}.{r[1]}"
            for r in conn.execute(
                text("""
                    SELECT rel.relname, con.conname
                    FROM pg_constraint con
                    JOIN pg_class rel ON rel.oid = con.conrelid
                    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                    WHERE ns.nspname = 'public'
                """)
            )
        }
        indexes = {
            r[0]
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        }
        # Functions the migration created, excluding anything owned by an extension. citext
        # installs ~50 functions into public and the downgrade deliberately leaves the extension
        # in place — it may predate this migration and other schemas could depend on it — so a
        # bare count here would report the downgrade as incomplete when it was correct.
        functions = {
            r[0]
            for r in conn.execute(
                text("""
                    SELECT p.proname FROM pg_proc p
                    JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'
                      AND NOT EXISTS (
                          SELECT 1 FROM pg_depend d
                          WHERE d.objid = p.oid AND d.deptype = 'e'
                      )
                """)
            )
        }
        triggers = {
            r[0]
            for r in conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
        }
        return {
            "tables": tables,
            "constraints": constraints,
            "indexes": indexes,
            "functions": functions,
            "triggers": triggers,
        }


class TestMigrationGuards:
    def test_refuses_to_run_without_a_url(self) -> None:
        import os

        env = {k: v for k, v in os.environ.items() if k != "PG_MIGRATE_DATABASE_URL"}
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "current"],
            cwd=API_DIR,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode != 0
        assert "PG_MIGRATE_DATABASE_URL is not set" in result.stderr

    def test_refuses_to_migrate_as_the_app_role(self, db_engine: object) -> None:
        """The guard in migrations/env.py.

        Connecting Alembic as postgame_app would defeat the two-role split with no visible sign,
        because the migration would simply fail on a permission error that looks like a
        configuration problem rather than a design violation.
        """
        from tests.conftest import app_database_url

        result = alembic("current", url=app_database_url())
        assert result.returncode != 0
        assert "Refusing to migrate as" in (result.stderr + result.stdout)
