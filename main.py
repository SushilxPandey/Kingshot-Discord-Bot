"""
Kingshot Discord bot — entry point.

Multi-server, self-configuring: it creates its own roles and channels, gates
newcomers behind a Verify button, keeps a per-server player database, and tracks
in-game name changes in the background. See the cogs/ package for each feature.
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import kingshot_api
from views.verify_view import VerifyView
from cogs.setup_cog import SetupPanelView
from cogs.points import PointsPanelView

# ─────────────────────────────────────────────
# ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
# Optional: set TEST_GUILD to a server ID to make slash commands appear there
# instantly while developing (global sync can take up to an hour to propagate).
TEST_GUILD = os.getenv("TEST_GUILD")

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")

INITIAL_EXTENSIONS = [
    "cogs.setup_cog",
    "cogs.verification",
    "cogs.roster",
    "cogs.tracker",
    "cogs.moderation",
    "cogs.giftcode",
    "cogs.points",
]

# ─────────────────────────────────────────────
# BOT
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True   # profanity filter
intents.members = True           # join gating + member lookups


class KingshotBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Load every feature cog.
        for ext in INITIAL_EXTENSIONS:
            await self.load_extension(ext)

        # Register persistent views so their buttons work after restarts.
        self.add_view(VerifyView())
        self.add_view(SetupPanelView())
        self.add_view(PointsPanelView())

        # Sync application (slash) commands. Don't let a sync failure crash startup.
        try:
            if TEST_GUILD:
                guild = discord.Object(id=int(TEST_GUILD))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logging.info("Synced commands to test guild %s", TEST_GUILD)
            else:
                await self.tree.sync()
                logging.info("Synced global commands")
        except discord.Forbidden:
            logging.error(
                "Command sync failed with 403 Missing Access. The bot needs the "
                "'applications.commands' scope — re-invite it using an OAuth2 URL that "
                "includes both 'bot' and 'applications.commands'. Starting without sync."
            )
        except Exception as exc:  # noqa: BLE001 - never block startup on sync
            logging.error("Command sync failed: %s. Starting anyway.", exc)

    async def on_ready(self):
        logging.info("We are ready to go! Logged in as %s", self.user)
        print(f"Logged in as {self.user} (id: {self.user.id})")

    async def close(self):
        # Clean up the shared Kingshot API session on shutdown.
        await kingshot_api.close()
        await super().close()


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in the environment / .env file.")
    discord.utils.setup_logging(handler=handler, level=logging.INFO, root=True)
    bot = KingshotBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
