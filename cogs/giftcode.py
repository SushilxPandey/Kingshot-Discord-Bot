"""
Leader gift-code announcements.

Century Games locked their gift-code redemption API behind bot-protection
(Akamai), so automatic redemption from a bot is no longer viable. Instead, an
alliance leader runs ``/giftcode CODE`` and the bot posts the code to that
alliance's chat channel, pinging the alliance members with clear instructions to
redeem it themselves (in-game or via the official portal). No OCR, no API, no
fragile dependencies.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import database

REDEEM_URL = "https://ks-giftcode.centurygame.com/"


class GiftCode(commands.Cog, name="GiftCode"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _led_alliances(self, member: discord.Member, alliances: list[dict]) -> list[dict]:
        role_ids = {r.id for r in member.roles}
        return [a for a in alliances if a["leader_role_id"] in role_ids]

    @app_commands.command(name="giftcode", description="Announce a gift code to your alliance. Leaders only.")
    @app_commands.describe(code="The gift code to share", alliance="Which alliance (only if you lead more than one)")
    @app_commands.guild_only()
    async def giftcode(self, interaction: discord.Interaction, code: str, alliance: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        code = code.strip()

        alliances = await database.all_alliances(guild.id)
        led = self._led_alliances(interaction.user, alliances)
        if not led:
            await interaction.followup.send(
                "Only alliance **leaders** can use this — you need a `<tag> Leaders` role.",
                ephemeral=True,
            )
            return

        if alliance:
            wanted = alliance.strip().upper()
            led = [a for a in led if a["tag"] == wanted]
            if not led:
                await interaction.followup.send(f"You don't lead **{wanted}**.", ephemeral=True)
                return
        elif len(led) > 1:
            tags = ", ".join(a["tag"] for a in led)
            await interaction.followup.send(
                f"You lead multiple alliances ({tags}). Re-run with the `alliance:` option to pick one.",
                ephemeral=True,
            )
            return

        target = led[0]
        tag = target["tag"]
        channel = guild.get_channel(int(target["chat_channel_id"])) or interaction.channel
        member_role = guild.get_role(target["member_role_id"])

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
        embed.set_footer(text=f"Shared by {interaction.user.display_name} for {tag}")

        content = member_role.mention if member_role else None
        allowed = discord.AllowedMentions(roles=[member_role] if member_role else False)
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except discord.HTTPException:
            await interaction.followup.send(
                f"Couldn't post to {channel.mention if channel else 'the alliance channel'}. "
                "Check my permissions there.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Announced `{code}` to **{tag}** in {channel.mention}.", ephemeral=True
        )

        # Audit to the bot-log channel.
        config = await database.get_config(guild.id)
        if config and config.get("log_channel_id"):
            log_channel = guild.get_channel(int(config["log_channel_id"]))
            if log_channel:
                try:
                    await log_channel.send(
                        f"🎁 {interaction.user.mention} shared gift code `{code}` with **{tag}**."
                    )
                except discord.HTTPException:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GiftCode(bot))
