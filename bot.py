# bot.py
import os, re, tempfile, asyncio, shutil
from typing import Optional
from urllib.parse import urlparse

import discord
from discord.ext import commands
import yt_dlp

# ===================== Config =====================
TOKEN = os.getenv("DISCORD_TOKEN")  # PowerShell: $env:DISCORD_TOKEN='...'
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")

ALLOWED_DOMAINS = {
    "youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be",
    "soundcloud.com", "www.soundcloud.com", "on.soundcloud.com"
}
# ===================================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="*", intents=intents, help_command=None)

HELP_TEXT = (
    "**ripperRoo — a Discord mp3 ripper created by d-rod**\n"
    "• `*rip <link>` — rip YouTube or SoundCloud audio and post it here\n"
    "• `*ripdm <link>` — rip and DM you the file\n\n"
    "_Tip: to auto-delete your command after sending files, give my role **Manage Messages** in this channel._"
)

# ---------- helpers ----------
def ok_domain(link: str) -> bool:
    try:
        host = urlparse(link).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        return host in ALLOWED_DOMAINS
    except Exception:
        return False

def ydl_opts(tmpdir: str) -> dict:
    return {
        "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "noprogress": True,
        "quiet": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ],
        "ffmpeg_location": FFMPEG_BIN if FFMPEG_BIN else None,
    }

async def download_to_mp3(link: str, tmpdir: str) -> tuple[str, str]:
    with yt_dlp.YoutubeDL(ydl_opts(tmpdir)) as ydl:
        info = ydl.extract_info(link, download=True)
        if "requested_downloads" in info and info["requested_downloads"]:
            path = info["requested_downloads"][0]["filepath"]
        else:
            base = ydl.prepare_filename(info)
            root, _ = os.path.splitext(base)
            path = root + ".mp3"
        title = info.get("title") or "audio"
        if not path.lower().endswith(".mp3"):
            root, _ = os.path.splitext(path)
            candidate = root + ".mp3"
            if os.path.exists(candidate):
                path = candidate
        return path, f"{title}.mp3"

async def send_and_cleanup(ctx: commands.Context, file_path: str, file_name: str, to_dm: bool = False):
    """
    Sends the file. In servers, adds 'ripped by: DisplayName(Username)'.
    After a successful send, deletes the invoking message (if permitted).
    """
    if to_dm:
        # DM: no attribution line, just send the file and an ack
        dm = await ctx.author.create_dm()
        await dm.send(file=discord.File(file_path, filename=file_name))
        ack = await ctx.reply("📩 Sent to your DMs.", mention_author=False)
    else:
        # In a guild channel, attach the attribution line
        attribution = None
        if ctx.guild is not None:
            display = ctx.author.display_name  # server nickname/display name
            uname = ctx.author.name            # global username
            attribution = f"ripped by: {display}({uname})"
        await ctx.send(
            content=attribution,
            file=discord.File(file_path, filename=file_name)
        )
        ack = None

    # Try to delete the invoking message
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        if ack:
            try:
                await ack.edit(content="📩 Sent to your DMs. (Grant **Manage Messages** if you want me to delete your command.)")
            except discord.HTTPException:
                pass

# ---------- commands ----------
@bot.command(name="help")
async def _help(ctx: commands.Context):
    countdown = 5
    msg = await ctx.reply(
        f"{HELP_TEXT}\n\n[This message will go away in {countdown} seconds]",
        mention_author=False
    )
    # delete the user's help invocation
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

    try:
        for t in range(countdown - 1, -1, -1):
            await asyncio.sleep(1)
            await msg.edit(content=f"{HELP_TEXT}\n\n[This message will go away in {t} seconds]")
    except discord.HTTPException:
        pass
    finally:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass

@bot.command(name="rip")
async def rip(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        return await ctx.reply("Usage: `*rip <link>`", mention_author=False)
    if not ok_domain(link):
        return await ctx.reply("Unsupported link. Try YouTube or SoundCloud.", mention_author=False)

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        path, name = await download_to_mp3(link, tmpdir)
        await status.edit(content="📤 Uploading…")
        await send_and_cleanup(ctx, path, name, to_dm=False)
        try:
            await status.delete()
        except discord.HTTPException:
            pass
    except Exception as e:
        try:
            await status.edit(content=f"❌ Rip failed: `{e}`")
        except discord.HTTPException:
            await ctx.reply(f"❌ Rip failed: `{e}`", mention_author=False)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@bot.command(name="ripdm")
async def ripdm(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        return await ctx.reply("Usage: `*ripdm <link>`", mention_author=False)
    if not ok_domain(link):
        return await ctx.reply("Unsupported link. Try YouTube or SoundCloud.", mention_author=False)

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        path, name = await download_to_mp3(link, tmpdir)
        await status.edit(content="📤 Uploading to DM…")
        await send_and_cleanup(ctx, path, name, to_dm=True)
        try:
            await status.delete()
        except discord.HTTPException:
            pass
    except Exception as e:
        try:
            await status.edit(content=f"❌ Rip failed: `{e}`")
        except discord.HTTPException:
            await ctx.reply(f"❌ Rip failed: `{e}`", mention_author=False)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ---------- startup ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    print("Ready.")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment.")
    bot.run(TOKEN)
