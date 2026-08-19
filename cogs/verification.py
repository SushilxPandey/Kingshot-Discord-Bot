"""
Verification flow (self-attested model).

Kingshot removed its public player-lookup API, so the bot can no longer confirm a
player's identity from the game. Instead:

  * ``handle_verification`` trusts the details the member types (in-game ID, name,
    kingdom, alliance), records them, swaps Unverified → Verified, sets the
    nickname, announces it in general, and writes an audit line to the admin log.
  * ``on_member_join`` auto-assigns the Unverified role so every newcomer is gated
    to the verify channel until they verify.
"""

import logging

import discord
from discord.ext import commands

import database


class Verification(commands.Cog, name="Verification"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── called by views.verify_view.VerifyModal ───────────────
    async def handle_verification(self, interaction: discord.Interaction, ingame_id: int,
                                  ingame_name: str, kingdom: int, alliance: str | None = None,
                                  is_reverify: bool = False):
        # Self-attested: Kingshot removed the public player-lookup API, so the bot
        # trusts the details the member enters, records them, grants roles, and
        # writes an audit line to the admin log for spot-checking.
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

        # Validate the alliance tag against the ones the owner created.
        alliance = (alliance or "").strip().upper()
        valid_tags = {a["tag"] for a in await database.all_alliances(guild.id)}
        if valid_tags and alliance not in valid_tags:
            await interaction.followup.send(
                "That alliance isn't set up here. Valid tags: " + ", ".join(sorted(valid_tags)),
                ephemeral=True,
            )
            return

        # Soft kingdom check against the entered value.
        if kingdom != allowed_kingdom:
            await interaction.followup.send(
                f"Only kingdom **{allowed_kingdom}** is allowed on this server.",
                ephemeral=True,
            )
            return

        ingame_name = ingame_name.strip()[:32]

        # Remember the previous alliance so we can swap roles on a change / reverify.
        existing = await database.get_player(guild.id, member.id)
        old_tag = (existing or {}).get("alliance")

        # Save to THIS guild's database (level unknown — no API to fetch it).
        await database.save_player(
            guild.id, member.id, ingame_name, ingame_id, kingdom, alliance, None
        )

        # Role swap + nickname.
        verified_role = guild.get_role(config["verified_role_id"])
        unverified_role = guild.get_role(config["unverified_role_id"])
        note = await self._apply_verified_state(
            member, verified_role, unverified_role, kingdom, alliance, ingame_name
        )
        await self._sync_alliance_roles(member, old_tag, alliance)

        verb = "re-verified" if is_reverify else "verified"
        alliance_txt = f" in **{alliance}**" if alliance else ""
        await interaction.followup.send(
            f"✅ You're {verb} as **{ingame_name}**{alliance_txt}! Welcome in." + note,
            ephemeral=True,
        )

        # Public announcement in the general channel (visible to everyone).
        general_id = config.get("general_channel_id")
        if general_id and not is_reverify:
            channel = guild.get_channel(int(general_id))
            if channel:
                try:
                    await channel.send(f"✅ **{ingame_name}**{alliance_txt} has verified — welcome!")
                except discord.HTTPException:
                    pass

        # Admin audit log (this is the "auditable" half of the chosen model).
        log_id = config.get("log_channel_id")
        if log_id:
            log_channel = guild.get_channel(int(log_id))
            if log_channel:
                try:
                    await log_channel.send(
                        f"🔎 {member.mention} {verb} — name **{ingame_name}**, "
                        f"kingdom **{kingdom}**, alliance **{alliance or '—'}**, ID `{ingame_id}`"
                    )
                except discord.HTTPException:
                    pass

        # Refresh the staff member-list channel.
        setup_cog = self.bot.get_cog("Setup")
        if setup_cog:
            await setup_cog.refresh_member_list(guild)

    async def _sync_alliance_roles(self, member, old_tag, new_tag):
        """Remove the previous alliance's member role and grant the new one."""
        guild = member.guild
        if old_tag and old_tag != new_tag:
            old = await database.get_alliance(guild.id, old_tag)
            if old:
                role = guild.get_role(old["member_role_id"])
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Kingshot alliance change")
                    except discord.HTTPException:
                        pass
        if new_tag:
            new = await database.get_alliance(guild.id, new_tag)
            if new:
                role = guild.get_role(new["member_role_id"])
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

        nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
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
                        "Head to the verify channel and tap **Verify** to unlock the server."
                    ),
                    color=discord.Color.blurple(),
                )
                try:
                    await channel.send(content=member.mention, embed=embed)
                except discord.HTTPException:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
