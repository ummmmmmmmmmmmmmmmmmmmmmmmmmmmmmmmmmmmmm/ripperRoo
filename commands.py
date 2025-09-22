# commands.py
from discord import app_commands, Interaction, Object
import discord
from typing import List, Callable
from config import APP_GUILD_IDS
from rip_logic import run_rip, user_has_active, ACTIVE_RIPS, HELP_TEXT
from views import AbortConfirmView
from utils import ok_domain, detect_playlist

async def on_startup():
    from utils import clean_stale_tmp
    try: clean_stale_tmp()
    except Exception: pass

async def thank_new_guild(guild: discord.Guild):
    chan = guild.system_channel
    if not chan or not chan.permissions_for(guild.me).send_messages:
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages:
                chan = c; break
    if chan:
        try: await chan.send("Thanks for adding me! Use `/help` for commands.")
        except: pass

def _scope_decorator(gobjs: List[Object] | None) -> Callable:
    return app_commands.guilds(*gobjs) if gobjs else (lambda f: f)

def register_commands(tree: app_commands.CommandTree, guild_objs_supplier: Callable[[], List[Object]] | None):
    gobjs = guild_objs_supplier() if guild_objs_supplier else None
    scope = _scope_decorator(gobjs)

    @scope
    @tree.command(name="help", description="Show commands")
    async def help_cmd(inter: Interaction):
        await _e(inter, HELP_TEXT)

    @scope
    @tree.command(name="abort", description="Abort your current rip")
    async def abort_cmd(inter: Interaction):
        job = ACTIVE_RIPS.get(inter.user.id)
        if not job:
            await _e(inter, "You don't have an active rip."); return
        view = AbortConfirmView(inter.user.id)
        msg = await _e(inter, "Download currently in process, are you SURE? [Y/N]", view=view)
        await view.wait()
        try: await msg.edit(view=None)
        except: pass
        if not view.confirmed:
            await _e(inter, "Abort cancelled."); return
        job["pr"].cancelled = True
        await _e(inter, "Aborted.")

    @scope
    @tree.command(name="rip", description="Rip audio and post to this channel")
    @app_commands.describe(link="YouTube / SoundCloud / Bandcamp link")
    async def rip_cmd(inter: Interaction, link: str):
        if not ok_domain(link):
            is_pl, prov = detect_playlist(link)
            if prov == "spotify":
                await _e(inter, "Spotify playlists aren’t supported."); return
            await _e(inter, "Unsupported link. Try YouTube, SoundCloud, or Bandcamp."); return
        if user_has_active(inter.user.id):
            await _e(inter, "You already have a rip running. Use `/abort` to cancel it."); return
        await inter.response.defer(ephemeral=True)
        await run_rip(inter, link, to_dm=False)

    @scope
    @tree.command(name="ripdm", description="Rip audio and DM it to you")
    @app_commands.describe(link="YouTube / SoundCloud / Bandcamp link")
    async def ripdm_cmd(inter: Interaction, link: str):
        if not ok_domain(link):
            is_pl, prov = detect_playlist(link)
            if prov == "spotify":
                await _e(inter, "Spotify playlists aren’t supported."); return
            await _e(inter, "Unsupported link. Try YouTube, SoundCloud, or Bandcamp."); return
        if user_has_active(inter.user.id):
            await _e(inter, "You already have a rip running. Use `/abort` to cancel it."); return
        await inter.response.defer(ephemeral=True)
        await run_rip(inter, link, to_dm=True)

    @scope
    @tree.command(name="sync", description="(Owner) Force re-sync of slash commands here")
    async def sync_here(inter: Interaction):
        app = await inter.client.application_info()
        if inter.user.id != app.owner.id:
            await inter.response.send_message("Only the bot owner can run this.", ephemeral=True); return
        await inter.response.defer(ephemeral=True)
        try:
            res = await tree.sync(guild=inter.guild)
            await inter.followup.send(f"Synced {len(res)} command(s) to this guild.", ephemeral=True)
        except Exception as e:
            await inter.followup.send(f"Sync failed: `{e}`", ephemeral=True)

async def _e(inter: Interaction, content: str, *, view: discord.ui.View | None = None) -> discord.Message:
    if not inter.response.is_done():
        await inter.response.send_message(content=content, view=view, ephemeral=True)
        return await inter.original_response()
    else:
        return await inter.followup.send(content=content, view=view, ephemeral=True, wait=True)
