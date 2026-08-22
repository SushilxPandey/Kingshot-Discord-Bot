"""
Insights: daily power snapshots → live leaderboard + inactivity watch.

A background loop snapshots every verified member's power / kills / last-active from
the game data, stores it, and refreshes two auto-updating embeds:

  * a **📈 leaderboard** channel — top players by power, plus who gained the most this
    week (from the stored history), and
  * an **inactivity report** in the staff member-list channel — members who haven't
    logged in for ``INACTIVE_DAYS`` days, so leaders can nudge them before KvK.

Everything is built on data we already fetch; no extra services.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database
import kingshot_api
from cogs.scout import power_fmt

SNAPSHOT_HOURS = 12          # how often to snapshot members
SNAPSHOT_DELAY = 1.2         # pace between per-member lookups (be polite to the site)
INACTIVE_DAYS = 5            # flag members not seen in this many days
TOP_N = 10
GAIN_WINDOW_DAYS = 8         # look back this far for "weekly" gains


def _parse_last_active(data: dict):
    """Return an aware UTC datetime for a player's last activity, or None."""
    for key in ("last_active_at", "last_login", "last_seen_at"):
        val = data.get(key)
        if val in (None, "", 0):
            continue
        try:
            secs = float(val)
            if secs > 1_000_000_000:
                return datetime.fromtimestamp(secs, timezone.utc)
        except (TypeError, ValueError):
            pass
        s = str(val)
        if "T" in s:
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def _ago(dt: datetime) -> str:
    if dt is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (now - dt).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


class Insights(commands.Cog, name="Insights"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.snapshot_loop.start()

    def cog_unload(self):
        if self.snapshot_loop.is_running():
            self.snapshot_loop.cancel()

    # ── background snapshot ────────────────────────────────────
    @tasks.loop(hours=SNAPSHOT_HOURS)
    async def snapshot_loop(self):
        for guild_id in await database.list_guild_ids():
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            try:
                await self._snapshot_guild(guild)
                await self.refresh_leaderboard(guild)
                await self.refresh_inactivity(guild)
            except Exception as exc:  # noqa: BLE001 - never let one guild kill the loop
                logging.warning("Insights snapshot failed for guild %s: %s", guild_id, exc)

    @snapshot_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _snapshot_guild(self, guild: discord.Guild):
        players = await database.all_players(guild.id)
        for p in players:
            iid = p.get("ingame_id")
            if not iid:
                continue
            try:
                info = await kingshot_api.get_player_info(int(iid))
                data = info.get("data") or {}
            except Exception:
                continue  # site hiccup — skip this member this round
            if not data:
                continue
            power = _to_int(data.get("power"))
            kills = _to_int(data.get("kills"))
            last_active = _parse_last_active(data)
            await database.record_snapshot(guild.id, p["discord_id"], power, kills, last_active)
            await asyncio.sleep(SNAPSHOT_DELAY)

    # ── leaderboard ────────────────────────────────────────────
    async def _weekly_gains(self, guild_id, current: dict) -> list[tuple[str, int]]:
        """Return [(discord_id, gain)] sorted desc, using earliest snapshot in the window."""
        since = date.today() - timedelta(days=GAIN_WINDOW_DAYS)
        rows = await database.power_history_since(guild_id, since)
        earliest: dict[str, int] = {}
        for r in rows:  # rows are oldest-first, so first seen per member is the earliest
            did = r["discord_id"]
            if did not in earliest and r["power"] is not None:
                earliest[did] = r["power"]
        gains = []
        for did, base in earliest.items():
            cur = current.get(did)
            if cur is not None and cur > base:
                gains.append((did, cur - base))
        gains.sort(key=lambda x: x[1], reverse=True)
        return gains

    async def refresh_leaderboard(self, guild: discord.Guild):
        config = await database.get_config(guild.id) or {}
        cid = config.get("leaderboard_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if not channel:
            return
        players = await database.players_with_power(guild.id)
        current = {p["discord_id"]: p["power"] for p in players}

        medals = ["🥇", "🥈", "🥉"]

        def label(did):
            m = guild.get_member(int(did))
            return m.display_name if m else "a member"

        top_lines = []
        for i, p in enumerate(players[:TOP_N]):
            rank = medals[i] if i < 3 else f"`{i + 1:>2}.`"
            tag = f" [{p['alliance']}]" if p.get("alliance") else ""
            top_lines.append(f"{rank} **{p['ingame_name'] or label(p['discord_id'])}**{tag} — {power_fmt(p['power'])}")

        gains = await self._weekly_gains(guild.id, current)
        gain_lines = []
        pmap = {p["discord_id"]: p for p in players}
        for i, (did, gain) in enumerate(gains[:TOP_N]):
            p = pmap.get(did, {})
            gain_lines.append(f"{medals[i] if i < 3 else f'`{i + 1:>2}.`'} "
                              f"**{p.get('ingame_name') or label(did)}** — +{power_fmt(gain)}")

        embed = discord.Embed(title="🏆 Power Leaderboard", color=discord.Color.gold())
        embed.add_field(name="⚡ Top power", value="\n".join(top_lines) or "No data yet.", inline=False)
        embed.add_field(name="📈 Biggest gains this week",
                        value="\n".join(gain_lines) or "Gains show up once there's a week of history.",
                        inline=False)
        embed.set_footer(text="Auto-updates from live game data")
        await self._post_or_edit(guild, channel, "leaderboard_message_id", embed)

    async def refresh_inactivity(self, guild: discord.Guild):
        config = await database.get_config(guild.id) or {}
        cid = config.get("member_list_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if not channel:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
        inactive = await database.inactive_players(guild.id, cutoff)
        lines = []
        for p in inactive[:40]:
            m = guild.get_member(int(p["discord_id"]))
            who = m.mention if m else (p.get("ingame_name") or f"ID {p.get('ingame_id')}")
            lines.append(f"⚠️ {who} — last seen {_ago(p.get('last_active'))}")
        embed = discord.Embed(
            title=f"😴 Inactive members ({INACTIVE_DAYS}+ days)",
            color=discord.Color.orange(),
            description="\n".join(lines) if lines else "Everyone's been active recently. 🎉",
        )
        embed.set_footer(text="Auto-updates · based on in-game last-login")
        await self._post_or_edit(guild, channel, "inactivity_message_id", embed)

    async def _post_or_edit(self, guild, channel, message_key, embed):
        config = await database.get_config(guild.id) or {}
        msg = None
        msg_id = config.get(message_key)
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
            except (discord.NotFound, discord.HTTPException):
                msg = None
        if msg:
            try:
                await msg.edit(embed=embed)
                return
            except discord.HTTPException:
                pass
        try:
            new = await channel.send(embed=embed)
            await database.upsert_config(guild.id, **{message_key: new.id})
        except discord.HTTPException:
            pass

    async def ensure_boards(self, guild: discord.Guild):
        """Post initial leaderboard + inactivity embeds (called by setup)."""
        await self.refresh_leaderboard(guild)
        await self.refresh_inactivity(guild)

    # ── admin: force a refresh now ─────────────────────────────
    @app_commands.command(name="refreshstats", description="Snapshot members now and refresh the boards. Admins only.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def refreshstats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._snapshot_guild(interaction.guild)
        await self.refresh_leaderboard(interaction.guild)
        await self.refresh_inactivity(interaction.guild)
        await interaction.followup.send("✅ Snapshotted members and refreshed the leaderboard & inactivity report.", ephemeral=True)


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def setup(bot: commands.Bot):
    await bot.add_cog(Insights(bot))
