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

# Per-file upload cap check used to warn before attempting upload (best-effort).
# NOTE: Discord's real limit depends on server/Nitro. We'll still try to send;
# if Discord rejects, we catch and report cleanly. You can raise this if your server allows larger.
MAX_FILE_BYTES_HINT = int(os.getenv("MAX_FILE_BYTES_HINT", str(25 * 1024 * 1024)))  # ~25 MiB

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
    """Returns (is_playlist, provider) where provider in {'youtube','soundcloud','spotify','unknown'}."""
    try:
        u = urlparse(link)
        host = (u.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]

        if host in {"youtube.com", "music.youtube.com", "m.youtube.com"}:
            qs = parse_qs(u.query or "")
            if "list" in qs or (u.path or "").startswith("/playlist"):
                return True, "youtube"
        if host == "youtu.be":
            qs = parse_qs(u.query or "")
            if "list" in qs:
                return True, "youtube"

        if "soundcloud.com" in host and "/sets/" in (u.path or ""):
            return True, "soundcloud"

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
        "yesplaylist": True,
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
    """Downloads single videos or whole playlists. Returns ([(filepath, display_name)], title, is_playlist)."""
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
    return re.sub(r'[\\/:*?"<>|]+', '_', name).strip() or "playlist"

def make_single_zip(files: List[Tuple[str, str]], out_zip_path: str):
    """Create one zip at out_zip_path including (path, arcname) pairs."""
    with zipfile.ZipFile(out_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p, display_name in files:
            try:
                zf.write(p, arcname=display_name)
            except FileNotFoundError:
                continue

async def send_single_file_with_banner(
    ctx: commands.Context,
    filepath: str,
    filename: str,
    *,
    to_dm: bool,
    attribution: Optional[str],
    trailing_text: Optional[str] = None,
):
    """Sends exactly one attachment with optional banner + trailing text (e.g., source link)."""
    parts = []
    if ctx.guild and attribution:
        parts.append(attribution)
    if trailing_text:
        parts.append(trailing_text)
    content = "\n".join(parts) if parts else None

    try:
        if to_dm:
            dm = await ctx.author.create_dm()
            await dm.send(content=content, file=discord.File(filepath, filename=filename))
        else:
            await ctx.send(content=content, file=discord.File(filepath, filename=filename))
    except discord.HTTPException as e:
        # Report cleanly that single-zip upload failed (likely size cap)
        hint = ""
        try:
            size = os.path.getsize(filepath)
            hint = f" (size ~{size/1024/1024:.1f} MiB; server cap may be lower)"
        except Exception:
            pass
        await ctx.reply(f"❌ Failed to upload single zip{hint}. Discord likely rejected it due to file size limits.", mention_author=False)
        raise e

async def delete_invoke_safely(ctx: commands.Context):
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

    @discord.ui.button(label="Proceed (download all & single ZIP)", style=discord.ButtonStyle.danger)
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
    # countdown -> delete prompt -> then delete user's invoke
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
        await delete_invoke_safely(ctx)

async def maybe_confirm_playlist(ctx: commands.Context, link: str) -> Tuple[bool, Optional[str], Optional[discord.Message]]:
    """Returns (confirmed, provider, prompt_message)."""
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
        f"This will download **all tracks** and send them as a **single .zip**. Proceed?",
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
    if confirmed is False:
        return

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        items, title, is_pl = await download_all_to_mp3(link, tmpdir)
        if is_pl:
            await status.edit(content=f"🗜️ Zipping… ({len(items)} track{'s' if len(items)!=1 else ''})")
            safe = _safe_base(title)
            zip_path = os.path.join(tmpdir, f"{safe}.zip")
            make_single_zip(items, zip_path)

            # Optional: warn if obviously larger than common caps (we'll still try)
            try:
                if os.path.getsize(zip_path) > MAX_FILE_BYTES_HINT:
                    await status.edit(content="🗜️ Zipping… (note: zip may exceed this server's upload cap)")
            except Exception:
                pass

            await status.edit(content=f"📤 Uploading zip…")
            attribution = f"ripped by: {ctx.author.display_name}({ctx.author.name})" if ctx.guild else None
            trailing = link  # raw URL so Discord embeds
            await send_single_file_with_banner(
                ctx, zip_path, os.path.basename(zip_path),
                to_dm=False, attribution=attribution, trailing_text=trailing
            )

            # Delete the confirmation prompt (after upload)
            if prompt_msg:
                try:
                    await prompt_msg.delete()
                except discord.HTTPException:
                    pass
            await delete_invoke_safely(ctx)
        else:
            # Single item: send the audio as-is
            await status.edit(content="📤 Uploading…")
            attribution = f"ripped by: {ctx.author.display_name}({ctx.author.name})" if ctx.guild else None
            (p, n) = items[0]
            await send_single_file_with_banner(ctx, p, n, to_dm=False, attribution=attribution, trailing_text=None)
            await delete_invoke_safely(ctx)

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

    confirmed, provider, prompt_msg = await maybe_confirm_playlist(ctx, link)
    if confirmed is False:
        return

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        items, title, is_pl = await download_all_to_mp3(link, tmpdir)
        if is_pl:
            await status.edit(content=f"🗜️ Zipping… ({len(items)} track{'s' if len(items)!=1 else ''})")
            safe = _safe_base(title)
            zip_path = os.path.join(tmpdir, f"{safe}.zip")
            make_single_zip(items, zip_path)

            await status.edit(content=f"📤 Uploading to DM…")
            # DMs: no attribution banner; still include source link
            await send_single_file_with_banner(
                ctx, zip_path, os.path.basename(zip_path),
                to_dm=True, attribution=None, trailing_text=link
            )

            if prompt_msg:
                try:
                    await prompt_msg.delete()
                except discord.HTTPException:
                    pass
            await delete_invoke_safely(ctx)
        else:
            await status.edit(content="📤 Uploading to DM…")
            (p, n) = items[0]
            await send_single_file_with_banner(ctx, p, n, to_dm=True, attribution=None, trailing_text=None)
            await delete_invoke_safely(ctx)

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
