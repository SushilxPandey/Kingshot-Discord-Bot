import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import logging
import os
import re
from datetime import datetime, timezone
import asyncio

from database import (
    init_db, save_player, get_player, delete_player,
    save_server_config, get_server_config
)

# ─────────────────────────────────────────────
# ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────
load_dotenv()
token = os.getenv("DISCORD_TOKEN")

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
logging.basicConfig(level=logging.INFO, handlers=[handler])

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)

async def close_session():
    if hasattr(bot, "session") and not bot.session.closed:
        await bot.session.close()
bot.close_session = close_session

# ─────────────────────────────────────────────
# BAD WORDS
# ─────────────────────────────────────────────
with open("badwords.txt", "r") as f:
    bad_words = [line.strip() for line in f if line.strip()]

# ─────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────
async def ensure_session():
    if not hasattr(bot, "session") or bot.session.closed:
        bot.session = aiohttp.ClientSession()
    return bot.session

async def get_player_info(ingame_id):
    session = await ensure_session()
    url = f"https://kingshot.net/api/player-info?playerId={ingame_id}"
    async with session.get(url) as response:
        if response.status != 200:
            raise ValueError("Kingshot API error")
        return await response.json()

async def get_kingdom_stats(kingdom_id):
    session = await ensure_session()
    url = f"https://kingshot.net/api/kingdom-tracker?kingdomId={kingdom_id}&recent=1&limit=20&sort=openTime-desc"
    async with session.get(url) as response:
        if response.status != 200:
            raise ValueError("Kingshot API error")
        data = await response.json()
        if "data" not in data or "servers" not in data["data"] or len(data["data"]["servers"]) == 0:
            raise ValueError("Invalid response from Kingshot API")
        return data

# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    await ensure_session()
    await bot.tree.sync()
    logging.info(f"Bot ready: {bot.user.name}")

@bot.event
async def on_member_join(member):
    config = get_server_config(str(member.guild.id))
    if not config:
        logging.warning(f"Server {member.guild.id} missing configuration.")
        return

    start_role = config[1]
    verify_channel_name = config[4]

    role = discord.utils.get(member.guild.roles, name=start_role)
    if role:
        try:
            await member.add_roles(role)
            logging.info(f"Assigned {start_role} role to {member.name}")
        except Exception as e:
            logging.error(f"Failed to add role {start_role}: {e}")

    verify_channel = discord.utils.get(member.guild.text_channels, name=verify_channel_name)
    if verify_channel:
        await verify_channel.send(
            f"Welcome {member.mention}!\n"
            f"Verify your account using:\n"
            f"```/verify <player_id> <alliance>```\n"
            f"Example: ```/verify 12345678 ABC```"
        )

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    cleaned = message.content
    for word in bad_words:
        cleaned = re.sub(re.escape(word), lambda m: "*" * len(m.group()), cleaned, flags=re.IGNORECASE)

    if cleaned != message.content:
        await message.delete()
        await message.channel.send(f"{message.author.display_name}: {cleaned}")
        return

    await bot.process_commands(message)

# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────
@bot.tree.command(name="setup", description="Initial setup for the bot. Admin only.")
@app_commands.default_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction,
    start_role_name: str,
    verified_role_name: str,
    owner_role_name: str,
    verify_channel_name: str,
    general_channel_name: str,
    allowed_kingdoms: str  # comma-separated
):
    save_server_config(
        str(interaction.guild.id),
        start_role_name,
        verified_role_name,
        owner_role_name,
        verify_channel_name,
        general_channel_name,
        allowed_kingdoms
    )
    await interaction.response.send_message(
        "Server configuration saved with allowed kingdoms!", ephemeral=True
    )

@bot.tree.command(name="hello", description="Greet the bot in the general channel")
async def hello(interaction: discord.Interaction):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured yet.", ephemeral=True)
        return

    general_channel = config[5]
    if interaction.channel.name != general_channel:
        await interaction.response.send_message(f"Use this command in #{general_channel} only.", ephemeral=True)
        return

    await interaction.response.send_message(f"Hello, {interaction.user.mention}! Hope you're having a great day.")

@bot.tree.command(name="verify", description="Verify your in-game account")
async def verify(interaction: discord.Interaction, ingame_id: int, alliance: str):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured. Ask admin to run /setup.", ephemeral=True)
        return

    verify_channel_name = config[4]
    start_role = config[1]
    verified_role = config[2]
    allowed_kingdoms = [k.strip() for k in config[6].split(",")]

    if interaction.channel.name != verify_channel_name:
        await interaction.response.send_message(f"Use this command in #{verify_channel_name} only.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    existing = get_player(discord_id)

    try:
        player_info = await get_player_info(ingame_id)
        data = player_info["data"]
        ingame_name = data["name"]
        kingdom = data["kingdom"]
    except Exception:
        await interaction.response.send_message("Could not reach Kingshot API. Try again later.", ephemeral=True)
        return

    if kingdom not in allowed_kingdoms:
        await interaction.response.send_message(
            f"Your kingdom `{kingdom}` is not allowed. Allowed: {', '.join(allowed_kingdoms)}",
            ephemeral=True
        )
        return

    verified_role_obj = discord.utils.get(interaction.guild.roles, name=verified_role)
    start_role_obj = discord.utils.get(interaction.guild.roles, name=start_role)

    if existing:
        if verified_role_obj:
            await interaction.user.add_roles(verified_role_obj)
        if start_role_obj:
            await interaction.user.remove_roles(start_role_obj)
        nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
        await interaction.user.edit(nick=nick)
        await interaction.response.send_message("You are already verified! Info updated.", ephemeral=True)
        return

    save_player(discord_id, ingame_name, ingame_id, kingdom, alliance)

    if verified_role_obj:
        await interaction.user.add_roles(verified_role_obj)
    if start_role_obj:
        await interaction.user.remove_roles(start_role_obj)

    nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
    await interaction.user.edit(nick=nick)
    await interaction.response.send_message(f"Verified! Welcome [{kingdom}] {alliance} - {ingame_name}")

@bot.tree.command(name="unverify", description="Unverify a player and revert their role")
@app_commands.default_permissions(administrator=True)
async def unverify(interaction: discord.Interaction, member: discord.Member):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured.", ephemeral=True)
        return

    verified_role = config[2]
    start_role = config[1]
    discord_id = str(member.id)
    existing = get_player(discord_id)

    if not existing:
        await interaction.response.send_message(f"{member.mention} is not verified.", ephemeral=True)
        return

    delete_player(discord_id)

    settler = discord.utils.get(interaction.guild.roles, name=verified_role)
    commoner = discord.utils.get(interaction.guild.roles, name=start_role)

    if settler and settler in member.roles:
        await member.remove_roles(settler)
    if commoner:
        await member.add_roles(commoner)

    await interaction.response.send_message(f"{member.mention} has been unverified and reverted to {start_role}.", ephemeral=True)

@bot.tree.command(name="mystats", description="Get your in-game stats from Kingshot API")
async def mystats(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    player = get_player(discord_id)
    if not player:
        await interaction.response.send_message("You are not verified. Use /verify first.", ephemeral=True)
        return

    player_info = await get_player_info(player[2])
    if not player_info or "data" not in player_info:
        await interaction.response.send_message("Could not retrieve stats from Kingshot API.", ephemeral=True)
        return

    data = player_info["data"]
    embed = discord.Embed(title=f"{data.get('name','Unknown')}'s Stats", color=discord.Color.blue())
    embed.add_field(name="Player ID", value=data.get("playerId","Unknown"), inline=False)
    embed.add_field(name="Kingdom", value=data.get("kingdom","Unknown"), inline=True)
    embed.add_field(name="Alliance", value=player[4], inline=True)
    embed.add_field(name="Town Center Level", value=data.get("levelRendered","Unknown"), inline=False)
    embed.set_thumbnail(url=data.get("profilePhoto",""))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="age", description="Get kingdom age")
async def age(interaction: discord.Interaction, kingdom_id: int):
    try:
        kingdom_data = await get_kingdom_stats(kingdom_id)
        open_time_str = kingdom_data["data"]["servers"][0]["openTime"]
        open_time = datetime.fromisoformat(open_time_str.replace("Z","+00:00"))
        now = datetime.now(timezone.utc)
        days = (now - open_time).days
        embed = discord.Embed(title=f"Kingdom {kingdom_id} Age",
                              description=f"{days} days open!",
                              color=discord.Color.green())
        embed.add_field(name="Open Date", value=open_time.strftime("%Y-%m-%d"), inline=True)
        await interaction.response.send_message(embed=embed)
    except ValueError:
        await interaction.response.send_message(f"Kingdom {kingdom_id} not found.", ephemeral=True)

# ─────────────────────────────────────────────
# AUTOSYNC
# ─────────────────────────────────────────────
async def auto_sync():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
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
                        discord_id = str(member.id)
                        player = get_player(discord_id)
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
                        logging.error(f"Auto-sync guild {guild.id}, member {member.id}: {e}")
        except Exception as e:
            logging.error(f"Auto-sync outer loop error: {e}")
        await asyncio.sleep(3*60*60)

bot.loop.create_task(auto_sync())

# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
try:
    bot.run(token, log_handler=handler, log_level=logging.DEBUG)
finally:
    asyncio.run(bot.close_session())