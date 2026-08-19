"""
Contribution points.

Alliance leaders (and admins) award/deduct points to members of their alliance
to recognise participation. Awarding happens privately in a leaders-only
#award-points channel via a dropdown member-picker panel, so rewards stay fair.
Everyone can see the live leaderboard in the public #leaderboard channel and
check totals with /points and /leaderboard.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import database

TOP_N = 10


# ──────────────────────────────────────────────────────────────
# Award panel (persistent, lives in the leaders-only channel)
# ──────────────────────────────────────────────────────────────
class AmountModal(discord.ui.Modal, title="Contribution points"):
    amount = discord.ui.TextInput(label="Points", placeholder="e.g. 50", required=True, max_length=6)
    reason = discord.ui.TextInput(label="Reason (optional)", required=False, max_length=100)

    def __init__(self, cog, member: discord.Member, tag: str, sign: int):
        super().__init__()
        self.cog = cog
        self.member = member
        self.tag = tag
        self.sign = sign

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).strip()
        if not raw.isdigit() or int(raw) <= 0:
            await interaction.response.send_message("Points must be a positive number.", ephemeral=True)
            return
        await self.cog.apply_points(
            interaction, self.member, self.tag, int(raw), self.sign, str(self.reason.value or "").strip()
        )


class AwardUserSelect(discord.ui.UserSelect):
    def __init__(self, cog, sign: int):
        self.cog = cog
        self.sign = sign
        verb = "award" if sign > 0 else "deduct"
        super().__init__(placeholder=f"Pick a member to {verb} points…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        member = self.values[0]
        ok, info = await self.cog.can_award(interaction.guild, interaction.user, member)
        if not ok:
            await interaction.response.send_message(info, ephemeral=True)
            return
        await interaction.response.send_modal(AmountModal(self.cog, member, info, self.sign))


class AwardMemberView(discord.ui.View):
    def __init__(self, cog, sign: int):
        super().__init__(timeout=180)
        self.add_item(AwardUserSelect(cog, sign))


class PointsPanelView(discord.ui.View):
    """Persistent Award / Deduct panel for the leaders-only channel."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, interaction):
        cog = interaction.client.get_cog("Points")
        if cog is None:
            await interaction.response.send_message("Points are unavailable right now.", ephemeral=True)
            return None
        perms = interaction.user.guild_permissions
        led = await cog._led_tags(interaction.guild, interaction.user)
        if not led and not (perms.administrator or interaction.user.id == interaction.guild.owner_id):
            await interaction.response.send_message(
                "Only alliance leaders or admins can distribute points.", ephemeral=True
            )
            return None
        return cog

    @discord.ui.button(label="Award points", style=discord.ButtonStyle.success, emoji="🏅", custom_id="ks_pts_award")
    async def award_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        await interaction.response.send_message(
            "Choose who to award:", view=AwardMemberView(cog, +1), ephemeral=True
        )

    @discord.ui.button(label="Deduct points", style=discord.ButtonStyle.danger, emoji="➖", custom_id="ks_pts_deduct")
    async def deduct_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        await interaction.response.send_message(
            "Choose who to deduct from:", view=AwardMemberView(cog, -1), ephemeral=True
        )


# ──────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────
class Points(commands.Cog, name="Points"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _led_tags(self, guild, member) -> set[str]:
        role_ids = {r.id for r in member.roles}
        return {a["tag"] for a in await database.all_alliances(guild.id)
                if a["leader_role_id"] in role_ids}

    async def _member_alliance(self, guild, member_id) -> str | None:
        player = await database.get_player(guild.id, member_id)
        return (player or {}).get("alliance")

    async def can_award(self, guild, awarder, target) -> tuple[bool, str]:
        """Returns (ok, alliance_tag_or_error_message)."""
        if target.bot:
            return False, "You can't award points to a bot."
        target_tag = await self._member_alliance(guild, target.id)
        if not target_tag:
            return False, f"{target.mention} isn't a verified alliance member yet."
        is_admin = awarder.guild_permissions.administrator or awarder.id == guild.owner_id
        if is_admin:
            return True, target_tag
        led = await self._led_tags(guild, awarder)
        if target_tag not in led:
            return False, "You can only award members of your own alliance."
        return True, target_tag

    async def apply_points(self, interaction, member, tag, amount, sign, reason):
        guild = interaction.guild
        new_total = await database.award_points(guild.id, member.id, sign * amount)
        verb = "awarded" if sign > 0 else "deducted"
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"✅ {verb.capitalize()} **{amount}** point(s) — {member.mention} now has **{new_total}**.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ {verb.capitalize()} **{amount}** point(s) — {member.mention} now has **{new_total}**.",
                ephemeral=True,
            )
        await self.refresh_leaderboard(guild)

        config = await database.get_config(guild.id)
        if config and config.get("log_channel_id"):
            ch = guild.get_channel(int(config["log_channel_id"]))
            if ch:
                note = f" ({reason})" if reason else ""
                try:
                    await ch.send(
                        f"🏅 {interaction.user.mention} {verb} **{amount}** pts "
                        f"{'to' if sign > 0 else 'from'} {member.mention} [{tag}]{note} — total {new_total}."
                    )
                except discord.HTTPException:
                    pass

    # ── slash commands ────────────────────────────────────────
    @app_commands.command(name="award", description="Give points to an alliance member. Leaders/admins.")
    @app_commands.describe(member="Who to award", amount="Points (positive)", reason="Optional reason")
    @app_commands.guild_only()
    async def award(self, interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
        ok, info = await self.can_award(interaction.guild, interaction.user, member)
        if not ok:
            await interaction.response.send_message(info, ephemeral=True)
            return
        await self.apply_points(interaction, member, info, abs(amount), +1, reason)

    @app_commands.command(name="deduct", description="Remove points from an alliance member. Leaders/admins.")
    @app_commands.describe(member="Who to deduct from", amount="Points (positive)", reason="Optional reason")
    @app_commands.guild_only()
    async def deduct(self, interaction: discord.Interaction, member: discord.Member, amount: int, reason: str | None = None):
        ok, info = await self.can_award(interaction.guild, interaction.user, member)
        if not ok:
            await interaction.response.send_message(info, ephemeral=True)
            return
        await self.apply_points(interaction, member, info, abs(amount), -1, reason)

    @app_commands.command(name="points", description="Show contribution points (yours or someone else's).")
    @app_commands.describe(member="Whose points to show (defaults to you)")
    @app_commands.guild_only()
    async def points(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        total = await database.get_points(interaction.guild.id, member.id)
        tag = await self._member_alliance(interaction.guild, member.id)
        await interaction.response.send_message(
            f"🏅 {member.mention}{f' [{tag}]' if tag else ''} has **{total}** contribution point(s).",
            ephemeral=True,
        )

    @app_commands.command(name="leaderboard", description="Show the contribution leaderboard.")
    @app_commands.describe(alliance="Optional: a single alliance tag to rank")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction, alliance: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await self._build_leaderboard(interaction.guild, only_tag=(alliance.strip().upper() if alliance else None))
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── leaderboard rendering ─────────────────────────────────
    async def _build_leaderboard(self, guild, only_tag=None) -> discord.Embed:
        rows = await database.all_points(guild.id)
        players = {p["discord_id"]: p for p in await database.all_players(guild.id)}

        def label(discord_id):
            m = guild.get_member(int(discord_id))
            if m:
                return m.display_name
            return (players.get(discord_id) or {}).get("ingame_name", f"user {discord_id}")

        def tag_of(discord_id):
            return (players.get(discord_id) or {}).get("alliance")

        medals = ["🥇", "🥈", "🥉"]

        def rank(i: int) -> str:
            return medals[i] if i < 3 else f"`{i + 1}.`"

        if only_tag:
            filtered = [r for r in rows if tag_of(r["discord_id"]) == only_tag][:TOP_N]
            lines = []
            for i, r in enumerate(filtered):
                lines.append(f"{rank(i)} {label(r['discord_id'])} — **{r['points']}**")
            return discord.Embed(
                title=f"🏆 {only_tag} leaderboard",
                description="\n".join(lines) or "No points yet.",
                color=discord.Color.gold(),
            )

        top_lines = []
        for i, r in enumerate(rows[:TOP_N]):
            t = tag_of(r["discord_id"])
            suffix = f" [{t}]" if t else ""
            top_lines.append(f"{rank(i)} {label(r['discord_id'])}{suffix} — **{r['points']}**")

        totals: dict[str, int] = {}
        for r in rows:
            t = tag_of(r["discord_id"])
            if t:
                totals[t] = totals.get(t, 0) + r["points"]
        alliance_lines = []
        for i, (t, pts) in enumerate(sorted(totals.items(), key=lambda x: x[1], reverse=True)):
            alliance_lines.append(f"{rank(i)} **{t}** — {pts}")

        embed = discord.Embed(title="🏆 Contribution Leaderboard", color=discord.Color.gold())
        embed.add_field(name="Top members", value="\n".join(top_lines) or "No points yet.", inline=False)
        if alliance_lines:
            embed.add_field(name="By alliance (total points)", value="\n".join(alliance_lines), inline=False)
        embed.set_footer(text="Leaders award points in #award-points · check yours with /points")
        return embed

    async def refresh_leaderboard(self, guild: discord.Guild):
        config = await database.get_config(guild.id) or {}
        cid = config.get("points_board_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if not channel:
            return
        embed = await self._build_leaderboard(guild)
        msg = None
        msg_id = config.get("points_board_message_id")
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
            except (discord.NotFound, discord.HTTPException):
                msg = None
        if msg:
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass
        else:
            new = await channel.send(embed=embed)
            await database.upsert_config(guild.id, points_board_message_id=new.id)

    async def ensure_panel(self, guild: discord.Guild):
        """Post the Award/Deduct panel in the leaders-only channel if missing."""
        config = await database.get_config(guild.id) or {}
        cid = config.get("points_admin_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if not channel:
            return
        msg_id = config.get("points_panel_message_id")
        if msg_id:
            try:
                await channel.fetch_message(int(msg_id))
                return
            except (discord.NotFound, discord.HTTPException):
                pass
        embed = discord.Embed(
            title="🏅 Award Contribution Points",
            color=discord.Color.gold(),
            description=(
                "Leaders & admins only. Use the buttons to award or deduct points for "
                "members of your alliance — pick the member from the dropdown, then enter "
                "the amount. Totals show on the public leaderboard."
            ),
        )
        message = await channel.send(embed=embed, view=PointsPanelView())
        await database.upsert_config(guild.id, points_panel_message_id=message.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Points(bot))
