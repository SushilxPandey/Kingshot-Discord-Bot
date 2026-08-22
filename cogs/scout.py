"""
KvK scouting.

Give the bot any player's game ID from an enemy alliance and it reports that
alliance's top members with their power, Town Center, heroes (star tier + level +
gear), and governor-gear status — a quick war-room briefing.

Driven from the buttons-only **#scout-opponents** panel (or the `/scout` command).
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import database
import kingshot_api

DETAIL_DELAY = 1.0   # pace per-player detail fetches to be polite to the site


def power_fmt(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return str(int(n))


def heroes_line(detail: dict) -> str:
    heroes = detail.get("arena_heroes") or detail.get("heroes") or []
    parts = []
    for h in heroes[:5]:
        name = h.get("name", "?")
        star = h.get("star_label") or (f"{h.get('stars', '?')}★")
        lv = h.get("hero_level") or h.get("lv") or "?"
        avg = (h.get("gear_summary") or {}).get("avg_gear_level")
        gear = f" · gear {avg:.0f}" if isinstance(avg, (int, float)) else ""
        parts.append(f"{name} {star} L{lv}{gear}")
    return "\n".join(parts) if parts else "Hero/gear detail not scanned on the stats site yet."


def gov_gear(detail: dict) -> str:
    if detail.get("lords_gear_privacy"):
        return "🔒 Governor gear: hidden"
    gear = detail.get("lords_gear") or []
    if not gear:
        return "Governor gear: none on record"
    return f"Governor gear: {len(gear)} pieces"


# Backwards-compatible aliases (older callers used the underscored names).
_power_fmt = power_fmt
_heroes_line = heroes_line
_gov_gear = gov_gear


async def build_scout_embed(player_id: int, count: int = 5) -> discord.Embed:
    """Resolve the player's alliance and build the scouting embed. Raises ValueError
    with a user-facing message on any lookup problem."""
    count = max(1, min(count, 10))

    try:
        info = await kingshot_api.get_player_info(player_id)
    except Exception:
        raise ValueError("Couldn't reach the stats site right now. Try again in a bit.")
    seed = info.get("data") or {}
    aid = seed.get("aid")
    if not aid:
        raise ValueError(f"Couldn't find a player with ID **{player_id}** (or they have no alliance).")

    alliance = await kingshot_api.get_alliance(aid)
    if not alliance or not alliance.get("members"):
        raise ValueError("Couldn't load that alliance's members.")

    a = alliance.get("alliance") or {}
    members = alliance["members"]  # already sorted by power desc
    total_power = sum(m.get("power") or 0 for m in members)
    tag = a.get("abbr") or seed.get("alliance_abbr") or "?"
    name = a.get("name") or seed.get("alliance_name") or ""
    kid = a.get("kid") or seed.get("kingdom")

    embed = discord.Embed(
        title=f"🎯 Scouting [{tag}] {name}",
        color=discord.Color.dark_red(),
        description=(
            f"**Kingdom:** {kid}   •   **Members:** {len(members)}   •   "
            f"**Total power:** {power_fmt(total_power)}\n"
            f"Top **{min(count, len(members))}** by power:"
        ),
    )

    with_detail = 0
    for i, m in enumerate(members[:count], start=1):
        detail = await kingshot_api.get_player_detail(m.get("uid"))
        rank = m.get("alliance_rank_label") or "—"
        tc = m.get("town_center_level") or m.get("stove_lv") or "?"
        title = f"#{i} {m.get('nick_name', '?')} — {rank} · {power_fmt(m.get('power'))} · TC{tc}"
        heroes = (detail or {}).get("arena_heroes") or (detail or {}).get("heroes") or []
        if heroes:
            with_detail += 1
            value = "🦸 " + heroes_line(detail) + "\n" + gov_gear(detail)
        elif detail:
            value = "🦸 Hero/gear detail not scanned on the stats site yet.\n" + gov_gear(detail)
        else:
            value = "(couldn't load detail — try again in a moment)"
        embed.add_field(name=title, value=value[:1024], inline=False)
        await asyncio.sleep(DETAIL_DELAY)

    shown = min(count, len(members))
    embed.set_footer(
        text=(f"Live game data · full hero/gear for {with_detail}/{shown} shown "
              "· the source only has it for already-scanned players")
    )
    return embed


# ──────────────────────────────────────────────────────────────
# Panel UI (buttons-only #scout-opponents channel)
# ──────────────────────────────────────────────────────────────
class ScoutModal(discord.ui.Modal, title="Scout an enemy alliance"):
    player_id = discord.ui.TextInput(
        label="Enemy player's in-game ID", placeholder="e.g. 12345678", required=True, max_length=20
    )
    count = discord.ui.TextInput(
        label="How many top players (1–10)", required=False, max_length=2, default="5"
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.player_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("The in-game ID should be numbers only.", ephemeral=True)
            return
        n = str(self.count.value or "5").strip()
        n = int(n) if n.isdigit() else 5
        await interaction.response.defer(thinking=True)
        try:
            embed = await build_scout_embed(int(raw), n)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed, view=ScoutPanelView())


class ScoutPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Scout an alliance", style=discord.ButtonStyle.danger, emoji="🎯", custom_id="ks_scout_open")
    async def scout_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScoutModal())


class Scout(commands.Cog, name="Scout"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="scout", description="Scout an enemy alliance's top players (heroes & gear) for KvK.")
    @app_commands.describe(
        player_id="A player's in-game ID from the alliance you want to scout",
        count="How many top players to show (default 5, max 10)",
    )
    @app_commands.guild_only()
    async def scout(self, interaction: discord.Interaction, player_id: int, count: int = 5):
        await interaction.response.defer(thinking=True)
        try:
            embed = await build_scout_embed(player_id, count)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=embed, view=ScoutPanelView())

    async def ensure_panel(self, guild: discord.Guild, force: bool = False):
        await _ensure_panel(
            guild, "scout_channel_id", "scout_panel_message_id", ScoutPanelView(),
            title="🎯 Scout Opponents",
            description=(
                "Press **Scout an alliance** and enter any enemy player's in-game ID. "
                "I'll pull their whole alliance and show the top players with their power, "
                "Town Center, heroes, and gear — ready for KvK planning."
            ),
            force=force,
        )


async def _ensure_panel(guild, channel_key, message_key, view, title, description, force=False):
    """Post a persistent panel in the configured channel. If ``force``, replace the
    existing panel (used on /resync so refreshed buttons/text actually appear)."""
    config = await database.get_config(guild.id) or {}
    cid = config.get(channel_key)
    channel = guild.get_channel(int(cid)) if cid else None
    if not channel:
        return
    msg_id = config.get(message_key)
    if msg_id:
        try:
            existing = await channel.fetch_message(int(msg_id))
            if not force:
                return
            await existing.delete()   # re-post below with the current view/text
        except (discord.NotFound, discord.HTTPException):
            pass
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    try:
        message = await channel.send(embed=embed, view=view)
    except discord.HTTPException:
        return
    await database.upsert_config(guild.id, **{message_key: message.id})


async def setup(bot: commands.Bot):
    await bot.add_cog(Scout(bot))
