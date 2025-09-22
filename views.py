# views.py
from typing import Optional
import discord

class ArtChoiceView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.include_art: Optional[bool] = None

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message("This prompt isn’t for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Include", style=discord.ButtonStyle.primary)
    async def yes(self, itx: discord.Interaction, _: discord.ui.Button):
        self.include_art = True
        for c in self.children:
            c.disabled = True
        await itx.response.edit_message(content="Artwork will be included.", view=self)
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, itx: discord.Interaction, _: discord.ui.Button):
        self.include_art = False
        for c in self.children:
            c.disabled = True
        await itx.response.edit_message(content="Audio only.", view=self)
        self.stop()


class GeoDecisionView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 45.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.choice: Optional[str] = None

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message("This prompt isn’t for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Ignore & Continue", style=discord.ButtonStyle.primary)
    async def cont(self, itx: discord.Interaction, _: discord.ui.Button):
        self.choice = "continue"
        for c in self.children:
            c.disabled = True
        await itx.response.edit_message(content="Continuing without the blocked tracks…", view=self)
        self.stop()

    @discord.ui.button(label="Abort", style=discord.ButtonStyle.danger)
    async def abort(self, itx: discord.Interaction, _: discord.ui.Button):
        self.choice = "abort"
        for c in self.children:
            c.disabled = True
        await itx.response.edit_message(content="Cancelling…", view=self)
        self.stop()


class AbortConfirmView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed = False

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message("This prompt isn’t for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Y", style=discord.ButtonStyle.danger)
    async def yes(self, itx: discord.Interaction, _: discord.ui.Button):
        self.confirmed = True
        for c in self.children:
            c.disabled = True
        await itx.response.edit_message(content="Aborting…", view=self)
        self.stop()

    @discord.ui.button(label="N", style=discord.ButtonStyle.secondary)
    async def no(self, itx: discord.Interaction, _: discord.ui.Button):
        self.confirmed = False
        for c in self.children:
            c.disabled = True
        await itx.response.edit_message(content="Abort cancelled.", view=self)
        self.stop()
