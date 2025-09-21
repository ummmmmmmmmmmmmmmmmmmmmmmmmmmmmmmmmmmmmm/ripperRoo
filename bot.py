# bot.py
import os, re, tempfile, asyncio, shutil
from typing import Optional
from urllib.parse import urlparse

import discord
from discord.ext import commands
import yt_dlp

# ===================== Config =====================
TOKEN = os.getenv("DISCORD_TOKEN")  # PowerShell: $env:DISCORD_TOKEN='...'
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")  # set if ffmpeg lives elsewhere

# Where we allow links from
ALLOWED_DOMAINS = {
    "youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be",
    "soundcloud.com", "www.soundcloud.com", "on.soundcloud.com"
}
# ===================================================

# Intents (message content needed for text commands)
intents = discord.Intents.default()
intents.message_content = True

# Use "*" as the prefix so users type "*rip <link>" and "*help"
bot = commands.Bot(command_prefix="*", intents=intents, help_command=None)

# ---------- helpers ----------
def ok_domain(link: str) -> bool:
    try:
        host = urlparse(link).hostname or ""
        # strip leading "www."
        if host.startswith("www."):
            host = host[4:]
        return host in ALLOWED_DOMAINS
    except Exception:
        return False

def ydl_opts(tmpdir: str) -> dict:
    # Save best audio, convert to mp3 via ffmpeg
    return {
        "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "noprogress": True,
        "quiet": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ],
        # If ffmpeg isn't on PATH, point here
        "ffmpeg_location": FFMPEG_BIN if FFMPEG_BIN else None,
    }

async def download_to_mp3(link: str, tmpdir: str) -> tuple[str, str]:
    """
    Returns (filepath, display_name). Raises on failure.
    """
    with yt_dlp.YoutubeDL(ydl_opts(tmpdir)) as ydl:
        info = ydl.extract_info(link, download=True)
        # Resolve output path (handles playlists/single items)
        if "requested_downloads" in info and info["requested_downloads"]:
            path = info["requested_downloads"][0]["filepath"]
        else:
            # fallback: prepare base name and swap to .mp3
            base = ydl.prepare_filename(info)
            root, _ = os.path.splitext(base)
            path = root + ".mp3"
        title = info.get("title") or "audio"
        # Normalize to .mp3 extension for sending name
        if not path.lower().endswith(".mp3"):
            root, _ = os.path.splitext(path)
            candidate = root + ".mp3"
            if os.path.exists(candidate):
                path = candidate
        return path, f"{title}.mp3"

async def send_and_cleanup(ctx: commands.Context, file_path: str, file_name: str, to_dm: bool = False):
    """
    Sends the file (channel or DM). If successful, attempts to delete the user's command message.
    """
    # Actually send the file
    if to_dm:
        dm = await ctx.author.create_dm()
        await dm.send(file=discord.File(file_path, filename=file_name))
        ack = await ctx.reply("📩 Sent to your DMs.", mention_author=False)
    else:
        await ctx.send(file=discord.File(file_path, filename=file_name))
        ack = None

    # Try to delete the invoking message (requires Manage Messages)
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        # Lacking permission — optionally nudge in a quiet way
        if ack:
            try:
                await ack.edit(content="📩 Sent to your DMs. (I need **Manage Messages** to delete the command here.)")
            except discord.HTTPException:
                pass
    except discord.HTTPException:
        pass

# ---------- commands ----------
@bot.command(name="help")
async def _help(ctx: commands.Context):
    text = (
        "**ripperRoo — quick ripper**\n"
        "• `*rip <link>` — rip YouTube or SoundCloud audio and post it here\n"
        "• `*ripdm <link>` — rip and DM you the file\n\n"
        "_Tip: to auto-delete your command after the file sends, make sure my role has **Manage Messages** in this channel._"
    )
    await ctx.reply(text, mention_author=False)

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
        # Clean up status message too (optional)
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
