# bot.py
import os, re, tempfile, asyncio, shutil, subprocess
from typing import Tuple, Optional

import discord
from discord.ext import commands
import yt_dlp

# ===================== Config =====================
TOKEN = os.getenv("DISCORD_TOKEN")  # PowerShell: $env:DISCORD_TOKEN='...'
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")  # set if ffmpeg lives elsewhere

# Allow YouTube and SoundCloud (covers subdomains like music.youtube.com, m.soundcloud.com, on.soundcloud.com)
ALLOWED_DOMAINS = {"youtube.com", "youtu.be", "soundcloud.com"}
# ===================================================

# Intents (message content required for text commands)
intents = discord.Intents.default()
intents.message_content = True

# Use "*" as the command prefix; we’ll provide our own help command
bot = commands.Bot(command_prefix="*", intents=intents, help_command=None)

# ---- helpers ----
URL_RE = re.compile(r"(https?://[^\s>]+)", re.IGNORECASE)

def _is_allowed_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.split(":", 1)[0].lower()
        for d in ALLOWED_DOMAINS:
            if host == d or host.endswith("." + d):
                return True
        return False
    except Exception:
        return False

def _slugify(name: str) -> str:
    name = re.sub(r"[^\w\s-]", "", name).strip()
    name = re.sub(r"[-\s]+", "-", name)
    return name or "audio"

def _download_mp3(url: str, tempdir: str, kbps: int = 192) -> Tuple[str, str]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(tempdir, "%(id)s.%(ext)s"),
        "format": "bestaudio/best",   # works for YouTube + SoundCloud
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(kbps),
        }],
        "noplaylist": True,           # ignore playlists/sets; take a single item
        "retries": 3,
        "socket_timeout": 15,
        "ffmpeg_location": FFMPEG_BIN,  # where yt-dlp finds ffmpeg/ffprobe
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:  # guard against accidental playlists
            info = info["entries"][0]
        title = info.get("title", "audio")
        vid_id = info.get("id")
        mp3_path = os.path.join(tempdir, f"{vid_id}.mp3")
        if not os.path.exists(mp3_path):
            cands = [os.path.join(tempdir, p) for p in os.listdir(tempdir) if p.lower().endswith(".mp3")]
            if not cands:
                raise RuntimeError("MP3 not produced (check ffmpeg).")
            mp3_path = max(cands, key=os.path.getmtime)
        return mp3_path, title

def _reencode_mp3(src: str, dst: str, bitrate_kbps: int) -> None:
    subprocess.run(
        [os.path.join(FFMPEG_BIN, "ffmpeg") if os.name == "nt" else "ffmpeg",
         "-y", "-i", src, "-b:a", f"{bitrate_kbps}k", dst],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )

async def _send_audio(ctx: commands.Context, file_path: str, title: str, to_dm: bool=False):
    filename = f"{_slugify(title)}.mp3"
    dest = ctx.author if to_dm else ctx.channel
    await dest.send(
        content=f"Here you go: **{title}**" + (" (sent via DM)" if to_dm else ""),
        file=discord.File(file_path, filename=filename)
    )

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")

# === Commands ===
@commands.cooldown(1, 20, commands.BucketType.user)
@bot.command(name="rip")
async def rip(ctx: commands.Context, url: Optional[str]=None):
    # If no arg, try to scrape first URL from the message
    if not url:
        m = URL_RE.search(ctx.message.content)
        url = m.group(1) if m else None

    if not url or not _is_allowed_url(url):
        return await ctx.reply("Give me a valid YouTube or SoundCloud link, e.g. `*rip https://youtu.be/...` or `*rip https://soundcloud.com/...`")

    async with ctx.typing():
        tempdir = tempfile.mkdtemp(prefix="rip_")
        try:
            loop = asyncio.get_event_loop()
            mp3_path, title = await loop.run_in_executor(None, lambda: _download_mp3(url, tempdir, 192))

            limit = (ctx.guild.filesize_limit if ctx.guild else 8 * 1024 * 1024) or (8 * 1024 * 1024)
            if os.path.getsize(mp3_path) > limit:
                for br in (128, 96, 64, 48):
                    cand = os.path.join(tempdir, f"re_{br}.mp3")
                    await loop.run_in_executor(None, lambda: _reencode_mp3(mp3_path, cand, br))
                    if os.path.getsize(cand) < limit:
                        mp3_path = cand
                        break
                else:
                    mb = limit / (1024*1024)
                    return await ctx.reply(f"⚠️ File too large for this server (~{mb:.0f} MB). Try `*ripdm <url>`.")

            await _send_audio(ctx, mp3_path, title, to_dm=False)
        except commands.CommandOnCooldown as c:
            await ctx.reply(f"⏳ Cooldown: try again in {c.retry_after:.1f}s.")
        except Exception as e:
            await ctx.reply(f"❌ Rip failed: `{e}`")
        finally:
            try: shutil.rmtree(tempdir)
            except Exception: pass

@commands.cooldown(1, 20, commands.BucketType.user)
@bot.command(name="ripdm")
async def ripdm(ctx: commands.Context, url: Optional[str]=None):
    if not url:
        m = URL_RE.search(ctx.message.content)
        url = m.group(1) if m else None
    if not url or not _is_allowed_url(url):
        return await ctx.reply("Give me a valid YouTube or SoundCloud link, e.g. `*ripdm https://youtu.be/...` or `*ripdm https://soundcloud.com/...`")

    async with ctx.typing():
        tempdir = tempfile.mkdtemp(prefix="rip_")
        try:
            loop = asyncio.get_event_loop()
            mp3_path, title = await loop.run_in_executor(None, lambda: _download_mp3(url, tempdir, 128))

            limit = 8 * 1024 * 1024
            if os.path.getsize(mp3_path) > limit:
                for br in (96, 64, 48, 32):
                    cand = os.path.join(tempdir, f"re_{br}.mp3")
                    await loop.run_in_executor(None, lambda: _reencode_mp3(mp3_path, cand, br))
                    if os.path.getsize(cand) < limit:
                        mp3_path = cand
                        break
                else:
                    return await ctx.reply("⚠️ Still too large to DM. Try a shorter track/video.")

            try: await ctx.author.create_dm()
            except Exception: pass
            await _send_audio(ctx, mp3_path, title, to_dm=True)
        except commands.CommandOnCooldown as c:
            await ctx.reply(f"⏳ Cooldown: try again in {c.retry_after:.1f}s.")
        except Exception as e:
            await ctx.reply(f"❌ Rip failed: `{e}`")
        finally:
            try: shutil.rmtree(tempdir)
            except Exception: pass

@bot.command(name="help")
async def _help(ctx: commands.Context):
    await ctx.send(
        "Usage:\n"
        "• `*rip <YouTube or SoundCloud URL>` — upload MP3 here\n"
        "• `*ripdm <YouTube or SoundCloud URL>` — DM the MP3 to you\n"
        "Notes: I need **Send Messages** and **Attach Files** permissions. "
        "Large files will be down-bitrated; DM has an 8MB limit."
    )

# No custom on_message needed now—prefix commands handle everything
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN env var.")
    bot.run(TOKEN)
