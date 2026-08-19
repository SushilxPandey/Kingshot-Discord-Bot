"""
Gift-code announcements.

Century Games locked their gift-code redemption API behind bot-protection, so the
bot can't redeem codes automatically. Instead it:

  * **Auto-announces** newly-active codes to #general on a schedule (the public
    active-codes LIST endpoint is still open), and
  * lets an **admin post a code manually** — via `/giftcode` or the "Post a gift
    code" button on the bot-commands panel — which broadcasts it to #general and
    pings the Verified role.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database
import kingshot_api

REDEEM_URL = "https://ks-giftcode.centurygame.com/"
POLL_HOURS = 2   # how often to check for new active codes


def _code_embed(code: str, author: discord.abc.User | None = None) -> discord.Embed:
    embed = discord.Embed(
        title="🎁 New Gift Code!",
        color=discord.Color.gold(),
        description=(
            f"**Code:** `{code}`\n\n"
            "**How to redeem:**\n"
            "• In-game: **Settings → Gift Code**, paste the code.\n"
            f"• Or online at {REDEEM_URL} — enter your **Player ID**, the code, "
            "solve the captcha, and hit redeem.\n\n"
            "Codes expire, so grab it soon!"
        ),
    )
    if author is not None:
        embed.set_footer(text=f"Shared by {author.display_name}")
    return embed


class GiftCode(commands.Cog, name="GiftCode"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_codes.start()

    def cog_unload(self):
        if self.check_codes.is_running():
            self.check_codes.cancel()

    async def post_code(self, guild: discord.Guild, code: str, author: discord.abc.User) -> tuple[bool, str]:
        """Broadcast a gift code to #general, pinging Verified. Returns (ok, message)."""
        code = (code or "").strip()
        if not code:
            return False, "Please enter a code."
        config = await database.get_config(guild.id)
        cid = (config or {}).get("giftcode_channel_id") or (config or {}).get("general_channel_id")
        if not config or not cid:
            return False, "This server isn't set up yet (no gift-code channel)."
        channel = guild.get_channel(int(cid))
        if not channel:
            return False, "I couldn't find the gift-code channel."
        verified_role = guild.get_role(config["verified_role_id"]) if config.get("verified_role_id") else None
        content = verified_role.mention if verified_role else None
        allowed = discord.AllowedMentions(roles=[verified_role] if verified_role else False)
        try:
            await channel.send(content=content, embed=_code_embed(code, author), allowed_mentions=allowed)
        except discord.HTTPException:
            return False, f"Couldn't post to {channel.mention}. Check my permissions there."

        # Remember it so the auto-poller doesn't re-announce it.
        await database.mark_code_announced(guild.id, code)

        log_channel = guild.get_channel(int(config["log_channel_id"])) if config.get("log_channel_id") else None
        if log_channel:
            try:
                await log_channel.send(f"🎁 {author.mention} posted gift code `{code}`.")
            except discord.HTTPException:
                pass
        return True, f"✅ Posted `{code}` to {channel.mention}."

    @app_commands.command(name="giftcode", description="Broadcast a gift code to everyone. Admins only.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(code="The gift code to share")
    @app_commands.guild_only()
    async def giftcode(self, interaction: discord.Interaction, code: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await self.post_code(interaction.guild, code, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    # ── auto-announce new codes (the LIST endpoint is live) ───
    @tasks.loop(hours=POLL_HOURS)
    async def check_codes(self):
        codes = await kingshot_api.get_active_codes()
        if not codes:
            return
        for guild_id in await database.list_guild_ids():
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            config = await database.get_config(guild_id)
            if not config:
                continue
            channel_id = config.get("giftcode_channel_id") or config.get("general_channel_id")
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            verified_role = guild.get_role(config["verified_role_id"]) if config.get("verified_role_id") else None
            # First poll for this guild: seed silently so we don't dump the backlog.
            seed = await database.announced_count(guild_id) == 0
            for c in codes:
                code = c.get("code")
                if not code:
                    continue
                is_new = await database.mark_code_announced(guild_id, code, c.get("createdAt"))
                if is_new and not seed and channel:
                    await self._announce_new_code(channel, verified_role, code)

    @check_codes.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _announce_new_code(self, channel, ping_role, code):
        content = ping_role.mention if ping_role else None
        allowed = discord.AllowedMentions(roles=[ping_role] if ping_role else False)
        try:
            await channel.send(content=content, embed=_code_embed(code), allowed_mentions=allowed)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GiftCode(bot))
