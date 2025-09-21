# bot.py
import os, re, tempfile, asyncio, shutil
from typing import Optional, Tuple, List
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands
import yt_dlp

# ===================== Config =====================
TOKEN = os.getenv("DISCORD_TOKEN")  # PowerShell: $env:DISCORD_TOKEN='...'
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")

# Batching caps (change with env vars if your server has higher limits)
MAX_ATTACHMENTS_PER_MESSAGE = int(os.getenv("MAX_ATTACHMENTS_PER_MESSAGE", "10"))
MAX_BATCH_BYTES = int(os.getenv("MAX_BATCH_BYTES", str(24 * 1024 * 1024)))  # ~24 MiB

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

# ---------- playlist detection ----------
def detect_playlist(link: str) -> Tuple[bool, str]:
    """
    Returns (is_playlist, provider) where provider in {"youtube","soundcloud","spotify","unknown"}.
    """
    try:
        u = urlparse(link)
        host = (u.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]

        # YouTube: list= param or /playlist path
        if host in {"youtube.com", "music.youtube.com", "m.youtube.com"}:
            qs = parse_qs(u.query or "")
            if "list" in qs or u.path.startswith("/playlist"):
                return True, "youtube"
        if host == "youtu.be":
            qs = parse_qs(u.query or "")
            if "list" in qs:
                return True, "youtube"

        # SoundCloud: /sets/ is a playlist
        if "soundcloud.com" in host and "/sets/" in (u.path or ""):
            return True, "soundcloud"

        # Spotify: not supported by this bot
        if host == "open.spotify.com" and "/playlist/" in (u.path or ""):
            return True, "spotify"

        return False, "unknown"
    except Exception:
        return False, "unknown"

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
        "outtmpl": os.path.join(tmpdir, "%(playlist_index)03d - %(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "noprogress": True,
        "quiet": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ],
        "ffmpeg_location": FFMPEG_BIN if FFMPEG_BIN else None,
        "yesplaylist": True,   # allow playlists
    }

def _resolve_outpath(ydl: yt_dlp.YoutubeDL, entry: dict, fallback_exts=("mp3","m4a","webm","opus")) -> str:
    # Prefer requested_downloads if present
    if "requested_downloads" in entry and entry["requested_downloads"]:
        return entry["requested_downloads"][0]["filepath"]
    base = ydl.prepare_filename(entry)
    root, _ = os.path.splitext(base)
    # Prefer .mp3 (postprocessor), but try some fallbacks just in case
    for ext in fallback_exts:
        candidate = f"{root}.{ext}"
        if os.path.exists(candidate):
            return candidate
    # Fallback to root.mp3 even if missing (caller may still find it)
    return f"{root}.mp3"

async def download_all_to_mp3(link: str, tmpdir: str) -> List[Tuple[str, str]]:
    """
    Downloads single videos or whole playlists, returns [(filepath, display_name), ...] in order.
    """
    items: List[Tuple[str, str]] = []
    with yt_dlp.YoutubeDL(ydl_opts(tmpdir)) as ydl:
        info = ydl.extract_info(link, download=True)

        if "entries" in info and info["entries"]:
            # Playlist: keep input order
            for ent in info["entries"]:
                if not ent:
                    continue
                path = _resolve_outpath(ydl, ent)
                title = ent.get("title") or "audio"
                # normalize to .mp3 if present
                root, _ = os.path.splitext(path)
                mp3 = root + ".mp3"
                items.append((mp3 if os.path.exists(mp3) else path, f"{title}.mp3"))
        else:
            # Single item
            path = _resolve_outpath(ydl, info)
            title = info.get("title") or "audio"
            root, _ = os.path.splitext(path)
            mp3 = root + ".mp3"
            items.append((mp3 if os.path.exists(mp3) else path, f"{title}.mp3"))

    return items

async def send_files_batched(
    ctx: commands.Context,
    files: List[Tuple[str, str]],
    *,
    to_dm: bool,
    attribution: Optional[str]
):
    """
    Sends files in efficient batches under Discord limits, preserving order.
    Uses MAX_ATTACHMENTS_PER_MESSAGE and MAX_BATCH_BYTES.
    """
    # Build batches by count and total bytes
    batches: List[List[Tuple[str, str]]] = []
    cur: List[Tuple[str, str]] = []
    cur_bytes = 0

    def flush():
        nonlocal cur, cur_bytes
        if cur:
            batches.append(cur)
            cur = []
            cur_bytes = 0

    for path, name in files:
        size = os.path.getsize(path) if os.path.exists(path) else 0
        # If adding this file would exceed limits, flush current batch first
        if (len(cur) >= MAX_ATTACHMENTS_PER_MESSAGE) or (cur and cur_bytes + size > MAX_BATCH_BYTES):
            flush()
        # If single file itself exceeds limit, put it alone (may still fail if > per-file limit)
        if not cur and size > MAX_BATCH_BYTES:
            batches.append([(path, name)])
            continue
        cur.append((path, name))
        cur_bytes += size
    flush()

    # Send each batch
    total = len(batches)
    for idx, batch in enumerate(batches, start=1):
        files_payload = [discord.File(p, filename=n) for (p, n) in batch]
        content = None
        if ctx.guild is not None and attribution:
            suffix = f" — part {idx}/{total}" if total > 1 else ""
            content = attribution + suffix
        try:
            if to_dm:
                dm = await ctx.author.create_dm()
                await dm.send(content=content, files=files_payload)
            else:
                await ctx.send(content=content, files=files_payload)
        except discord.HTTPException as e:
            # If a batch fails (e.g., byte cap still too high), retry by halving the batch
            if len(batch) > 1:
                mid = len(batch) // 2
                await send_files_batched(ctx, batch[:mid], to_dm=to_dm, attribution=attribution)
                await send_files_batched(ctx, batch[mid:], to_dm=to_dm, attribution=attribution)
            else:
                await ctx.reply(f"❌ Failed to upload `{batch[0][1]}`: `{e}`", mention_author=False)

async def send_and_cleanup(ctx: commands.Context, files: List[Tuple[str, str]], to_dm: bool = False):
    """
    Sends one or many files. In servers, adds 'ripped by: DisplayName(Username)'.
    After a successful send, deletes the invoking message (if permitted).
    """
    attribution = None
    if not to_dm and ctx.guild is not None:
        display = ctx.author.display_name
        uname = ctx.author.name
        attribution = f"ripped by: {display}({uname})"

    await send_files_batched(ctx, files, to_dm=to_dm, attribution=attribution)

    # Try to delete the invoking message
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

# ---------- UI: confirmation view ----------
class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This prompt isn’t for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Proceed (download all)", style=discord.ButtonStyle.danger)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="✅ Proceeding…", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❎ Cancelled.", view=self)
        self.stop()

# ---------- commands ----------
@bot.command(name="help")
async def _help(ctx: commands.Context):
    countdown = 5
    msg = await ctx.reply(
        f"{HELP_TEXT}\n\n[This message will go away in {countdown} seconds]",
        mention_author=False
    )
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

async def maybe_confirm_playlist(ctx: commands.Context, link: str) -> bool:
    is_pl, provider = detect_playlist(link)
    if not is_pl:
        return True

    if provider == "spotify":
        await ctx.reply("⚠️ That looks like a Spotify playlist. Spotify downloads aren’t supported.", mention_author=False)
        return False

    view = ConfirmView(ctx.author.id, timeout=30)
    provider_nice = "YouTube" if provider == "youtube" else "SoundCloud"
    prompt = await ctx.reply(
        f"⚠️ The link you sent appears to be a **{provider_nice} playlist**.\n"
        f"This will attempt to download **all tracks**. Are you sure?",
        view=view, mention_author=False
    )
    await view.wait()
    if view.value is True:
        try:
            await prompt.edit(content="✅ Confirmed. Starting download…", view=None)
        except discord.HTTPException:
            pass
        return True
    else:
        try:
            await prompt.edit(content="❎ Cancelled.", view=None)
        except discord.HTTPException:
            pass
        return False

@bot.command(name="rip")
async def rip(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        return await ctx.reply("Usage: `*rip <link>`", mention_author=False)
    if not ok_domain(link):
        is_pl, provider = detect_playlist(link)
        if provider == "spotify":
            return await ctx.reply("⚠️ Spotify playlists aren’t supported.", mention_author=False)
        return await ctx.reply("Unsupported link. Try YouTube or SoundCloud.", mention_author=False)

    # Confirm playlists
    confirmed = await maybe_confirm_playlist(ctx, link)
    if not confirmed:
        return

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        items = await download_all_to_mp3(link, tmpdir)  # [(path,name), ...]
        await status.edit(content=f"📤 Uploading… ({len(items)} track{'s' if len(items)!=1 else ''})")
        await send_and_cleanup(ctx, items, to_dm=False)
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
        is_pl, provider = detect_playlist(link)
        if provider == "spotify":
            return await ctx.reply("⚠️ Spotify playlists aren’t supported.", mention_author=False)
        return await ctx.reply("Unsupported link. Try YouTube or SoundCloud.", mention_author=False)

    confirmed = await maybe_confirm_playlist(ctx, link)
    if not confirmed:
        return

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        items = await download_all_to_mp3(link, tmpdir)
        await status.edit(content=f"📤 Uploading to DM… ({len(items)} track{'s' if len(items)!=1 else ''})")
        await send_and_cleanup(ctx, items, to_dm=True)
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
