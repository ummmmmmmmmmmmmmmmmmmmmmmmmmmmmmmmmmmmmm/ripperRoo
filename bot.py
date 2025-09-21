# bot.py
import os, re, tempfile, asyncio, shutil, zipfile
from typing import Optional, Tuple, List
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands
import yt_dlp

# ===================== Config =====================
TOKEN = os.getenv("DISCORD_TOKEN")  # PowerShell: $env:DISCORD_TOKEN='...'
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")

# Per-file upload cap (Discord default ~25 MiB for many servers; tune as needed)
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(24 * 1024 * 1024)))  # ~24 MiB

# Optional: soft guess for per-message cap. We first try "all-in-one-message" regardless;
# if Discord rejects, we automatically fall back to multiple messages.
# You can leave this unset; it's just used for sizing batches on fallback.
MAX_MESSAGE_BYTES = int(os.getenv("MAX_MESSAGE_BYTES", str(24 * 1024 * 1024)))  # ~24 MiB

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
            if "list" in qs or (u.path or "").startswith("/playlist"):
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
    if "requested_downloads" in entry and entry["requested_downloads"]:
        return entry["requested_downloads"][0]["filepath"]
    base = ydl.prepare_filename(entry)
    root, _ = os.path.splitext(base)
    for ext in fallback_exts:
        candidate = f"{root}.{ext}"
        if os.path.exists(candidate):
            return candidate
    return f"{root}.mp3"

async def download_all_to_mp3(link: str, tmpdir: str) -> Tuple[List[Tuple[str, str]], str, bool]:
    """
    Downloads single videos or whole playlists.
    Returns ([(filepath, display_name), ...], collection_title, is_playlist)
    """
    items: List[Tuple[str, str]] = []
    with yt_dlp.YoutubeDL(ydl_opts(tmpdir)) as ydl:
        info = ydl.extract_info(link, download=True)

        if "entries" in info and info["entries"]:
            title = info.get("title") or "playlist"
            for ent in info["entries"]:
                if not ent:
                    continue
                path = _resolve_outpath(ydl, ent)
                track_title = ent.get("title") or "audio"
                root, _ = os.path.splitext(path)
                mp3 = root + ".mp3"
                items.append((mp3 if os.path.exists(mp3) else path, f"{track_title}.mp3"))
            return items, title, True
        else:
            title = info.get("title") or "audio"
            path = _resolve_outpath(ydl, info)
            root, _ = os.path.splitext(path)
            mp3 = root + ".mp3"
            items.append((mp3 if os.path.exists(mp3) else path, f"{title}.mp3"))
            return items, title, False

def _safe_base(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', '_', name).strip()

def chunk_by_size(paths: List[Tuple[str, str]], max_bytes: int) -> List[List[Tuple[str, str]]]:
    """Greedy chunking by total size, preserves order."""
    chunks: List[List[Tuple[str, str]]] = []
    cur: List[Tuple[str, str]] = []
    cur_bytes = 0
    def flush():
        nonlocal cur, cur_bytes
        if cur:
            chunks.append(cur)
            cur = []
            cur_bytes = 0
    for p, name in paths:
        size = os.path.getsize(p) if os.path.exists(p) else 0
        if cur and cur_bytes + size > max_bytes:
            flush()
        if not cur and size > max_bytes:
            chunks.append([(p, name)])
            continue
        cur.append((p, name))
        cur_bytes += size
    flush()
    return chunks

def make_zip_of_files(files: List[Tuple[str, str]], out_zip_path: str):
    with zipfile.ZipFile(out_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p, display_name in files:
            arcname = display_name
            try:
                zf.write(p, arcname=arcname)
            except FileNotFoundError:
                continue

async def send_files_batched(
    ctx: commands.Context,
    files: List[Tuple[str, str]],
    *,
    to_dm: bool,
    attribution: Optional[str]
):
    """Send one or more files, adding attribution in servers (one file per message)."""
    total = len(files)
    for idx, (path, name) in enumerate(files, start=1):
        content = None
        if ctx.guild is not None and attribution:
            suffix = f" — part {idx}/{total}" if total > 1 else ""
            content = attribution + suffix
        try:
            if to_dm:
                dm = await ctx.author.create_dm()
                await dm.send(content=content, file=discord.File(path, filename=name))
            else:
                await ctx.send(content=content, file=discord.File(path, filename=name))
        except discord.HTTPException as e:
            await ctx.reply(f"❌ Failed to upload `{name}`: `{e}`", mention_author=False)

async def send_zip_parts_prefer_single_message(
    ctx: commands.Context,
    zip_parts: List[Tuple[str, str]],
    *,
    to_dm: bool,
    attribution: Optional[str]
):
    """
    Try to send ALL zip parts in ONE message first.
    If Discord rejects (HTTPException), fall back to minimal number of messages by total size.
    """
    files_payload = [discord.File(p, filename=n) for (p, n) in zip_parts]
    single_msg_content = None
    if ctx.guild is not None and attribution:
        single_msg_content = f"{attribution} — zip ({len(zip_parts)} part{'s' if len(zip_parts)!=1 else ''})"
    try:
        if to_dm:
            dm = await ctx.author.create_dm()
            await dm.send(content=single_msg_content, files=files_payload)
        else:
            await ctx.send(content=single_msg_content, files=files_payload)
        return  # success in one message
    except discord.HTTPException:
        pass  # fall back to batching below

    # Fallback: split into the smallest number of messages by MAX_MESSAGE_BYTES
    batches: List[List[Tuple[str, str]]] = []
    cur: List[Tuple[str, str]] = []
    cur_bytes = 0
    def flush():
        nonlocal cur, cur_bytes
        if cur:
            batches.append(cur)
            cur = []
            cur_bytes = 0

    for p, n in zip_parts:
        size = os.path.getsize(p) if os.path.exists(p) else 0
        if cur and cur_bytes + size > MAX_MESSAGE_BYTES:
            flush()
        if not cur and size > MAX_MESSAGE_BYTES:
            batches.append([(p, n)])
            continue
        cur.append((p, n))
        cur_bytes += size
    flush()

    total = len(batches)
    for i, batch in enumerate(batches, start=1):
        batch_payload = [discord.File(p, filename=n) for (p, n) in batch]
        content = None
        if ctx.guild is not None and attribution:
            content = f"{attribution} — part {i}/{total}"
        try:
            if to_dm:
                dm = await ctx.author.create_dm()
                await dm.send(content=content, files=batch_payload)
            else:
                await ctx.send(content=content, files=batch_payload)
        except discord.HTTPException as e:
            # If even a batch fails, fall back to one file per message for that batch
            await send_files_batched(ctx, batch, to_dm=to_dm, attribution=attribution)

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

    # Default path: one file per message
    await send_files_batched(ctx, files, to_dm=to_dm, attribution=attribution)

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

    @discord.ui.button(label="Proceed (download all & zip)", style=discord.ButtonStyle.danger)
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

    # LIVE countdown, then delete prompt, THEN delete the user's *help invoke
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
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

async def maybe_confirm_playlist(ctx: commands.Context, link: str) -> Tuple[bool, Optional[str], Optional[discord.Message]]:
    """
    Returns (confirmed, provider, prompt_message)
    """
    is_pl, provider = detect_playlist(link)
    if not is_pl:
        return True, None, None

    if provider == "spotify":
        await ctx.reply("⚠️ That looks like a Spotify playlist. Spotify downloads aren’t supported.", mention_author=False)
        return False, None, None

    view = ConfirmView(ctx.author.id, timeout=30)
    provider_nice = "YouTube" if provider == "youtube" else "SoundCloud"
    prompt = await ctx.reply(
        f"⚠️ The link you sent appears to be a **{provider_nice} playlist**.\n"
        f"This will attempt to download **all tracks** and send them as a **.zip**. Proceed?",
        view=view, mention_author=False
    )
    await view.wait()
    if view.value is True:
        try:
            await prompt.edit(content="✅ Confirmed. Starting download…", view=None)
        except discord.HTTPException:
            pass
        return True, provider, prompt
    else:
        try:
            await prompt.edit(content="❎ Cancelled.", view=None)
        except discord.HTTPException:
            pass
        return False, provider, prompt

def build_zip_parts(tmpdir: str, bundle_name: str, items: List[Tuple[str,str]]) -> List[Tuple[str, str]]:
    """
    Build one or more zip files under MAX_FILE_BYTES from items (path, display_name).
    Returns list of (zip_path, zip_display_name).
    """
    safe = _safe_base(bundle_name) or "playlist"
    chunks = chunk_by_size(items, MAX_FILE_BYTES)
    out: List[Tuple[str, str]] = []
    if len(chunks) == 1:
        zip_path = os.path.join(tmpdir, f"{safe}.zip")
        make_zip_of_files(chunks[0], zip_path)
        if os.path.getsize(zip_path) > MAX_FILE_BYTES and len(chunks[0]) > 1:
            os.remove(zip_path)
            half = len(chunks[0]) // 2
            for i, sub in enumerate([chunks[0][:half], chunks[0][half:]], start=1):
                part_path = os.path.join(tmpdir, f"{safe}_part{i}.zip")
                make_zip_of_files(sub, part_path)
                out.append((part_path, os.path.basename(part_path)))
            return out
        out.append((zip_path, os.path.basename(zip_path)))
        return out
    else:
        for i, chunk in enumerate(chunks, start=1):
            zip_path = os.path.join(tmpdir, f"{safe}_part{i}.zip")
            make_zip_of_files(chunk, zip_path)
            out.append((zip_path, os.path.basename(zip_path)))
        return out

@bot.command(name="rip")
async def rip(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        return await ctx.reply("Usage: `*rip <link>`", mention_author=False)
    if not ok_domain(link):
        is_pl, provider = detect_playlist(link)
        if provider == "spotify":
            return await ctx.reply("⚠️ Spotify playlists aren’t supported.", mention_author=False)
        return await ctx.reply("Unsupported link. Try YouTube or SoundCloud.", mention_author=False)

    confirmed, provider, prompt_msg = await maybe_confirm_playlist(ctx, link)
    if not confirmed:
        return

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        items, title, is_pl = await download_all_to_mp3(link, tmpdir)
        if is_pl:
            await status.edit(content=f"🗜️ Zipping… ({len(items)} track{'s' if len(items)!=1 else ''})")
            zip_parts = build_zip_parts(tmpdir, title, items)  # [(zip_path, zip_name)]
            await status.edit(content=f"📤 Uploading zip{'s' if len(zip_parts)>1 else ''}…")

            # Prefer a single message for ALL zip parts
            attribution = f"ripped by: {ctx.author.display_name}({ctx.author.name})" if ctx.guild else None
            await send_zip_parts_prefer_single_message(ctx, zip_parts, to_dm=False, attribution=attribution)

            # Delete the playlist confirmation prompt after uploads finish
            if prompt_msg:
                try:
                    await prompt_msg.delete()
                except discord.HTTPException:
                    pass
        else:
            await status.edit(content="📤 Uploading…")
            await send_and_cleanup(ctx, items, to_dm=False)

        try:
            await status.delete()
        except discord.HTTPException:
            pass
        # Delete the invoking message last for the standard path (handled inside send_and_cleanup for singles)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
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

    confirmed, provider, prompt_msg = await maybe_confirm_playlist(ctx, link)
    if not confirmed:
        return

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        items, title, is_pl = await download_all_to_mp3(link, tmpdir)
        if is_pl:
            await status.edit(content=f"🗜️ Zipping… ({len(items)} track{'s' if len(items)!=1 else ''})")
            zip_parts = build_zip_parts(tmpdir, title, items)

            await status.edit(content=f"📤 Uploading to DM…")
            attribution = None  # DMs don't get the "ripped by" banner
            await send_zip_parts_prefer_single_message(ctx, zip_parts, to_dm=True, attribution=attribution)

            if prompt_msg:
                try:
                    await prompt_msg.delete()
                except discord.HTTPException:
                    pass
        else:
            await status.edit(content="📤 Uploading to DM…")
            await send_and_cleanup(ctx, items, to_dm=True)

        try:
            await status.delete()
        except discord.HTTPException:
            pass
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
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
