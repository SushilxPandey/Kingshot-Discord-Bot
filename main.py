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
    init_db,
    save_player,
    get_player,
    delete_player,
    save_server_config,
    get_server_config
)

# ─────────────────────────────────────────────
# ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")

# ─────────────────────────────────────────────
# INTENTS
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# ─────────────────────────────────────────────
# BAD WORDS
# ─────────────────────────────────────────────
with open("badwords.txt", "r") as f:
    bad_words = [line.strip() for line in f if line.strip()]

# ─────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────
async def get_player_info(bot, ingame_id: int):
    if not hasattr(bot, "session") or bot.session.closed:
        bot.session = aiohttp.ClientSession()
    url = f"https://kingshot.net/api/player-info?playerId={ingame_id}"
    async with bot.session.get(url) as resp:
        if resp.status != 200:
            raise ValueError("Kingshot API error")
        return await resp.json()

async def get_kingdom_stats(bot, kingdom_id: int):
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

# ─────────────────────────────────────────────
# BOT CLASS
# ─────────────────────────────────────────────
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)
        self.session = None

    async def close_session(self):
        if hasattr(self, "session") and not self.session.closed:
            await self.session.close()

    async def setup_hook(self):
        # Start background task
        self.bg_task = self.loop.create_task(self.auto_sync())
        await self.tree.sync()

    # ─────────────────────────────────────────
    # AUTO-SYNC BACKGROUND TASK
    # ─────────────────────────────────────────
    async def auto_sync(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for guild in self.guilds:
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

                            player_info = await get_player_info(self, ingame_id)
                            data = player_info["data"]
                            ingame_name = data["name"]
                            kingdom = data["kingdom"]

                            nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
                            if member.nick != nick:
                                await member.edit(nick=nick)
                        except Exception as e:
                            logging.error(f"Auto-sync error guild {guild.id}, member {member.id}: {e}")
            except Exception as e:
                logging.error(f"Auto-sync outer loop error: {e}")

            await asyncio.sleep(3 * 60 * 60)  # 3 hours

# ─────────────────────────────────────────────
# CREATE BOT INSTANCE
# ─────────────────────────────────────────────
bot = MyBot()

# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    bot.session = aiohttp.ClientSession()
    logging.info(f"We are ready! {bot.user.name}")

@bot.event
async def on_member_join(member):
    config = get_server_config(str(member.guild.id))
    if not config:
        logging.warning(f"Server config missing for guild {member.guild.id}")
        return

    start_role = config[1]
    verify_channel_name = config[4]

    role = discord.utils.get(member.guild.roles, name=start_role)
    if role:
        await member.add_roles(role)

    verify_channel = discord.utils.get(member.guild.text_channels, name=verify_channel_name)
    if verify_channel:
        await verify_channel.send(
            f"Welcome {member.mention}!\n"
            f"Verify your account using:\n```/verify <player_id> <alliance>```"
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
@bot.tree.command(name="setup", description="Initial setup for the bot. Owner only.")
@app_commands.default_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction,
    start_role_name: str,
    verified_role_name: str,
    owner_role_name: str,
    verify_channel_name: str,
    general_channel_name: str,
    allowed_kingdoms: str
):
    save_server_config(
        str(interaction.guild.id),
        start_role_name,
        verified_role_name,
        owner_role_name,
        verify_channel_name,
        general_channel_name,
        allowed_kingdoms  # Store allowed kingdoms as CSV string
    )
    await interaction.response.send_message("Server configuration saved!", ephemeral=True)

@bot.tree.command(name="verify", description="Verify your in-game account")
async def verify(interaction: discord.Interaction, ingame_id: int, alliance: str):
    await interaction.response.defer(ephemeral=True)  # <-- tells Discord to wait
    config = get_server_config(str(interaction.guild.id))
    
    if not config:
        await interaction.followup.send("Server not configured. Ask an admin to run /setup.", ephemeral=True)
        return

    verify_channel_name = config[4]
    start_role = config[1]
    verified_role = config[2]
    allowed_kingdoms = config[6].split(",") if len(config) > 6 else []

    if interaction.channel.name != verify_channel_name:
        await interaction.followup.send(f"Use this command in #{verify_channel_name} only.", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    existing = get_player(discord_id)

    start = discord.utils.get(interaction.guild.roles, name=start_role)
    verified = discord.utils.get(interaction.guild.roles, name=verified_role)

    try:
        player_info = await get_player_info(bot, ingame_id)  # Might take time
        data = player_info["data"]
        ingame_name = data["name"]
        kingdom = data["kingdom"]
    except Exception:
        await interaction.followup.send("Could not reach Kingshot API. Try later.", ephemeral=True)
        return

    if allowed_kingdoms and str(kingdom) not in allowed_kingdoms:
        await interaction.followup.send(f"Your kingdom {kingdom} is not allowed to verify.", ephemeral=True)
        return

    # Save/update player
    if existing:
        if verified: await interaction.user.add_roles(verified)
        if start: await interaction.user.remove_roles(start)
        nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
        await interaction.user.edit(nick=nick)
        await interaction.followup.send("You are already verified. Contact admin to update info.", ephemeral=True)
        return

    save_player(discord_id, ingame_name, ingame_id, kingdom, alliance)
    if verified: await interaction.user.add_roles(verified)
    if start: await interaction.user.remove_roles(start)
    nick = f"[{kingdom}] {alliance}- {ingame_name}"[:32]
    await interaction.user.edit(nick=nick)
    await interaction.followup.send(f"Verified! Welcome [{kingdom}] {alliance}-{ingame_name}", ephemeral=False)

    
@bot.tree.command(name="unverify", description="Unverify a player. Owner only.")
@app_commands.default_permissions(administrator=True)
async def unverify(interaction: discord.Interaction, member: discord.Member):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured.", ephemeral=True)
        return

    discord_id = str(member.id)
    start_role = config[1]
    verified_role = config[2]

    player = get_player(discord_id)
    if not player:
        await interaction.response.send_message(f"{member.mention} is not verified.", ephemeral=True)
        return

    delete_player(discord_id)
    settler = discord.utils.get(interaction.guild.roles, name=verified_role)
    commoner = discord.utils.get(interaction.guild.roles, name=start_role)

    if settler and settler in member.roles:
        await member.remove_roles(settler)
    if commoner:
        await member.add_roles(commoner)

    await interaction.response.send_message(f"{member.mention} has been unverified.", ephemeral=True)

@bot.tree.command(name="mystats", description="Get your in-game stats")
async def mystats(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    player = get_player(discord_id)
    if not player:
        await interaction.response.send_message("You are not verified.", ephemeral=True)
        return

    try:
        player_info = await get_player_info(bot, player[2])
        data = player_info["data"]
    except Exception:
        await interaction.response.send_message("Could not retrieve stats.", ephemeral=True)
        return

    embed = discord.Embed(title=f"{data.get('name','Unknown')}'s Stats", color=discord.Color.blue())
    embed.add_field(name="Player ID", value=data.get("playerId","Unknown"), inline=False)
    embed.add_field(name="Kingdom", value=data.get("kingdom","Unknown"), inline=True)
    embed.add_field(name="Alliance", value=player[4], inline=True)
    embed.add_field(name="Town Center Level", value=data.get("levelRendered","Unknown"), inline=False)
    embed.set_thumbnail(url=data.get("profilePhoto",""))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="age", description="Tracks kingdom age")
async def age(interaction: discord.Interaction, kingdom_id: int):
    try:
        kingdom_data = await get_kingdom_stats(bot, kingdom_id)
        open_time_str = kingdom_data["data"]["servers"][0]["openTime"]
        open_time = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days = (now - open_time).days

        embed = discord.Embed(
            title=f"Kingdom {kingdom_id} Age",
            description=f"{days} days",
            color=discord.Color.green()
        )
        embed.add_field(name="Open Date", value=open_time.strftime("%Y-%m-%d"), inline=True)
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message(f"Kingdom {kingdom_id} not found.", ephemeral=True)

@bot.tree.command(name="hello", description="Greet the bot")
async def hello(interaction: discord.Interaction):
    config = get_server_config(str(interaction.guild.id))
    if not config:
        await interaction.response.send_message("Server not configured.", ephemeral=True)
        return

    general_channel = config[5]
    if interaction.channel.name != general_channel:
        await interaction.response.send_message(f"Use #{general_channel} channel only.", ephemeral=True)
        return

    await interaction.response.send_message(f"Hello {interaction.user.mention}! Hope you are having a great day.")

# ─────────────────────────────────────────────
# RUN BOT
# ─────────────────────────────────────────────
try:
    bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
finally:
    asyncio.run(bot.close_session())