import discord

class ArtChoice(discord.ui.View):
    def __init__(self, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.choice = None

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = True
        await interaction.response.edit_message(content="✅ Album art will be included.", view=None)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = False
        await interaction.response.edit_message(content="🚫 No album art.", view=None)

class ZipChoice(discord.ui.View):
    def __init__(self, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.choice = None

    @discord.ui.button(label="Zip it", style=discord.ButtonStyle.primary)
    async def zip_it(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = True
        await interaction.response.edit_message(content="📦 Will zip files.", view=None)

    @discord.ui.button(label="Keep separate", style=discord.ButtonStyle.secondary)
    async def no_zip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = False
        await interaction.response.edit_message(content="🗂️ Will send files individually.", view=None)
