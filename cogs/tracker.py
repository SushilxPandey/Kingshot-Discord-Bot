"""
Name tracker — disabled.

Kingshot removed its public player-lookup API, so automatically detecting in-game
name changes is no longer possible. This cog is kept as a harmless no-op (no
commands, no background loop) so the extension list stays stable; re-implement it
here if a working player-lookup endpoint ever returns.
"""

import logging

from discord.ext import commands


class Tracker(commands.Cog, name="Tracker"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logging.info("Name tracker disabled — Kingshot player-lookup API is unavailable.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Tracker(bot))
