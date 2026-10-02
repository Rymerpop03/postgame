"""Initial schema.

Revision ID: 0001
Revises: none
Created: 2026-08-05

Phase 2 of ../../postgame/BACKEND-PLAN.md. Fifteen tables, and the constraints are the point:
every field limit is enforced here as well as at the request boundary, so a bug in the API or a
future second writer cannot put a value in that the application would refuse.

Written as explicit SQL rather than the Alembic DSL. The DSL cannot express generated columns,
partial indexes, or non-trivial CHECKs without escaping to raw SQL anyway, and the constraints
are the reviewable part of this file — burying them in keyword arguments would hide them.

Field limits match the frontend's existing caps exactly (NAME_MAX 40, BIO_MAX 240,
COMMENT_MAX 600, MESSAGE_MAX 1000, REVIEW_MAX 6000) so the three layers agree.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # ------------------------------------------------------------------------------- users
    #
    # `hue` carries the same 0..359 invariant that ui.js coerces client-side. The coercion
    # there is display armour; this is where the invariant actually lives.
    #
    # `password_hash IS NULL` means the account cannot be logged into at all. That is how the
    # ten seeded demo members stay unclaimable (decision 0.8) and how a tombstone user for
    # anonymised content (decision 0.7) is represented — neither is a real login.
    op.execute("""
        CREATE TABLE users (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            username          citext NOT NULL UNIQUE
                              CONSTRAINT users_username_shape
                              CHECK (username ~ '^[a-z0-9_]{3,20}$'),
            email_norm        citext UNIQUE,
            email_raw         text,
            display_name      text NOT NULL
                              CONSTRAINT users_display_name_len
                              CHECK (char_length(display_name) BETWEEN 1 AND 40),
            bio               text
                              CONSTRAINT users_bio_len
                              CHECK (bio IS NULL OR char_length(bio) <= 240),
            hue               smallint NOT NULL DEFAULT 260
                              CONSTRAINT users_hue_range CHECK (hue >= 0 AND hue < 360),
            avatar_id         uuid,
            password_hash     text,
            password_algo     text,
            email_verified_at timestamptz,
            is_demo           boolean NOT NULL DEFAULT false,
            is_tombstone      boolean NOT NULL DEFAULT false,
            role              text NOT NULL DEFAULT 'user'
                              CONSTRAINT users_role_known
                              CHECK (role IN ('user', 'moderator', 'admin')),
            status            text NOT NULL DEFAULT 'active'
                              CONSTRAINT users_status_known
                              CHECK (status IN ('active', 'suspended', 'deleted')),
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            deleted_at        timestamptz,

            -- A demo account must not be loggable-into, and must not carry an email that
            -- someone could use to claim it. Stating it as a constraint means the login
            -- handler's check is a second line rather than the only one.
            CONSTRAINT users_demo_has_no_credentials
                CHECK (NOT is_demo OR (password_hash IS NULL AND email_norm IS NULL)),
            -- An email address is only meaningful alongside its normalised form.
            CONSTRAINT users_email_pairs
                CHECK ((email_norm IS NULL) = (email_raw IS NULL)),
            -- You cannot be verified without an address to have verified.
            CONSTRAINT users_verified_needs_email
                CHECK (email_verified_at IS NULL OR email_norm IS NOT NULL)
        )
    """)
    op.execute("CREATE INDEX users_active_idx ON users (username) WHERE status = 'active'")
    op.execute(
        "CREATE INDEX users_deleted_at_idx ON users (deleted_at) WHERE deleted_at IS NOT NULL"
    )

    # ----------------------------------------------------------------------------- avatars
    op.execute("""
        CREATE TABLE avatars (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            mime       text NOT NULL
                       CONSTRAINT avatars_mime_known
                       CHECK (mime IN ('image/webp', 'image/png', 'image/jpeg')),
            byte_size  integer NOT NULL
                       CONSTRAINT avatars_size CHECK (byte_size > 0 AND byte_size <= 5242880),
            width      smallint NOT NULL
                       CONSTRAINT avatars_width CHECK (width > 0 AND width <= 512),
            height     smallint NOT NULL
                       CONSTRAINT avatars_height CHECK (height > 0 AND height <= 512),
            sha256     bytea NOT NULL
                       CONSTRAINT avatars_sha256_len CHECK (octet_length(sha256) = 32),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX avatars_user_idx ON avatars (user_id)")
    # Deliberately not ON DELETE CASCADE: dropping a user's avatar row should not silently
    # delete the user. SET NULL keeps the account and orphans the picture, which the Phase 14
    # purge job then collects.
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT users_avatar_fk "
        "FOREIGN KEY (avatar_id) REFERENCES avatars(id) ON DELETE SET NULL"
    )

    # ---------------------------------------------------------------------------- sessions
    #
    # token_hash is sha256 of the opaque token; the raw value is never stored, so a database
    # dump yields no usable sessions (decision 0.4).
    op.execute("""
        CREATE TABLE sessions (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash   bytea NOT NULL UNIQUE
                         CONSTRAINT sessions_token_hash_len
                         CHECK (octet_length(token_hash) = 32),
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz NOT NULL,
            revoked_at   timestamptz,
            ip_hash      bytea
                         CONSTRAINT sessions_ip_hash_len
                         CHECK (ip_hash IS NULL OR octet_length(ip_hash) = 16),
            user_agent   text
                         CONSTRAINT sessions_ua_len
                         CHECK (user_agent IS NULL OR char_length(user_agent) <= 400),
            CONSTRAINT sessions_expiry_after_creation CHECK (expires_at > created_at)
        )
    """)
    op.execute("CREATE INDEX sessions_live_idx ON sessions (user_id) WHERE revoked_at IS NULL")
    op.execute("CREATE INDEX sessions_expiry_idx ON sessions (expires_at)")

    # ------------------------------------------------------------------------ email_tokens
    op.execute("""
        CREATE TABLE email_tokens (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            purpose    text NOT NULL
                       CONSTRAINT email_tokens_purpose_known
                       CHECK (purpose IN ('verify', 'reset', 'email_change')),
            token_hash bytea NOT NULL UNIQUE
                       CONSTRAINT email_tokens_hash_len
                       CHECK (octet_length(token_hash) = 32),
            new_email  citext,
            expires_at timestamptz NOT NULL,
            used_at    timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT email_tokens_change_has_target
                CHECK (purpose <> 'email_change' OR new_email IS NOT NULL)
        )
    """)
    op.execute("CREATE INDEX email_tokens_user_idx ON email_tokens (user_id, purpose)")
    op.execute("CREATE INDEX email_tokens_expiry_idx ON email_tokens (expires_at)")

    # ------------------------------------------------------------------------------- games
    #
    # id is the existing slug from js/games-*.js ('elden-ring'), so Phase 3's seed loader is a
    # translation of scratchpad/emit.py rather than a re-keying exercise.
    #
    # search_tsv is generated and stored: computing it per query would defeat the GIN index.
    # The regconfig is a literal so the expression is IMMUTABLE, which a generated column
    # requires.
    op.execute("""
        CREATE TABLE games (
            id         text PRIMARY KEY
                       CONSTRAINT games_id_shape CHECK (id ~ '^[a-z0-9][a-z0-9-]{0,80}$'),
            title      text NOT NULL
                       CONSTRAINT games_title_len
                       CHECK (char_length(title) BETWEEN 1 AND 200),
            studio     text NOT NULL DEFAULT ''
                       CONSTRAINT games_studio_len CHECK (char_length(studio) <= 200),
            year       smallint
                       CONSTRAINT games_year_range
                       CHECK (year IS NULL OR (year BETWEEN 1958 AND 2100)),
            hue        smallint NOT NULL DEFAULT 260
                       CONSTRAINT games_hue_range CHECK (hue >= 0 AND hue < 360),
            platforms  text[] NOT NULL DEFAULT '{}',
            genres     text[] NOT NULL DEFAULT '{}',
            popularity integer NOT NULL DEFAULT 0
                       CONSTRAINT games_popularity_nonneg CHECK (popularity >= 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            search_tsv tsvector GENERATED ALWAYS AS (
                to_tsvector('english'::regconfig,
                    coalesce(title, '') || ' ' || coalesce(studio, ''))
            ) STORED
        )
    """)
    op.execute("CREATE INDEX games_search_idx ON games USING GIN (search_tsv)")
    op.execute("CREATE INDEX games_genres_idx ON games USING GIN (genres)")
    op.execute("CREATE INDEX games_platforms_idx ON games USING GIN (platforms)")
    # Keyset pagination sorts on (popularity DESC, id) and (year DESC, id) — the tiebreaker is
    # part of the index or the cursor is not stable.
    op.execute("CREATE INDEX games_popularity_idx ON games (popularity DESC, id)")
    op.execute("CREATE INDEX games_year_idx ON games (year DESC NULLS LAST, id)")
    op.execute("CREATE INDEX games_title_idx ON games (title, id)")

    # ------------------------------------------------------------------------- game_covers
    op.execute("""
        CREATE TABLE game_covers (
            game_id    text PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
            url        text,
            state      text NOT NULL DEFAULT 'pending'
                       CONSTRAINT game_covers_state_known
                       CHECK (state IN ('pending', 'ok', 'none')),
            source     text,
            attempts   smallint NOT NULL DEFAULT 0
                       CONSTRAINT game_covers_attempts CHECK (attempts >= 0 AND attempts <= 20),
            checked_at timestamptz,
            CONSTRAINT game_covers_ok_has_url CHECK (state <> 'ok' OR url IS NOT NULL)
        )
    """)
    op.execute(
        "CREATE INDEX game_covers_pending_idx ON game_covers (state) WHERE state = 'pending'"
    )

    # -------------------------------------------------------------------------------- logs
    #
    # UNIQUE (user_id, game_id) is decision 0.5: one log per game. The entire profile UI —
    # the shelves and the "See more" routes — assumes it. Relaxing a constraint later is far
    # easier than introducing one.
    #
    # rating is 1..10 half-stars (decision 0.6), never a float. `status` maps to the
    # frontend's shelves: 'finished' is what app.js calls "Recently logged", and favourites
    # are the boolean below rather than a status, because a game can be both finished and a
    # favourite.
    op.execute("""
        CREATE TABLE logs (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            game_id      text NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            status       text NOT NULL
                         CONSTRAINT logs_status_known
                         CHECK (status IN ('playing', 'finished', 'abandoned', 'backlog')),
            rating       smallint
                         CONSTRAINT logs_rating_range
                         CHECK (rating IS NULL OR (rating BETWEEN 1 AND 10)),
            review       text
                         CONSTRAINT logs_review_len
                         CHECK (review IS NULL OR char_length(review) <= 6000),
            review_state text NOT NULL DEFAULT 'visible'
                         CONSTRAINT logs_review_state_known
                         CHECK (review_state IN ('visible', 'hidden', 'pending')),
            favorite     boolean NOT NULL DEFAULT false,
            played_on    date,
            hours        numeric(6, 1)
                         CONSTRAINT logs_hours_range
                         CHECK (hours IS NULL OR (hours >= 0 AND hours <= 99999)),
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT logs_one_per_user_game UNIQUE (user_id, game_id),
            -- A backlog entry is a game you have not played, so it cannot carry a rating, a
            -- review or hours. Without this the shelves can disagree with the content.
            CONSTRAINT logs_backlog_is_unplayed
                CHECK (status <> 'backlog'
                       OR (rating IS NULL AND review IS NULL AND hours IS NULL))
        )
    """)
    op.execute("CREATE INDEX logs_user_recent_idx ON logs (user_id, updated_at DESC)")
    op.execute("CREATE INDEX logs_game_idx ON logs (game_id)")
    # Serves GET /api/games/{slug}/reviews, which returns visible reviews only.
    op.execute("""
        CREATE INDEX logs_game_reviews_idx ON logs (game_id, created_at DESC)
        WHERE review IS NOT NULL AND review_state = 'visible'
    """)
    op.execute("CREATE INDEX logs_favorites_idx ON logs (user_id) WHERE favorite")
    # played_on must not be in the future. A CHECK cannot call now() (not IMMUTABLE), so this
    # is a trigger — the constraint still lives in the database rather than only in Pydantic.
    op.execute("""
        CREATE FUNCTION logs_reject_future_played_on() RETURNS trigger AS $$
        BEGIN
            IF NEW.played_on IS NOT NULL
               AND NEW.played_on > (now() AT TIME ZONE 'utc')::date THEN
                RAISE EXCEPTION 'played_on is in the future: %', NEW.played_on
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER logs_played_on_not_future
        BEFORE INSERT OR UPDATE OF played_on ON logs
        FOR EACH ROW EXECUTE FUNCTION logs_reject_future_played_on()
    """)

    # -------------------------------------------------------------------------- game_stats
    #
    # Maintained by trigger inside the same transaction as the write, so a rating and its
    # aggregate can never disagree. Computing AVG() over logs per request would be both a
    # performance and a consistency problem.
    op.execute("""
        CREATE TABLE game_stats (
            game_id        text PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
            rating_count   integer NOT NULL DEFAULT 0
                           CONSTRAINT game_stats_rating_count CHECK (rating_count >= 0),
            rating_sum     bigint NOT NULL DEFAULT 0
                           CONSTRAINT game_stats_rating_sum CHECK (rating_sum >= 0),
            avg_rating     numeric(4, 2),
            log_count      integer NOT NULL DEFAULT 0
                           CONSTRAINT game_stats_log_count CHECK (log_count >= 0),
            review_count   integer NOT NULL DEFAULT 0
                           CONSTRAINT game_stats_review_count CHECK (review_count >= 0),
            favorite_count integer NOT NULL DEFAULT 0
                           CONSTRAINT game_stats_favorite_count CHECK (favorite_count >= 0),
            updated_at     timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE FUNCTION game_stats_apply(target text, d_count int, d_sum bigint,
                                         d_logs int, d_reviews int, d_favorites int)
        RETURNS void AS $$
        BEGIN
            INSERT INTO game_stats AS gs (game_id, rating_count, rating_sum, log_count,
                                          review_count, favorite_count)
            VALUES (target, GREATEST(d_count, 0), GREATEST(d_sum, 0), GREATEST(d_logs, 0),
                    GREATEST(d_reviews, 0), GREATEST(d_favorites, 0))
            ON CONFLICT (game_id) DO UPDATE SET
                rating_count   = gs.rating_count   + d_count,
                rating_sum     = gs.rating_sum     + d_sum,
                log_count      = gs.log_count      + d_logs,
                review_count   = gs.review_count   + d_reviews,
                favorite_count = gs.favorite_count + d_favorites,
                updated_at     = now();

            UPDATE game_stats SET
                avg_rating = CASE WHEN rating_count > 0
                                  THEN ROUND(rating_sum::numeric / rating_count, 2)
                                  ELSE NULL END
            WHERE game_id = target;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION logs_sync_game_stats() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM game_stats_apply(NEW.game_id,
                    CASE WHEN NEW.rating IS NOT NULL THEN 1 ELSE 0 END,
                    coalesce(NEW.rating, 0)::bigint,
                    1,
                    CASE WHEN NEW.review IS NOT NULL THEN 1 ELSE 0 END,
                    CASE WHEN NEW.favorite THEN 1 ELSE 0 END);
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                PERFORM game_stats_apply(OLD.game_id,
                    CASE WHEN OLD.rating IS NOT NULL THEN -1 ELSE 0 END,
                    -coalesce(OLD.rating, 0)::bigint,
                    -1,
                    CASE WHEN OLD.review IS NOT NULL THEN -1 ELSE 0 END,
                    CASE WHEN OLD.favorite THEN -1 ELSE 0 END);
                RETURN OLD;
            END IF;

            -- UPDATE. A game_id change is two moves, so back the old row out of its game and
            -- add the new one to its own rather than trying to net the deltas.
            IF OLD.game_id <> NEW.game_id THEN
                PERFORM game_stats_apply(OLD.game_id,
                    CASE WHEN OLD.rating IS NOT NULL THEN -1 ELSE 0 END,
                    -coalesce(OLD.rating, 0)::bigint, -1,
                    CASE WHEN OLD.review IS NOT NULL THEN -1 ELSE 0 END,
                    CASE WHEN OLD.favorite THEN -1 ELSE 0 END);
                PERFORM game_stats_apply(NEW.game_id,
                    CASE WHEN NEW.rating IS NOT NULL THEN 1 ELSE 0 END,
                    coalesce(NEW.rating, 0)::bigint, 1,
                    CASE WHEN NEW.review IS NOT NULL THEN 1 ELSE 0 END,
                    CASE WHEN NEW.favorite THEN 1 ELSE 0 END);
            ELSE
                PERFORM game_stats_apply(NEW.game_id,
                    (CASE WHEN NEW.rating IS NOT NULL THEN 1 ELSE 0 END)
                        - (CASE WHEN OLD.rating IS NOT NULL THEN 1 ELSE 0 END),
                    coalesce(NEW.rating, 0)::bigint - coalesce(OLD.rating, 0)::bigint,
                    0,
                    (CASE WHEN NEW.review IS NOT NULL THEN 1 ELSE 0 END)
                        - (CASE WHEN OLD.review IS NOT NULL THEN 1 ELSE 0 END),
                    (CASE WHEN NEW.favorite THEN 1 ELSE 0 END)
                        - (CASE WHEN OLD.favorite THEN 1 ELSE 0 END));
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER logs_game_stats_sync
        AFTER INSERT OR UPDATE OR DELETE ON logs
        FOR EACH ROW EXECUTE FUNCTION logs_sync_game_stats()
    """)

    # ---------------------------------------------------------------------------- comments
    op.execute("""
        CREATE TABLE comments (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            log_id     uuid NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
            user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            parent_id  uuid REFERENCES comments(id) ON DELETE CASCADE,
            body       text NOT NULL
                       CONSTRAINT comments_body_len
                       CHECK (char_length(body) BETWEEN 1 AND 600),
            state      text NOT NULL DEFAULT 'visible'
                       CONSTRAINT comments_state_known
                       CHECK (state IN ('visible', 'hidden')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz,
            CONSTRAINT comments_not_own_parent CHECK (parent_id <> id)
        )
    """)
    op.execute("CREATE INDEX comments_log_idx ON comments (log_id, created_at)")
    op.execute("CREATE INDEX comments_user_idx ON comments (user_id)")
    op.execute(
        "CREATE INDEX comments_parent_idx ON comments (parent_id) WHERE parent_id IS NOT NULL"
    )

    # ----------------------------------------------------------------------------- follows
    op.execute("""
        CREATE TABLE follows (
            follower_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            followee_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (follower_id, followee_id),
            CONSTRAINT follows_no_self CHECK (follower_id <> followee_id)
        )
    """)
    op.execute("CREATE INDEX follows_followee_idx ON follows (followee_id, created_at DESC)")

    # ------------------------------------------------------------------------------ blocks
    op.execute("""
        CREATE TABLE blocks (
            blocker_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            blocked_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (blocker_id, blocked_id),
            CONSTRAINT blocks_no_self CHECK (blocker_id <> blocked_id)
        )
    """)
    op.execute("CREATE INDEX blocks_blocked_idx ON blocks (blocked_id)")

    # ----------------------------------------------------- conversations and messages
    #
    # Membership lives in its own table so that every message query can join it and filter on
    # the authenticated user in the same WHERE clause. Phase 10 is built on that: the
    # membership predicate is part of the fetch, not a check performed afterwards.
    op.execute("""
        CREATE TABLE conversations (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE conversation_members (
            conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            last_read_at    timestamptz,
            muted           boolean NOT NULL DEFAULT false,
            joined_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (conversation_id, user_id)
        )
    """)
    op.execute("CREATE INDEX conversation_members_user_idx ON conversation_members (user_id)")
    op.execute("""
        CREATE TABLE messages (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            sender_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body            text NOT NULL
                            CONSTRAINT messages_body_len
                            CHECK (char_length(body) BETWEEN 1 AND 1000),
            created_at      timestamptz NOT NULL DEFAULT now(),
            deleted_at      timestamptz
        )
    """)
    op.execute(
        "CREATE INDEX messages_conversation_idx ON messages (conversation_id, created_at DESC)"
    )
    # A sender who is not a member of the conversation is the exact shape of the IDOR bug
    # Phase 10 exists to prevent. The application will filter on membership; this makes the
    # invalid row unrepresentable even if it does not.
    op.execute("""
        CREATE FUNCTION messages_require_membership() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM conversation_members
                WHERE conversation_id = NEW.conversation_id AND user_id = NEW.sender_id
            ) THEN
                RAISE EXCEPTION 'sender % is not a member of conversation %',
                    NEW.sender_id, NEW.conversation_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER messages_sender_is_member
        BEFORE INSERT OR UPDATE OF conversation_id, sender_id ON messages
        FOR EACH ROW EXECUTE FUNCTION messages_require_membership()
    """)

    # ----------------------------------------------------------------------------- reports
    op.execute("""
        CREATE TABLE reports (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            reporter_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            target_type text NOT NULL
                        CONSTRAINT reports_target_type_known
                        CHECK (target_type IN ('log', 'comment', 'message', 'user')),
            target_id   uuid NOT NULL,
            reason      text NOT NULL
                        CONSTRAINT reports_reason_known
                        CHECK (reason IN ('spam', 'abuse', 'hate', 'sexual',
                                          'spoiler', 'other')),
            note        text
                        CONSTRAINT reports_note_len
                        CHECK (note IS NULL OR char_length(note) <= 1000),
            status      text NOT NULL DEFAULT 'open'
                        CONSTRAINT reports_status_known
                        CHECK (status IN ('open', 'actioned', 'dismissed')),
            created_at  timestamptz NOT NULL DEFAULT now(),
            resolved_by uuid REFERENCES users(id) ON DELETE SET NULL,
            resolved_at timestamptz,
            -- One report per person per thing. Report abuse is itself abusable.
            CONSTRAINT reports_one_per_reporter_target
                UNIQUE (reporter_id, target_type, target_id),
            CONSTRAINT reports_resolution_is_complete
                CHECK ((status = 'open') = (resolved_at IS NULL))
        )
    """)
    op.execute("CREATE INDEX reports_open_idx ON reports (created_at) WHERE status = 'open'")

    # ------------------------------------------------------------------ moderation_events
    #
    # rule_id and a hash, never the offending text. Storing what we refused would defeat the
    # point of refusing it — the same reasoning as js/moderation.js.
    op.execute("""
        CREATE TABLE moderation_events (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            actor_id    uuid REFERENCES users(id) ON DELETE SET NULL,
            action      text NOT NULL
                        CONSTRAINT moderation_events_action_known
                        CHECK (action IN ('blocked_write', 'hidden', 'unhidden',
                                          'suspended', 'unsuspended', 'deleted')),
            target_type text NOT NULL
                        CONSTRAINT moderation_events_target_known
                        CHECK (target_type IN ('log', 'comment', 'message', 'user', 'profile')),
            target_id   uuid,
            rule_id     text
                        CONSTRAINT moderation_events_rule_len
                        CHECK (rule_id IS NULL OR char_length(rule_id) <= 80),
            content_sha bytea
                        CONSTRAINT moderation_events_sha_len
                        CHECK (content_sha IS NULL OR octet_length(content_sha) = 32),
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX moderation_events_created_idx ON moderation_events (created_at)")
    op.execute(
        "CREATE INDEX moderation_events_target_idx ON moderation_events "
        "(target_type, target_id)"
    )

    # --------------------------------------------------------------------------- audit_log
    op.execute("""
        CREATE TABLE audit_log (
            id            bigserial PRIMARY KEY,
            at            timestamptz NOT NULL DEFAULT now(),
            actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
            event         text NOT NULL
                          CONSTRAINT audit_log_event_len
                          CHECK (char_length(event) BETWEEN 1 AND 80),
            ip_hash       bytea
                          CONSTRAINT audit_log_ip_hash_len
                          CHECK (ip_hash IS NULL OR octet_length(ip_hash) = 16),
            detail        jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("CREATE INDEX audit_log_at_idx ON audit_log (at DESC)")
    op.execute("CREATE INDEX audit_log_actor_idx ON audit_log (actor_user_id, at DESC)")

    # ------------------------------------------------------------------------ rate_limits
    #
    # The Postgres token bucket that replaces Phase 1's in-memory scaffolding. Here rather
    # than in Phase 5 because the table is schema, and config already refuses to boot in
    # production with the memory backend.
    op.execute("""
        CREATE TABLE rate_limits (
            bucket_key text PRIMARY KEY
                       CONSTRAINT rate_limits_key_len
                       CHECK (char_length(bucket_key) BETWEEN 1 AND 200),
            hits       integer NOT NULL DEFAULT 0
                       CONSTRAINT rate_limits_hits_nonneg CHECK (hits >= 0),
            resets_at  timestamptz NOT NULL
        )
    """)
    op.execute("CREATE INDEX rate_limits_resets_idx ON rate_limits (resets_at)")

    # --------------------------------------------------------------------------- updated_at
    # One trigger function, applied to every table carrying the column. Doing this in the
    # application means one forgotten assignment produces a silently stale timestamp.
    op.execute("""
        CREATE FUNCTION touch_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    # Written out rather than looped. The loop needed an interpolated table name, which meant
    # an escape-hatch annotation on the SQL gate for four statements — cheaper to just say them.
    op.execute(
        "CREATE TRIGGER users_touch_updated_at BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER games_touch_updated_at BEFORE UPDATE ON games "
        "FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER logs_touch_updated_at BEFORE UPDATE ON logs "
        "FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER comments_touch_updated_at BEFORE UPDATE ON comments "
        "FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
    )


def downgrade() -> None:
    # Reverse dependency order. Tables first, then the functions nothing references any more.
    for table in (
        "rate_limits",
        "audit_log",
        "moderation_events",
        "reports",
        "messages",
        "conversation_members",
        "conversations",
        "blocks",
        "follows",
        "comments",
        "game_stats",
        "logs",
        "game_covers",
        "games",
        "email_tokens",
        "sessions",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")  # noqa: S608  # sql-safe: literal

    # users and avatars reference each other, so the constraint goes before the tables.
    op.execute("ALTER TABLE IF EXISTS users DROP CONSTRAINT IF EXISTS users_avatar_fk")
    op.execute("DROP TABLE IF EXISTS avatars CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")

    for function in (
        "touch_updated_at()",
        "messages_require_membership()",
        "logs_sync_game_stats()",
        "game_stats_apply(text, int, bigint, int, int, int)",
        "logs_reject_future_played_on()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")  # noqa: S608  # sql-safe: literal

    # citext is left in place: it may predate this migration and other schemas could use it.
