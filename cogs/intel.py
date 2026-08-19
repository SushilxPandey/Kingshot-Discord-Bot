"""
Player & kingdom intel — the button-first tool pages.

Four member-facing tools, each living in its own buttons-only channel with a
persistent panel (plus a matching slash command for the bot-commands page):

  * **Locate** — where a player is: kingdom, map coordinates, alliance, activity.
  * **Look Yourself** — your own detailed stats with visuals (uses your verified ID).
  * **Kingdom Knowledge** — a kingdom's battle stats + its top 10 players.
  * **Compare** — two players side by side.

Also provides the **Bot Commands** panel (a commands-only channel) with an admin
"Post a gift code" button.

Data comes from kingshotstats via ``get_player_info`` (search), ``get_player_detail``
(full profile), and ``get_kingdom``.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import database
import kingshot_api
from cogs.scout import power_fmt, heroes_line, gov_gear, _ensure_panel


# ──────────────────────────────────────────────────────────────
# Small formatting helpers
# ──────────────────────────────────────────────────────────────
def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rank(v) -> str:
    n = _int(v)
    return f"#{n:,}" if n else "—"


def _num(v) -> str:
    n = _int(v)
    return f"{n:,}" if n is not None else "—"


def _activity(detail: dict) -> str:
    if detail.get("online"):
        return "🟢 Online now"
    for key in ("last_active_at", "last_login"):
        val = detail.get(key)
        if val:
            return f"🕓 Last seen {str(val)[:16].replace('T', ' ')}"
    return "—"


def _coords(detail: dict) -> str:
    x, y = detail.get("x"), detail.get("y")
    if x is None or y is None:
        return "Unknown"
    return f"X **{x}**, Y **{y}**"


def _tc(detail: dict):
    return detail.get("town_center_level") or detail.get("stove_lv") or detail.get("level") or "?"


def _top_hero(detail: dict) -> str:
    heroes = detail.get("arena_heroes") or detail.get("heroes") or []
    if not heroes:
        return "—"
    h = heroes[0]
    star = h.get("star_label") or f"{h.get('stars', '?')}★"
    lv = h.get("hero_level") or h.get("lv") or "?"
    return f"{h.get('name', '?')} {star} L{lv}"


async def _resolve(player_id: int) -> tuple[dict | None, dict | None, str | None]:
    """Return (search_data, full_detail, error). error is a user-facing message or None."""
    try:
        info = await kingshot_api.get_player_info(player_id)
    except Exception:
        return None, None, "Couldn't reach the stats site right now. Try again in a bit."
    data = info.get("data") or None
    if not data:
        return None, None, f"Couldn't find a Kingshot player with ID **{player_id}**."
    detail = await kingshot_api.get_player_detail(data.get("uid")) if data.get("uid") else None
    return data, detail, None


def _merge(data: dict, detail: dict | None) -> dict:
    """Full detail preferred; fall back to the lighter search record."""
    merged = dict(data or {})
    if detail:
        merged.update({k: v for k, v in detail.items() if v is not None})
    return merged


def build_player_embed(data: dict, detail: dict | None, *, title_prefix: str = "") -> discord.Embed:
    m = _merge(data, detail)
    name = m.get("name") or m.get("nick_name") or "Unknown"
    embed = discord.Embed(
        title=f"{title_prefix}{name}",
        color=discord.Color.blue(),
        description=(
            f"**Kingdom** {m.get('kingdom') or m.get('kid') or '—'}   •   "
            f"**Alliance** {m.get('alliance_abbr') or '—'}   •   "
            f"**TC** {_tc(m)}"
        ),
    )
    embed.add_field(name="⚡ Power", value=power_fmt(m.get("power")), inline=True)
    embed.add_field(name="🏆 Power rank", value=_rank(m.get("power_rank")), inline=True)
    embed.add_field(name="⚔️ Kills", value=_num(m.get("kills")), inline=True)
    if m.get("kills_rank") is not None:
        embed.add_field(name="🎖️ Kill rank", value=_rank(m.get("kills_rank")), inline=True)
    if m.get("vip") is not None:
        embed.add_field(name="👑 VIP", value=str(m.get("vip")), inline=True)
    embed.add_field(name="📍 Location", value=f"K{m.get('kingdom') or m.get('kid') or '?'} · {_coords(m)}", inline=True)
    embed.add_field(name="🕓 Activity", value=_activity(m), inline=True)
    if detail:
        embed.add_field(name="🦸 Heroes", value=heroes_line(detail)[:1024], inline=False)
        embed.add_field(name="🛡️ Governor gear", value=gov_gear(detail), inline=False)
    photo = m.get("profilePhoto") or m.get("avatar_url")
    if photo:
        embed.set_thumbnail(url=photo)
    embed.set_footer(text="Live game data")
    return embed


# ──────────────────────────────────────────────────────────────
# Locate
# ──────────────────────────────────────────────────────────────
async def build_locate_embed(player_id: int) -> discord.Embed:
    data, detail, err = await _resolve(player_id)
    if err:
        raise ValueError(err)
    m = _merge(data, detail)
    name = m.get("name") or m.get("nick_name") or "Unknown"
    embed = discord.Embed(
        title=f"📍 {name}",
        color=discord.Color.teal(),
        description=f"Alliance **{m.get('alliance_abbr') or '—'}**  ·  Power **{power_fmt(m.get('power'))}**",
    )
    embed.add_field(name="Kingdom", value=str(m.get("kingdom") or m.get("kid") or "?"), inline=True)
    embed.add_field(name="Map coordinates", value=_coords(m), inline=True)
    embed.add_field(name="Activity", value=_activity(m), inline=False)
    photo = m.get("profilePhoto") or m.get("avatar_url")
    if photo:
        embed.set_thumbnail(url=photo)
    embed.set_footer(text="Live game data · coordinates are the player's world-map tile")
    return embed


class LocateModal(discord.ui.Modal, title="Locate a player"):
    player_id = discord.ui.TextInput(label="In-game ID", placeholder="e.g. 73372825", required=True, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.player_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("The in-game ID should be numbers only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            embed = await build_locate_embed(int(raw))
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)


class LocatePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Locate a player", style=discord.ButtonStyle.primary, emoji="📍", custom_id="ks_locate_open")
    async def locate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LocateModal())


# ──────────────────────────────────────────────────────────────
# Look Yourself
# ──────────────────────────────────────────────────────────────
class SelfPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Show my stats", style=discord.ButtonStyle.success, emoji="🪞", custom_id="ks_self_open")
    async def self_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _send_self_stats(interaction)


async def _send_self_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    player = await database.get_player(interaction.guild.id, interaction.user.id)
    if not player or not player.get("ingame_id"):
        await interaction.followup.send(
            "You're not verified yet — tap **Verify** in the verify channel first.", ephemeral=True
        )
        return
    data, detail, err = await _resolve(int(player["ingame_id"]))
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return
    embed = build_player_embed(data, detail, title_prefix="🪞 ")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ──────────────────────────────────────────────────────────────
# Kingdom Knowledge
# ──────────────────────────────────────────────────────────────
def _health(kingdom: dict) -> str:
    h = kingdom.get("health") or {}
    if isinstance(h, dict) and (h.get("grade") or h.get("label")):
        grade = h.get("grade") or "?"
        label = h.get("label") or ""
        return f"{grade} — {label}" if label else str(grade)
    return "—"


async def build_kingdom_embed(kid: int) -> discord.Embed:
    kingdom = await kingshot_api.get_kingdom(kid)
    if not kingdom:
        raise ValueError(f"Couldn't load kingdom **{kid}** right now. Try again shortly.")
    embed = discord.Embed(
        title=f"🏰 Kingdom {kid}",
        color=discord.Color.dark_gold(),
        description=(
            f"**Players** {_num(kingdom.get('player_count'))}   •   "
            f"**Alliances** {_num(kingdom.get('alliance_count'))}   •   "
            f"**Health** {_health(kingdom)}"
        ),
    )
    embed.add_field(name="Total power", value=power_fmt(kingdom.get("power")), inline=True)
    embed.add_field(name="Avg power", value=power_fmt(kingdom.get("avg_power")), inline=True)
    embed.add_field(name="Top power", value=power_fmt(kingdom.get("top_power")), inline=True)
    if kingdom.get("active_7d") is not None:
        embed.add_field(name="Active (7d)", value=_num(kingdom.get("active_7d")), inline=True)

    players = kingdom.get("players") or []
    players = sorted(players, key=lambda p: p.get("power") or 0, reverse=True)[:10]
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(players):
        rank = medals[i] if i < 3 else f"`{i + 1:>2}.`"
        tag = p.get("alliance_abbr")
        tag_txt = f" [{tag}]" if tag else ""
        kills = p.get("kills")
        kill_txt = f" · ⚔️ {power_fmt(kills)}" if kills else ""
        lines.append(f"{rank} **{p.get('nick_name', '?')}**{tag_txt} — {power_fmt(p.get('power'))}{kill_txt}")
    embed.add_field(
        name="🏅 Top 10 players", value="\n".join(lines) or "No player data.", inline=False
    )
    embed.set_footer(text="Live game data")
    return embed


class KingdomModal(discord.ui.Modal, title="Look up a kingdom"):
    kingdom_id = discord.ui.TextInput(label="Kingdom number", placeholder="e.g. 466", required=True, max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.kingdom_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("Kingdom number should be numbers only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            embed = await build_kingdom_embed(int(raw))
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed)


class KingdomPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Our kingdom", style=discord.ButtonStyle.success, emoji="🏰", custom_id="ks_kingdom_home")
    async def home_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = await database.get_config(interaction.guild.id) or {}
        kid = config.get("allowed_kingdom")
        if not kid:
            await interaction.response.send_message("This server hasn't been set up yet.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            embed = await build_kingdom_embed(int(kid))
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed)

    @discord.ui.button(label="Another kingdom", style=discord.ButtonStyle.secondary, emoji="🔎", custom_id="ks_kingdom_other")
    async def other_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KingdomModal())


# ──────────────────────────────────────────────────────────────
# Compare
# ──────────────────────────────────────────────────────────────
def _cmp_row(label: str, a, b) -> str:
    return f"**{label}**\n{a}  vs  {b}"


async def build_compare_embed(id_a: int, id_b: int) -> discord.Embed:
    da, deta, erra = await _resolve(id_a)
    if erra:
        raise ValueError(f"Player A: {erra}")
    db, detb, errb = await _resolve(id_b)
    if errb:
        raise ValueError(f"Player B: {errb}")
    ma, mb = _merge(da, deta), _merge(db, detb)
    na = ma.get("name") or ma.get("nick_name") or "A"
    nb = mb.get("name") or mb.get("nick_name") or "B"

    embed = discord.Embed(
        title="⚖️ Player comparison",
        color=discord.Color.purple(),
        description=f"**🅰️ {na}**   vs   **🅱️ {nb}**",
    )

    def row(label, key, fmt=lambda v: v):
        va = fmt(ma.get(key)) if ma.get(key) is not None else "—"
        vb = fmt(mb.get(key)) if mb.get(key) is not None else "—"
        embed.add_field(name=label, value=f"🅰️ {va}\n🅱️ {vb}", inline=True)

    row("⚡ Power", "power", power_fmt)
    row("🏆 Power rank", "power_rank", lambda v: _rank(v))
    row("⚔️ Kills", "kills", lambda v: _num(v))
    embed.add_field(name="🏯 Town Center", value=f"🅰️ {_tc(ma)}\n🅱️ {_tc(mb)}", inline=True)
    embed.add_field(name="🛡️ Alliance", value=f"🅰️ {ma.get('alliance_abbr') or '—'}\n🅱️ {mb.get('alliance_abbr') or '—'}", inline=True)
    embed.add_field(name="👑 Kingdom", value=f"🅰️ {ma.get('kingdom') or ma.get('kid') or '?'}\n🅱️ {mb.get('kingdom') or mb.get('kid') or '?'}", inline=True)
    embed.add_field(name="🦸 Top hero", value=f"🅰️ {_top_hero(ma)}\n🅱️ {_top_hero(mb)}", inline=False)
    embed.set_footer(text="Live game data")
    return embed


class CompareModal(discord.ui.Modal, title="Compare two players"):
    id_a = discord.ui.TextInput(label="Player A — in-game ID", placeholder="e.g. 73372825", required=True, max_length=20)
    id_b = discord.ui.TextInput(label="Player B — in-game ID", placeholder="e.g. 18163446", required=True, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        ra, rb = str(self.id_a.value).strip(), str(self.id_b.value).strip()
        if not ra.isdigit() or not rb.isdigit():
            await interaction.response.send_message("Both in-game IDs should be numbers only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            embed = await build_compare_embed(int(ra), int(rb))
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed)


class ComparePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Compare players", style=discord.ButtonStyle.primary, emoji="⚖️", custom_id="ks_compare_open")
    async def compare_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CompareModal())


# ──────────────────────────────────────────────────────────────
# Bot Commands panel (commands-only channel) + admin gift-code button
# ──────────────────────────────────────────────────────────────
class GiftCodeModal(discord.ui.Modal, title="Post a gift code"):
    code = discord.ui.TextInput(label="Gift code", placeholder="e.g. KINGSHOT2026", required=True, max_length=40)

    async def on_submit(self, interaction: discord.Interaction):
        giftcode = interaction.client.get_cog("GiftCode")
        if giftcode is None:
            await interaction.response.send_message("Gift-code posting is unavailable right now.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await giftcode.post_code(interaction.guild, str(self.code.value).strip(), interaction.user)
        await interaction.followup.send(msg, ephemeral=True)


class CommandsPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Post a gift code", style=discord.ButtonStyle.success, emoji="🎁", custom_id="ks_gift_post")
    async def gift_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        perms = interaction.user.guild_permissions
        if not (perms.administrator or interaction.user.id == interaction.guild.owner_id):
            await interaction.response.send_message("Only admins can post gift codes.", ephemeral=True)
            return
        await interaction.response.send_modal(GiftCodeModal())


# ──────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────
class Intel(commands.Cog, name="Intel"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="locate", description="Find a player: kingdom, map coordinates, alliance, activity.")
    @app_commands.describe(player_id="The player's in-game ID")
    @app_commands.guild_only()
    async def locate(self, interaction: discord.Interaction, player_id: int):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            embed = await build_locate_embed(player_id)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="mystats", description="Show your detailed Kingshot stats.")
    @app_commands.guild_only()
    async def mystats(self, interaction: discord.Interaction):
        await _send_self_stats(interaction)

    @app_commands.command(name="kingdom", description="Show a kingdom's battle stats and its top 10 players.")
    @app_commands.describe(kingdom_id="Kingdom number (defaults to this server's kingdom)")
    @app_commands.guild_only()
    async def kingdom(self, interaction: discord.Interaction, kingdom_id: int | None = None):
        if kingdom_id is None:
            config = await database.get_config(interaction.guild.id) or {}
            kingdom_id = config.get("allowed_kingdom")
            if not kingdom_id:
                await interaction.response.send_message("This server hasn't been set up yet.", ephemeral=True)
                return
        await interaction.response.defer(thinking=True)
        try:
            embed = await build_kingdom_embed(int(kingdom_id))
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="compare", description="Compare two players side by side.")
    @app_commands.describe(player_a="First player's in-game ID", player_b="Second player's in-game ID")
    @app_commands.guild_only()
    async def compare(self, interaction: discord.Interaction, player_a: int, player_b: int):
        await interaction.response.defer(thinking=True)
        try:
            embed = await build_compare_embed(player_a, player_b)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed)

    # ── panel provisioning (called by setup) ──────────────────
    async def ensure_panels(self, guild: discord.Guild):
        await _ensure_panel(
            guild, "locate_channel_id", "locate_panel_message_id", LocatePanelView(),
            title="📍 Locate a Player",
            description="Press the button and enter a player's in-game ID to see their kingdom, "
                        "map coordinates, alliance, and activity.",
        )
        await _ensure_panel(
            guild, "selfstats_channel_id", "selfstats_panel_message_id", SelfPanelView(),
            title="🪞 Look Yourself Up",
            description="Press **Show my stats** to see your own detailed Kingshot profile — power, "
                        "ranks, kills, heroes, and gear. (You need to be verified.)",
        )
        await _ensure_panel(
            guild, "kingdom_channel_id", "kingdom_panel_message_id", KingdomPanelView(),
            title="🏰 Kingdom Knowledge",
            description="See a kingdom's battle stats and its top 10 players. **Our kingdom** shows "
                        "this server's kingdom; **Another kingdom** lets you look up any number.",
        )
        await _ensure_panel(
            guild, "compare_channel_id", "compare_panel_message_id", ComparePanelView(),
            title="⚖️ Compare Players",
            description="Press **Compare players** and enter two in-game IDs to see them side by side.",
        )
        await _ensure_panel(
            guild, "commands_channel_id", "commands_panel_message_id", CommandsPanelView(),
            title="🤖 Bot Commands",
            description=(
                "This channel is for slash commands (type `/` to see them):\n"
                "• `/scout` — scout an enemy alliance\n"
                "• `/locate` — find a player\n"
                "• `/mystats` — your detailed stats\n"
                "• `/kingdom` — kingdom stats & top 10\n"
                "• `/compare` — compare two players\n"
                "• `/age` — how long a kingdom has been open\n"
                "• `/roster`, `/unverify` — admin tools\n\n"
                "**Admins:** use the button below to broadcast a gift code to everyone."
            ),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Intel(bot))
