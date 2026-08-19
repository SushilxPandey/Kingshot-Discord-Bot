# Kingshot Alliance Bot

A self-configuring Discord bot for **Kingshot** alliances. Add it to your server, press
one **Setup** button, and it builds its own roles and channels, gates newcomers behind a
one-tap verification, gives everyone live in-game intel through clickable panels, keeps
chat clean, and auto-announces new gift codes.

> **Unofficial community tool.** Not affiliated with, endorsed by, or sponsored by
> Century Games. "Kingshot" and related marks belong to their respective owners. See
> [`LEGAL_NOTES.md`](LEGAL_NOTES.md).

---

## What it does

**Verification (one tap).** New members are auto-gated behind an **Unverified** role that
can only see the verify channel. They press **Verify** and enter *only their in-game ID* —
the bot pulls their real name, kingdom, Town Center level, and alliance from live game
data, checks they're in the allowed kingdom and meet the minimum level, sets their
nickname, and unlocks the server. One Discord account can verify as exactly one in-game
player. If the data source is momentarily unreachable, they're let in on their ID and the
bot backfills the rest automatically within a few minutes.

**Button-first tool pages.** A `🛰️ Bot Tools` section where nothing but the tool works:

- **🎯 Scout Opponents** — enter any enemy player's ID; see their alliance's top players
  with power, Town Center, heroes, and gear (a KvK war-room briefing).
- **📍 Locate a Player** — kingdom, world-map coordinates, alliance, and activity.
- **🪞 My Stats** — your own detailed profile (power, ranks, kills, VIP, heroes, gear).
- **🏰 Kingdom Knowledge** — a kingdom's battle stats plus its top 10 players.
- **⚖️ Compare Players** — two players side by side.
- **🤖 Bot Commands** — the one channel where slash commands work (chat is blocked).

**Gift codes.** A dedicated read-only `#gift-codes` channel. The bot polls the public
active-codes list every couple of hours and announces genuinely new codes (pinging
Verified); admins can also push a code manually from the Bot Commands panel. *Redemption
stays manual by design* — the bot never automates redemption or bypasses bot-protection.

**Moderation.** A profanity filter with a 5-strike system (DM warnings, then a 1-hour
timeout), and a `#gifs` channel that only accepts GIFs.

**Community & War.** A social section (general, memes, gifs, Lobby voice) and a War group
(strategy, rally-leaders, rally-joiners, War voice) with ping roles.

**Admin controls.** A staff-only **Member Management** panel (and `/unverify`, `/ban`,
`/roster`) — unverifying deletes the player's record; banning removes them and deletes
their record. A live, auto-updating member-list and an audit log round it out.

---

## Requirements

- Python 3.11+
- A Discord bot application (with the **Server Members** and **Message Content** intents)
- A PostgreSQL database (e.g. a free [Neon](https://neon.tech) project)

---

## Setup

**1. Create the bot.** In the [Discord Developer Portal](https://discord.com/developers/applications):
create an application → **Bot** → enable **Server Members Intent** and **Message Content
Intent** → copy the token.

**2. Invite it.** Use an OAuth2 URL with **both** the `bot` and `applications.commands`
scopes, and grant at least: Manage Roles, Manage Channels, Manage Nicknames, Ban Members,
Moderate Members, Send Messages, Read Message History. Put the bot's role near the top of
the role list (it can only manage roles/nicknames below its own).

**3. Configure.** Copy `.env.example` to `.env` and fill in your values:

```
DISCORD_TOKEN=your-bot-token
DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require
```

Never commit `.env` — it's already gitignored.

**4. Install & run.**

```bash
python -m pip install -r requirements.txt
python main.py
```

**5. In Discord.** The bot creates a private `#bot-setup` channel only you can see. Open
it, press **Setup**, enter your kingdom number and minimum Town Center level, and it builds
everything. Existing members are moved to Unverified so they can verify with the button.

To rebuild cleanly at any time, press **Re-Setup** (it wipes only the bot's own
roles/channels and rebuilds them; your player records are kept).

---

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Your Discord bot token. |
| `DATABASE_URL` | ✅ | PostgreSQL connection string. |
| `TEST_GUILD` | — | A server ID for instant slash-command sync while developing. |
| `BADWORDS_FILE` | — | Path to the profanity word list (default `badwords.txt`). |
| `KSTATS_BASE` / `KINGSHOT_NET_BASE` | — | Override the data-source base URLs. |

---

## Project layout

```
main.py            Entry point: loads cogs, registers persistent views, syncs commands.
database.py        PostgreSQL storage (asyncpg); one DB, every table keyed by guild_id.
kingshot_api.py    Live game-data lookups (players, alliances, kingdoms, gift codes).
views/             Persistent verify button + modal.
cogs/
  setup_cog.py     Setup control room, channel/role provisioning, member-list.
  verification.py  ID-only verification + ceremonial alliance roles.
  scout.py         KvK scouting panel + /scout.
  intel.py         Locate / My Stats / Kingdom / Compare panels + commands.
  giftcode.py      Auto-announce new gift codes + admin post button.
  moderation.py    Profanity filter + gifs-only channel.
  roster.py        /roster, /age, /unverify, /ban + Member Management panel.
  tracker.py       Background backfill for provisionally-verified players.
```

---

## Data & privacy

The bot stores only what it needs — Discord ID, in-game ID/name, kingdom, alliance, and
moderation counters — **partitioned per server** so no server can see another's data. A
member's record is deleted when they're unverified or banned. See
[`LEGAL_NOTES.md`](LEGAL_NOTES.md) for the full compliance rundown.

---

## Notes

- The bot reads publicly available game stats from a third-party source; that data is
  only as complete as the source has crawled (e.g. hero/gear detail shows for players it
  has already scanned).
- Multi-server: data is isolated per guild in a single database — each server effectively
  has its own dataset.
