import os
import re
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timezone
import asyncio
import logging

from database import init_db, save_player, get_player, delete_player, save_server_config, get_server_config

# ─────────────────────────────────────────────
# ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, filename="discord.log", filemode="w",
                    format="%(asctime)s - %(levelname)s - %(message)s")

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

bot.session = None
async def close_session():
    if bot.session and not bot.session.closed:
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
async def get_player_info(bot, ingame_id):
    if not bot.session or bot.session.closed:
        bot.session = aiohttp.ClientSession()
    url = f"https://kingshot.net/api/player-info?playerId={ingame_id}"
    async with bot.session.get(url) as response:
        if response.status != 200:
            raise ValueError("Kingshot API error")
        return await response.json()

async def get_kingdom_stats(bot, kingdom_id):
    if not bot.session or bot.session.closed:
        bot.session = aiohttp.ClientSession()
    url = f"https://kingshot.net/api/kingdom-tracker?kingdomId={kingdom_id}&recent=1&limit=20&sort=openTime-desc"
    async with bot.session.get(url) as response:
        if response.status != 200:
            raise ValueError("Kingshot API error")
        data = await response.json()
        if "data" not in data or "servers" not in data["data"] or not data["data"]["servers"]:
            raise ValueError("Invalid response")
        return data

# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    bot.session = aiohttp.ClientSession()
    await bot.tree.sync()
    logging.info(f"Bot ready! Logged in as {bot.user}")

@bot.event
async def on_member_join(member):
    config = get_server_config(str(member.guild.id))
    if not config:
        logging.warning(f"No config for guild {member.guild.id}")
        return

    start_role, verify_channel_name = config[1], config[4]
    role = discord.utils.get(member.guild.roles, name=start_role)
    if role:
        await member.add_roles(role)
        logging.info(f"Assigned {start_role} to {member.name}")

    verify_channel = discord.utils.get(member.guild.text_channels, name=verify_channel_name)
    if verify_channel:
        await verify_channel.send(
            f"Welcome {member.mention}!\n"
            f"Verify with `/verify <player_id> <alliance>`.\n"
            f"Example: `/verify 12345678 ABC`"
        )

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    cleaned = message.content
    for word in bad_words:
        cleaned = re.sub(re.escape(word), lambda m: "*"*len(m.group()), cleaned, flags=re.IGNORECASE)
    if cleaned != message.content:
        await message.delete()
        await message.channel.send(f"{message.author.display_name}: {cleaned}")
        return
    await bot.process_commands(message)

# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────
@bot.tree.command(name="setup", description="Initial server setup")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction, start_role_name: str, verified_role_name: str,
                owner_role_name: str, verify_channel_name: str, general_channel_name: str, allowed_kingdoms: str):
    """Save server configuration"""
    kingdoms_list = [k.strip() for k in allowed_kingdoms.split(",") if k.strip()]
    save_server_config(str(interaction.guild.id),
                       start_role_name,
                       verified_role_name,
                       owner_role_name,
                       verify_channel_name,
                       general_channel_name,
                       kingdoms_list)
    await interaction.response.send_message("✅ Configuration saved!", ephemeral=True)

@bot.tree.command(name="verify", description="Verify your in-game account")
async def verify(interaction: discord.Interaction, ingame_id: int, alliance: str):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured.", ephemeral=True)
        return

    verify_channel_name, start_role, verified_role, allowed_kingdoms = config[4], config[1], config[2], config[6]

    if interaction.channel.name != verify_channel_name:
        await interaction.response.send_message(f"Use this command in #{verify_channel_name}.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    verified = discord.utils.get(interaction.guild.roles, name=verified_role)
    start = discord.utils.get(interaction.guild.roles, name=start_role)

    try:
        player_info = await asyncio.wait_for(get_player_info(bot, ingame_id), timeout=3)
        data = player_info["data"]
        ingame_name, kingdom = data["name"], data["kingdom"]
    except asyncio.TimeoutError:
        await interaction.response.send_message("Kingshot API is slow. Try again.", ephemeral=True)
        return
    except Exception:
        await interaction.response.send_message("Could not reach Kingshot API.", ephemeral=True)
        return

    if allowed_kingdoms and str(kingdom) not in allowed_kingdoms:
        await interaction.response.send_message(f"Your kingdom {kingdom} cannot verify here.", ephemeral=True)
        return

    existing = get_player(discord_id)
    if existing:
        if verified: await interaction.user.add_roles(verified)
        if start: await interaction.user.remove_roles(start)
        await interaction.user.edit(nick=f"[{kingdom}] {alliance}- {ingame_name}"[:32])
        await interaction.response.send_message("✅ Already verified! Info updated.", ephemeral=True)
        return

    # Save and assign
    save_player(discord_id, ingame_name, ingame_id, kingdom, alliance)
    if verified: await interaction.user.add_roles(verified)
    if start: await interaction.user.remove_roles(start)
    await interaction.user.edit(nick=f"[{kingdom}] {alliance}- {ingame_name}"[:32])

    await interaction.response.send_message(f"✅ Verified! Welcome [{kingdom}] {alliance} - {ingame_name}!")

@bot.tree.command(name="unverify", description="Unverify a player")
@app_commands.default_permissions(administrator=True)
async def unverify(interaction: discord.Interaction, member: discord.Member):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured.", ephemeral=True)
        return

    verified_role, start_role = config[2], config[1]
    discord_id = str(member.id)
    player = get_player(discord_id)
    if not player:
        await interaction.response.send_message(f"{member.mention} is not verified.", ephemeral=True)
        return

    delete_player(discord_id)
    settler = discord.utils.get(interaction.guild.roles, name=verified_role)
    commoner = discord.utils.get(interaction.guild.roles, name=start_role)
    if settler and settler in member.roles: await member.remove_roles(settler)
    if commoner: await member.add_roles(commoner)
    await interaction.response.send_message(f"{member.mention} has been unverified.", ephemeral=True)

@bot.tree.command(name="mystats", description="Get your in-game stats")
async def mystats(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    player = get_player(discord_id)
    if not player:
        await interaction.response.send_message("Not verified yet.", ephemeral=True)
        return

    player_info = await get_player_info(bot, player[2])
    data = player_info.get("data")
    if not data:
        await interaction.response.send_message("Could not fetch stats.", ephemeral=True)
        return

    embed = discord.Embed(title=f"{data.get('name', 'Unknown')}'s Stats", color=discord.Color.blue())
    embed.add_field(name="Player ID", value=data.get("playerId", "Unknown"), inline=False)
    embed.add_field(name="Kingdom", value=data.get("kingdom", "Unknown"), inline=True)
    embed.add_field(name="Alliance", value=player[4], inline=True)
    embed.add_field(name="Town Center Level", value=data.get("levelRendered", "Unknown"), inline=False)
    embed.set_thumbnail(url=data.get("profilePhoto", ""))

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="age", description="Kingdom age tracker")
async def age(interaction: discord.Interaction, kingdom_id: int):
    try:
        kingdom_data = await get_kingdom_stats(bot, kingdom_id)
        open_time = datetime.fromisoformat(kingdom_data["data"]["servers"][0]["openTime"].replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - open_time).days
        embed = discord.Embed(title=f"Kingdom {kingdom_id} Age", description=f"{days} days!", color=discord.Color.green())
        embed.add_field(name="Open Date", value=open_time.strftime("%Y-%m-%d"), inline=True)
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message(f"Kingdom {kingdom_id} not found.", ephemeral=True)

# ─────────────────────────────────────────────
# AUTO-SYNC
# ─────────────────────────────────────────────
async def auto_sync():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                config = get_server_config(str(guild.id))
                if not config: continue
                verified_role_name, allowed_kingdoms = config[2], config[6]
                verified_role = discord.utils.get(guild.roles, name=verified_role_name)
                if not verified_role: continue

                for member in verified_role.members:
                    discord_id = str(member.id)
                    player = get_player(discord_id)
                    if not player: continue
                    try:
                        player_info = await asyncio.wait_for(get_player_info(bot, player[2]), timeout=3)
                        data = player_info["data"]
                        ingame_name, kingdom, alliance = data["name"], data["kingdom"], player[4]
                        if allowed_kingdoms and str(kingdom) not in allowed_kingdoms: continue
                        nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
                        if member.nick != nick: await member.edit(nick=nick)
                    except Exception as e:
                        logging.error(f"Auto-sync error guild {guild.id} member {member.id}: {e}")
        except Exception as e:
            logging.error(f"Auto-sync outer loop error: {e}")
        await asyncio.sleep(3*60*60)

# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
async def main():
    bot.loop.create_task(auto_sync())
    await bot.start(TOKEN)

asyncio.run(main())