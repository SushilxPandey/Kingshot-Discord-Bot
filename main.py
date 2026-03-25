from email.mime import message

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
import logging
import os
import re
from datetime import datetime, timezone



from database import init_db, save_player, get_player, delete_player

# ─────────────────────────────────────────────
#  ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────
load_dotenv()
token = os.getenv('DISCORD_TOKEN')

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# ─────────────────────────────────────────────
#  BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='/', intents=intents)

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
start_role = "Commoner"      # Role assigned to new members on join
verified_role = "Settler"      # Role assigned after successful verification
owner_role = "Owner"       # Role that can manage the bot ( reserved for admin commands)

# ─────────────────────────────────────────────
#  BAD WORDS
# ─────────────────────────────────────────────
with open("badwords.txt", "r") as f:
    bad_words = [line.strip() for line in f if line.strip()]

# ─────────────────────────────────────────────
#  HELPER FUNCTIONS
# ─────────────────────────────────────────────
async def get_player_info(ingame_id):
    """Fetch player information from the Kingshot API."""
    url = f"https://kingshot.net/api/player-info?playerId={ingame_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            data = await response.json()
            return data
        
async def get_kingdom_stats(kingdom_id):
    """Fetch kingdom stats from the Kingshot API."""
    url = f"https://kingshot.net/api/kingdom-tracker?kingdomId={kingdom_id}&recent=1&limit=20&sort=openTime-desc"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            data = await response.json()

            # Basic validation to ensure we have the expected data structure    
            if "data" not in data or "servers" not in data["data"] or len(data["data"]["servers"]) == 0:
                raise ValueError("Invalid response from Kingshot API: missing 'data.servers'")
            return data

# ─────────────────────────────────────────────
#  EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    print(f"We are ready to go!, {bot.user.name}")


@bot.event
async def on_member_join(member):
  
    """Welcome new members and assign the default role."""

    role = discord.utils.get(member.guild.roles, name=start_role)
    if role:
        try:
            await member.add_roles(role)
            print(f"Assigned {start_role} role to {member.name}")
        except Exception as e:
            print(f"Error occurred while assigning role: {e}")
    else:
        print(f"Role '{start_role}' not found. Please contact the owner or try again.")

    #makes sure the verify channel exists and sends a welcome message with instructions on how to verify their account.
    verify_channel = discord.utils.get(member.guild.text_channels, name="verify")
    if verify_channel:
        await verify_channel.send(
            f"Welcome {member.mention}!\n\n"
            f"To gain full access, please verify your Kingshot account using:\n"
            f"```/verify <ingame_name> <ingame_id> <kingdom> <alliance>```\n"
            f"Example:\n"
            f"```/verify Trojan 123456 466 RTL```"
        )


@bot.event
async def on_message(message):
    """Censor bad words and process commands."""
    if message.author == bot.user:
        return

    if any(word in message.content.lower() for word in bad_words):
        cleaned = message.content
        for word in bad_words:
            cleaned = re.sub(word, lambda match: "*" * len(match.group()), cleaned, flags=re.IGNORECASE)        
        await message.delete()
        await message.channel.send(f"{message.author.display_name}: {cleaned}")
        return

    await bot.process_commands(message)

# ─────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────

#This command is to just greet whenever a user wants to say hello.
@bot.tree.command(name = "hello", description="Greet the bot and receive a friendly message back!")
async def hello(interaction: discord.Interaction):
    if interaction.channel.name != "general":
        await interaction.response.send_message(
            "Please use this command in the #general channel only.", ephemeral=True
        )
        return
    
    await interaction.response.send_message(f"Hello, {interaction.user.mention}! I hope you are having a great day.")

    


#This command is to verify a player's Kingshot account and assign the Verified role.

@bot.tree.command(name="verify", description="Verify your in-game account with the bot")
async def verify(interaction: discord.Interaction, ingame_name: str, ingame_id: int, kingdom: int, alliance: str):
    """Verify a player's Kingshot account and assign the Verified role."""

    if interaction.channel.name != "verify":
        await interaction.response.send_message(
            "Please use this command in the #verify channel only.", ephemeral=True
        )
        return

    discord_id = str(interaction.user.id)

    # Check if already verified
    existing = get_player(discord_id)
    if existing:
        await interaction.user.add_roles(discord.utils.get(interaction.guild.roles, name=verified_role))
        await interaction.user.remove_roles(discord.utils.get(interaction.guild.roles, name=start_role))
        await interaction.user.edit(nick=f"[{kingdom}] {alliance}- {ingame_name}")

        await interaction.user.send(
            f"You have already verified your account, {interaction.user.mention}. "
            f"If you want to update your information, please contact an admin.",
            )
        await interaction.response.send_message("Done!", ephemeral=True)
        return

    # Call the Kingshot API
    try:
        player_info = await get_player_info(ingame_id)
        if player_info["data"]["name"] == ingame_name and player_info["data"]["kingdom"] == kingdom:
            save_player(discord_id, ingame_name, ingame_id, kingdom, alliance)

            role = discord.utils.get(interaction.guild.roles, name=verified_role)
            if role:
                await interaction.user.add_roles(role)
                await interaction.user.remove_roles(discord.utils.get(interaction.guild.roles, name=start_role))
                await interaction.user.edit(nick=f"[{kingdom}] {alliance}- {ingame_name}")
                print(f"Assigned '{verified_role}' role to {interaction.user.name}")

            await interaction.response.send_message(
            f"Your account has been verified, {interaction.user.mention}!"
            )
        else:
            await interaction.response.send_message(
                f"Verification failed, {interaction.user.mention}. "
                f"Please make sure your in-game name, kingdom, and player ID are correct."
                )
    except Exception:
        await interaction.response.send_message("Could not reach the Kingshot API. Try again later.")
        return

    # Check if the provided details match the API response
#_________________THIS IS ADMIN ONLY COMMAND____________________
@bot.tree.command(name="unverify", description="Unverify a player and revert their role to Commoner")
async def unverify(interaction: discord.Interaction, member: discord.Member):
    """Unverify a player's account and remove the Verified role. Owner only."""

    # Check if the user has the Owner role
    owner_server_role = discord.utils.get(interaction.guild.roles, name="Owner")
    if owner_server_role not in interaction.user.roles:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    # Check if the target member is actually verified
    discord_id = str(member.id)
    existing = get_player(discord_id)
    if not existing:
        await interaction.response.send_message(f"{member.mention} is not verified.", ephemeral=True)
        return

    # Remove from database
    delete_player(discord_id)

    # Swap roles
    settler = discord.utils.get(interaction.guild.roles, name=verified_role)
    commoner = discord.utils.get(interaction.guild.roles, name=start_role)

    if settler and settler in member.roles:
        await member.remove_roles(settler)
    if commoner:
        await member.add_roles(commoner)

    await interaction.response.send_message(
        f"{member.mention} has been unverified and reverted to {start_role}.", ephemeral=True
    )

#___________________________________________________


@bot.tree.command(name = "mystats", description="Get your in-game stats from the Kingshot API")
async def mystats(interaction: discord.Interaction):
    """This commmand allows user to see their in-game stats and show it to other users in the server"""
    discord_id = str(interaction.user.id)
    player = get_player(discord_id)

    #if the player is not verified.
    if not player:
        await interaction.response.send_message(
            "You are not verified yet. Please verify your account first using /verify command.", ephemeral=True
        )
        return
    player_info = await get_player_info(player[2])  # player[2] is ingame_id
    if not player_info or "data" not in player_info:
        await interaction.response.send_message("Could not retrieve your stats from the Kingshot API. Try again later.", ephemeral=True)
        return
    data = player_info["data"]

    embed = discord.Embed(title=f"{data.get('name', 'Unknown')}'s Stats", color=discord.Color.blue())
    embed.add_field(name="Player ID", value=data.get("playerId", "Unknown"), inline=False)
    embed.add_field(name="Kingdom", value=data.get("kingdom", "Unknown"), inline=True)
    embed.add_field(name="Alliance", value=player[4], inline=True)
    embed.add_field(name="Town Center Level", value=data.get("levelRendered", "Unknown"), inline=False)
    embed.set_thumbnail(url=data.get("profilePhoto", ""))

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="age", description="Tracks the age of the Kingdhot server")
async def age(interaction: discord.Interaction, kingdom_id: int):
    # try/except is used for error handeling in case the API response is not as expected or if the kingdom ID is invalid.
    try:
        kingdom_data = await get_kingdom_stats(kingdom_id)  # which kingdom?
    
        open_time_str = kingdom_data["data"]["servers"][0]["openTime"]
        open_time = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))  # fix timezone
    
        now = datetime.now(timezone.utc)
        days = (now - open_time).days

        embed = discord.Embed(title=f"Kingdom {kingdom_id} Age", description=f"Kingdom {kingdom_id} has been open for {days} days!", color=discord.Color.green())
        embed.add_field(name="Open Date", value=open_time.strftime("%Y-%m-%d"), inline=True)
        await interaction.response.send_message(embed=embed)

    except ValueError:
        await interaction.response.send_message(
        f"Kingdom {kingdom_id} was not found. Please check the kingdom number.", ephemeral=True
    )

    
    
# ─────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────
bot.run(token, log_handler=handler, log_level=logging.DEBUG)