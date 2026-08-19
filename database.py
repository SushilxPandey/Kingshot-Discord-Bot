"""
Postgres storage for the Kingshot bot (asyncpg).

A single database holds every server's data, keyed by ``guild_id`` on each table
(replacing the old one-SQLite-file-per-guild model). The public async API is
unchanged, so the cogs did not need edits — only the backend swapped.

Set ``DATABASE_URL`` in the environment, e.g.
    postgresql://user:pass@host/dbname?sslmode=require
"""

import os
import re

import asyncpg

_pool: asyncpg.Pool | None = None

# Config columns that are Discord snowflakes — must be BIGINT in Postgres
# (INTEGER/int4 would overflow). guild_id / discord_id are stored as TEXT.
_ID_COLUMNS = (
    "unverified_role_id", "verified_role_id", "category_id", "verify_channel_id",
    "info_channel_id", "log_channel_id", "verify_message_id", "welcome_channel_id",
    "setup_channel_id", "setup_panel_message_id", "member_list_channel_id",
    "member_list_message_id", "general_channel_id", "community_category_id",
    "memes_channel_id", "gifs_channel_id", "lobby_voice_id",
    "points_board_channel_id", "points_board_message_id",
    "points_admin_channel_id", "points_panel_message_id",
    "war_category_id", "war_strategy_id", "war_voice_id",
    "rally_leaders_channel_id", "rally_joiners_channel_id",
    "rally_leader_role_id", "rally_joiner_role_id",
    # Phase 4 — GUI tool pages (each panel channel + its posted panel message).
    "tools_category_id",
    "scout_channel_id", "scout_panel_message_id",
    "locate_channel_id", "locate_panel_message_id",
    "selfstats_channel_id", "selfstats_panel_message_id",
    "kingdom_channel_id", "kingdom_panel_message_id",
    "compare_channel_id", "compare_panel_message_id",
    "commands_channel_id", "commands_panel_message_id",
    "info_message_id", "manage_panel_message_id", "giftcode_channel_id",
)
# Small integer config columns.
_INT_COLUMNS = ("allowed_kingdom", "allowed_level", "lockdown_existing")

# Everything writable via upsert_config.
CONFIG_FIELDS = _ID_COLUMNS + _INT_COLUMNS


# ──────────────────────────────────────────────────────────────
# Pool + schema
# ──────────────────────────────────────────────────────────────
def _clean_dsn(url: str) -> str:
    # asyncpg doesn't understand libpq's channel_binding parameter — strip it.
    return re.sub(r"[?&]channel_binding=\w+", "", url or "")


async def init_pool() -> None:
    """Create the connection pool and ensure the schema exists."""
    global _pool
    if _pool is None:
        url = os.getenv("DATABASE_URL")  # read at call time, after load_dotenv()
        if not url:
            raise RuntimeError("DATABASE_URL is not set in the environment / .env file.")
        _pool = await asyncpg.create_pool(dsn=_clean_dsn(url), min_size=1, max_size=10)
    await init_db()


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _pool_or_raise() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized — call init_pool() first.")
    return _pool


async def init_db() -> None:
    id_cols_sql = ",\n                ".join(f"{c} BIGINT" for c in _ID_COLUMNS)
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS config (
                guild_id          TEXT PRIMARY KEY,
                {id_cols_sql},
                allowed_kingdom   INTEGER,
                allowed_level     INTEGER,
                lockdown_existing INTEGER DEFAULT 0
            )
            """
        )
        # Upgrade older config tables in place: add any columns introduced after
        # the table was first created. CREATE TABLE IF NOT EXISTS never alters an
        # existing table, so live databases would otherwise miss new columns.
        for col in _ID_COLUMNS:
            await conn.execute(f"ALTER TABLE config ADD COLUMN IF NOT EXISTS {col} BIGINT")
        for col in _INT_COLUMNS:
            await conn.execute(f"ALTER TABLE config ADD COLUMN IF NOT EXISTS {col} INTEGER")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                guild_id     TEXT NOT NULL,
                discord_id   TEXT NOT NULL,
                ingame_name  TEXT,
                ingame_id    BIGINT,
                kingdom      INTEGER,
                alliance     TEXT,
                town_level   INTEGER,
                verified_at  TIMESTAMPTZ,
                last_checked TIMESTAMPTZ,
                PRIMARY KEY (guild_id, discord_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alliances (
                guild_id           TEXT NOT NULL,
                tag                TEXT NOT NULL,
                name               TEXT,
                member_role_id     BIGINT,
                leader_role_id     BIGINT,
                category_id        BIGINT,
                chat_channel_id    BIGINT,
                leaders_channel_id BIGINT,
                voice_channel_id   BIGINT,
                PRIMARY KEY (guild_id, tag)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                guild_id   TEXT NOT NULL,
                discord_id TEXT NOT NULL,
                count      INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, discord_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS points (
                guild_id   TEXT NOT NULL,
                discord_id TEXT NOT NULL,
                points     INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, discord_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS announced_codes (
                guild_id   TEXT NOT NULL,
                code       TEXT NOT NULL,
                created_at TEXT,
                PRIMARY KEY (guild_id, code)
            )
            """
        )


async def init_guild(guild_id) -> None:
    """Ensure a config row exists for a guild (schema is global in Postgres)."""
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            "INSERT INTO config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", str(guild_id)
        )


def _player_row(record) -> dict:
    d = dict(record)
    for k in ("verified_at", "last_checked"):
        if d.get(k) is not None and not isinstance(d[k], str):
            d[k] = d[k].isoformat(sep=" ", timespec="seconds")
    return d


# ──────────────────────────────────────────────────────────────
# Players
# ──────────────────────────────────────────────────────────────
async def save_player(guild_id, discord_id, ingame_name, ingame_id, kingdom, alliance, town_level=None):
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO players
                (guild_id, discord_id, ingame_name, ingame_id, kingdom, alliance,
                 town_level, verified_at, last_checked)
            VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())
            ON CONFLICT (guild_id, discord_id) DO UPDATE SET
                ingame_name = EXCLUDED.ingame_name,
                ingame_id   = EXCLUDED.ingame_id,
                kingdom     = EXCLUDED.kingdom,
                alliance    = EXCLUDED.alliance,
                town_level  = EXCLUDED.town_level,
                verified_at = now(),
                last_checked = now()
            """,
            str(guild_id), str(discord_id), ingame_name, ingame_id, kingdom, alliance, town_level,
        )


async def get_player(guild_id, discord_id):
    async with _pool_or_raise().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM players WHERE guild_id = $1 AND discord_id = $2",
            str(guild_id), str(discord_id),
        )
        return _player_row(row) if row else None


async def delete_player(guild_id, discord_id) -> None:
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            "DELETE FROM players WHERE guild_id = $1 AND discord_id = $2",
            str(guild_id), str(discord_id),
        )


async def all_players(guild_id) -> list[dict]:
    async with _pool_or_raise().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM players WHERE guild_id = $1 ORDER BY ingame_name",
            str(guild_id),
        )
        return [_player_row(r) for r in rows]


async def players_to_check(guild_id, limit: int = 25) -> list[dict]:
    async with _pool_or_raise().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM players WHERE guild_id = $1 ORDER BY last_checked ASC NULLS FIRST LIMIT $2",
            str(guild_id), limit,
        )
        return [_player_row(r) for r in rows]


async def update_player_name(guild_id, discord_id, new_name) -> None:
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            "UPDATE players SET ingame_name = $3, last_checked = now() WHERE guild_id = $1 AND discord_id = $2",
            str(guild_id), str(discord_id), new_name,
        )


async def touch_player(guild_id, discord_id) -> None:
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            "UPDATE players SET last_checked = now() WHERE guild_id = $1 AND discord_id = $2",
            str(guild_id), str(discord_id),
        )


async def player_by_ingame_id(guild_id, ingame_id):
    """Who (if anyone) on this server has already claimed a given in-game player ID."""
    async with _pool_or_raise().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM players WHERE guild_id = $1 AND ingame_id = $2",
            str(guild_id), int(ingame_id),
        )
        return _player_row(row) if row else None


async def players_pending(guild_id) -> list[dict]:
    """Players verified provisionally (no in-game name yet) — awaiting API backfill."""
    async with _pool_or_raise().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM players WHERE guild_id = $1 AND ingame_name IS NULL "
            "ORDER BY last_checked ASC NULLS FIRST",
            str(guild_id),
        )
        return [_player_row(r) for r in rows]


async def players_by_alliance(guild_id, tag) -> list[dict]:
    async with _pool_or_raise().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM players WHERE guild_id = $1 AND alliance = $2 ORDER BY ingame_name",
            str(guild_id), str(tag).upper(),
        )
        return [_player_row(r) for r in rows]


# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────
async def get_config(guild_id):
    async with _pool_or_raise().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM config WHERE guild_id = $1", str(guild_id))
        return dict(row) if row else None


async def upsert_config(guild_id, **values) -> None:
    fields = {k: v for k, v in values.items() if k in CONFIG_FIELDS}
    async with _pool_or_raise().acquire() as conn:
        await conn.execute("INSERT INTO config (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", str(guild_id))
        if fields:
            cols = list(fields)
            assignments = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(cols))
            await conn.execute(
                f"UPDATE config SET {assignments} WHERE guild_id = $1",
                str(guild_id), *[fields[c] for c in cols],
            )


async def list_guild_ids() -> list[str]:
    async with _pool_or_raise().acquire() as conn:
        rows = await conn.fetch("SELECT guild_id FROM config")
        return [r["guild_id"] for r in rows]


# ──────────────────────────────────────────────────────────────
# Alliances
# ──────────────────────────────────────────────────────────────
async def add_alliance(guild_id, tag, name, member_role_id, leader_role_id,
                       category_id, chat_channel_id, leaders_channel_id, voice_channel_id) -> None:
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO alliances
                (guild_id, tag, name, member_role_id, leader_role_id, category_id,
                 chat_channel_id, leaders_channel_id, voice_channel_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (guild_id, tag) DO UPDATE SET
                name = EXCLUDED.name,
                member_role_id = EXCLUDED.member_role_id,
                leader_role_id = EXCLUDED.leader_role_id,
                category_id = EXCLUDED.category_id,
                chat_channel_id = EXCLUDED.chat_channel_id,
                leaders_channel_id = EXCLUDED.leaders_channel_id,
                voice_channel_id = EXCLUDED.voice_channel_id
            """,
            str(guild_id), str(tag).upper(), name, member_role_id, leader_role_id,
            category_id, chat_channel_id, leaders_channel_id, voice_channel_id,
        )


async def upsert_alliance_role(guild_id, tag, member_role_id) -> None:
    """Record a ceremonial alliance role (tag → member role); no channels attached.

    Used when a member's alliance (from the API) first appears on a server and we
    auto-create just a role for it.
    """
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO alliances (guild_id, tag, name, member_role_id)
            VALUES ($1, $2, $2, $3)
            ON CONFLICT (guild_id, tag) DO UPDATE SET member_role_id = EXCLUDED.member_role_id
            """,
            str(guild_id), str(tag).upper(), member_role_id,
        )


async def get_alliance(guild_id, tag):
    async with _pool_or_raise().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM alliances WHERE guild_id = $1 AND tag = $2",
            str(guild_id), str(tag).upper(),
        )
        return dict(row) if row else None


async def all_alliances(guild_id) -> list[dict]:
    async with _pool_or_raise().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM alliances WHERE guild_id = $1 ORDER BY tag", str(guild_id))
        return [dict(r) for r in rows]


async def delete_alliance(guild_id, tag) -> None:
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            "DELETE FROM alliances WHERE guild_id = $1 AND tag = $2",
            str(guild_id), str(tag).upper(),
        )


async def clear_alliances(guild_id) -> None:
    async with _pool_or_raise().acquire() as conn:
        await conn.execute("DELETE FROM alliances WHERE guild_id = $1", str(guild_id))


# ──────────────────────────────────────────────────────────────
# Warnings
# ──────────────────────────────────────────────────────────────
async def add_warning(guild_id, discord_id) -> int:
    async with _pool_or_raise().acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO warnings (guild_id, discord_id, count) VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, discord_id) DO UPDATE SET count = warnings.count + 1
            RETURNING count
            """,
            str(guild_id), str(discord_id),
        )


async def get_warning(guild_id, discord_id) -> int:
    async with _pool_or_raise().acquire() as conn:
        val = await conn.fetchval(
            "SELECT count FROM warnings WHERE guild_id = $1 AND discord_id = $2",
            str(guild_id), str(discord_id),
        )
        return int(val) if val is not None else 0


async def reset_warning(guild_id, discord_id) -> None:
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            "DELETE FROM warnings WHERE guild_id = $1 AND discord_id = $2",
            str(guild_id), str(discord_id),
        )


# ──────────────────────────────────────────────────────────────
# Contribution points
# ──────────────────────────────────────────────────────────────
async def award_points(guild_id, discord_id, delta) -> int:
    async with _pool_or_raise().acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO points (guild_id, discord_id, points) VALUES ($1, $2, GREATEST(0, $3))
            ON CONFLICT (guild_id, discord_id) DO UPDATE SET points = GREATEST(0, points.points + $3)
            RETURNING points
            """,
            str(guild_id), str(discord_id), int(delta),
        )


async def get_points(guild_id, discord_id) -> int:
    async with _pool_or_raise().acquire() as conn:
        val = await conn.fetchval(
            "SELECT points FROM points WHERE guild_id = $1 AND discord_id = $2",
            str(guild_id), str(discord_id),
        )
        return int(val) if val is not None else 0


async def all_points(guild_id) -> list[dict]:
    async with _pool_or_raise().acquire() as conn:
        rows = await conn.fetch(
            "SELECT discord_id, points FROM points WHERE guild_id = $1 AND points > 0 ORDER BY points DESC",
            str(guild_id),
        )
        return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────
# Announced gift codes
# ──────────────────────────────────────────────────────────────
async def mark_code_announced(guild_id, code, created_at=None) -> bool:
    """Insert a code; returns True if NEW (first time), False if already there."""
    async with _pool_or_raise().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO announced_codes (guild_id, code, created_at) VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, code) DO NOTHING
            RETURNING code
            """,
            str(guild_id), str(code), created_at,
        )
        return row is not None


async def code_already_announced(guild_id, code) -> bool:
    async with _pool_or_raise().acquire() as conn:
        val = await conn.fetchval(
            "SELECT 1 FROM announced_codes WHERE guild_id = $1 AND code = $2",
            str(guild_id), str(code),
        )
        return val is not None


async def announced_count(guild_id) -> int:
    async with _pool_or_raise().acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM announced_codes WHERE guild_id = $1", str(guild_id)
        ))
