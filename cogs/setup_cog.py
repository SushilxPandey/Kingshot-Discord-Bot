"""
Setup control room + provisioning.

Setup is driven from an **owner-only `bot-setup` channel** holding a persistent
Setup / Re-Setup panel. Both actions first **wipe every bot-created role and channel**
(except the setup channel; the player DB is kept) and rebuild from scratch:

  * Unverified / Verified roles;
  * a Bot Center category — verify / bot-info / bot-log / welcome / member-list;
  * a Community section — general / memes / gifs / Lobby voice;
  * a War group — strategy / voice / rally-leaders / rally-joiners + Rally roles;
  * a **Bot Tools** section of button-driven tool pages — Scout / Locate / Look
    Yourself / Kingdom / Compare (buttons only) and a Bot Commands page (commands only);
  * a server-wide lockdown so Unverified sees only verify + welcome.

Alliances are no longer created here — a member's alliance comes from the game API and
gets a ceremonial role on demand (see the Verification cog). Only tracked IDs are ever
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
GIFTCODE_CHANNEL_NAME = "gift-codes"
MEMES_CHANNEL_NAME = "memes"
GIFS_CHANNEL_NAME = "gifs"
LOBBY_VOICE_NAME = "Lobby"

# Bot Tools section — button-first tool pages.
TOOLS_CATEGORY_NAME = "🛰️ Bot Tools"
SCOUT_CHANNEL_NAME = "scout-opponents"
LOCATE_CHANNEL_NAME = "locate-player"
SELFSTATS_CHANNEL_NAME = "my-stats"
KINGDOM_CHANNEL_NAME = "kingdom-intel"
COMPARE_CHANNEL_NAME = "compare-players"
COMMANDS_CHANNEL_NAME = "bot-commands"

SETUP_MSG_TTL = 15                   # setup status messages self-delete after 15s

WAR_CATEGORY_NAME = "⚔️ War"
WAR_STRATEGY_NAME = "war-strategy"
WAR_VOICE_NAME = "War Voice"
RALLY_LEADERS_CHANNEL = "rally-leaders"
RALLY_JOINERS_CHANNEL = "rally-joiners"
RALLY_LEADER_ROLE = "Rally Leaders"
RALLY_JOINER_ROLE = "Rally Joiners"

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
    """One pop-up that collects the kingdom + minimum Town Center level."""
    kingdom = discord.ui.TextInput(
        label="Allowed kingdom number", placeholder="e.g. 466", required=True, max_length=10
    )
    min_level = discord.ui.TextInput(
        label="Minimum Town Center level (0 = no limit)",
        placeholder="e.g. 10", required=True, max_length=3, default="0",
    )

    def __init__(self, cog: "SetupCog", wipe: bool = True):
        super().__init__()
        self.cog = cog
        self.wipe = wipe

    async def on_submit(self, interaction: discord.Interaction):
        raw_k = str(self.kingdom.value).strip()
        raw_l = str(self.min_level.value).strip() or "0"
        if not raw_k.isdigit() or not raw_l.isdigit():
            await interaction.response.send_message("Kingdom and level must be numbers.", ephemeral=True)
            return
        await self.cog.run_provision(interaction, int(raw_k), level=int(raw_l), wipe=self.wipe)


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

    def _perm_gate(self, interaction) -> str | None:
        missing = missing_permissions(interaction.guild.me)
        if missing:
            return ("I'm missing these permissions: " + ", ".join(missing)
                    + ". Grant them and make sure my role is near the top, then try again.")
        return None

    @discord.ui.button(label="Setup", style=discord.ButtonStyle.success, emoji="🛠️", custom_id="ks_setup")
    async def setup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        gate = self._perm_gate(interaction)
        if gate:
            await interaction.response.send_message(gate, ephemeral=True)
            return
        await interaction.response.send_modal(SetupModal(cog, wipe=True))

    @discord.ui.button(label="Re-Setup", style=discord.ButtonStyle.danger, emoji="♻️", custom_id="ks_resetup")
    async def resetup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = await self._guard(interaction)
        if not cog:
            return
        gate = self._perm_gate(interaction)
        if gate:
            await interaction.response.send_message(gate, ephemeral=True)
            return
        await interaction.response.send_modal(SetupModal(cog, wipe=True))


# ──────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────
class SetupCog(commands.Cog, name="Setup"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── slash bootstrap ───────────────────────────────────────
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
            f"Your control panel is ready in {channel.mention} — use the buttons there to set up the bot.",
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
            interaction, config["allowed_kingdom"], level=config.get("allowed_level") or 0,
            wipe=False, reapply_backfill=False,
        )

    # ── overwrite helpers ─────────────────────────────────────
    def _staff_overwrites(self, guild: discord.Guild, read_only: bool = False) -> dict:
        """View for the owner + roles positioned ABOVE the bot's top role; hidden from
        everyone else. ``read_only`` blocks their sending (bot still posts)."""
        me = guild.me
        staff = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=not read_only,
            use_application_commands=not read_only,
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if guild.owner:
            overwrites[guild.owner] = staff
        bot_position = me.top_role.position
        for role in guild.roles:
            if role.is_default() or role == me.top_role:
                continue
            if role.position > bot_position:
                overwrites[role] = staff
        return overwrites

    def _tool_overwrites(self, guild: discord.Guild, verified: discord.Role, commands_only: bool) -> dict:
        """Verified can view but not chat. Buttons always work (component interactions
        aren't gated); slash commands work only when ``commands_only`` is True."""
        me = guild.me
        return {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            verified: discord.PermissionOverwrite(
                view_channel=True, send_messages=False,
                use_application_commands=commands_only, add_reactions=False,
            ),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }

    # ── setup channel + control panel ─────────────────────────
    async def ensure_setup_channel(self, guild: discord.Guild) -> discord.TextChannel:
        config = await database.get_config(guild.id) or {}
        overwrites = self._staff_overwrites(guild)

        channel = None
        if config.get("setup_channel_id"):
            channel = guild.get_channel(int(config["setup_channel_id"]))
        if isinstance(channel, discord.TextChannel):
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
                    name = p["ingame_name"] or "⏳ pending"
                    verified_lines.append(
                        f"✅ `{p['ingame_id']}` **{name}** · K{p['kingdom']} · {p['alliance'] or '—'}"
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
                "(your player records are kept).\n\n"
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
    async def run_provision(self, interaction, kingdom, level=0, wipe=True, reapply_backfill=True):
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

        # 2. Bot Center channels (verify / info / log / welcome / member-list).
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
            overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                                        use_application_commands=False),
                        unverified: hidden, me: bot_allow},
        )
        log_channel = await self._ensure_text_channel(
            guild, config.get("log_channel_id"), LOG_CHANNEL_NAME, category,
            overwrites=self._staff_overwrites(guild, read_only=True),
        )
        welcome_channel = await self._ensure_text_channel(
            guild, config.get("welcome_channel_id"), WELCOME_CHANNEL_NAME, category,
            overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                        me: bot_allow},
        )
        member_list_channel = await self._ensure_text_channel(
            guild, config.get("member_list_channel_id"), MEMBER_LIST_CHANNEL_NAME, category,
            overwrites=self._staff_overwrites(guild, read_only=True),
        )

        # 3. Community + War + Bot Tools sections.
        community = await self._provision_community(guild, verified, config)
        general_channel = community["general"]
        war = await self._provision_war(guild, verified, config)
        tools = await self._provision_tools(guild, verified, config)

        # 4. Persist config.
        await database.upsert_config(
            guild.id,
            unverified_role_id=unverified.id, verified_role_id=verified.id,
            category_id=category.id, verify_channel_id=verify_channel.id,
            info_channel_id=info_channel.id, log_channel_id=log_channel.id,
            welcome_channel_id=welcome_channel.id, member_list_channel_id=member_list_channel.id,
            community_category_id=community["category"].id, general_channel_id=general_channel.id,
            giftcode_channel_id=community["giftcodes"].id,
            memes_channel_id=community["memes"].id, gifs_channel_id=community["gifs"].id,
            lobby_voice_id=community["lobby"].id,
            war_category_id=war["category"].id, war_strategy_id=war["strategy"].id,
            war_voice_id=war["voice"].id, rally_leaders_channel_id=war["rally_leaders"].id,
            rally_joiners_channel_id=war["rally_joiners"].id,
            rally_leader_role_id=war["leader_role"].id, rally_joiner_role_id=war["joiner_role"].id,
            tools_category_id=tools["category"].id,
            scout_channel_id=tools["scout"].id, locate_channel_id=tools["locate"].id,
            selfstats_channel_id=tools["selfstats"].id, kingdom_channel_id=tools["kingdom"].id,
            compare_channel_id=tools["compare"].id, commands_channel_id=tools["commands"].id,
            allowed_kingdom=kingdom, allowed_level=level, lockdown_existing=1,
        )

        # 5. Lockdown: Unverified sees only verify + welcome.
        swept, skipped = await self._apply_lockdown_permissions(
            guild, unverified, allowed_channel_ids={verify_channel.id, welcome_channel.id}
        )

        # 6. Verify button + bot-info help.
        await self._post_verify_message(guild, verify_channel, kingdom, level)
        await self._post_info_help(guild, info_channel)

        # 7. Post every tool panel + tell moderation which channel is gifs-only.
        scout_cog = self.bot.get_cog("Scout")
        if scout_cog:
            await scout_cog.ensure_panel(guild)
        intel_cog = self.bot.get_cog("Intel")
        if intel_cog:
            await intel_cog.ensure_panels(guild)
        mod_cog = self.bot.get_cog("Moderation")
        if mod_cog:
            mod_cog.note_gifs_channel(guild.id, community["gifs"].id)
        roster_cog = self.bot.get_cog("Roster")
        if roster_cog:
            await roster_cog.ensure_manage_panel(guild)

        await self.refresh_member_list(guild)

        # 8. Re-gate existing members so they can (re-)verify. Skipped on /resync.
        backfilled = 0
        if reapply_backfill:
            backfilled = await self._backfill_members(guild, unverified, verified)
            await self.refresh_member_list(guild)

        summary = discord.Embed(
            title="✅ Setup complete", color=discord.Color.green(),
            description=(
                f"**Verify:** {verify_channel.mention}  •  **Welcome:** {welcome_channel.mention}\n"
                f"**Community:** {general_channel.mention}, {community['giftcodes'].mention}, "
                f"{community['memes'].mention}, {community['gifs'].mention}, 🔊 {community['lobby'].name}\n"
                f"**Staff:** {member_list_channel.mention}, {log_channel.mention}, {info_channel.mention}\n"
                f"**War:** {war['strategy'].mention}, {war['rally_leaders'].mention}, "
                f"{war['rally_joiners'].mention}, 🔊 {war['voice'].name}\n"
                f"**Bot Tools:** {tools['scout'].mention}, {tools['locate'].mention}, "
                f"{tools['selfstats'].mention}, {tools['kingdom'].mention}, "
                f"{tools['compare'].mention}, {tools['commands'].mention}\n"
                f"**Allowed kingdom:** {kingdom}"
                + (f"  •  **Min TC level:** {level}" if level else "") + "\n"
                + f"**Channels locked for Unverified:** {swept}"
                + (f" (skipped {skipped})" if skipped else "")
                + (f"\n**Existing members set to Unverified:** {backfilled}" if reapply_backfill else "")
            ),
        )
        try:
            await interaction.channel.send(embed=summary, delete_after=SETUP_MSG_TTL)
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
        # Gift codes: everyone verified can read, only the bot posts (read-only).
        readonly = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            verified: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                  use_application_commands=False, add_reactions=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        giftcodes = await self._ensure_text_channel(guild, config.get("giftcode_channel_id"), GIFTCODE_CHANNEL_NAME, category, readonly)
        memes = await self._ensure_text_channel(guild, config.get("memes_channel_id"), MEMES_CHANNEL_NAME, category, overwrites)
        gifs = await self._ensure_text_channel(guild, config.get("gifs_channel_id"), GIFS_CHANNEL_NAME, category, overwrites)
        lobby = await self._ensure_voice_channel(guild, config.get("lobby_voice_id"), LOBBY_VOICE_NAME, category, overwrites)
        return {"category": category, "general": general, "giftcodes": giftcodes,
                "memes": memes, "gifs": gifs, "lobby": lobby}

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

    async def _provision_tools(self, guild, verified, config) -> dict:
        """Button-first tool pages. Five are buttons-only; bot-commands allows commands."""
        me = guild.me
        cat_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            verified: discord.PermissionOverwrite(view_channel=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        category = config.get("tools_category_id") and guild.get_channel(int(config["tools_category_id"]))
        if not isinstance(category, discord.CategoryChannel):
            category = await guild.create_category(TOOLS_CATEGORY_NAME, overwrites=cat_overwrites, reason="Kingshot bot tools")

        buttons_only = self._tool_overwrites(guild, verified, commands_only=False)
        commands_only = self._tool_overwrites(guild, verified, commands_only=True)
        scout = await self._ensure_text_channel(guild, config.get("scout_channel_id"), SCOUT_CHANNEL_NAME, category, buttons_only)
        locate = await self._ensure_text_channel(guild, config.get("locate_channel_id"), LOCATE_CHANNEL_NAME, category, buttons_only)
        selfstats = await self._ensure_text_channel(guild, config.get("selfstats_channel_id"), SELFSTATS_CHANNEL_NAME, category, buttons_only)
        kingdom = await self._ensure_text_channel(guild, config.get("kingdom_channel_id"), KINGDOM_CHANNEL_NAME, category, buttons_only)
        compare = await self._ensure_text_channel(guild, config.get("compare_channel_id"), COMPARE_CHANNEL_NAME, category, buttons_only)
        commands = await self._ensure_text_channel(guild, config.get("commands_channel_id"), COMMANDS_CHANNEL_NAME, category, commands_only)
        return {"category": category, "scout": scout, "locate": locate, "selfstats": selfstats,
                "kingdom": kingdom, "compare": compare, "commands": commands}

    # ── wipe ──────────────────────────────────────────────────
    async def wipe_bot_artifacts(self, guild: discord.Guild):
        """Delete every bot-created role/channel (except the setup channel). Keeps players."""
        config = await database.get_config(guild.id) or {}

        # Ceremonial alliance roles (from the API) — delete the role, no channels.
        for a in await database.all_alliances(guild.id):
            await self.remove_alliance(guild, a["tag"])

        channel_keys = (
            "verify_channel_id", "info_channel_id", "log_channel_id", "welcome_channel_id",
            "member_list_channel_id", "general_channel_id", "giftcode_channel_id",
            "memes_channel_id", "gifs_channel_id", "lobby_voice_id", "community_category_id",
            "war_strategy_id", "war_voice_id", "rally_leaders_channel_id", "rally_joiners_channel_id",
            "war_category_id", "category_id",
            "scout_channel_id", "locate_channel_id", "selfstats_channel_id", "kingdom_channel_id",
            "compare_channel_id", "commands_channel_id", "tools_category_id",
            # legacy (pre-phase-4) points channels — clean up if present on older servers.
            "points_admin_channel_id", "points_board_channel_id",
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
            welcome_channel_id=None, verify_message_id=None, info_message_id=None,
            general_channel_id=None, giftcode_channel_id=None,
            member_list_channel_id=None, member_list_message_id=None, manage_panel_message_id=None,
            community_category_id=None, memes_channel_id=None, gifs_channel_id=None, lobby_voice_id=None,
            war_category_id=None, war_strategy_id=None, war_voice_id=None,
            rally_leaders_channel_id=None, rally_joiners_channel_id=None,
            rally_leader_role_id=None, rally_joiner_role_id=None,
            tools_category_id=None,
            scout_channel_id=None, scout_panel_message_id=None,
            locate_channel_id=None, locate_panel_message_id=None,
            selfstats_channel_id=None, selfstats_panel_message_id=None,
            kingdom_channel_id=None, kingdom_panel_message_id=None,
            compare_channel_id=None, compare_panel_message_id=None,
            commands_channel_id=None, commands_panel_message_id=None,
        )

    async def remove_alliance(self, guild, tag: str) -> bool:
        """Delete a ceremonial alliance's role (and any legacy channels, if present)."""
        tag = tag.upper()
        alliance = await database.get_alliance(guild.id, tag)
        if not alliance:
            return False
        for key in ("chat_channel_id", "leaders_channel_id", "voice_channel_id", "category_id"):
            cid = alliance.get(key)
            chan = guild.get_channel(int(cid)) if cid else None
            if chan:
                try:
                    await chan.delete(reason="Kingshot alliance removed")
                except discord.HTTPException:
                    pass
        for key in ("member_role_id", "leader_role_id"):
            rid = alliance.get(key)
            role = guild.get_role(int(rid)) if rid else None
            if role:
                try:
                    await role.delete(reason="Kingshot alliance removed")
                except discord.HTTPException:
                    pass
        await database.delete_alliance(guild.id, tag)
        return True

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
                "Click **Verify** below and enter just your **in-game ID** — I'll pull your name, "
                "kingdom, and alliance from the game automatically.\n\n"
                f"**Requirements:** kingdom **{kingdom}**"
                + (f", Town Center **{level}+**" if level else "") + ".\n\n"
                "Already verified and want to refresh your stats? Use **Reverify**."
            ),
        )
        message = await verify_channel.send(embed=embed, view=VerifyView())
        await database.upsert_config(guild.id, verify_message_id=message.id)

    async def _post_info_help(self, guild, info_channel):
        config = await database.get_config(guild.id) or {}
        msg_id = config.get("info_message_id")
        if msg_id:
            try:
                await info_channel.fetch_message(int(msg_id))
                return
            except (discord.NotFound, discord.HTTPException):
                pass
        embed = discord.Embed(
            title="🤖 About this bot — how everything works",
            color=discord.Color.blurple(),
            description=(
                "Welcome! This bot runs the server and gives everyone live Kingshot intel. "
                "Almost everything is done with **buttons** — you rarely need to type a command."
            ),
        )
        embed.add_field(
            name="✅ Getting verified",
            value=("Go to the verify channel and tap **Verify**, then enter your **in-game ID** "
                   "(Profile → the number under your name). I confirm your kingdom, set your "
                   "nickname, and unlock the rest of the server. Tap **Reverify** any time to "
                   "refresh your details."),
            inline=False,
        )
        embed.add_field(
            name="🛰️ Bot Tools (click the buttons)",
            value=("🎯 **Scout Opponents** — see any enemy alliance's top players, heroes & gear.\n"
                   "📍 **Locate a Player** — kingdom, map coordinates, alliance, activity.\n"
                   "🪞 **My Stats** — your own detailed profile with power, ranks, heroes.\n"
                   "🏰 **Kingdom Knowledge** — a kingdom's battle stats and its top 10 players.\n"
                   "⚖️ **Compare Players** — two players side by side."),
            inline=False,
        )
        embed.add_field(
            name="🎁 Gift codes & 🗣️ community",
            value=("New gift codes are announced automatically in the gift-codes channel. Chat in general/memes, "
                   "share clips in the gifs channel (**GIFs only there**), and hop into the Lobby "
                   "or War voice channels to coordinate."),
            inline=False,
        )
        embed.set_footer(text="Questions? Ping an admin.")
        message = await info_channel.send(embed=embed)
        await database.upsert_config(guild.id, info_message_id=message.id)

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
            await self.ensure_setup_channel(guild)
        except discord.HTTPException:
            pass
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
        for guild in list(self.bot.guilds):
            try:
                await self.ensure_setup_channel(guild)
            except discord.HTTPException:
                continue


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))
