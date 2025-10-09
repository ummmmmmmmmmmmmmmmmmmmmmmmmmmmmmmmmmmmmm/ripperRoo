# ui_components.py
import discord

class ArtChoice(discord.ui.View):
    """Yes/No view for including album art. Does NOT edit message content itself."""
    def __init__(self, timeout: float | None = 60):
        super().__init__(timeout=timeout)
        self.choice: bool | None = None

    @discord.ui.button(label="Include art", style=discord.ButtonStyle.success, emoji="🖼️")
    async def include(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = True
        for child in self.children:
            child.disabled = True
        await interaction.response.defer()  # don't change the message text/content
        self.stop()

    @discord.ui.button(label="No art", style=discord.ButtonStyle.secondary, emoji="🚫")
    async def exclude(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = False
        for child in self.children:
            child.disabled = True
        await interaction.response.defer()
        self.stop()


class ZipChoice(discord.ui.View):
    """Optional zip prompt used earlier in the project; unchanged behavior."""
    def __init__(self, timeout: float | None = 60):
        super().__init__(timeout=timeout)
        self.choice: bool | None = None

    @discord.ui.button(label="Zip files", style=discord.ButtonStyle.primary, emoji="📦")
    async def do_zip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = True
        for child in self.children:
            child.disabled = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Send separately", style=discord.ButtonStyle.secondary, emoji="📄")
    async def no_zip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = False
        for child in self.children:
            child.disabled = True
        await interaction.response.defer()
        self.stop()
