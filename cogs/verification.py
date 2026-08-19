"""
Verification flow (API-driven, ID only).

Members enter only their in-game ID. The bot looks them up live on kingshotstats
and pulls their real name, kingdom, Town Center level, and alliance:

  * **Found:** confirm the kingdom matches this server and they meet the Town Center
    requirement, then verify them, set their nickname from the real name, and grant a
    ceremonial role for their in-game alliance (auto-created the first time that
    alliance appears — no channels, just a label).
  * **Not found (but the site is up):** reject — the ID is probably wrong.
  * **Site unreachable:** provisional pass on the ID alone; the tracker backfills their
    name/kingdom/alliance automatically once the site is reachable again.

``on_member_join`` auto-assigns the Unverified role so every newcomer is gated to the
verify channel until they verify.
"""

import logging

import discord
from discord.ext import commands

import database
import kingshot_api


class Verification(commands.Cog, name="Verification"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── called by views.verify_view.VerifyModal ───────────────
    async def handle_verification(self, interaction: discord.Interaction, ingame_id: int,
                                  is_reverify: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        member = interaction.user

        config = await database.get_config(guild.id)
        if not config or not config.get("verified_role_id"):
            await interaction.followup.send(
                "This server isn't set up yet. Ask an admin to run setup.",
                ephemeral=True,
            )
            return

        allowed_kingdom = config["allowed_kingdom"]
        allowed_level = config.get("allowed_level") or 0

        # One in-game player may only be linked to one Discord account per server.
        claim = await database.player_by_ingame_id(guild.id, ingame_id)
        if claim and str(claim.get("discord_id")) != str(member.id):
            await interaction.followup.send(
                f"In-game player **{ingame_id}** is already linked to another member on this "
                "server. If that's a mistake, ask an admin to unverify the other account first.",
                ephemeral=True,
            )
            return

        # Live lookup by game ID. Distinguish "site down" (exception) from
        # "site up but no such player" (empty data) — they're handled differently.
        reachable = True
        lookup = None
        try:
            info = await kingshot_api.get_player_info(ingame_id)
            lookup = info.get("data") or None
        except Exception:
            reachable = False

        if lookup:
            try:
                real_kingdom = int(lookup.get("kingdom"))
            except (TypeError, ValueError):
                real_kingdom = None
            try:
                real_level = int(lookup.get("level"))
            except (TypeError, ValueError):
                real_level = 0
            if real_kingdom != allowed_kingdom:
                await interaction.followup.send(
                    f"That account is in kingdom **{real_kingdom}**, but this server only "
                    f"allows kingdom **{allowed_kingdom}**.",
                    ephemeral=True,
                )
                return
            if allowed_level and real_level < allowed_level:
                await interaction.followup.send(
                    f"You must be at least Town Center **{allowed_level}** to verify "
                    f"(that account is TC {real_level}).",
                    ephemeral=True,
                )
                return
            ingame_name = (lookup.get("name") or f"Player {ingame_id}").strip()[:32]
            kingdom = real_kingdom
            town_level = real_level
            tag = (lookup.get("alliance_abbr") or "").strip().upper() or None
            source = "✅ live-verified"
        elif reachable:
            # Site is up but returned no record — almost always a mistyped ID.
            await interaction.followup.send(
                f"I couldn't find a Kingshot player with ID **{ingame_id}**. "
                "Double-check your in-game ID (Profile → the number under your name) and try again.",
                ephemeral=True,
            )
            return
        else:
            # Site unreachable → provisional pass on the ID alone; tracker fills the rest.
            ingame_name = None
            kingdom = allowed_kingdom
            town_level = None
            tag = None
            source = "⏳ provisional (lookup unavailable — will backfill)"

        # Remember the previous alliance so we can swap the ceremonial role on a change.
        existing = await database.get_player(guild.id, member.id)
        old_tag = (existing or {}).get("alliance")

        await database.save_player(
            guild.id, member.id, ingame_name, ingame_id, kingdom, tag, town_level
        )

        # Role swap + nickname.
        verified_role = guild.get_role(config["verified_role_id"])
        unverified_role = guild.get_role(config["unverified_role_id"])
        note = await self._apply_verified_state(
            member, verified_role, unverified_role, kingdom, tag, ingame_name
        )
        await self._sync_alliance_roles(member, old_tag, tag)

        verb = "re-verified" if is_reverify else "verified"
        display = ingame_name or f"ID {ingame_id}"
        alliance_txt = f" in **{tag}**" if tag else ""
        if lookup:
            await interaction.followup.send(
                f"✅ You're {verb} as **{display}**{alliance_txt}! Welcome in." + note,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "✅ You're verified for now on your game ID. The game stats site is briefly "
                "unreachable, so I'll fill in your name, kingdom, and alliance automatically "
                "within the next little while." + note,
                ephemeral=True,
            )

        # Public announcement in the general channel (visible to everyone).
        general_id = config.get("general_channel_id")
        if general_id and not is_reverify:
            channel = guild.get_channel(int(general_id))
            if channel:
                try:
                    await channel.send(f"✅ **{display}**{alliance_txt} has verified — welcome!")
                except discord.HTTPException:
                    pass

        # Admin audit log.
        log_id = config.get("log_channel_id")
        if log_id:
            log_channel = guild.get_channel(int(log_id))
            if log_channel:
                try:
                    await log_channel.send(
                        f"🔎 {member.mention} {verb} ({source}) — name **{display}**, "
                        f"kingdom **{kingdom}**, alliance **{tag or '—'}**, ID `{ingame_id}`"
                    )
                except discord.HTTPException:
                    pass

        # Refresh the staff member-list channel.
        setup_cog = self.bot.get_cog("Setup")
        if setup_cog:
            await setup_cog.refresh_member_list(guild)

    async def ensure_alliance_role(self, guild: discord.Guild, tag: str) -> discord.Role | None:
        """Find or create the ceremonial role for an in-game alliance tag (no channels)."""
        if not tag:
            return None
        record = await database.get_alliance(guild.id, tag)
        role = guild.get_role(record["member_role_id"]) if record and record.get("member_role_id") else None
        if role is None:
            try:
                role = await guild.create_role(name=tag, mentionable=True, reason="Kingshot alliance (from API)")
            except discord.HTTPException:
                logging.warning("Could not create ceremonial role %s in %s", tag, guild.id)
                return None
            await database.upsert_alliance_role(guild.id, tag, role.id)
        return role

    async def _sync_alliance_roles(self, member, old_tag, new_tag):
        """Remove the previous alliance's ceremonial role and grant the new one."""
        guild = member.guild
        if old_tag and old_tag != new_tag:
            old = await database.get_alliance(guild.id, old_tag)
            if old and old.get("member_role_id"):
                role = guild.get_role(old["member_role_id"])
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Kingshot alliance change")
                    except discord.HTTPException:
                        pass
        if new_tag:
            role = await self.ensure_alliance_role(guild, new_tag)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Kingshot alliance member")
                except discord.HTTPException:
                    pass

    async def _apply_verified_state(self, member, verified_role, unverified_role, kingdom, alliance, ingame_name) -> str:
        note = ""
        try:
            if verified_role and verified_role not in member.roles:
                await member.add_roles(verified_role, reason="Kingshot verified")
            if unverified_role and unverified_role in member.roles:
                await member.remove_roles(unverified_role, reason="Kingshot verified")
        except discord.Forbidden:
            note += "\n⚠️ I couldn't update your roles — my role may be too low in the list."
            logging.warning("Role update forbidden for %s in guild %s", member, member.guild.id)

        if ingame_name:
            tag_part = f"{alliance}- " if alliance else ""
            nick = f"[{kingdom}] {tag_part}{ingame_name}"[:32]
            try:
                await member.edit(nick=nick, reason="Kingshot verified")
            except discord.Forbidden:
                note += "\n⚠️ I couldn't change your nickname (server owners and higher roles can't be renamed by bots)."
            except discord.HTTPException:
                pass
        return note

    # ── gate every newcomer ───────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        config = await database.get_config(member.guild.id)
        if not config or not config.get("unverified_role_id"):
            return  # server not set up yet
        unverified_role = member.guild.get_role(config["unverified_role_id"])
        if unverified_role:
            try:
                await member.add_roles(unverified_role, reason="Kingshot auto-gate on join")
            except discord.HTTPException:
                logging.warning("Could not assign Unverified to %s in %s", member, member.guild.id)

        setup_cog = self.bot.get_cog("Setup")
        if setup_cog:
            await setup_cog.refresh_member_list(member.guild)

        # Public welcome note in the welcome channel (visible to everyone).
        welcome_id = config.get("welcome_channel_id")
        if welcome_id:
            channel = member.guild.get_channel(int(welcome_id))
            if channel:
                embed = discord.Embed(
                    description=(
                        f"👋 Welcome {member.mention} to **{member.guild.name}**!\n"
                        "Head to the verify channel and tap **Verify** — just enter your in-game ID."
                    ),
                    color=discord.Color.blurple(),
                )
                try:
                    await channel.send(content=member.mention, embed=embed)
                except discord.HTTPException:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
