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
        file=discord.File(file_path, filen_
