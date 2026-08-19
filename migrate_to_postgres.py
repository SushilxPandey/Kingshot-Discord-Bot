"""
One-time migration: copy the old per-guild SQLite files (data/<guild_id>.db)
into Postgres, keyed by guild_id.

Run ONCE after setting DATABASE_URL in your .env:
    python migrate_to_postgres.py

Safe to re-run — it upserts config and skips rows that already exist. It reads
the guild_id from each file name, so nothing is lost.
"""

import asyncio
import glob
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

import database

load_dotenv()
DATA_DIR = os.getenv("DATA_DIR", "data")


def _ts(value):
    """Parse SQLite's 'YYYY-MM-DD HH:MM:SS' (UTC) into an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _read_table(conn, table):
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    except sqlite3.OperationalError:
        return []  # table doesn't exist in this (older) file


async def _migrate_file(pool, path):
    guild_id = os.path.splitext(os.path.basename(path))[0]
    sconn = sqlite3.connect(path)
    counts = {}
    async with pool.acquire() as pg:
        # config (dynamic columns — all non-timestamp)
        for row in _read_table(sconn, "config"):
            cols = ["guild_id"] + [c for c in database.CONFIG_FIELDS if c in row]
            values = [guild_id] + [row[c] for c in cols[1:]]
            ph = ",".join(f"${i+1}" for i in range(len(cols)))
            updates = ",".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "guild_id")
            await pg.execute(
                f"INSERT INTO config ({','.join(cols)}) VALUES ({ph}) "
                f"ON CONFLICT (guild_id) DO UPDATE SET {updates}",
                *values,
            )
            counts["config"] = counts.get("config", 0) + 1

        for row in _read_table(sconn, "players"):
            await pg.execute(
                """INSERT INTO players (guild_id, discord_id, ingame_name, ingame_id, kingdom,
                        alliance, town_level, verified_at, last_checked)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (guild_id, discord_id) DO NOTHING""",
                guild_id, str(row.get("discord_id")), row.get("ingame_name"), row.get("ingame_id"),
                row.get("kingdom"), row.get("alliance"), row.get("town_level"),
                _ts(row.get("verified_at")), _ts(row.get("last_checked")),
            )
            counts["players"] = counts.get("players", 0) + 1

        for row in _read_table(sconn, "alliances"):
            await pg.execute(
                """INSERT INTO alliances (guild_id, tag, name, member_role_id, leader_role_id,
                        category_id, chat_channel_id, leaders_channel_id, voice_channel_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (guild_id, tag) DO NOTHING""",
                guild_id, str(row.get("tag")).upper(), row.get("name"), row.get("member_role_id"),
                row.get("leader_role_id"), row.get("category_id"), row.get("chat_channel_id"),
                row.get("leaders_channel_id"), row.get("voice_channel_id"),
            )
            counts["alliances"] = counts.get("alliances", 0) + 1

        for row in _read_table(sconn, "warnings"):
            await pg.execute(
                "INSERT INTO warnings (guild_id, discord_id, count) VALUES ($1,$2,$3) "
                "ON CONFLICT (guild_id, discord_id) DO NOTHING",
                guild_id, str(row.get("discord_id")), row.get("count") or 0,
            )
            counts["warnings"] = counts.get("warnings", 0) + 1

        for row in _read_table(sconn, "points"):
            await pg.execute(
                "INSERT INTO points (guild_id, discord_id, points) VALUES ($1,$2,$3) "
                "ON CONFLICT (guild_id, discord_id) DO NOTHING",
                guild_id, str(row.get("discord_id")), row.get("points") or 0,
            )
            counts["points"] = counts.get("points", 0) + 1

        for row in _read_table(sconn, "announced_codes"):
            await pg.execute(
                "INSERT INTO announced_codes (guild_id, code, created_at) VALUES ($1,$2,$3) "
                "ON CONFLICT (guild_id, code) DO NOTHING",
                guild_id, str(row.get("code")), row.get("created_at"),
            )
            counts["announced_codes"] = counts.get("announced_codes", 0) + 1

    sconn.close()
    return guild_id, counts


async def main():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.db")))
    if not files:
        print(f"No SQLite files found in '{DATA_DIR}/'. Nothing to migrate.")
        return
    await database.init_pool()   # creates the Postgres schema
    print(f"Migrating {len(files)} guild database(s) → Postgres…\n")
    for path in files:
        guild_id, counts = await _migrate_file(database._pool, path)
        summary = ", ".join(f"{k}:{v}" for k, v in counts.items()) or "empty"
        print(f"  guild {guild_id}: {summary}")
    await database.close_pool()
    print("\n✅ Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
