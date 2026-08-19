"""
Verification UI: a persistent Verify / Reverify button row and a one-field modal.

Players only enter their **in-game ID** — everything else (name, kingdom, alliance)
is pulled live from kingshotstats. All the API / role / nickname work lives in the
Verification cog.
"""

import logging

import discord


class VerifyModal(discord.ui.Modal):
    """Collects just the in-game ID; the cog pulls the rest from the API."""

    def __init__(self, is_reverify: bool = False):
        super().__init__(title="Reverify your account" if is_reverify else "Verify your account")
        self.is_reverify = is_reverify

        self.ingame_id = discord.ui.TextInput(
            label="In-game ID", placeholder="e.g. 73372825", required=True, max_length=20
        )
        self.add_item(self.ingame_id)

    async def on_submit(self, interaction: discord.Interaction):
        raw_id = str(self.ingame_id.value).strip()
        if not raw_id.isdigit():
            await interaction.response.send_message(
                "Your in-game ID should be numbers only. Please try again.", ephemeral=True
            )
            return

        cog = interaction.client.get_cog("Verification")
        if cog is None:
            await interaction.response.send_message(
                "Verification is temporarily unavailable. Please try again later.",
                ephemeral=True,
            )
            logging.error("VerifyModal submitted but Verification cog is not loaded.")
            return

        await cog.handle_verification(
            interaction, ingame_id=int(raw_id), is_reverify=self.is_reverify
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logging.exception("Error in verification modal: %s", error)
        try:
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender("Something went wrong during verification. Please try again.", ephemeral=True)
        except discord.HTTPException:
            pass


async def _start_flow(interaction: discord.Interaction, is_reverify: bool):
    """Both buttons just open the one-field modal."""
    await interaction.response.send_modal(VerifyModal(is_reverify=is_reverify))


class VerifyView(discord.ui.View):
    """Persistent view with Verify + Reverify buttons."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅", custom_id="ks_verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _start_flow(interaction, is_reverify=False)

    @discord.ui.button(label="Reverify / refresh", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="ks_reverify")
    async def reverify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _start_flow(interaction, is_reverify=True)
