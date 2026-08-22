"""
AI game-questions assistant (Google Gemini, free tier).

Members ask Kingshot questions by simply typing in the dedicated **#ask-the-bot**
channel; the bot answers with a short, friendly, game-scoped reply from Gemini.

Setup:
  * Set ``GEMINI_API_KEY`` in the environment (free key from Google AI Studio).
  * Optionally set ``GEMINI_MODEL`` (defaults to ``gemini-2.0-flash``).

Safety / cost control: a per-user cooldown, an input-length cap, an output-token
cap, and honest "I'm not sure" behaviour via the system prompt. No conversation
memory — each question is answered on its own (simple and cheap).
"""

import logging
import os
import time

import aiohttp
import discord
from discord.ext import commands

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.0-flash"

USER_COOLDOWN = 12          # seconds between questions per user
MAX_QUESTION_CHARS = 600    # ignore/trim overly long prompts
MAX_ANSWER_CHARS = 1900     # keep under Discord's 2000-char message limit

SYSTEM_PROMPT = (
    "You are a friendly assistant for the mobile strategy game Kingshot, living in a "
    "Discord server for a Kingshot alliance. Answer players' questions about the game "
    "clearly and concisely — usually a few sentences. Cover things like heroes, gear, "
    "troops, buildings, events, alliance/KvK strategy, and general tips. "
    "If you are not certain about an exact number, a very recent change, or a detail that "
    "may vary by version, say so honestly instead of inventing specifics. "
    "Stay on the topic of Kingshot and being a helpful community assistant; politely decline "
    "unrelated, unsafe, or rule-breaking requests. Keep a warm, encouraging tone. "
    "Do not use headers; short paragraphs or a tight list are fine."
)


class Ask(commands.Cog, name="Ask"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
        self._session: aiohttp.ClientSession | None = None
        self._last_used: dict[int, float] = {}          # user_id -> monotonic time
        self._warned_missing_key: set[int] = set()      # guild_ids we've told once
        self.ask_channels: dict[int, int] = {}           # guild_id -> ask channel id

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def note_ask_channel(self, guild_id, channel_id):
        """Called by setup so the listener knows which channel is the Q&A channel."""
        if channel_id:
            self.ask_channels[int(guild_id)] = int(channel_id)

    @commands.Cog.listener()
    async def on_ready(self):
        import database
        for guild_id in await database.list_guild_ids():
            config = await database.get_config(guild_id) or {}
            if config.get("ask_channel_id"):
                self.ask_channels[int(guild_id)] = int(config["ask_channel_id"])

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        cid = self.ask_channels.get(message.guild.id)
        if not cid or message.channel.id != cid:
            return
        question = (message.content or "").strip()
        if not question:
            return  # attachment-only / empty
        # Ignore anything that looks like a command invocation.
        if question.startswith(("/", "!", ".", "?", "-")) and len(question) < 3:
            return

        if not self.api_key:
            # Tell the guild's admins once, then stay quiet.
            if message.guild.id not in self._warned_missing_key:
                self._warned_missing_key.add(message.guild.id)
                try:
                    await message.reply(
                        "🤖 The AI assistant isn't switched on yet — an admin needs to add a "
                        "`GEMINI_API_KEY`. Ping them and I'll be ready to answer!",
                        mention_author=False,
                    )
                except discord.HTTPException:
                    pass
            return

        # Per-user cooldown.
        now = time.monotonic()
        last = self._last_used.get(message.author.id, 0)
        if now - last < USER_COOLDOWN:
            wait = int(USER_COOLDOWN - (now - last)) + 1
            try:
                await message.reply(f"⏳ One sec — try again in {wait}s.", mention_author=False,
                                    delete_after=6)
            except discord.HTTPException:
                pass
            return
        self._last_used[message.author.id] = now

        try:
            async with message.channel.typing():
                answer = await self._ask_gemini(question[:MAX_QUESTION_CHARS])
        except Exception as exc:  # noqa: BLE001
            logging.warning("Gemini request failed: %s", exc)
            answer = None

        if not answer:
            try:
                await message.reply(
                    "I couldn't come up with an answer just now — try rephrasing, or ask again in a moment.",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass
            return

        answer = answer.strip()
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS].rsplit(" ", 1)[0] + " …"
        try:
            await message.reply(answer, mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def _ask_gemini(self, question: str) -> str | None:
        url = GEMINI_URL.format(model=self.model)
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 600},
        }
        async with self._get_session().post(
            url, params={"key": self.api_key}, json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(f"Gemini HTTP {response.status}: {body[:200]}")
            data = await response.json(content_type=None)

        candidates = data.get("candidates") or []
        if not candidates:
            # Blocked by a safety filter or empty.
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            if reason:
                return "I can't help with that one — try a different question about the game."
            return None
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None


async def setup(bot: commands.Bot):
    await bot.add_cog(Ask(bot))
