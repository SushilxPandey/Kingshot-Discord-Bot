"""
Per-guild SQLite storage for the Kingshot bot.

Each Discord server gets its OWN database file at ``data/<guild_id>.db`` so a
server's roster is fully isolated from every other server's (requirement 7).

Every public function is async and offloads the blocking ``sqlite3`` work to a
worker thread via ``asyncio.to_thread`` so the Discord event loop never stalls,
even while the background name-tracker is running across many guilds.
"""

import asyncio
import os
import sqlite3
from typing import Any, Optional

# Directory that holds one <guild_id>.db per server.
DATA_DIR = os.getenv("DATA_DIR", "data")


# ──────────────────────────────────────────────────────────────
# Low-level helpers (synchronous; always called through asyncio.to_thread)
# ──────────────────────────────────────────────────────────────
def _db_path(guild_id: int | str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"{guild_id}.db")


def _connect(guild_id: int | str) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(guild_id))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_schema(guild_id: int | str) -> None:
    conn = _connect(guild_id)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                discord_id   TEXT PRIMARY KEY,
                ingame_name  TEXT,
                ingame_id    INTEGER UNIQUE,
                kingdom      INTEGER,
                alliance     TEXT,
                town_level   INTEGER,
                verified_at  TEXT,
                last_checked TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                guild_id           TEXT PRIMARY KEY,
                unverified_role_id INTEGER,
                verified_role_id   INTEGER,
                category_id        INTEGER,
                verify_channel_id  INTEGER,
                info_channel_id    INTEGER,
                log_channel_id     INTEGER,
                verify_message_id  INTEGER,
                welcome_channel_id INTEGER,
                setup_channel_id   INTEGER,
                setup_panel_message_id INTEGER,
                member_list_channel_id INTEGER,
                member_list_message_id INTEGER,
                general_channel_id INTEGER,
                community_category_id INTEGER,
                memes_channel_id   INTEGER,
                gifs_channel_id    INTEGER,
                lobby_voice_id     INTEGER,
                points_board_channel_id INTEGER,
                points_board_message_id INTEGER,
                points_admin_channel_id INTEGER,
                points_panel_message_id INTEGER,
                war_category_id    INTEGER,
                war_strategy_id    INTEGER,
                war_voice_id       INTEGER,
                rally_leaders_channel_id INTEGER,
                rally_joiners_channel_id INTEGER,
                rally_leader_role_id INTEGER,
                rally_joiner_role_id INTEGER,
                allowed_kingdom    INTEGER,
                allowed_level      INTEGER,
                lockdown_existing  INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alliances (
                tag                TEXT PRIMARY KEY,
                name               TEXT,
                member_role_id     INTEGER,
                leader_role_id     INTEGER,
                category_id        INTEGER,
                chat_channel_id    INTEGER,
                leaders_channel_id INTEGER,
                voice_channel_id   INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                discord_id TEXT PRIMARY KEY,
                count      INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS points (
                discord_id TEXT PRIMARY KEY,
                points     INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS announced_codes (
                code       TEXT PRIMARY KEY,
                created_at TEXT
            )
            """
        )
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created (phase-2+)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(config)")}
    added_columns = (
        "welcome_channel_id",
        "setup_channel_id",
        "setup_panel_message_id",
        "member_list_channel_id",
        "member_list_message_id",
        "general_channel_id",
        "community_category_id",
        "memes_channel_id",
        "gifs_channel_id",
        "lobby_voice_id",
        "points_board_channel_id",
        "points_board_message_id",
        "points_admin_channel_id",
        "points_panel_message_id",
        "war_category_id",
        "war_strategy_id",
        "war_voice_id",
        "rally_leaders_channel_id",
        "rally_joiners_channel_id",
        "rally_leader_role_id",
        "rally_joiner_role_id",
    )
    for column in added_columns:
        if column not in existing:
            conn.execute(f"ALTER TABLE config ADD COLUMN {column} INTEGER")


# ──────────────────────────────────────────────────────────────
# Public async API — schema
# ──────────────────────────────────────────────────────────────
async def init_guild(guild_id: int | str) -> None:
    """Create (if needed) the database file and tables for one guild."""
    await asyncio.to_thread(_ensure_schema, guild_id)


# ──────────────────────────────────────────────────────────────
# Public async API — players
# ──────────────────────────────────────────────────────────────
def _save_player(guild_id, discord_id, ingame_name, ingame_id, kingdom, alliance, town_level) -> None:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO players
                (discord_id, ingame_name, ingame_id, kingdom, alliance,
                 town_level, verified_at, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (str(discord_id), ingame_name, ingame_id, kingdom, alliance, town_level),
        )
        conn.commit()
    finally:
        conn.close()


async def save_player(guild_id, discord_id, ingame_name, ingame_id, kingdom, alliance, town_level=None):
    await asyncio.to_thread(
        _save_player, guild_id, discord_id, ingame_name, ingame_id, kingdom, alliance, town_level
    )


def _get_player(guild_id, discord_id) -> Optional[dict]:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        row = conn.execute(
            "SELECT * FROM players WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def get_player(guild_id, discord_id) -> Optional[dict]:
    return await asyncio.to_thread(_get_player, guild_id, discord_id)


def _delete_player(guild_id, discord_id) -> None:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute("DELETE FROM players WHERE discord_id = ?", (str(discord_id),))
        conn.commit()
    finally:
        conn.close()


async def delete_player(guild_id, discord_id) -> None:
    await asyncio.to_thread(_delete_player, guild_id, discord_id)


def _all_players(guild_id) -> list[dict]:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        rows = conn.execute(
            "SELECT * FROM players ORDER BY ingame_name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def all_players(guild_id) -> list[dict]:
    """Full roster for one guild (used by /roster export)."""
    return await asyncio.to_thread(_all_players, guild_id)


def _players_to_check(guild_id, limit: int) -> list[dict]:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        rows = conn.execute(
            "SELECT * FROM players ORDER BY last_checked IS NULL DESC, last_checked ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def players_to_check(guild_id, limit: int = 25) -> list[dict]:
    """Stalest-first slice of players for the name-change tracker."""
    return await asyncio.to_thread(_players_to_check, guild_id, limit)


def _update_player_name(guild_id, discord_id, new_name) -> None:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute(
            "UPDATE players SET ingame_name = ?, last_checked = datetime('now') WHERE discord_id = ?",
            (new_name, str(discord_id)),
        )
        conn.commit()
    finally:
        conn.close()


async def update_player_name(guild_id, discord_id, new_name) -> None:
    await asyncio.to_thread(_update_player_name, guild_id, discord_id, new_name)


def _touch_player(guild_id, discord_id) -> None:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute(
            "UPDATE players SET last_checked = datetime('now') WHERE discord_id = ?",
            (str(discord_id),),
        )
        conn.commit()
    finally:
        conn.close()


async def touch_player(guild_id, discord_id) -> None:
    """Stamp last_checked without changing the name (nothing changed this cycle)."""
    await asyncio.to_thread(_touch_player, guild_id, discord_id)


# ──────────────────────────────────────────────────────────────
# Public async API — per-guild config (single row)
# ──────────────────────────────────────────────────────────────
def _get_config(guild_id) -> Optional[dict]:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        row = conn.execute(
            "SELECT * FROM config WHERE guild_id = ?", (str(guild_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def get_config(guild_id) -> Optional[dict]:
    return await asyncio.to_thread(_get_config, guild_id)


# Columns that upsert_config is allowed to write.
_CONFIG_FIELDS = (
    "unverified_role_id",
    "verified_role_id",
    "category_id",
    "verify_channel_id",
    "info_channel_id",
    "log_channel_id",
    "verify_message_id",
    "welcome_channel_id",
    "setup_channel_id",
    "setup_panel_message_id",
    "member_list_channel_id",
    "member_list_message_id",
    "general_channel_id",
    "community_category_id",
    "memes_channel_id",
    "gifs_channel_id",
    "lobby_voice_id",
    "points_board_channel_id",
    "points_board_message_id",
    "points_admin_channel_id",
    "points_panel_message_id",
    "war_category_id",
    "war_strategy_id",
    "war_voice_id",
    "rally_leaders_channel_id",
    "rally_joiners_channel_id",
    "rally_leader_role_id",
    "rally_joiner_role_id",
    "allowed_kingdom",
    "allowed_level",
    "lockdown_existing",
)


def _upsert_config(guild_id, values: dict[str, Any]) -> None:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        # Ensure the row exists, then update only the provided fields so callers
        # can save partial progress through the setup wizard.
        conn.execute(
            "INSERT OR IGNORE INTO config (guild_id) VALUES (?)", (str(guild_id),)
        )
        fields = {k: v for k, v in values.items() if k in _CONFIG_FIELDS}
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            params = list(fields.values()) + [str(guild_id)]
            conn.execute(
                f"UPDATE config SET {assignments} WHERE guild_id = ?", params
            )
        conn.commit()
    finally:
        conn.close()


async def upsert_config(guild_id, **values) -> None:
    """Create-or-update the guild's config row, writing only the given fields."""
    await asyncio.to_thread(_upsert_config, guild_id, values)


def _list_guild_ids() -> list[str]:
    if not os.path.isdir(DATA_DIR):
        return []
    return [
        os.path.splitext(f)[0]
        for f in os.listdir(DATA_DIR)
        if f.endswith(".db")
    ]


async def list_guild_ids() -> list[str]:
    """Every guild that has a database file (used by the tracker loop)."""
    return await asyncio.to_thread(_list_guild_ids)


# ──────────────────────────────────────────────────────────────
# Public async API — alliances
# ──────────────────────────────────────────────────────────────
def _add_alliance(guild_id, tag, name, member_role_id, leader_role_id,
                  category_id, chat_channel_id, leaders_channel_id, voice_channel_id) -> None:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO alliances
                (tag, name, member_role_id, leader_role_id, category_id,
                 chat_channel_id, leaders_channel_id, voice_channel_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tag, name, member_role_id, leader_role_id, category_id,
             chat_channel_id, leaders_channel_id, voice_channel_id),
        )
        conn.commit()
    finally:
        conn.close()


async def add_alliance(guild_id, tag, name, member_role_id, leader_role_id,
                       category_id, chat_channel_id, leaders_channel_id, voice_channel_id) -> None:
    await asyncio.to_thread(
        _add_alliance, guild_id, tag, name, member_role_id, leader_role_id,
        category_id, chat_channel_id, leaders_channel_id, voice_channel_id,
    )


def _get_alliance(guild_id, tag) -> Optional[dict]:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        row = conn.execute(
            "SELECT * FROM alliances WHERE tag = ?", (str(tag).upper(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def get_alliance(guild_id, tag) -> Optional[dict]:
    return await asyncio.to_thread(_get_alliance, guild_id, tag)


def _all_alliances(guild_id) -> list[dict]:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        rows = conn.execute("SELECT * FROM alliances ORDER BY tag").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def all_alliances(guild_id) -> list[dict]:
    return await asyncio.to_thread(_all_alliances, guild_id)


def _delete_alliance(guild_id, tag) -> None:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute("DELETE FROM alliances WHERE tag = ?", (str(tag).upper(),))
        conn.commit()
    finally:
        conn.close()


async def delete_alliance(guild_id, tag) -> None:
    await asyncio.to_thread(_delete_alliance, guild_id, tag)


def _clear_alliances(guild_id) -> None:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute("DELETE FROM alliances")
        conn.commit()
    finally:
        conn.close()


async def clear_alliances(guild_id) -> None:
    """Remove every alliance row for a guild (used by the setup wipe)."""
    await asyncio.to_thread(_clear_alliances, guild_id)


def _players_by_alliance(guild_id, tag) -> list[dict]:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        rows = conn.execute(
            "SELECT * FROM players WHERE alliance = ? ORDER BY ingame_name COLLATE NOCASE",
            (str(tag).upper(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def players_by_alliance(guild_id, tag) -> list[dict]:
    """Every stored player whose alliance tag matches (used by /giftcode)."""
    return await asyncio.to_thread(_players_by_alliance, guild_id, tag)


# ──────────────────────────────────────────────────────────────
# Public async API — moderation warnings
# ──────────────────────────────────────────────────────────────
def _add_warning(guild_id, discord_id) -> int:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute(
            """
            INSERT INTO warnings (discord_id, count) VALUES (?, 1)
            ON CONFLICT(discord_id) DO UPDATE SET count = count + 1
            """,
            (str(discord_id),),
        )
        conn.commit()
        row = conn.execute(
            "SELECT count FROM warnings WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        return int(row["count"]) if row else 0
    finally:
        conn.close()


async def add_warning(guild_id, discord_id) -> int:
    """Increment a member's strike count and return the new total."""
    return await asyncio.to_thread(_add_warning, guild_id, discord_id)


def _get_warning(guild_id, discord_id) -> int:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        row = conn.execute(
            "SELECT count FROM warnings WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
        return int(row["count"]) if row else 0
    finally:
        conn.close()


async def get_warning(guild_id, discord_id) -> int:
    return await asyncio.to_thread(_get_warning, guild_id, discord_id)


def _reset_warning(guild_id, discord_id) -> None:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute("DELETE FROM warnings WHERE discord_id = ?", (str(discord_id),))
        conn.commit()
    finally:
        conn.close()


async def reset_warning(guild_id, discord_id) -> None:
    """Clear a member's strikes (after escalation, or an admin reset)."""
    await asyncio.to_thread(_reset_warning, guild_id, discord_id)


# ──────────────────────────────────────────────────────────────
# Public async API — contribution points
# ──────────────────────────────────────────────────────────────
def _award_points(guild_id, discord_id, delta) -> int:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        conn.execute("INSERT OR IGNORE INTO points (discord_id, points) VALUES (?, 0)", (str(discord_id),))
        conn.execute(
            "UPDATE points SET points = MAX(0, points + ?) WHERE discord_id = ?",
            (int(delta), str(discord_id)),
        )
        conn.commit()
        row = conn.execute("SELECT points FROM points WHERE discord_id = ?", (str(discord_id),)).fetchone()
        return int(row["points"]) if row else 0
    finally:
        conn.close()


async def award_points(guild_id, discord_id, delta) -> int:
    """Add (or subtract, if delta<0) points; total is clamped at 0. Returns new total."""
    return await asyncio.to_thread(_award_points, guild_id, discord_id, delta)


def _get_points(guild_id, discord_id) -> int:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        row = conn.execute("SELECT points FROM points WHERE discord_id = ?", (str(discord_id),)).fetchone()
        return int(row["points"]) if row else 0
    finally:
        conn.close()


async def get_points(guild_id, discord_id) -> int:
    return await asyncio.to_thread(_get_points, guild_id, discord_id)


def _all_points(guild_id) -> list[dict]:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        rows = conn.execute(
            "SELECT discord_id, points FROM points WHERE points > 0 ORDER BY points DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def all_points(guild_id) -> list[dict]:
    """All members with a positive points total, highest first."""
    return await asyncio.to_thread(_all_points, guild_id)


# ──────────────────────────────────────────────────────────────
# Public async API — announced gift codes (auto-announce dedupe)
# ──────────────────────────────────────────────────────────────
def _mark_code_announced(guild_id, code, created_at) -> bool:
    """Returns True if this is a NEW code (inserted), False if already announced."""
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO announced_codes (code, created_at) VALUES (?, ?)",
            (str(code), created_at),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


async def mark_code_announced(guild_id, code, created_at=None) -> bool:
    return await asyncio.to_thread(_mark_code_announced, guild_id, code, created_at)


def _code_already_announced(guild_id, code) -> bool:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        row = conn.execute("SELECT 1 FROM announced_codes WHERE code = ?", (str(code),)).fetchone()
        return row is not None
    finally:
        conn.close()


async def code_already_announced(guild_id, code) -> bool:
    return await asyncio.to_thread(_code_already_announced, guild_id, code)


def _announced_count(guild_id) -> int:
    _ensure_schema(guild_id)
    conn = _connect(guild_id)
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM announced_codes").fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


async def announced_count(guild_id) -> int:
    """How many codes we've recorded — 0 means this guild has never polled."""
    return await asyncio.to_thread(_announced_count, guild_id)
