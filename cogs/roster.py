"""
Player-facing and owner-facing commands (requirements 5, 8).

  * /roster   — admin/owner CSV export of this server's roster.
  * /mystats  — a member's own live in-game stats.
  * /age      — how long a kingdom has been open.
  * /unverify — admin removes a member and reverts their roles.
"""

import csv
import io
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import database
import kingshot_api

CSV_COLUMNS = [
    "discord_id",
    "ingame_name",
    "ingame_id",
    "kingdom",
    "alliance",
    "town_level",
    "verified_at",
    "last_checked",
]


class Roster(commands.Cog, name="Roster"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="roster", description="Export this server's verified players as a CSV. Admins only.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def roster(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        players = await database.all_players(interaction.guild.id)
        if not players:
            await interaction.followup.send("No players verified yet on this server.", ephemeral=True)
            return

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in players:
            writer.writerow(row)
        data = io.BytesIO(buffer.getvalue().encode("utf-8"))

        file = discord.File(data, filename=f"roster_{interaction.guild.id}.csv")
        await interaction.followup.send(
            f"Here's your roster — **{len(players)}** verified player(s).",
            file=file,
            ephemeral=True,
        )

    @app_commands.command(name="mystats", description="Show your in-game Kingshot stats.")
    @app_commands.guild_only()
    async def mystats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = await database.get_player(interaction.guild.id, interaction.user.id)
        if not player:
            await interaction.followup.send("You're not verified yet. Use the Verify button first.", ephemeral=True)
            return

        # Kingshot removed its player API, so we show the details on record.
        embed = discord.Embed(title=f"{player['ingame_name']}'s Details", color=discord.Color.blue())
        embed.add_field(name="In-game name", value=player["ingame_name"], inline=True)
        embed.add_field(name="Player ID", value=player["ingame_id"], inline=True)
        embed.add_field(name="Kingdom", value=player["kingdom"], inline=True)
        embed.add_field(name="Alliance", value=player["alliance"] or "—", inline=True)
        if player.get("verified_at"):
            embed.add_field(name="Verified", value=player["verified_at"], inline=True)
        embed.set_footer(text="Use Reverify to update any of these.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="age", description="Show how long a kingdom has been open.")
    @app_commands.describe(kingdom_id="The kingdom number to look up")
    @app_commands.guild_only()
    async def age(self, interaction: discord.Interaction, kingdom_id: int):
        await interaction.response.defer(thinking=True)
        try:
            kingdom_data = await kingshot_api.get_kingdom_stats(kingdom_id)
        except ValueError:
            await interaction.followup.send(f"Kingdom {kingdom_id} not found.", ephemeral=True)
            return
        except Exception:
            await interaction.followup.send("Couldn't reach the Kingshot API right now. Try again later.", ephemeral=True)
            return

        open_time_str = kingdom_data["data"]["servers"][0]["openTime"]
        open_time = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - open_time).days
        embed = discord.Embed(
            title=f"Kingdom {kingdom_id} Age",
            description=f"Kingdom {kingdom_id} has been open for **{days}** days!",
            color=discord.Color.green(),
        )
        embed.add_field(name="Open Date", value=open_time.strftime("%Y-%m-%d"), inline=True)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="unverify", description="Unverify a member and revert their roles. Admins only.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="The member to unverify")
    @app_commands.guild_only()
    async def unverify(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await database.get_config(interaction.guild.id)
        if not config or not config.get("verified_role_id"):
            await interaction.followup.send("This server isn't set up yet. Run `/setup`.", ephemeral=True)
            return

        existing = await database.get_player(interaction.guild.id, member.id)
        if not existing:
            await interaction.followup.send(f"{member.mention} isn't verified.", ephemeral=True)
            return

        await database.delete_player(interaction.guild.id, member.id)
        verified_role = interaction.guild.get_role(config["verified_role_id"])
        unverified_role = interaction.guild.get_role(config["unverified_role_id"])
        try:
            if verified_role and verified_role in member.roles:
                await member.remove_roles(verified_role, reason="Kingshot unverify")
            if unverified_role:
                await member.add_roles(unverified_role, reason="Kingshot unverify")
        except discord.Forbidden:
            await interaction.followup.send(
                f"Removed {member.mention} from the database, but I couldn't change their roles "
                "(my role may be too low).",
                ephemeral=True,
            )
            return

        setup_cog = self.bot.get_cog("Setup")
        if setup_cog:
            await setup_cog.refresh_member_list(interaction.guild)

        await interaction.followup.send(
            f"{member.mention} has been unverified and moved back to Unverified.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Roster(bot))
