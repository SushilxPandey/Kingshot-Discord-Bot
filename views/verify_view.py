"""
Verification UI: a persistent Verify / Reverify button row, an alliance picker,
and the details modal.

Discord modals can't contain dropdowns, so when the server has alliances the flow
is two-step: the button shows an alliance ``Select`` (ephemeral), and choosing one
opens the modal for in-game ID + kingdom (the chosen tag is carried on the modal).
If no alliances are configured yet, the modal falls back to a free-text alliance
field. All the actual API / role / nickname work lives in the Verification cog.
"""

import logging

import discord

import database


class VerifyModal(discord.ui.Modal):
    """Collects in-game ID + kingdom (+ alliance text only when none are configured)."""

    def __init__(self, alliance_tag: str | None = None, is_reverify: bool = False):
        super().__init__(title="Reverify your account" if is_reverify else "Verify your account")
        self.alliance_tag = alliance_tag
        self.is_reverify = is_reverify

        self.ingame_id = discord.ui.TextInput(
            label="In-game ID", placeholder="e.g. 123456", required=True, max_length=20
        )
        self.ingame_name = discord.ui.TextInput(
            label="In-game name", placeholder="Your exact Kingshot name", required=True, max_length=32
        )
        self.kingdom = discord.ui.TextInput(
            label="Kingdom number", placeholder="e.g. 466", required=True, max_length=10
        )
        self.add_item(self.ingame_id)
        self.add_item(self.ingame_name)
        self.add_item(self.kingdom)

        # Only ask for a free-text alliance when the server has no alliance list.
        self.alliance_input = None
        if alliance_tag is None:
            self.alliance_input = discord.ui.TextInput(
                label="Alliance tag", placeholder="e.g. RTL", required=True, max_length=10
            )
            self.add_item(self.alliance_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_id = str(self.ingame_id.value).strip()
        raw_kingdom = str(self.kingdom.value).strip()
        raw_name = str(self.ingame_name.value).strip()
        if not raw_id.isdigit() or not raw_kingdom.isdigit():
            await interaction.response.send_message(
                "In-game ID and Kingdom must both be numbers. Please try again.",
                ephemeral=True,
            )
            return
        if not raw_name:
            await interaction.response.send_message(
                "Please enter your in-game name.", ephemeral=True
            )
            return

        tag = self.alliance_tag
        if tag is None and self.alliance_input is not None:
            tag = str(self.alliance_input.value).strip().upper()

        cog = interaction.client.get_cog("Verification")
        if cog is None:
            await interaction.response.send_message(
                "Verification is temporarily unavailable. Please try again later.",
                ephemeral=True,
            )
            logging.error("VerifyModal submitted but Verification cog is not loaded.")
            return

        await cog.handle_verification(
            interaction,
            ingame_id=int(raw_id),
            ingame_name=raw_name,
            kingdom=int(raw_kingdom),
            alliance=tag,
            is_reverify=self.is_reverify,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logging.exception("Error in verification modal: %s", error)
        try:
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender("Something went wrong during verification. Please try again.", ephemeral=True)
        except discord.HTTPException:
            pass


class AllianceSelect(discord.ui.Select):
    def __init__(self, alliances: list[dict], is_reverify: bool):
        self.is_reverify = is_reverify
        options = [
            discord.SelectOption(label=a["tag"], description=(a.get("name") or a["tag"])[:100])
            for a in alliances[:25]
        ]
        super().__init__(placeholder="Choose your alliance…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            VerifyModal(alliance_tag=self.values[0], is_reverify=self.is_reverify)
        )


class AllianceSelectView(discord.ui.View):
    def __init__(self, alliances: list[dict], is_reverify: bool):
        super().__init__(timeout=180)
        self.add_item(AllianceSelect(alliances, is_reverify))


async def _start_flow(interaction: discord.Interaction, is_reverify: bool):
    """Shared entry for both buttons: pick alliance (if any) then open the modal."""
    alliances = await database.all_alliances(interaction.guild.id)
    if alliances:
        await interaction.response.send_message(
            "First, pick your alliance:",
            view=AllianceSelectView(alliances, is_reverify),
            ephemeral=True,
        )
    else:
        await interaction.response.send_modal(VerifyModal(alliance_tag=None, is_reverify=is_reverify))


class VerifyView(discord.ui.View):
    """Persistent view with Verify + Reverify buttons."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅", custom_id="ks_verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _start_flow(interaction, is_reverify=False)

    @discord.ui.button(label="Reverify / change details", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="ks_reverify")
    async def reverify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _start_flow(interaction, is_reverify=True)
