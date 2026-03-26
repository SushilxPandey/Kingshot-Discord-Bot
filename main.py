import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import logging
import os
import re
from datetime import datetime, timezone

from database import init_db, save_player, get_player, delete_player, save_server_config, get_server_config

# ─────────────────────────────────────────────
# ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────
load_dotenv()
token = os.getenv('DISCORD_TOKEN')
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='/', intents=intents)

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
async def get_player_info(ingame_id):
    url = f"https://kingshot.net/api/player-info?playerId={ingame_id}"
    async with bot.session.get(url) as response:
        if response.status != 200:
            raise ValueError("Kingshot API error")
        return await response.json()

async def get_kingdom_stats(kingdom_id):
    url = f"https://kingshot.net/api/kingdom-tracker?kingdomId={kingdom_id}&recent=1&limit=20&sort=openTime-desc"
    async with bot.session.get(url) as response:
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
    bot.session = aiohttp.ClientSession()
    await bot.tree.sync()
    logging.info(f"We are ready to go! {bot.user.name}")

@bot.event
async def on_member_join(member):
    config = get_server_config(str(member.guild.id))
    if not config:
        logging.warning(f"Server configuration not found for guild {member.guild.id}. Run /setup.")
        return

    start_role = config[1]
    verify_channel_name = config[4]

    role = discord.utils.get(member.guild.roles, name=start_role)
    if role:
        try:
            await member.add_roles(role)
        except Exception as e:
            logging.error(f"Error assigning role: {e}")

    verify_channel = discord.utils.get(member.guild.text_channels, name=verify_channel_name)
    if verify_channel:
        await verify_channel.send(
            f"Welcome {member.mention}!\n"
            f"Verify your Kingshot account using:\n"
            f"```/verify <ingame_id> <kingdom> <alliance>```\n"
            f"Example:\n```/verify 123456 466 RTL```"
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
@bot.tree.command(name="setup", description="Initial setup for the bot. Owner only.")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction, start_role_name: str, verified_role_name: str,
                owner_role_name: str, verify_channel_name: str, general_channel_name: str, allowed_kingdom: int, allowed_level: int):
    save_server_config(
        str(interaction.guild.id),
        start_role_name,
        verified_role_name,
        owner_role_name,
        verify_channel_name,
        general_channel_name,
        allowed_kingdom,
        allowed_level
    )
    await interaction.response.send_message("Server configuration saved successfully!", ephemeral=True)

@bot.tree.command(name="hello", description="Greet the bot")
async def hello(interaction: discord.Interaction):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured yet.", ephemeral=True)
        return
    general_channel = config[5]
    if interaction.channel.name != general_channel:
        await interaction.response.send_message(f"Use this command in #{general_channel} only.", ephemeral=True)
        return
    await interaction.response.send_message(f"Hello, {interaction.user.mention}! Have a great day.")

@bot.tree.command(name="verify", description="Verify your in-game account")
async def verify(interaction: discord.Interaction, ingame_id: int, kingdom: int, alliance: str):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured. Run /setup.", ephemeral=True)
        return
    verify_channel_name = config[4]
    allowed_kingdom = config[6]
    start_role = config[1]
    verified_role = config[2]
    allowed_level = config[7]

    if interaction.channel.name != verify_channel_name:
        await interaction.response.send_message(f"Use #{verify_channel_name} channel only.", ephemeral=True)
        return

    if kingdom != allowed_kingdom:
        await interaction.response.send_message(f"Only kingdom {allowed_kingdom} is allowed!", ephemeral=True)
        return
    

    discord_id = str(interaction.user.id)
    existing = get_player(discord_id)
    verified_role_obj = discord.utils.get(interaction.guild.roles, name=verified_role)
    start_role_obj = discord.utils.get(interaction.guild.roles, name=start_role)

    try:
        player_info = await get_player_info(ingame_id)
    except Exception:
        await interaction.response.send_message("Could not reach Kingshot API. Try again later.", ephemeral=True)
        return

    player_data = player_info.get("data", {})
    if not player_data or player_data.get("kingdom") != kingdom:
        await interaction.response.send_message("Verification failed. Check ID, kingdom, and alliance.", ephemeral=True)
        return
    
    if player_data.get("level", 0) < allowed_level:
        await interaction.response.send_message(f"You must be at least Town Center {allowed_level} to verify.", ephemeral=True)
        return

    ingame_name = player_data.get("name")
    if existing:
        if verified_role_obj:
            await interaction.user.add_roles(verified_role_obj)
        if start_role_obj:
            await interaction.user.remove_roles(start_role_obj)
        nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
        await interaction.user.edit(nick=nick)
        await interaction.response.send_message("You are already verified! Contact admin to update info.", ephemeral=True)
        return

    save_player(discord_id, ingame_name, ingame_id, kingdom, alliance)
    if verified_role_obj:
        await interaction.user.add_roles(verified_role_obj)
    if start_role_obj:
        await interaction.user.remove_roles(start_role_obj)
    nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
    await interaction.user.edit(nick=nick)
    await interaction.response.send_message(f"Your account has been verified, {interaction.user.mention}!")

@bot.tree.command(name="unverify", description="Unverify a player. Admin only.")
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

@bot.tree.command(name="mystats", description="Get your in-game stats")
async def mystats(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    player = get_player(discord_id)
    if not player:
        await interaction.response.send_message("Not verified. Use /verify.", ephemeral=True)
        return
    player_info = await get_player_info(player[2])
    data = player_info.get("data", {})
    embed = discord.Embed(title=f"{data.get('name', 'Unknown')}'s Stats", color=discord.Color.blue())
    embed.add_field(name="Player ID", value=data.get("playerId", "Unknown"), inline=False)
    embed.add_field(name="Kingdom", value=data.get("kingdom", "Unknown"), inline=True)
    embed.add_field(name="Alliance", value=player[4], inline=True)
    embed.add_field(name="Town Center Level", value=data.get("levelRendered", "Unknown"), inline=False)
    embed.set_thumbnail(url=data.get("profilePhoto", ""))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="age", description="Get the kingdom age")
async def age(interaction: discord.Interaction, kingdom_id: int):
    try:
        kingdom_data = await get_kingdom_stats(kingdom_id)
        open_time_str = kingdom_data["data"]["servers"][0]["openTime"]
        open_time = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days = (now - open_time).days
        embed = discord.Embed(title=f"Kingdom {kingdom_id} Age",
                              description=f"Kingdom {kingdom_id} has been open for {days} days!",
                              color=discord.Color.green())
        embed.add_field(name="Open Date", value=open_time.strftime("%Y-%m-%d"), inline=True)
        await interaction.response.send_message(embed=embed)
    except ValueError:
        await interaction.response.send_message(f"Kingdom {kingdom_id} not found.", ephemeral=True)

# ─────────────────────────────────────────────
# CLEANUP
# ─────────────────────────────────────────────
@bot.event
async def on_close():
    await bot.session.close()

# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
try:
    bot.run(token, log_handler=handler, log_level=logging.DEBUG)
finally:
    import asyncio
    asyncio.run(bot.close_session())