import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import logging
import os
import re
from datetime import datetime, timezone
import asyncio

from database import (
    init_db,
    save_player,
    get_player,
    delete_player,
    save_server_config,
    get_server_config,
)

# ─────────────────────────────────────────────
# ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    handlers=[logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")],
)

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)

# ─────────────────────────────────────────────
# BAD WORDS
# ─────────────────────────────────────────────
with open("badwords.txt", "r") as f:
    bad_words = [line.strip() for line in f if line.strip()]

# ─────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────
async def get_player_info(ingame_id: int):
    """Fetch player information from Kingshot API."""
    if not hasattr(bot, "session") or bot.session.closed:
        bot.session = aiohttp.ClientSession()
    url = f"https://kingshot.net/api/player-info?playerId={ingame_id}"
    async with bot.session.get(url) as resp:
        if resp.status != 200:
            raise ValueError("Kingshot API error")
        return await resp.json()


async def get_kingdom_stats(kingdom_id: int):
    """Fetch kingdom stats from Kingshot API."""
    if not hasattr(bot, "session") or bot.session.closed:
        bot.session = aiohttp.ClientSession()
    url = f"https://kingshot.net/api/kingdom-tracker?kingdomId={kingdom_id}&recent=1&limit=20&sort=openTime-desc"
    async with bot.session.get(url) as resp:
        if resp.status != 200:
            raise ValueError("Kingshot API error")
        data = await resp.json()
        if "data" not in data or "servers" not in data["data"] or len(data["data"]["servers"]) == 0:
            raise ValueError("Invalid response from Kingshot API")
        return data


async def close_session():
    if hasattr(bot, "session") and not bot.session.closed:
        await bot.session.close()


# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    bot.session = aiohttp.ClientSession()
    await bot.tree.sync()
    logging.info(f"{bot.user} is ready!")
    auto_sync.start()  # start background sync task


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    cleaned = message.content
    for word in bad_words:
        cleaned = re.sub(
            re.escape(word),
            lambda m: "*" * len(m.group()),
            cleaned,
            flags=re.IGNORECASE,
        )

    if cleaned != message.content:
        await message.delete()
        await message.channel.send(f"{message.author.display_name}: {cleaned}")
        return

    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
    config = get_server_config(str(member.guild.id))
    if not config:
        logging.warning(f"Server config not found for guild {member.guild.id}")
        return

    start_role_name = config[1]
    verify_channel_name = config[4]

    start_role = discord.utils.get(member.guild.roles, name=start_role_name)
    if start_role:
        try:
            await member.add_roles(start_role)
        except Exception as e:
            logging.error(f"Error assigning start role: {e}")

    verify_channel = discord.utils.get(member.guild.text_channels, name=verify_channel_name)
    if verify_channel:
        await verify_channel.send(
            f"Welcome {member.mention}! Please verify your Kingshot account using:\n"
            f"```/verify <player_id> <alliance> <kingdom>```\n"
            f"Example:\n```/verify 12345678 ABC 1```"
        )


# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────
@bot.tree.command(name="setup", description="Initial setup for the bot (Admin only)")
@app_commands.default_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction,
    start_role_name: str,
    verified_role_name: str,
    owner_role_name: str,
    verify_channel_name: str,
    general_channel_name: str,
    allowed_kingdoms: str,
):
    """Setup server configuration."""
    allowed = [k.strip() for k in allowed_kingdoms.split(",")]
    save_server_config(
        str(interaction.guild.id),
        start_role_name,
        verified_role_name,
        owner_role_name,
        verify_channel_name,
        general_channel_name,
        allowed,
    )
    await interaction.response.send_message(
        "Server configuration saved successfully!", ephemeral=True
    )


@bot.tree.command(name="verify", description="Verify your Kingshot account")
async def verify(
    interaction: discord.Interaction, ingame_id: int, alliance: str, kingdom: int
):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message(
            "Server not configured. Ask admin to run /setup.", ephemeral=True
        )
        return

    verify_channel_name = config[4]
    start_role_name = config[1]
    verified_role_name = config[2]
    allowed_kingdoms = config[6]

    if kingdom not in allowed_kingdoms:
        await interaction.response.send_message(
            f"Kingdom {kingdom} is not allowed to verify here.", ephemeral=True
        )
        return

    if interaction.channel.name != verify_channel_name:
        await interaction.response.send_message(
            f"Please use this command in #{verify_channel_name}", ephemeral=True
        )
        return

    discord_id = str(interaction.user.id)
    existing = get_player(discord_id)

    verified_role = discord.utils.get(interaction.guild.roles, name=verified_role_name)
    start_role = discord.utils.get(interaction.guild.roles, name=start_role_name)

    try:
        player_info = await get_player_info(ingame_id)
        data = player_info["data"]
        ingame_name = data["name"]
        player_kingdom = data["kingdom"]
    except Exception:
        await interaction.response.send_message(
            "Could not fetch Kingshot data. Try again later.", ephemeral=True
        )
        return

    nick = f"[{player_kingdom}] {alliance}- {ingame_name}"[:32]

    if existing:
        if verified_role:
            await interaction.user.add_roles(verified_role)
        if start_role:
            await interaction.user.remove_roles(start_role)
        await interaction.user.edit(nick=nick)
        await interaction.response.send_message(
            "You are already verified. Updated info.", ephemeral=True
        )
        return

    save_player(discord_id, ingame_name, ingame_id, player_kingdom, alliance)
    if verified_role:
        await interaction.user.add_roles(verified_role)
    if start_role:
        await interaction.user.remove_roles(start_role)
    await interaction.user.edit(nick=nick)
    await interaction.response.send_message(
        f"Verified! Welcome [{player_kingdom}] {alliance}- {ingame_name}, {interaction.user.mention}!"
    )


@bot.tree.command(name="unverify", description="Unverify a player (Admin only)")
@app_commands.default_permissions(administrator=True)
async def unverify(interaction: discord.Interaction, member: discord.Member):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message(
            "Server not configured.", ephemeral=True
        )
        return

    start_role_name = config[1]
    verified_role_name = config[2]
    discord_id = str(member.id)
    existing = get_player(discord_id)

    if not existing:
        await interaction.response.send_message(
            f"{member.mention} is not verified.", ephemeral=True
        )
        return

    delete_player(discord_id)
    verified_role = discord.utils.get(interaction.guild.roles, name=verified_role_name)
    start_role = discord.utils.get(interaction.guild.roles, name=start_role_name)

    if verified_role and verified_role in member.roles:
        await member.remove_roles(verified_role)
    if start_role:
        await member.add_roles(start_role)

    await interaction.response.send_message(
        f"{member.mention} has been unverified.", ephemeral=True
    )


@bot.tree.command(name="mystats", description="Get your in-game stats")
async def mystats(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    player = get_player(discord_id)
    if not player:
        await interaction.response.send_message(
            "You are not verified. Use /verify.", ephemeral=True
        )
        return

    try:
        player_info = await get_player_info(player[2])
        data = player_info["data"]
    except Exception:
        await interaction.response.send_message(
            "Could not fetch Kingshot data.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"{data.get('name', 'Unknown')}'s Stats", color=discord.Color.blue()
    )
    embed.add_field(name="Player ID", value=data.get("playerId", "Unknown"), inline=False)
    embed.add_field(name="Kingdom", value=data.get("kingdom", "Unknown"), inline=True)
    embed.add_field(name="Alliance", value=player[4], inline=True)
    embed.add_field(name="Town Center Level", value=data.get("levelRendered", "Unknown"), inline=False)
    embed.set_thumbnail(url=data.get("profilePhoto", ""))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="age", description="Get kingdom age in days")
async def age(interaction: discord.Interaction, kingdom_id: int):
    try:
        kingdom_data = await get_kingdom_stats(kingdom_id)
        open_time_str = kingdom_data["data"]["servers"][0]["openTime"]
        open_time = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days = (now - open_time).days
        embed = discord.Embed(
            title=f"Kingdom {kingdom_id} Age",
            description=f"{days} days since opening",
            color=discord.Color.green(),
        )
        embed.add_field(name="Open Date", value=open_time.strftime("%Y-%m-%d"), inline=True)
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message(
            f"Kingdom {kingdom_id} not found.", ephemeral=True
        )


@bot.tree.command(name="hello", description="Say hello to the bot")
async def hello(interaction: discord.Interaction):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured.", ephemeral=True)
        return
    general_channel = config[5]
    if interaction.channel.name != general_channel:
        await interaction.response.send_message(
            f"Please use #{general_channel} channel.", ephemeral=True
        )
        return
    await interaction.response.send_message(f"Hello {interaction.user.mention}!")


# ─────────────────────────────────────────────
# AUTOSYNC BACKGROUND TASK
# ─────────────────────────────────────────────
@tasks.loop(hours=3)
async def auto_sync():
    for guild in bot.guilds:
        config = get_server_config(str(guild.id))
        if not config:
            continue
        verified_role_name = config[2]
        verified_role = discord.utils.get(guild.roles, name=verified_role_name)
        if not verified_role:
            continue
        for member in verified_role.members:
            try:
                player = get_player(str(member.id))
                if not player:
                    continue
                ingame_id = player[2]
                alliance = player[4]
                player_info = await get_player_info(ingame_id)
                data = player_info["data"]
                ingame_name = data["name"]
                kingdom = data["kingdom"]
                nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
                if member.nick != nick:
                    await member.edit(nick=nick)
            except Exception as e:
                logging.error(f"Auto-sync error guild {guild.id}, member {member.id}: {e}")


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
async def main():
    async with bot:
        await bot.start(TOKEN)
        await close_session()

if __name__ == "__main__":
    asyncio.run(main())