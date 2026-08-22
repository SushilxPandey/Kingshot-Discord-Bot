"""
Enemy watchlist + change alerts.

Any verified member can add enemy players (by game ID) to a per-server watchlist from
the **🕵️ war-intel** channel. A background loop re-checks each watched player and posts
an alert to that channel when something notable changes — a big power swing or a move on
the map — so your war team gets a heads-up without manually re-scouting.
"""

import asyncio
import logging

import discord
from discord.ext import commands, tasks

import database
import kingshot_api
from cogs.scout import power_fmt

WATCH_MINUTES = 60          # how often to re-check watched players
WATCH_DELAY = 1.2           # pace between per-player lookups
POWER_ALERT_PCT = 0.03      # alert on a >=3% power swing
MAX_WATCH = 50              # cap the watchlist size per server


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────
# Panel UI
# ──────────────────────────────────────────────────────────────
class WatchAddModal(discord.ui.Modal, title="Watch a player"):
    player_id = discord.ui.TextInput(label="Enemy player's in-game ID", placeholder="e.g. 12345678", required=True, max_length=20)
    note = discord.ui.TextInput(label="Label (optional)", placeholder="e.g. RTL R5", required=False, max_length=40)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.player_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("The in-game ID should be numbers only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if len(await database.all_watch(interaction.guild.id)) >= MAX_WATCH:
            await interaction.followup.send(f"The watchlist is full ({MAX_WATCH}). Remove someone first.", ephemeral=True)
            return
        try:
            info = await kingshot_api.get_player_info(int(raw))
            data = info.get("data") or {}
        except Exception:
            data = {}
        if not data:
            await interaction.followup.send(f"Couldn't find a player with ID **{raw}** to watch.", ephemeral=True)
            return
        label = str(self.note.value).strip() or (data.get("name") or f"ID {raw}")
        await database.add_watch(interaction.guild.id, int(raw), label, interaction.user.id)
        await database.update_watch(interaction.guild.id, int(raw),
                                    _to_int(data.get("power")), _to_int(data.get("x")), _to_int(data.get("y")))
        await interaction.followup.send(
            f"👁️ Now watching **{label}** (`{raw}`) — power {power_fmt(data.get('power'))}. "
            "You'll get alerts here on big power or location changes.",
            ephemeral=True,
        )


class WatchRemoveModal(discord.ui.Modal, title="Stop watching a player"):
    player_id = discord.ui.TextInput(label="In-game ID to remove", placeholder="e.g. 12345678", required=True, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.player_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("The in-game ID should be numbers only.", ephemeral=True)
            return
        removed = await database.remove_watch(interaction.guild.id, int(raw))
        msg = f"🗑️ Stopped watching `{raw}`." if removed else f"`{raw}` wasn't on the watchlist."
        await interaction.response.send_message(msg, ephemeral=True)


class WarIntelPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Watch a player", style=discord.ButtonStyle.danger, emoji="👁️", custom_id="ks_watch_add")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WatchAddModal())

    @discord.ui.button(label="Stop watching", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="ks_watch_remove")
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WatchRemoveModal())

    @discord.ui.button(label="View watchlist", style=discord.ButtonStyle.primary, emoji="📋", custom_id="ks_watch_list")
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = await database.all_watch(interaction.guild.id)
        if not rows:
            await interaction.response.send_message("The watchlist is empty. Add an enemy with **Watch a player**.", ephemeral=True)
            return
        lines = []
        for r in rows:
            coords = f"({r['last_x']}, {r['last_y']})" if r.get("last_x") is not None else "—"
            lines.append(f"• **{r.get('label') or r['ingame_id']}** (`{r['ingame_id']}`) — "
                         f"{power_fmt(r.get('last_power'))} · {coords}")
        embed = discord.Embed(title="🕵️ Watchlist", description="\n".join(lines)[:4000], color=discord.Color.dark_red())
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ──────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────
class Watch(commands.Cog, name="Watch"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.watch_loop.start()

    def cog_unload(self):
        if self.watch_loop.is_running():
            self.watch_loop.cancel()

    async def ensure_panel(self, guild: discord.Guild, force: bool = False):
        config = await database.get_config(guild.id) or {}
        cid = config.get("warintel_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if not channel:
            return
        msg_id = config.get("warintel_panel_message_id")
        if msg_id:
            try:
                existing = await channel.fetch_message(int(msg_id))
                if not force:
                    return
                await existing.delete()
            except (discord.NotFound, discord.HTTPException):
                pass
        embed = discord.Embed(
            title="🕵️ Enemy Watch",
            color=discord.Color.dark_red(),
            description=(
                "Track enemy players and get alerts here when they make a big power move or "
                "relocate on the map.\n\n"
                "**Watch a player** — add an enemy by their in-game ID.\n"
                "**Stop watching** — remove one.\n"
                "**View watchlist** — see everyone you're tracking."
            ),
        )
        try:
            message = await channel.send(embed=embed, view=WarIntelPanelView())
        except discord.HTTPException:
            return
        await database.upsert_config(guild.id, warintel_panel_message_id=message.id)

    # ── background checks ──────────────────────────────────────
    @tasks.loop(minutes=WATCH_MINUTES)
    async def watch_loop(self):
        for guild_id in await database.list_guild_ids():
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            config = await database.get_config(guild_id) or {}
            cid = config.get("warintel_channel_id")
            channel = guild.get_channel(int(cid)) if cid else None
            try:
                for row in await database.all_watch(guild_id):
                    await self._check_one(guild, channel, row)
                    await asyncio.sleep(WATCH_DELAY)
            except Exception as exc:  # noqa: BLE001
                logging.warning("Watch loop failed for guild %s: %s", guild_id, exc)

    @watch_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _check_one(self, guild, channel, row):
        iid = row["ingame_id"]
        try:
            info = await kingshot_api.get_player_info(int(iid))
            data = info.get("data") or {}
        except Exception:
            return
        if not data:
            return
        new_power = _to_int(data.get("power"))
        new_x, new_y = _to_int(data.get("x")), _to_int(data.get("y"))
        old_power, old_x, old_y = row.get("last_power"), row.get("last_x"), row.get("last_y")
        name = row.get("label") or data.get("name") or f"ID {iid}"

        alerts = []
        if old_power and new_power and old_power > 0:
            pct = (new_power - old_power) / old_power
            if abs(pct) >= POWER_ALERT_PCT:
                arrow = "📈" if pct > 0 else "📉"
                alerts.append(f"{arrow} Power {power_fmt(old_power)} → **{power_fmt(new_power)}** ({pct * 100:+.0f}%)")
        if old_x is not None and new_x is not None and (new_x != old_x or new_y != old_y):
            alerts.append(f"🗺️ Moved to **({new_x}, {new_y})**")

        await database.update_watch(guild.id, int(iid), new_power, new_x, new_y)

        if alerts and channel:
            embed = discord.Embed(
                title=f"🕵️ {name}",
                description="\n".join(alerts),
                color=discord.Color.dark_red(),
            )
            embed.set_footer(text=f"ID {iid} · watchlist alert")
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Watch(bot))
