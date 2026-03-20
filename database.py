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
    