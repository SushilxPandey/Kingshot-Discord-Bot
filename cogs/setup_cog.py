"""
Setup control room + provisioning.

Setup is driven from an **owner-only `bot-setup` channel** that holds a persistent
control panel (Setup / Re-Setup / Add Alliance / Remove Alliance buttons). Both
Setup and Re-Setup first **wipe every bot-created role and channel** (except the
setup channel; the player DB is kept) and then rebuild from scratch:

  * Unverified / Verified roles (referenced by ID);
  * a Bot Center category (verify / info / log / welcome / general channels);
  * a War category (strategy / voice / rally-leaders / rally-joiners) visible to
    all Verified, plus Rally Leaders / Rally Joiners ping roles;
  * a server-wide lockdown so Unverified sees only verify + welcome.

Alliances are added/removed from the same panel. Only tracked IDs are ever
deleted, so the server's own roles and channels are never touched.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import database
from views.verify_view import VerifyView

CATEGORY_NAME = "🤖 Bot Center"
VERIFY_CHANNEL_NAME = "verify-here"
INFO_CHANNEL_NAME = "bot-info"
LOG_CHANNEL_NAME = "bot-log"
WELCOME_CHANNEL_NAME = "welcome"
SETUP_CHANNEL_NAME = "bot-setup"
MEMBER_LIST_CHANNEL_NAME = "member-list"
UNVERIFIED_ROLE_NAME = "Unverified"
VERIFIED_ROLE_NAME = "Verified"

# Community (social) section — lives OUTSIDE the bot category.
COMMUNITY_CATEGORY_NAME = "💬 Community"
GENERAL_CHANNEL_NAME = "general"
MEMES_CHANNEL_NAME = "memes"
GIFS_CHANNEL_NAME = "gifs"
LOBBY_VOICE_NAME = "Lobby"

SETUP_MSG_TTL = 15                   # setup status messages self-delete after 15s

WAR_CATEGORY_NAME = "⚔️ War"
WAR_STRATEGY_NAME = "war-strategy"
WAR_VOICE_NAME = "War Voice"
RALLY_LEADERS_CHANNEL = "rally-leaders"
RALLY_JOINERS_CHANNEL = "rally-joiners"
RALLY_LEADER_ROLE = "Rally Leaders"
RALLY_JOINER_ROLE = "Rally Joiners"

LEADER_ROLE_SUFFIX = "Leaders"      # alliance leader role = "<TAG> Leaders"
VANISH_SECONDS = 3600               # public setup notices self-delete after an hour

REQUIRED_PERMS = {
    "manage_roles": "Manage Roles",
    "manage_channels": "Manage Channels",
    "manage_nicknames": "Manage Nicknames",
}


def missing_permissions(me: discord.Member) -> list[str]:
    perms = me.guild_permissions
    return [label for attr, label in REQUIRED_PERMS.items() if not getattr(perms, attr)]


# ──────────────────────────────────────────────────────────────
# Wizard + panel UI
# ──────────────────────────────────────────────────────────────
class SetupModal(discord.ui.Modal, title="Bot Setup"):
    """One pop-up that collects everything: kingdom + alliances."""
    kingdom = discord.ui.TextInput(
        label="Allowed kingdom number", placeholder="e.g. 466", required=True, max_length=10
    )
    alliances = discord.ui.TextInput(
        label="Alliances — comma-separated tags",
        placeholder="e.g. RTL, XYZ, ABC   (leave blank to add later)",
        required=False, style=discord.TextStyle.paragraph, max_length=200,
    )

    def __init__(self, cog: "SetupCog", wipe: bool = True):
        super().__init__()
        self.cog = cog
        self.wipe = wipe

    async def on_submit(self, interaction: discord.Interaction):
        raw_k = str(self.kingdom.value).strip()
        if not raw_k.isdigit():
            await interaction.response.send_message("Kingdom must be a number.", ephemeral=True)
            return
        tags = SetupCog.parse_alliance_tags(str(self.alliances.value or ""))
        await self.cog.run_provision(interaction, int(raw_k), wipe=self.wipe, alliance_tags=tags)


class AllianceModal(discord.ui.Modal, title="Add an alliance"):
    tag = discord.ui.TextInput(
        label="Alliance tag (letters/numbers)", placeholder="e.g. RTL",
        min_length=2, max_length=4, required=True
    )
    name = discord.ui.TextInput(label="Full alliance name (optional)", required=False, max_length=50)

    def __init__(self, cog: "SetupCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        tag = str(self.tag.value).strip().upper()
        if not tag.isalnum():
            await interaction.response.send_message("Tag must be letters or numbers only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        alliance = await self.cog.provision_alliance(interaction.guild, tag, str(self.name.value).strip() or None)
        member_role = interaction.guild.get_role(alliance["member_role_id"])
        leader_role = interaction.guild.get_role(alliance["leader_role_id"])
        await interaction.followup.send(
            f"✅ Alliance **{tag}** ready — {member_role.mention} / {leader_role.mention} and its "
            f"channel group. Give the **{tag} Leaders** role to its leaders.",
            ephemeral=True,
        )


class RemoveAllianceSelect(discord.ui.Select):
    def __init__(self, cog: "SetupCog", alliances: list[dict]):
        self.cog = cog
        options = [
            discord.SelectOption(label=a["tag"], description=(a.get("name") or a["tag"])[:100])
            for a in alliances[:25]
        ]
        super().__init__(placeholder="Pick an alliance to remove…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        tag = self.values[0]
        await self.cog.remove_alliance(interaction.guild, tag)
        await interaction.followup.send(f"🗑️ Removed alliance **{tag}** and its roles/channels.", ephemeral=True)


class RemoveAllianceView(discord.ui.View):
    def __init__(self, cog: "SetupCog", alliances: list[dict]):
        super().__init__(timeout=180)
        self.add_item(RemoveAllianceSelect(cog, alliances))


class SetupPanelView(discord.ui.View):
    """Persistent owner-only control panel living in the setup channel."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, interaction: discord.Interaction):
        perms = interaction.user.guild_permissions
        if not (perms.administrator or interaction.user.id == interaction.guild.owner_id):
            await interaction.response.send_message(
                "Only the server owner or admins can use this panel.", ephemeral=True
            )
            return None
        cog = interaction.client.get_cog("Setup")
        if cog is None:
            await interaction.response.send_message("Setup is unavailable right now.", ephemeral=True)
        return cog

    @discord.ui.button(label="Setup", style=discord.ButtonStyle.success, emoji="🛠️", custom_id="ks_setup")
    async def setup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        missing = missing_permissions(interaction.guild.me)
        if missing:
            await interaction.response.send_message(
                "I'm missing these permissions: " + ", ".join(missing)
                + ". Grant them and make sure my role is near the top, then try again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(SetupModal(cog, wipe=True))

    @discord.ui.button(label="Re-Setup", style=discord.ButtonStyle.danger, emoji="♻️", custom_id="ks_resetup")
    async def resetup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        missing = missing_permissions(interaction.guild.me)
        if missing:
            await interaction.response.send_message(
                "I'm missing these permissions: " + ", ".join(missing) + ".", ephemeral=True
            )
            return
        await interaction.response.send_modal(SetupModal(cog, wipe=True))

    @discord.ui.button(label="Add Alliance", style=discord.ButtonStyle.primary, emoji="➕", custom_id="ks_addalliance")
    async def add_alliance_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        config = await database.get_config(interaction.guild.id)
        if not config or not config.get("verified_role_id"):
            await interaction.response.send_message("Run **Setup** first.", ephemeral=True)
            return
        await interaction.response.send_modal(AllianceModal(cog))

    @discord.ui.button(label="Remove Alliance", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="ks_removealliance")
    async def remove_alliance_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        alliances = await database.all_alliances(interaction.guild.id)
        if not alliances:
            await interaction.response.send_message("There are no alliances to remove.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Pick an alliance to remove:", view=RemoveAllianceView(cog, alliances), ephemeral=True
        )


# ──────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────
class SetupCog(commands.Cog, name="Setup"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── slash bootstrap + legacy commands ─────────────────────
    @app_commands.command(name="setup", description="Open the bot control panel. Admins only.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def setup(self, interaction: discord.Interaction):
        missing = missing_permissions(interaction.guild.me)
        if missing:
            await interaction.response.send_message(
                "I'm missing these permissions, please grant them first: " + ", ".join(missing),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = await self.ensure_setup_channel(interaction.guild)
        await interaction.followup.send(
            f"Your control panel is ready in {channel.mention} — use the buttons there to set up "
            "the bot, add alliances, and more.",
            ephemeral=True,
        )

    @app_commands.command(name="resync", description="Repair roles/channels WITHOUT wiping. Admins only.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def resync(self, interaction: discord.Interaction):
        config = await database.get_config(interaction.guild.id)
        if not config or not config.get("allowed_kingdom"):
            await interaction.response.send_message("Run setup first.", ephemeral=True)
            return
        await self.run_provision(
            interaction, config["allowed_kingdom"], wipe=False, reapply_backfill=False,
        )

    @staticmethod
    def parse_alliance_tags(text: str) -> list[str]:
        """Parse a comma/space-separated tag list into unique valid tags."""
        seen, tags = set(), []
        for raw in text.replace(",", " ").split():
            tag = raw.strip().upper()
            if tag.isalnum() and 2 <= len(tag) <= 4 and tag not in seen:
                seen.add(tag)
                tags.append(tag)
        return tags

    alliance = app_commands.Group(
        name="alliance", description="Manage alliances (admins only).",
        default_permissions=discord.Permissions(administrator=True), guild_only=True,
    )

    @alliance.command(name="add", description="Create an alliance: role, leader role, and channel group.")
    @app_commands.describe(tag="Short alliance tag (letters/numbers)", name="Optional full name")
    async def alliance_add(self, interaction: discord.Interaction, tag: str, name: str | None = None):
        config = await database.get_config(interaction.guild.id)
        if not config or not config.get("verified_role_id"):
            await interaction.response.send_message("Run setup first.", ephemeral=True)
            return
        tag = tag.strip().upper()
        if not tag.isalnum() or not (2 <= len(tag) <= 4):
            await interaction.response.send_message("Tag must be 2–4 letters/numbers.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        alliance = await self.provision_alliance(interaction.guild, tag, name)
        member_role = interaction.guild.get_role(alliance["member_role_id"])
        leader_role = interaction.guild.get_role(alliance["leader_role_id"])
        await interaction.followup.send(
            f"✅ Alliance **{tag}** ready — {member_role.mention} / {leader_role.mention}.",
            ephemeral=True,
        )

    @alliance.command(name="remove", description="Delete an alliance's roles and channels.")
    @app_commands.describe(tag="The alliance tag to remove")
    async def alliance_remove(self, interaction: discord.Interaction, tag: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await self.remove_alliance(interaction.guild, tag)
        msg = f"🗑️ Removed alliance **{tag.upper()}**." if ok else f"No alliance tagged **{tag.upper()}**."
        await interaction.followup.send(msg, ephemeral=True)

    @alliance.command(name="list", description="List this server's alliances.")
    async def alliance_list(self, interaction: discord.Interaction):
        alliances = await database.all_alliances(interaction.guild.id)
        if not alliances:
            await interaction.response.send_message("No alliances yet.", ephemeral=True)
            return
        lines = []
        for a in alliances:
            role = interaction.guild.get_role(a["member_role_id"])
            leader = interaction.guild.get_role(a["leader_role_id"])
            lines.append(f"**{a['tag']}** — {a['name']}  ·  "
                         f"{role.mention if role else '(role?)'} / "
                         f"{leader.mention if leader else '(leader?)'}")
        embed = discord.Embed(title="Alliances", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── staff-only visibility (owner + roles above the bot) ───
    def _staff_overwrites(self, guild: discord.Guild) -> dict:
        """View/send for the owner, the bot, and every role positioned ABOVE the
        bot's top role; hidden from everyone else."""
        me = guild.me
        allow = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: allow,
        }
        if guild.owner:
            overwrites[guild.owner] = allow
        bot_position = me.top_role.position
        for role in guild.roles:
            if role.is_default() or role == me.top_role:
                continue
            if role.position > bot_position:
                overwrites[role] = allow
        return overwrites

    # ── setup channel + control panel ─────────────────────────
    async def ensure_setup_channel(self, guild: discord.Guild) -> discord.TextChannel:
        """Create (or reuse) the staff-only bot-setup channel and its control panel."""
        config = await database.get_config(guild.id) or {}
        overwrites = self._staff_overwrites(guild)

        channel = None
        if config.get("setup_channel_id"):
            channel = guild.get_channel(int(config["setup_channel_id"]))
        if isinstance(channel, discord.TextChannel):
            # Re-apply visibility so a stale/older channel gets corrected.
            try:
                await channel.edit(overwrites=overwrites, reason="Kingshot setup room visibility")
            except discord.HTTPException:
                pass
        else:
            channel = await guild.create_text_channel(
                SETUP_CHANNEL_NAME, overwrites=overwrites, reason="Kingshot setup room"
            )
            await database.upsert_config(guild.id, setup_channel_id=channel.id)

        await self._ensure_panel_message(guild, channel)
        return channel

    # ── member-list / database channel ────────────────────────
    async def refresh_member_list(self, guild: discord.Guild):
        """Post/refresh the roster (verified + unverified, by role) in the staff channel."""
        config = await database.get_config(guild.id) or {}
        cid = config.get("member_list_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if not channel:
            return

        verified_role = guild.get_role(config["verified_role_id"]) if config.get("verified_role_id") else None
        if not guild.chunked:
            try:
                await guild.chunk()
            except discord.HTTPException:
                pass

        players = {p["discord_id"]: p for p in await database.all_players(guild.id)}
        verified_lines, unverified_lines = [], []
        for m in guild.members:
            if m.bot:
                continue
            if verified_role and verified_role in m.roles:
                p = players.get(str(m.id))
                if p:
                    verified_lines.append(
                        f"✅ `{p['ingame_id']}` **{p['ingame_name']}** · K{p['kingdom']} · {p['alliance'] or '—'}"
                    )
                else:
                    verified_lines.append(f"✅ {m.display_name}")
            else:
                unverified_lines.append(f"⬜ {m.display_name}")

        def block(lines):
            text = "\n".join(lines)
            if len(text) > 1000:
                text = text[:1000].rsplit("\n", 1)[0] + "\n… (more — /roster for full CSV)"
            return text or "—"

        total = len(verified_lines) + len(unverified_lines)
        embed = discord.Embed(
            title="📋 Member database",
            color=discord.Color.green(),
            description=(f"**Members:** {total}   •   **Verified:** {len(verified_lines)}   "
                         f"•   **Unverified:** {len(unverified_lines)}"),
        )
        embed.add_field(name=f"✅ Verified ({len(verified_lines)})", value=block(verified_lines), inline=False)
        embed.add_field(name=f"⬜ Unverified ({len(unverified_lines)})", value=block(unverified_lines), inline=False)
        embed.set_footer(text="Auto-updates on join/verify/unverify · /roster for CSV export")

        msg = None
        msg_id = config.get("member_list_message_id")
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
            await database.upsert_config(guild.id, member_list_message_id=new.id)

    async def _ensure_panel_message(self, guild, channel):
        config = await database.get_config(guild.id) or {}
        msg_id = config.get("setup_panel_message_id")
        if msg_id:
            try:
                await channel.fetch_message(int(msg_id))
                return
            except (discord.NotFound, discord.HTTPException):
                pass
        embed = discord.Embed(
            title="🛠️ Bot Control Panel",
            color=discord.Color.blurple(),
            description=(
                "Only you (the owner/admins) can see this.\n\n"
                "**Setup / Re-Setup** — wipe the bot's roles & channels and rebuild everything "
                "(your player records are kept).\n"
                "**Add / Remove Alliance** — manage alliances any time.\n\n"
                "Run **Setup** to get started."
            ),
        )
        message = await channel.send(embed=embed, view=SetupPanelView())
        await database.upsert_config(guild.id, setup_panel_message_id=message.id)

    async def _post_alert(self, guild, text: str):
        config = await database.get_config(guild.id) or {}
        cid = config.get("setup_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if channel:
            try:
                await channel.send(
                    embed=discord.Embed(description=text, color=discord.Color.orange()),
                    delete_after=SETUP_MSG_TTL,
                )
            except discord.HTTPException:
                pass

    # ── core provisioning ─────────────────────────────────────
    async def run_provision(self, interaction, kingdom,
                            wipe=True, reapply_backfill=True, alliance_tags=None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild

        await self._post_alert(guild, "⚙️ **Setup in progress** — building roles & channels, please wait…")

        if wipe:
            await self.wipe_bot_artifacts(guild)

        config = await database.get_config(guild.id) or {}
        me = guild.me
        hidden = discord.PermissionOverwrite(view_channel=False)
        bot_allow = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        # 1. Core roles.
        unverified = await self._ensure_role(guild, config.get("unverified_role_id"), UNVERIFIED_ROLE_NAME)
        verified = await self._ensure_role(
            guild, config.get("verified_role_id"), VERIFIED_ROLE_NAME, color=discord.Color.green()
        )

        # 2. Bot Center channels (verify / welcome / info / log / member-list).
        category = await self._ensure_category(guild, config.get("category_id"))
        verify_channel = await self._ensure_text_channel(
            guild, config.get("verify_channel_id"), VERIFY_CHANNEL_NAME, category,
            overwrites={
                guild.default_role: hidden,
                unverified: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
                me: bot_allow,
            },
        )
        info_channel = await self._ensure_text_channel(
            guild, config.get("info_channel_id"), INFO_CHANNEL_NAME, category,
            overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                        unverified: hidden, me: bot_allow},
        )
        log_channel = await self._ensure_text_channel(
            guild, config.get("log_channel_id"), LOG_CHANNEL_NAME, category,
            overwrites={guild.default_role: hidden, me: bot_allow},
        )
        welcome_channel = await self._ensure_text_channel(
            guild, config.get("welcome_channel_id"), WELCOME_CHANNEL_NAME, category,
            overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                        me: bot_allow},
        )
        member_list_channel = await self._ensure_text_channel(
            guild, config.get("member_list_channel_id"), MEMBER_LIST_CHANNEL_NAME, category,
            overwrites=self._staff_overwrites(guild),
        )

        # 3. Community section (OUTSIDE the bot category) + War group.
        community = await self._provision_community(guild, verified, config)
        general_channel = community["general"]
        war = await self._provision_war(guild, verified, config)

        # 4. Persist config.
        await database.upsert_config(
            guild.id,
            unverified_role_id=unverified.id, verified_role_id=verified.id,
            category_id=category.id, verify_channel_id=verify_channel.id,
            info_channel_id=info_channel.id, log_channel_id=log_channel.id,
            welcome_channel_id=welcome_channel.id, member_list_channel_id=member_list_channel.id,
            community_category_id=community["category"].id, general_channel_id=general_channel.id,
            memes_channel_id=community["memes"].id, gifs_channel_id=community["gifs"].id,
            lobby_voice_id=community["lobby"].id,
            war_category_id=war["category"].id, war_strategy_id=war["strategy"].id,
            war_voice_id=war["voice"].id, rally_leaders_channel_id=war["rally_leaders"].id,
            rally_joiners_channel_id=war["rally_joiners"].id,
            rally_leader_role_id=war["leader_role"].id, rally_joiner_role_id=war["joiner_role"].id,
            allowed_kingdom=kingdom, lockdown_existing=1,
        )

        # 5. Lockdown: Unverified sees only verify + welcome.
        swept, skipped = await self._apply_lockdown_permissions(
            guild, unverified, allowed_channel_ids={verify_channel.id, welcome_channel.id}
        )

        # 6. Verify button.
        await self._post_verify_message(guild, verify_channel, kingdom, 0)

        # 7. Alliances entered in the setup pop-up.
        created_tags = []
        for tag in (alliance_tags or []):
            try:
                await self.provision_alliance(guild, tag, None)
                created_tags.append(tag)
            except discord.HTTPException:
                logging.warning("Could not create alliance %s in %s", tag, guild.id)
        await self.refresh_member_list(guild)

        # 8. Re-gate existing members: everyone without the Verified role gets the
        #    Unverified role so they can (re-)verify. Skipped on /resync.
        backfilled = 0
        if reapply_backfill:
            backfilled = await self._backfill_members(guild, unverified, verified)
            await self.refresh_member_list(guild)

        summary = discord.Embed(
            title="✅ Setup complete", color=discord.Color.green(),
            description=(
                f"**Verify:** {verify_channel.mention}  •  **Welcome:** {welcome_channel.mention}\n"
                f"**Community:** {general_channel.mention}, {community['memes'].mention}, "
                f"{community['gifs'].mention}, 🔊 {community['lobby'].name}\n"
                f"**Staff:** {member_list_channel.mention}, {log_channel.mention}, {info_channel.mention}\n"
                f"**War:** {war['strategy'].mention}, {war['rally_leaders'].mention}, "
                f"{war['rally_joiners'].mention}, 🔊 {war['voice'].name}\n"
                f"**Allowed kingdom:** {kingdom}\n"
                + (f"**Alliances created:** {', '.join(created_tags)}\n" if created_tags else
                   "**Alliances:** none yet — use **Add Alliance** on the panel.\n")
                + f"**Channels locked for Unverified:** {swept}"
                + (f" (skipped {skipped})" if skipped else "")
                + (f"\n**Existing members set to Unverified:** {backfilled}" if reapply_backfill else "")
            ),
        )
        # Setup messages are temporary — post to the setup channel, self-delete in 15s.
        target = interaction.channel
        try:
            await target.send(embed=summary, delete_after=SETUP_MSG_TTL)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            "✅ Setup complete — a summary is posted here and will clear in 15 seconds.",
            ephemeral=True,
        )

    async def _provision_community(self, guild, verified, config) -> dict:
        """Social section outside the bot category: general, memes, gifs, Lobby voice."""
        me = guild.me
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            verified: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True),
        }
        category = config.get("community_category_id") and guild.get_channel(int(config["community_category_id"]))
        if not isinstance(category, discord.CategoryChannel):
            category = await guild.create_category(COMMUNITY_CATEGORY_NAME, overwrites=overwrites, reason="Kingshot community")
        general = await self._ensure_text_channel(guild, config.get("general_channel_id"), GENERAL_CHANNEL_NAME, category, overwrites)
        memes = await self._ensure_text_channel(guild, config.get("memes_channel_id"), MEMES_CHANNEL_NAME, category, overwrites)
        gifs = await self._ensure_text_channel(guild, config.get("gifs_channel_id"), GIFS_CHANNEL_NAME, category, overwrites)
        lobby = await self._ensure_voice_channel(guild, config.get("lobby_voice_id"), LOBBY_VOICE_NAME, category, overwrites)
        return {"category": category, "general": general, "memes": memes, "gifs": gifs, "lobby": lobby}

    async def _provision_war(self, guild, verified, config) -> dict:
        me = guild.me
        leader_role = await self._ensure_role(
            guild, config.get("rally_leader_role_id"), RALLY_LEADER_ROLE, color=discord.Color.orange()
        )
        joiner_role = await self._ensure_role(
            guild, config.get("rally_joiner_role_id"), RALLY_JOINER_ROLE, color=discord.Color.teal()
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            verified: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True),
        }
        category = config.get("war_category_id") and guild.get_channel(int(config["war_category_id"]))
        if not isinstance(category, discord.CategoryChannel):
            category = await guild.create_category(WAR_CATEGORY_NAME, overwrites=overwrites, reason="Kingshot war group")
        strategy = await self._ensure_text_channel(guild, config.get("war_strategy_id"), WAR_STRATEGY_NAME, category, overwrites)
        rally_leaders = await self._ensure_text_channel(guild, config.get("rally_leaders_channel_id"), RALLY_LEADERS_CHANNEL, category, overwrites)
        rally_joiners = await self._ensure_text_channel(guild, config.get("rally_joiners_channel_id"), RALLY_JOINERS_CHANNEL, category, overwrites)
        voice = await self._ensure_voice_channel(guild, config.get("war_voice_id"), WAR_VOICE_NAME, category, overwrites)
        return {"category": category, "strategy": strategy, "voice": voice,
                "rally_leaders": rally_leaders, "rally_joiners": rally_joiners,
                "leader_role": leader_role, "joiner_role": joiner_role}

    # ── wipe ──────────────────────────────────────────────────
    async def wipe_bot_artifacts(self, guild: discord.Guild):
        """Delete every bot-created role/channel (except the setup channel). Keeps players."""
        config = await database.get_config(guild.id) or {}

        for a in await database.all_alliances(guild.id):
            await self.remove_alliance(guild, a["tag"])

        channel_keys = (
            "verify_channel_id", "info_channel_id", "log_channel_id", "welcome_channel_id",
            "member_list_channel_id", "general_channel_id", "memes_channel_id", "gifs_channel_id",
            "lobby_voice_id", "community_category_id", "war_strategy_id", "war_voice_id",
            "rally_leaders_channel_id", "rally_joiners_channel_id", "war_category_id", "category_id",
        )
        for key in channel_keys:
            cid = config.get(key)
            chan = guild.get_channel(int(cid)) if cid else None
            if chan:
                try:
                    await chan.delete(reason="Kingshot setup wipe")
                except discord.HTTPException:
                    pass

        role_keys = ("unverified_role_id", "verified_role_id", "rally_leader_role_id", "rally_joiner_role_id")
        for key in role_keys:
            rid = config.get(key)
            role = guild.get_role(int(rid)) if rid else None
            if role:
                try:
                    await role.delete(reason="Kingshot setup wipe")
                except discord.HTTPException:
                    pass

        await database.clear_alliances(guild.id)
        await database.upsert_config(
            guild.id,
            unverified_role_id=None, verified_role_id=None, category_id=None,
            verify_channel_id=None, info_channel_id=None, log_channel_id=None,
            welcome_channel_id=None, verify_message_id=None, general_channel_id=None,
            member_list_channel_id=None, member_list_message_id=None,
            community_category_id=None, memes_channel_id=None, gifs_channel_id=None, lobby_voice_id=None,
            war_category_id=None, war_strategy_id=None, war_voice_id=None,
            rally_leaders_channel_id=None, rally_joiners_channel_id=None,
            rally_leader_role_id=None, rally_joiner_role_id=None,
        )

    # ── generic ensure helpers ────────────────────────────────
    async def _ensure_role(self, guild, role_id, name, **kwargs) -> discord.Role:
        if role_id:
            role = guild.get_role(int(role_id))
            if role:
                return role
        return await guild.create_role(name=name, reason="Kingshot bot setup", **kwargs)

    async def _ensure_category(self, guild, category_id) -> discord.CategoryChannel:
        if category_id:
            chan = guild.get_channel(int(category_id))
            if isinstance(chan, discord.CategoryChannel):
                return chan
        return await guild.create_category(CATEGORY_NAME, reason="Kingshot bot setup")

    async def _ensure_text_channel(self, guild, channel_id, name, category, overwrites):
        if channel_id:
            chan = guild.get_channel(int(channel_id))
            if isinstance(chan, discord.TextChannel):
                return chan
        return await guild.create_text_channel(name, category=category, overwrites=overwrites, reason="Kingshot bot setup")

    async def _ensure_voice_channel(self, guild, channel_id, name, category, overwrites):
        if channel_id:
            chan = guild.get_channel(int(channel_id))
            if isinstance(chan, discord.VoiceChannel):
                return chan
        return await guild.create_voice_channel(name, category=category, overwrites=overwrites, reason="Kingshot bot setup")

    async def _apply_lockdown_permissions(self, guild, unverified, allowed_channel_ids):
        swept = skipped = 0
        for channel in guild.channels:
            try:
                if channel.id in allowed_channel_ids:
                    await channel.set_permissions(unverified, view_channel=True, send_messages=False,
                                                  read_message_history=True, reason="Kingshot verify gate")
                else:
                    await channel.set_permissions(unverified, view_channel=False, reason="Kingshot verify gate")
                swept += 1
            except discord.Forbidden:
                skipped += 1
            except discord.HTTPException as e:
                skipped += 1
                logging.warning("Lockdown overwrite failed on %s: %s", channel, e)
        return swept, skipped

    # ── alliance provisioning ─────────────────────────────────
    async def provision_alliance(self, guild, tag: str, name: str | None):
        tag = tag.upper()
        name = (name or tag).strip()
        existing = await database.get_alliance(guild.id, tag) or {}

        member_role = await self._ensure_role(guild, existing.get("member_role_id"), tag)
        leader_role = await self._ensure_role(
            guild, existing.get("leader_role_id"), f"{tag} {LEADER_ROLE_SUFFIX}", color=discord.Color.gold()
        )
        me = guild.me
        base_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member_role: discord.PermissionOverwrite(view_channel=True),
            leader_role: discord.PermissionOverwrite(view_channel=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True),
        }
        category = existing.get("category_id") and guild.get_channel(int(existing["category_id"]))
        if not isinstance(category, discord.CategoryChannel):
            category = await guild.create_category(tag, overwrites=base_overwrites, reason="Kingshot alliance setup")

        chat = await self._ensure_text_channel(guild, existing.get("chat_channel_id"), f"{tag}-chat", category, base_overwrites)
        leaders_overwrites = dict(base_overwrites)
        leaders_overwrites[member_role] = discord.PermissionOverwrite(view_channel=False)
        leaders = await self._ensure_text_channel(guild, existing.get("leaders_channel_id"), f"{tag}-leaders", category, leaders_overwrites)
        voice = await self._ensure_voice_channel(guild, existing.get("voice_channel_id"), f"{tag} Voice", category, base_overwrites)

        await database.add_alliance(
            guild.id, tag, name,
            member_role_id=member_role.id, leader_role_id=leader_role.id, category_id=category.id,
            chat_channel_id=chat.id, leaders_channel_id=leaders.id, voice_channel_id=voice.id,
        )
        return await database.get_alliance(guild.id, tag)

    async def remove_alliance(self, guild, tag: str) -> bool:
        tag = tag.upper()
        alliance = await database.get_alliance(guild.id, tag)
        if not alliance:
            return False
        for cid in (alliance["chat_channel_id"], alliance["leaders_channel_id"],
                    alliance["voice_channel_id"], alliance["category_id"]):
            chan = guild.get_channel(int(cid)) if cid else None
            if chan:
                try:
                    await chan.delete(reason="Kingshot alliance removed")
                except discord.HTTPException:
                    pass
        for rid in (alliance["member_role_id"], alliance["leader_role_id"]):
            role = guild.get_role(int(rid)) if rid else None
            if role:
                try:
                    await role.delete(reason="Kingshot alliance removed")
                except discord.HTTPException:
                    pass
        await database.delete_alliance(guild.id, tag)
        return True

    async def _post_verify_message(self, guild, verify_channel, kingdom, level):
        config = await database.get_config(guild.id) or {}
        msg_id = config.get("verify_message_id")
        if msg_id:
            try:
                await verify_channel.fetch_message(int(msg_id))
                return
            except (discord.NotFound, discord.HTTPException):
                pass
        embed = discord.Embed(
            title="🔒 Verify to unlock the server", color=discord.Color.blurple(),
            description=(
                "Click **Verify** below and enter your Kingshot details.\n\n"
                "**You'll need:** your **in-game ID**, **in-game name**, **kingdom**, and **alliance**.\n\n"
                f"**This server accepts kingdom {kingdom}.**\n\n"
                "Already verified and want to change your details? Use **Reverify**."
            ),
        )
        message = await verify_channel.send(embed=embed, view=VerifyView())
        await database.upsert_config(guild.id, verify_message_id=message.id)

    async def _backfill_members(self, guild, unverified, verified) -> int:
        if not guild.chunked:
            try:
                await guild.chunk()
            except discord.HTTPException:
                pass
        count = 0
        for member in guild.members:
            if member.bot or member == guild.owner:
                continue
            if verified in member.roles or unverified in member.roles:
                continue
            try:
                await member.add_roles(unverified, reason="Kingshot lockdown backfill")
                count += 1
            except discord.HTTPException:
                continue
        return count

    # ── listeners ─────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        try:
            channel = await self.ensure_setup_channel(guild)
        except discord.HTTPException:
            channel = None
        note = discord.Embed(
            title="Thanks for adding the Kingshot bot!",
            color=discord.Color.blurple(),
            description=(
                f"I created a private **#{SETUP_CHANNEL_NAME}** channel only you can see — "
                "open it and press **Setup** to get started."
            ),
        )
        try:
            if guild.owner:
                await guild.owner.send(embed=note)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        # Backfill the setup channel for guilds the bot was already in.
        for guild in list(self.bot.guilds):
            try:
                await self.ensure_setup_channel(guild)
            except discord.HTTPException:
                continue


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))
