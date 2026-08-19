"""
Provisional-verify backfill.

When the stats site is briefly unreachable at verify time, a member is verified on
their game ID alone and their row is saved with a NULL in-game name. This cog runs a
light background loop that periodically retries those pending players against the API
and, once reachable, fills in their real name / kingdom / alliance, fixes their
nickname, grants the ceremonial alliance role, and refreshes the member-list.

It deliberately only touches pending (null-name) players — it is not a full
name-change tracker.
"""

import logging

import discord
from discord.ext import commands, tasks

import database
import kingshot_api

BACKFILL_MINUTES = 20      # how often to retry pending players
BATCH_PER_GUILD = 15       # cap lookups per guild per pass (be polite to the site)


class Tracker(commands.Cog, name="Tracker"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.backfill_pending.start()

    def cog_unload(self):
        if self.backfill_pending.is_running():
            self.backfill_pending.cancel()

    @tasks.loop(minutes=BACKFILL_MINUTES)
    async def backfill_pending(self):
        for guild_id in await database.list_guild_ids():
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            pending = await database.players_pending(guild_id)
            if not pending:
                continue
            config = await database.get_config(guild_id) or {}
            changed = False
            for row in pending[:BATCH_PER_GUILD]:
                if await self._try_fill(guild, config, row):
                    changed = True
            if changed:
                setup_cog = self.bot.get_cog("Setup")
                if setup_cog:
                    await setup_cog.refresh_member_list(guild)

    async def _try_fill(self, guild, config, row) -> bool:
        """Return True if this pending player was successfully filled in."""
        ingame_id = row.get("ingame_id")
        discord_id = row.get("discord_id")
        if not ingame_id or not discord_id:
            return False
        try:
            info = await kingshot_api.get_player_info(int(ingame_id))
            data = info.get("data") or None
        except Exception:
            return False  # still unreachable — try again next pass
        if not data:
            # Site is up but no record — just stamp last_checked so we don't hammer it.
            await database.touch_player(guild.id, discord_id)
            return False

        name = (data.get("name") or "").strip()[:32] or None
        try:
            kingdom = int(data.get("kingdom"))
        except (TypeError, ValueError):
            kingdom = row.get("kingdom")
        try:
            town_level = int(data.get("level"))
        except (TypeError, ValueError):
            town_level = None
        tag = (data.get("alliance_abbr") or "").strip().upper() or None

        await database.save_player(guild.id, discord_id, name, int(ingame_id), kingdom, tag, town_level)

        member = guild.get_member(int(discord_id))
        if member:
            verification = self.bot.get_cog("Verification")
            if verification and tag:
                await verification._sync_alliance_roles(member, row.get("alliance"), tag)
            if name:
                tag_part = f"{tag}- " if tag else ""
                nick = f"[{kingdom}] {tag_part}{name}"[:32]
                try:
                    await member.edit(nick=nick, reason="Kingshot backfill")
                except discord.HTTPException:
                    pass
        logging.info("Backfilled pending player %s in guild %s", ingame_id, guild.id)
        return True

    @backfill_pending.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Tracker(bot))
