"""
Profanity filter with a strike system.

On every message containing a banned word the bot deletes it and re-posts a
censored version (each banned word masked with ``*``×its length). It then counts
a strike against the author and **DMs them a warning** with their running total.
At the 5th strike the member is **timed out (muted) for 1 hour**, their strike
count resets, and the action is logged to the admin/bot-log channel.

Admins/mods are exempt from strikes (their messages are still censored).
"""

import logging
import os
import re
from datetime import timedelta

import discord
from discord.ext import commands

import database

BADWORDS_FILE = os.getenv("BADWORDS_FILE", "badwords.txt")
WARN_LIMIT = 5                       # strike that triggers the timeout
TIMEOUT_DURATION = timedelta(hours=1)


def _load_bad_words() -> list[str]:
    try:
        with open(BADWORDS_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logging.warning("%s not found — profanity filter disabled.", BADWORDS_FILE)
        return []


class Moderation(commands.Cog, name="Moderation"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bad_words = _load_bad_words()
        # One combined regex, longest words first, so multiple/overlapping bad
        # words in a row are all masked in a single left-to-right pass (the old
        # word-by-word loop could corrupt an overlapping match).
        self.pattern = self._compile(self.bad_words)

    @staticmethod
    def _compile(words):
        if not words:
            return None
        parts = sorted((re.escape(w) for w in words), key=len, reverse=True)
        return re.compile("|".join(parts), re.IGNORECASE)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not self.pattern:
            return

        cleaned = self.pattern.sub(lambda m: "*" * len(m.group()), message.content)
        if cleaned == message.content:
            return  # no banned words

        # Delete + repost censored (original behaviour).
        try:
            await message.delete()
            await message.channel.send(f"{message.author.display_name}: {cleaned}")
        except discord.Forbidden:
            logging.warning("Missing permission to moderate message in %s", message.channel)
        except discord.HTTPException:
            pass

        # Admins/mods are exempt from strikes.
        perms = message.author.guild_permissions
        if perms.administrator or perms.manage_messages:
            return

        await self._strike(message.guild, message.author)

    async def _strike(self, guild: discord.Guild, member: discord.Member):
        count = await database.add_warning(guild.id, member.id)

        if count < WARN_LIMIT:
            await self._dm(
                member,
                f"⚠️ Watch your language in **{guild.name}**. "
                f"That's strike **{count}/{WARN_LIMIT}** — at {WARN_LIMIT} you'll be muted for 1 hour.",
            )
            return

        # 5th strike → timeout, reset, log.
        muted = False
        try:
            await member.timeout(discord.utils.utcnow() + TIMEOUT_DURATION,
                                 reason="Kingshot: 5 profanity strikes")
            muted = True
        except discord.Forbidden:
            logging.warning("Cannot timeout %s in %s (missing perms or hierarchy).", member, guild.id)
        except discord.HTTPException:
            pass

        await database.reset_warning(guild.id, member.id)
        await self._dm(
            member,
            (f"🔇 You've hit **{WARN_LIMIT} strikes** in **{guild.name}** and have been "
             "muted for **1 hour**. Your strike count has been reset.")
            if muted else
            (f"⚠️ You've hit **{WARN_LIMIT} strikes** in **{guild.name}**. "
             "(I couldn't apply the mute — please watch your language.)"),
        )

        # Log to the admin channel.
        config = await database.get_config(guild.id)
        if config and config.get("log_channel_id"):
            channel = guild.get_channel(int(config["log_channel_id"]))
            if channel:
                verb = "muted for 1 hour" if muted else "reached 5 strikes (mute failed)"
                try:
                    await channel.send(f"🔇 {member.mention} {verb} for repeated profanity.")
                except discord.HTTPException:
                    pass

    async def _dm(self, member: discord.Member, text: str):
        try:
            await member.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass  # DMs closed — nothing we can do


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
