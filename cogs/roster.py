"""
Owner/admin member tools.

  * /roster   — CSV export of this server's players.
  * /age      — how long a kingdom has been open.
  * /unverify — remove a member's verification and **delete their record**.
  * /ban      — ban a member from the Discord server and delete their record.

Also provides a staff **Member Management** panel (buttons: Unverify / Ban) posted in
the staff-only member-list channel, so admins never have to type a command.
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
    "discord_id", "ingame_name", "ingame_id", "kingdom", "alliance",
    "town_level", "verified_at", "last_checked",
]


def _is_staff(user: discord.Member, guild: discord.Guild) -> bool:
    return user.guild_permissions.administrator or user.id == guild.owner_id


# ──────────────────────────────────────────────────────────────
# Member Management panel (staff-only member-list channel)
# ──────────────────────────────────────────────────────────────
class ManageUserSelect(discord.ui.UserSelect):
    def __init__(self, cog: "Roster", action: str):
        self.cog = cog
        self.action = action        # "unverify" or "ban"
        verb = "unverify" if action == "unverify" else "ban"
        super().__init__(placeholder=f"Pick a member to {verb}…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        member = self.values[0]
        if self.action == "unverify":
            await interaction.response.defer(ephemeral=True, thinking=True)
            msg = await self.cog.do_unverify(interaction.guild, member)
            await interaction.followup.send(msg, ephemeral=True)
        else:
            # Ban is destructive — confirm first.
            await interaction.response.send_message(
                f"⚠️ Ban {member.mention} from the server? This removes them and deletes their record.",
                view=BanConfirmView(self.cog, member), ephemeral=True,
            )


class ManageSelectView(discord.ui.View):
    def __init__(self, cog: "Roster", action: str):
        super().__init__(timeout=180)
        self.add_item(ManageUserSelect(cog, action))


class BanConfirmView(discord.ui.View):
    def __init__(self, cog: "Roster", member: discord.Member):
        super().__init__(timeout=60)
        self.cog = cog
        self.member = member

    @discord.ui.button(label="Confirm ban", style=discord.ButtonStyle.danger, emoji="🔨")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await self.cog.do_ban(interaction.guild, self.member, f"Banned by {interaction.user}")
        await interaction.followup.send(msg, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled — no action taken.", view=None)
        self.stop()


class ManagePanelView(discord.ui.View):
    """Persistent staff panel with Unverify / Ban buttons."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, interaction: discord.Interaction):
        if not _is_staff(interaction.user, interaction.guild):
            await interaction.response.send_message("Only admins/owner can manage members.", ephemeral=True)
            return None
        cog = interaction.client.get_cog("Roster")
        if cog is None:
            await interaction.response.send_message("Member management is unavailable right now.", ephemeral=True)
        return cog

    @discord.ui.button(label="Unverify a member", style=discord.ButtonStyle.secondary, emoji="↩️", custom_id="ks_mgmt_unverify")
    async def unverify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        await interaction.response.send_message(
            "Choose a member to unverify (this deletes their record):",
            view=ManageSelectView(cog, "unverify"), ephemeral=True,
        )

    @discord.ui.button(label="Ban a member", style=discord.ButtonStyle.danger, emoji="🔨", custom_id="ks_mgmt_ban")
    async def ban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        await interaction.response.send_message(
            "Choose a member to ban:", view=ManageSelectView(cog, "ban"), ephemeral=True,
        )


# ──────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────
class Roster(commands.Cog, name="Roster"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── shared actions ────────────────────────────────────────
    async def do_unverify(self, guild: discord.Guild, member: discord.Member) -> str:
        """Delete a member's record and revert their roles. Returns a status string."""
        config = await database.get_config(guild.id)
        if not config or not config.get("verified_role_id"):
            return "This server isn't set up yet."
        existing = await database.get_player(guild.id, member.id)

        await database.delete_player(guild.id, member.id)

        # Strip any ceremonial alliance role too.
        if existing and existing.get("alliance"):
            alliance = await database.get_alliance(guild.id, existing["alliance"])
            if alliance and alliance.get("member_role_id"):
                role = guild.get_role(alliance["member_role_id"])
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Kingshot unverify")
                    except discord.HTTPException:
                        pass

        verified_role = guild.get_role(config["verified_role_id"])
        unverified_role = guild.get_role(config["unverified_role_id"]) if config.get("unverified_role_id") else None
        note = ""
        try:
            if verified_role and verified_role in member.roles:
                await member.remove_roles(verified_role, reason="Kingshot unverify")
            if unverified_role:
                await member.add_roles(unverified_role, reason="Kingshot unverify")
        except discord.Forbidden:
            note = " (I couldn't change their roles — my role may be too low.)"

        await self._log(guild, f"↩️ {member.mention} was unverified and their record deleted.")
        setup_cog = self.bot.get_cog("Setup")
        if setup_cog:
            await setup_cog.refresh_member_list(guild)
        return f"✅ {member.mention} unverified and their record deleted." + note

    async def do_ban(self, guild: discord.Guild, member: discord.Member, reason: str) -> str:
        """Ban a member from the server and delete their record."""
        if member.id == guild.owner_id:
            return "You can't ban the server owner."
        await database.delete_player(guild.id, member.id)
        try:
            await guild.ban(member, reason=reason, delete_message_days=0)
        except discord.Forbidden:
            return ("I don't have permission to ban that member (I need the **Ban Members** "
                    "permission and my role must be above theirs). Their record was still deleted.")
        except discord.HTTPException:
            return "Something went wrong trying to ban them. Their record was deleted."
        await self._log(guild, f"🔨 {member} (`{member.id}`) was banned and their record deleted. {reason}")
        setup_cog = self.bot.get_cog("Setup")
        if setup_cog:
            await setup_cog.refresh_member_list(guild)
        return f"🔨 Banned **{member}** and deleted their record."

    async def _log(self, guild: discord.Guild, text: str):
        config = await database.get_config(guild.id)
        if config and config.get("log_channel_id"):
            channel = guild.get_channel(int(config["log_channel_id"]))
            if channel:
                try:
                    await channel.send(text)
                except discord.HTTPException:
                    pass

    async def ensure_manage_panel(self, guild: discord.Guild):
        """Post the staff Member Management panel in the member-list channel."""
        config = await database.get_config(guild.id) or {}
        cid = config.get("member_list_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if not channel:
            return
        msg_id = config.get("manage_panel_message_id")
        if msg_id:
            try:
                await channel.fetch_message(int(msg_id))
                return
            except (discord.NotFound, discord.HTTPException):
                pass
        embed = discord.Embed(
            title="🛡️ Member Management",
            color=discord.Color.dark_teal(),
            description=(
                "Admins/owner only.\n\n"
                "**Unverify** — remove a member's verification and delete their record (they "
                "keep their Discord account and can re-verify).\n"
                "**Ban** — remove them from the server entirely and delete their record."
            ),
        )
        try:
            message = await channel.send(embed=embed, view=ManagePanelView())
        except discord.HTTPException:
            return
        await database.upsert_config(guild.id, manage_panel_message_id=message.id)

    # ── slash commands ────────────────────────────────────────
    @app_commands.command(name="roster", description="Export this server's players as a CSV. Admins only.")
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
            f"Here's your roster — **{len(players)}** player(s).", file=file, ephemeral=True
        )

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
            await interaction.followup.send("Couldn't reach the game data right now. Try again later.", ephemeral=True)
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

    @app_commands.command(name="unverify", description="Unverify a member and delete their record. Admins only.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="The member to unverify")
    @app_commands.guild_only()
    async def unverify(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await self.do_unverify(interaction.guild, member)
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member from the server and delete their record. Admins only.")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.describe(member="The member to ban", reason="Optional reason")
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not _is_staff(interaction.user, interaction.guild):
            await interaction.followup.send("Only admins/owner can ban members.", ephemeral=True)
            return
        msg = await self.do_ban(interaction.guild, member, reason or f"Banned by {interaction.user}")
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Roster(bot))
