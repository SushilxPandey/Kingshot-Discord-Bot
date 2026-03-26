import sqlite3

DB_PATH = "kingshot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)  # creates the file if it doesn't exist
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            discord_id   TEXT PRIMARY KEY,              --  primary key
            ingame_name  TEXT,
            ingame_id    INTEGER UNIQUE,         --  unique constraint
            kingdom      INTEGER,
            alliance     TEXT,
            verified_at  TEXT
)
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS server_config(
            guild_id TEXT PRIMARY KEY,
            start_role TEXT,
            verified_role TEXT,
            owner_role TEXT,
            verify_channel TEXT,
            general_channel TEXT,
            allowed_kingdoms INTEGER,
            allowed_level INTEGER
)
    """)

    conn.commit()
    conn.close()



def save_player(discord_id, ingame_name, ingame_id, kingdom, alliance):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO players (discord_id, ingame_name, ingame_id, kingdom, alliance, verified_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
    """, (discord_id, ingame_name, ingame_id, kingdom, alliance))

    conn.commit()
    conn.close()

def get_player(discord_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE discord_id = ?", (discord_id,))
    player = c.fetchone()
    conn.close()
    return player

def delete_player(discord_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM players WHERE discord_id = ?", (discord_id,))
    conn.commit()
    conn.close()
    
def save_server_config(guild_id, start_role, verified_role, owner_role, verify_channel, general_channel, allowed_kingdoms, allowed_level):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO server_config (guild_id, start_role, verified_role, owner_role, verify_channel, general_channel, allowed_kingdoms, allowed_level)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (guild_id, start_role, verified_role, owner_role, verify_channel, general_channel, allowed_kingdoms, allowed_level))

    conn.commit()
    conn.close()

def get_server_config(guild_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM server_config WHERE guild_id = ?", (guild_id,))
    config = c.fetchone()
    conn.close()
    return config
