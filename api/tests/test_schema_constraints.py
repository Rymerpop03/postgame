"""Every CHECK constraint, proven to reject the value it exists for.

Phase 2 exit criterion: "a fixture script proves every CHECK rejects the value it is meant to".

The point is not coverage for its own sake. A constraint nobody has watched fail might be
inverted, might reference the wrong column, or might have been quietly dropped by a later
migration — and every one of those failures is silent. So each test here inserts a row that must
be refused, and asserts the *named* constraint refused it, not merely that something went wrong.

Skipped when no test database is reachable, so the suite still runs on a machine without
Postgres. That is a deliberate trade: these tests are worthless if they silently pass, so the
skip is loud (`-rs` in CI) rather than a green tick.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.db


def violates(exc: Exception, constraint: str) -> bool:
    """Did this failure come from the named constraint?

    Matching the constraint name rather than the error class is what makes these tests
    meaningful: a NOT NULL violation and the CHECK we care about are both IntegrityError, and a
    test that accepts either proves nothing about the CHECK.
    """
    return constraint in str(getattr(exc, "orig", exc))


@pytest.fixture
def user_id(db: Connection) -> uuid.UUID:
    new_id = uuid.uuid4()
    db.execute(
        text("""
            INSERT INTO users (id, username, display_name)
            VALUES (:id, :username, 'Fixture User')
        """),
        {"id": new_id, "username": f"u{new_id.hex[:12]}"},
    )
    return new_id


@pytest.fixture
def game_id(db: Connection) -> str:
    slug = f"g-{uuid.uuid4().hex[:12]}"
    db.execute(
        text("INSERT INTO games (id, title, studio, year) VALUES (:id, 'Fixture', 'S', 2020)"),
        {"id": slug},
    )
    return slug


@pytest.fixture
def second_user(db: Connection) -> uuid.UUID:
    new_id = uuid.uuid4()
    db.execute(
        text("""
            INSERT INTO users (id, username, display_name)
            VALUES (:id, :username, 'Other User')
        """),
        {"id": new_id, "username": f"o{new_id.hex[:12]}"},
    )
    return new_id


# ------------------------------------------------------------------------------------- users


class TestUsers:
    def test_hue_above_range_is_rejected(self, db: Connection) -> None:
        # The same invariant ui.js coerces client-side. That coercion is display armour; this is
        # where the invariant lives, and it is what a backend bug would otherwise walk past.
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO users (username, display_name, hue)
                    VALUES ('huetest', 'Hue', 400)
                """)
            )
        assert violates(exc.value, "users_hue_range")

    def test_hue_negative_is_rejected(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO users (username, display_name, hue)
                    VALUES ('huetest2', 'Hue', -1)
                """)
            )
        assert violates(exc.value, "users_hue_range")

    @pytest.mark.parametrize(
        "username",
        ["ab", "a" * 21, "Has-Capitals", "has space", "has.dot", "has-dash", "", "ünïcode"],
    )
    def test_malformed_usernames_are_rejected(self, db: Connection, username: str) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("INSERT INTO users (username, display_name) VALUES (:u, 'X')"),
                {"u": username},
            )
        assert violates(exc.value, "users_username_shape")

    def test_display_name_over_40_is_rejected(self, db: Connection) -> None:
        # Matches the frontend's NAME_MAX. Three layers agree: client, schema, database.
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("INSERT INTO users (username, display_name) VALUES ('namelen', :n)"),
                {"n": "x" * 41},
            )
        assert violates(exc.value, "users_display_name_len")

    def test_empty_display_name_is_rejected(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("INSERT INTO users (username, display_name) VALUES ('emptyname', '')")
            )
        assert violates(exc.value, "users_display_name_len")

    def test_bio_over_240_is_rejected(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO users (username, display_name, bio)
                    VALUES ('biolen', 'X', :b)
                """),
                {"b": "x" * 241},
            )
        assert violates(exc.value, "users_bio_len")

    def test_unknown_role_is_rejected(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO users (username, display_name, role)
                    VALUES ('roletest', 'X', 'superadmin')
                """)
            )
        assert violates(exc.value, "users_role_known")

    def test_unknown_status_is_rejected(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO users (username, display_name, status)
                    VALUES ('statustest', 'X', 'banned')
                """)
            )
        assert violates(exc.value, "users_status_known")

    def test_demo_account_cannot_hold_a_password(self, db: Connection) -> None:
        """Decision 0.8 made structural.

        The login handler refuses demo accounts, but that is one `if`. This makes a
        loggable-into demo account impossible to store at all, so "sign in as pixelvagrant"
        cannot become a real account someone claims.
        """
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO users (username, display_name, is_demo, password_hash)
                    VALUES ('demopw', 'X', true, '$argon2id$fake')
                """)
            )
        assert violates(exc.value, "users_demo_has_no_credentials")

    def test_demo_account_cannot_hold_an_email(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO users (username, display_name, is_demo, email_norm, email_raw)
                    VALUES ('demomail', 'X', true, 'a@b.test', 'a@b.test')
                """)
            )
        assert violates(exc.value, "users_demo_has_no_credentials")

    def test_email_norm_without_raw_is_rejected(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO users (username, display_name, email_norm)
                    VALUES ('halfmail', 'X', 'a@b.test')
                """)
            )
        assert violates(exc.value, "users_email_pairs")

    def test_verified_without_email_is_rejected(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO users (username, display_name, email_verified_at)
                    VALUES ('noverify', 'X', now())
                """)
            )
        assert violates(exc.value, "users_verified_needs_email")

    def test_username_is_case_insensitively_unique(self, db: Connection) -> None:
        # citext, so 'Ana' and 'ana' are the same account. Without it, impersonation by
        # changing the casing of someone's name is free.
        db.execute(text("INSERT INTO users (username, display_name) VALUES ('anaunique', 'A')"))
        with pytest.raises(IntegrityError):
            db.execute(
                text("INSERT INTO users (username, display_name) VALUES ('AnaUnique', 'B')")
            )


# ------------------------------------------------------------------------------------- games


class TestGames:
    @pytest.mark.parametrize("year", [1957, 2101])
    def test_year_outside_range_is_rejected(self, db: Connection, year: int) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("INSERT INTO games (id, title, year) VALUES ('yeartest', 'T', :y)"),
                {"y": year},
            )
        assert violates(exc.value, "games_year_range")

    def test_hue_out_of_range_is_rejected(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(text("INSERT INTO games (id, title, hue) VALUES ('huegame', 'T', 360)"))
        assert violates(exc.value, "games_hue_range")

    @pytest.mark.parametrize(
        "slug", ["Has-Capitals", "has space", "-leading", "has_underscore"]
    )
    def test_malformed_slug_is_rejected(self, db: Connection, slug: str) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(text("INSERT INTO games (id, title) VALUES (:i, 'T')"), {"i": slug})
        assert violates(exc.value, "games_id_shape")

    def test_negative_popularity_is_rejected(self, db: Connection) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("INSERT INTO games (id, title, popularity) VALUES ('poptest', 'T', -1)")
            )
        assert violates(exc.value, "games_popularity_nonneg")

    def test_search_vector_is_generated(self, db: Connection, game_id: str) -> None:
        # Generated and stored: computing it per query would defeat the GIN index entirely.
        found = db.execute(
            text("SELECT search_tsv::text FROM games WHERE id = :i"), {"i": game_id}
        ).scalar_one()
        assert "fixtur" in found  # 'Fixture' stemmed by the english config

    def test_search_vector_cannot_be_written(self, db: Connection, game_id: str) -> None:
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE games SET search_tsv = NULL WHERE id = :i"), {"i": game_id})


# -------------------------------------------------------------------------------------- logs


class TestLogs:
    @pytest.mark.parametrize("rating", [0, 11, -1])
    def test_rating_outside_1_to_10_is_rejected(
        self, db: Connection, user_id: uuid.UUID, game_id: str, rating: int
    ) -> None:
        # Half-stars 1..10 (decision 0.6). The ten-button rate strip cannot produce anything
        # else, but a backend bug could.
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO logs (user_id, game_id, status, rating)
                    VALUES (:u, :g, 'finished', :r)
                """),
                {"u": user_id, "g": game_id, "r": rating},
            )
        assert violates(exc.value, "logs_rating_range")

    def test_unknown_status_is_rejected(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO logs (user_id, game_id, status)
                    VALUES (:u, :g, 'wishlist')
                """),
                {"u": user_id, "g": game_id},
            )
        assert violates(exc.value, "logs_status_known")

    def test_review_over_6000_is_rejected(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        # store.js REVIEW_MAX, enforced again here.
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO logs (user_id, game_id, status, review)
                    VALUES (:u, :g, 'finished', :r)
                """),
                {"u": user_id, "g": game_id, "r": "x" * 6001},
            )
        assert violates(exc.value, "logs_review_len")

    def test_review_at_exactly_6000_is_accepted(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        # An off-by-one in the other direction is just as wrong and much easier to ship.
        db.execute(
            text("""
                INSERT INTO logs (user_id, game_id, status, review)
                VALUES (:u, :g, 'finished', :r)
            """),
            {"u": user_id, "g": game_id, "r": "x" * 6000},
        )

    def test_one_log_per_user_per_game(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        # Decision 0.5. The whole profile UI assumes it.
        db.execute(
            text("INSERT INTO logs (user_id, game_id, status) VALUES (:u, :g, 'playing')"),
            {"u": user_id, "g": game_id},
        )
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("INSERT INTO logs (user_id, game_id, status) VALUES (:u, :g, 'finished')"),
                {"u": user_id, "g": game_id},
            )
        assert violates(exc.value, "logs_one_per_user_game")

    # Three explicit tests rather than one parametrized over the column name. Varying the column
    # meant interpolating it, which needed an escape-hatch annotation on the SQL gate — and the
    # formatter kept moving that annotation off the line the gate inspects. Saying it three
    # times is cheaper than a comment fighting the formatter.
    #
    # The rule: a backlog entry is a game you have not played yet. Without it the shelves can
    # disagree with their own contents — a game in "Want to play" showing a rating.

    def test_backlog_cannot_carry_a_rating(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO logs (user_id, game_id, status, rating)
                    VALUES (:u, :g, 'backlog', 5)
                """),
                {"u": user_id, "g": game_id},
            )
        assert violates(exc.value, "logs_backlog_is_unplayed")

    def test_backlog_cannot_carry_a_review(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO logs (user_id, game_id, status, review)
                    VALUES (:u, :g, 'backlog', 'played it in my head')
                """),
                {"u": user_id, "g": game_id},
            )
        assert violates(exc.value, "logs_backlog_is_unplayed")

    def test_backlog_cannot_carry_hours(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO logs (user_id, game_id, status, hours)
                    VALUES (:u, :g, 'backlog', 10)
                """),
                {"u": user_id, "g": game_id},
            )
        assert violates(exc.value, "logs_backlog_is_unplayed")

    def test_future_played_on_is_rejected(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        # A CHECK cannot call now(), so this is a trigger — but the rule still lives in the
        # database rather than only in Pydantic.
        with pytest.raises(DBAPIError) as exc:
            db.execute(
                text("""
                    INSERT INTO logs (user_id, game_id, status, played_on)
                    VALUES (:u, :g, 'finished', (now() + interval '2 days')::date)
                """),
                {"u": user_id, "g": game_id},
            )
        assert "future" in str(exc.value).lower()

    def test_negative_hours_is_rejected(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO logs (user_id, game_id, status, hours)
                    VALUES (:u, :g, 'finished', -1)
                """),
                {"u": user_id, "g": game_id},
            )
        assert violates(exc.value, "logs_hours_range")


# -------------------------------------------------------------------------------- game_stats


class TestGameStats:
    """The aggregates, maintained by trigger in the same transaction as the write.

    Computing AVG() per request would be both a performance and a consistency problem; a trigger
    that gets the arithmetic wrong is worse than either, so the deltas are exercised directly.
    """

    def stats(self, db: Connection, game_id: str) -> Any:
        return (
            db.execute(
                text("""
                SELECT rating_count, rating_sum, avg_rating, log_count, review_count,
                       favorite_count
                FROM game_stats WHERE game_id = :g
            """),
                {"g": game_id},
            )
            .mappings()
            .one_or_none()
        )

    def test_insert_updates_stats(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        db.execute(
            text("""
                INSERT INTO logs (user_id, game_id, status, rating, review, favorite)
                VALUES (:u, :g, 'finished', 8, 'good', true)
            """),
            {"u": user_id, "g": game_id},
        )
        row = self.stats(db, game_id)
        assert row is not None
        assert row["rating_count"] == 1
        assert row["rating_sum"] == 8
        assert float(row["avg_rating"]) == 8.0
        assert row["log_count"] == 1
        assert row["review_count"] == 1
        assert row["favorite_count"] == 1

    def test_average_over_two_ratings(
        self, db: Connection, user_id: uuid.UUID, second_user: uuid.UUID, game_id: str
    ) -> None:
        for who, rating in ((user_id, 7), (second_user, 10)):
            db.execute(
                text("""
                    INSERT INTO logs (user_id, game_id, status, rating)
                    VALUES (:u, :g, 'finished', :r)
                """),
                {"u": who, "g": game_id, "r": rating},
            )
        row = self.stats(db, game_id)
        assert row["rating_count"] == 2
        assert row["rating_sum"] == 17
        assert float(row["avg_rating"]) == 8.5

    def test_update_adjusts_stats(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        db.execute(
            text("""
                INSERT INTO logs (user_id, game_id, status, rating)
                VALUES (:u, :g, 'finished', 4)
            """),
            {"u": user_id, "g": game_id},
        )
        db.execute(
            text("UPDATE logs SET rating = 9 WHERE user_id = :u AND game_id = :g"),
            {"u": user_id, "g": game_id},
        )
        row = self.stats(db, game_id)
        assert row["rating_count"] == 1
        assert row["rating_sum"] == 9
        assert float(row["avg_rating"]) == 9.0

    def test_clearing_a_rating_adjusts_stats(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        db.execute(
            text("""
                INSERT INTO logs (user_id, game_id, status, rating)
                VALUES (:u, :g, 'finished', 6)
            """),
            {"u": user_id, "g": game_id},
        )
        db.execute(
            text("UPDATE logs SET rating = NULL WHERE user_id = :u AND game_id = :g"),
            {"u": user_id, "g": game_id},
        )
        row = self.stats(db, game_id)
        assert row["rating_count"] == 0
        assert row["rating_sum"] == 0
        assert row["avg_rating"] is None

    def test_delete_adjusts_stats(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        db.execute(
            text("""
                INSERT INTO logs (user_id, game_id, status, rating, favorite)
                VALUES (:u, :g, 'finished', 5, true)
            """),
            {"u": user_id, "g": game_id},
        )
        db.execute(
            text("DELETE FROM logs WHERE user_id = :u AND game_id = :g"),
            {"u": user_id, "g": game_id},
        )
        row = self.stats(db, game_id)
        assert row["rating_count"] == 0
        assert row["log_count"] == 0
        assert row["favorite_count"] == 0
        assert row["avg_rating"] is None

    def test_stats_never_go_negative(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        # The CHECK constraints on game_stats are the safety net under the trigger arithmetic.
        # If a delta is ever wrong, this fails loudly instead of serving a negative count.
        db.execute(
            text("INSERT INTO logs (user_id, game_id, status) VALUES (:u, :g, 'playing')"),
            {"u": user_id, "g": game_id},
        )
        db.execute(
            text("DELETE FROM logs WHERE user_id = :u AND game_id = :g"),
            {"u": user_id, "g": game_id},
        )
        row = self.stats(db, game_id)
        assert row["log_count"] == 0


# ------------------------------------------------------------------- social and messaging


class TestSocial:
    def test_self_follow_is_rejected(self, db: Connection, user_id: uuid.UUID) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("INSERT INTO follows (follower_id, followee_id) VALUES (:u, :u)"),
                {"u": user_id},
            )
        assert violates(exc.value, "follows_no_self")

    def test_self_block_is_rejected(self, db: Connection, user_id: uuid.UUID) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("INSERT INTO blocks (blocker_id, blocked_id) VALUES (:u, :u)"),
                {"u": user_id},
            )
        assert violates(exc.value, "blocks_no_self")

    def test_duplicate_follow_is_rejected(
        self, db: Connection, user_id: uuid.UUID, second_user: uuid.UUID
    ) -> None:
        db.execute(
            text("INSERT INTO follows (follower_id, followee_id) VALUES (:a, :b)"),
            {"a": user_id, "b": second_user},
        )
        with pytest.raises(IntegrityError):
            db.execute(
                text("INSERT INTO follows (follower_id, followee_id) VALUES (:a, :b)"),
                {"a": user_id, "b": second_user},
            )

    def test_message_body_over_1000_is_rejected(
        self, db: Connection, user_id: uuid.UUID
    ) -> None:
        convo = db.execute(
            text("INSERT INTO conversations DEFAULT VALUES RETURNING id")
        ).scalar_one()
        db.execute(
            text("""
                INSERT INTO conversation_members (conversation_id, user_id) VALUES (:c, :u)
            """),
            {"c": convo, "u": user_id},
        )
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO messages (conversation_id, sender_id, body)
                    VALUES (:c, :u, :b)
                """),
                {"c": convo, "u": user_id, "b": "x" * 1001},
            )
        assert violates(exc.value, "messages_body_len")

    def test_non_member_cannot_send_a_message(
        self, db: Connection, user_id: uuid.UUID, second_user: uuid.UUID
    ) -> None:
        """The Phase 10 IDOR bug, made unrepresentable at the storage layer.

        The API will filter on membership in the same WHERE clause as the fetch. This is the
        layer beneath that: even a bug in the API cannot store a message from someone who is not
        in the conversation.
        """
        convo = db.execute(
            text("INSERT INTO conversations DEFAULT VALUES RETURNING id")
        ).scalar_one()
        db.execute(
            text("INSERT INTO conversation_members (conversation_id, user_id) VALUES (:c, :u)"),
            {"c": convo, "u": user_id},
        )
        with pytest.raises(DBAPIError) as exc:
            db.execute(
                text("""
                    INSERT INTO messages (conversation_id, sender_id, body)
                    VALUES (:c, :u, 'let me in')
                """),
                {"c": convo, "u": second_user},
            )
        assert "not a member" in str(exc.value).lower()

    def test_member_can_send_a_message(self, db: Connection, user_id: uuid.UUID) -> None:
        convo = db.execute(
            text("INSERT INTO conversations DEFAULT VALUES RETURNING id")
        ).scalar_one()
        db.execute(
            text("INSERT INTO conversation_members (conversation_id, user_id) VALUES (:c, :u)"),
            {"c": convo, "u": user_id},
        )
        db.execute(
            text("""
                INSERT INTO messages (conversation_id, sender_id, body)
                VALUES (:c, :u, 'hello')
            """),
            {"c": convo, "u": user_id},
        )

    def test_comment_body_over_600_is_rejected(
        self, db: Connection, user_id: uuid.UUID, game_id: str
    ) -> None:
        log_id = db.execute(
            text("""
                INSERT INTO logs (user_id, game_id, status) VALUES (:u, :g, 'finished')
                RETURNING id
            """),
            {"u": user_id, "g": game_id},
        ).scalar_one()
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO comments (log_id, user_id, body) VALUES (:l, :u, :b)
                """),
                {"l": log_id, "u": user_id, "b": "x" * 601},
            )
        assert violates(exc.value, "comments_body_len")


# ------------------------------------------------------------------ moderation and reports


class TestModerationAndReports:
    def test_duplicate_report_is_rejected(
        self, db: Connection, user_id: uuid.UUID, second_user: uuid.UUID
    ) -> None:
        # Report abuse is itself abusable, so one report per person per thing.
        for _ in range(1):
            db.execute(
                text("""
                    INSERT INTO reports (reporter_id, target_type, target_id, reason)
                    VALUES (:r, 'user', :t, 'spam')
                """),
                {"r": user_id, "t": second_user},
            )
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO reports (reporter_id, target_type, target_id, reason)
                    VALUES (:r, 'user', :t, 'abuse')
                """),
                {"r": user_id, "t": second_user},
            )
        assert violates(exc.value, "reports_one_per_reporter_target")

    def test_resolved_report_needs_a_timestamp(
        self, db: Connection, user_id: uuid.UUID, second_user: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO reports (reporter_id, target_type, target_id, reason, status)
                    VALUES (:r, 'user', :t, 'spam', 'actioned')
                """),
                {"r": user_id, "t": second_user},
            )
        assert violates(exc.value, "reports_resolution_is_complete")

    def test_unknown_report_reason_is_rejected(
        self, db: Connection, user_id: uuid.UUID, second_user: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO reports (reporter_id, target_type, target_id, reason)
                    VALUES (:r, 'user', :t, 'i just do not like them')
                """),
                {"r": user_id, "t": second_user},
            )
        assert violates(exc.value, "reports_reason_known")

    def test_moderation_event_hash_must_be_32_bytes(self, db: Connection) -> None:
        # The column holds a sha256 and nothing else — never the offending text.
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO moderation_events (action, target_type, content_sha)
                    VALUES ('blocked_write', 'comment', '\\x0102')
                """)
            )
        assert violates(exc.value, "moderation_events_sha_len")


# ---------------------------------------------------------------------- sessions and tokens


class TestSessionsAndTokens:
    def test_token_hash_must_be_32_bytes(self, db: Connection, user_id: uuid.UUID) -> None:
        # Storing anything other than a sha256 here means something other than a hash got
        # stored — most likely the raw token.
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO sessions (user_id, token_hash, expires_at)
                    VALUES (:u, '\\xdeadbeef', now() + interval '1 day')
                """),
                {"u": user_id},
            )
        assert violates(exc.value, "sessions_token_hash_len")

    def test_ip_hash_must_be_16_bytes(self, db: Connection, user_id: uuid.UUID) -> None:
        # Couples to logging.hash_ip, which returns 32 hex characters = 16 bytes.
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO sessions (user_id, token_hash, expires_at, ip_hash)
                    VALUES (:u, :t, now() + interval '1 day', '\\x01')
                """),
                {"u": user_id, "t": b"\x00" * 32},
            )
        assert violates(exc.value, "sessions_ip_hash_len")

    def test_expiry_must_follow_creation(self, db: Connection, user_id: uuid.UUID) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO sessions (user_id, token_hash, created_at, expires_at)
                    VALUES (:u, :t, now(), now() - interval '1 hour')
                """),
                {"u": user_id, "t": b"\x01" * 32},
            )
        assert violates(exc.value, "sessions_expiry_after_creation")

    def test_email_change_token_needs_a_target(
        self, db: Connection, user_id: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO email_tokens (user_id, purpose, token_hash, expires_at)
                    VALUES (:u, 'email_change', :t, now() + interval '1 hour')
                """),
                {"u": user_id, "t": b"\x02" * 32},
            )
        assert violates(exc.value, "email_tokens_change_has_target")

    def test_unknown_token_purpose_is_rejected(
        self, db: Connection, user_id: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO email_tokens (user_id, purpose, token_hash, expires_at)
                    VALUES (:u, 'magic_link', :t, now() + interval '1 hour')
                """),
                {"u": user_id, "t": b"\x03" * 32},
            )
        assert violates(exc.value, "email_tokens_purpose_known")


# ------------------------------------------------------------------------------- avatars


class TestAvatars:
    def test_svg_mime_is_rejected(self, db: Connection, user_id: uuid.UUID) -> None:
        """SVG is a script container. Phase 11 rejects it in the upload pipeline; the column
        refuses to record it even if that pipeline is ever bypassed."""
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO avatars (user_id, mime, byte_size, width, height, sha256)
                    VALUES (:u, 'image/svg+xml', 100, 64, 64, :s)
                """),
                {"u": user_id, "s": b"\x00" * 32},
            )
        assert violates(exc.value, "avatars_mime_known")

    def test_oversized_dimensions_are_rejected(
        self, db: Connection, user_id: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO avatars (user_id, mime, byte_size, width, height, sha256)
                    VALUES (:u, 'image/webp', 100, 4000, 64, :s)
                """),
                {"u": user_id, "s": b"\x00" * 32},
            )
        assert violates(exc.value, "avatars_width")

    def test_oversized_bytes_are_rejected(self, db: Connection, user_id: uuid.UUID) -> None:
        with pytest.raises(IntegrityError) as exc:
            db.execute(
                text("""
                    INSERT INTO avatars (user_id, mime, byte_size, width, height, sha256)
                    VALUES (:u, 'image/webp', 5242881, 64, 64, :s)
                """),
                {"u": user_id, "s": b"\x00" * 32},
            )
        assert violates(exc.value, "avatars_size")
