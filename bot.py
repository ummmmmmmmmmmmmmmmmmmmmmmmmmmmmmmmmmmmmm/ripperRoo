# bot.py
import os, re, tempfile, asyncio, shutil, subprocess
from typing import Tuple, Optional

import discord
from discord.ext import commands
import yt_dlp

# ===================== Config =====================
TOKEN = os.getenv("DISCORD_TOKEN")  # PowerShell: $env:DISCORD_TOKEN='...'
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")  # set if ffmpeg lives elsewhere
ALLOWED_DOMAINS = {"youtube.com", "www.youtube.com", "youtu.be", "music.youtube.com"}
# ===================================================

# Intents (message content required for custom text commands)
intents = discord.Intents.default()
intents.message_content = True

# We keep a prefix (unused for rip=/ripdm=) so :help still works if you want it.
bot = commands.Bot(command_prefix=":", intents=intents, help_command=None)

# ---- helpers ----
YTLINK = re.compile(r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s>]+)", re.IGNORECASE)

def _is_allowed_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        return any(host.endswith(d) for d in ALLOWED_DOMAINS)
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
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(kbps),
        }],
        "noplaylist": True,
        "retries": 3,
        "socket_timeout": 15,
        "ffmpeg_location": FFMPEG_BIN,  # <-- key bit so yt-dlp finds ffmpeg/ffprobe
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:  # guard against playlists
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

# === Core workers (kept as commands so we can reuse ctx & cooldowns) ===
@commands.cooldown(1, 20, commands.BucketType.user)
@bot.command(name="rip")
async def rip(ctx: commands.Context, url: Optional[str]=None):
    if not url:
        m = YTLINK.search(ctx.message.content)
        url = m.group(1) if m else None
    if not url or not _is_allowed_url(url):
        return await ctx.reply("Give me a valid YouTube link, e.g. `rip= https://youtu.be/...`")

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
                    return await ctx.reply(f"⚠️ File too large for this server (~{mb:.0f} MB). Try `ripdm= <url>`.")

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
        m = YTLINK.search(ctx.message.content)
        url = m.group(1) if m else None
    if not url or not _is_allowed_url(url):
        return await ctx.reply("Give me a valid YouTube link, e.g. `ripdm= https://youtu.be/...`")

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
                    return await ctx.reply("⚠️ Still too large to DM. Try a shorter video.")

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

# === Custom message-style command parser for "rip=" and "ripdm=" ===
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()

    # rip= <url>
    if content.lower().startswith("rip="):
        url = content[4:].strip()
        ctx = await bot.get_context(message)
        await rip(ctx, url=url)
        return

    # ripdm= <url>
    if content.lower().startswith("ripdm="):
        url = content[6:].strip()
        ctx = await bot.get_context(message)
        await ripdm(ctx, url=url)
        return

    # simple help keyword (optional)
    if content.lower().strip() in {"help", "rip help", "rip=help"}:
        await message.channel.send(
            "Usage:\n"
            "• `rip= <YouTube URL>` — upload MP3 here\n"
            "• `ripdm= <YouTube URL>` — DM the MP3 to you\n"
            "Make sure I have Send Messages + Attach Files permissions."
        )
        return

    # Allow any other (prefixed) commands you might add later
    await bot.process_commands(message)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN env var.")
    bot.run(TOKEN)
