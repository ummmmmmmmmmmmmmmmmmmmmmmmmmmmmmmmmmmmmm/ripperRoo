# bot.py
import os, re, tempfile, asyncio, shutil, zipfile, pathlib
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands
import yt_dlp

# ===================== Config =====================
TOKEN = os.getenv("DISCORD_TOKEN")  # PowerShell: $env:DISCORD_TOKEN='...'
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")

# Soft hint for whether a single file may exceed your server cap. We still try to upload.
MAX_FILE_BYTES_HINT = int(os.getenv("MAX_FILE_BYTES_HINT", str(25 * 1024 * 1024)))  # ~25 MiB
# ===================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="*", intents=intents, help_command=None)

HELP_TEXT = (
    "**ripperRoo — a Discord mp3 ripper created by d-rod**\n"
    "• `*rip <link>` — rip YouTube, SoundCloud, or Bandcamp audio and post it here\n"
    "• `*ripdm <link>` — rip and DM you the file\n\n"
    "_Tip: to auto-delete your command after sending files, give my role **Manage Messages** in this channel._"
)

# ---------- utilities ----------
async def countdown_delete_message(msg: discord.Message, seconds: int = 5, header: Optional[str] = None):
    """Live-edit a message to include a countdown, then delete it."""
    try:
        for t in range(seconds, 0, -1):
            prefix = f"{header}\n" if header else ""
            await msg.edit(content=f"{prefix}[This message will be deleted in {t}]")
            await asyncio.sleep(1)
    except discord.HTTPException:
        pass
    finally:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass

def provider_of(link: str) -> Tuple[str, str]:
    """Return (PrettyProvider, canonical link)."""
    try:
        u = urlparse(link)
        host = (u.hostname or "").lower()
        if host.startswith("www."): host = host[4:]
        if "soundcloud.com" in host: return "SoundCloud", link
        if "bandcamp.com" in host: return "Bandcamp", link
        if host in {"youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}: return "YouTube", link
        if host == "open.spotify.com": return "Spotify", link
        return "Source", link
    except Exception:
        return "Source", link

def detect_playlist(link: str) -> Tuple[bool, str]:
    """
    Returns (is_playlist, provider_key) where provider_key in
    {'youtube','soundcloud','bandcamp','spotify','unknown'}.
    """
    try:
        u = urlparse(link)
        host = (u.hostname or "").lower() if u.hostname else ""
        if host.startswith("www."): host = host[4:]

        # YouTube: list= param or /playlist path
        if host in {"youtube.com", "music.youtube.com", "m.youtube.com"}:
            qs = parse_qs(u.query or "")
            if "list" in qs or (u.path or "").startswith("/playlist"):
                return True, "youtube"
        if host == "youtu.be":
            qs = parse_qs(u.query or "")
            if "list" in qs: return True, "youtube"

        # SoundCloud: /sets/ = playlist
        if "soundcloud.com" in host and "/sets/" in (u.path or ""):
            return True, "soundcloud"

        # Bandcamp: /album/ = multi-track; /track/ = single
        if "bandcamp.com" in host:
            if "/album/" in (u.path or ""):
                return True, "bandcamp"
            return False, "bandcamp"

        # Spotify playlists not supported
        if host == "open.spotify.com" and "/playlist/" in (u.path or ""):
            return True, "spotify"

        return False, "unknown"
    except Exception:
        return False, "unknown"

def ok_domain(link: str) -> bool:
    """Allow YouTube, SoundCloud, Bandcamp."""
    try:
        host = (urlparse(link).hostname or "").lower()
        return (
            host.endswith("youtube.com") or host == "youtu.be" or
            host.endswith("soundcloud.com") or
            host.endswith("bandcamp.com")
        )
    except Exception:
        return False

def ydl_opts(tmpdir: str, include_thumbs: bool) -> dict:
    """Build yt_dlp options; thumbnails only when requested."""
    pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    opts = {
        "outtmpl": os.path.join(tmpdir, "%(playlist_index)03d - %(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "noprogress": True,
        "quiet": True,
        "ffmpeg_location": FFMPEG_BIN if FFMPEG_BIN else None,
        "yesplaylist": True,
    }
    if include_thumbs:
        opts["writethumbnail"] = True
        pp.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})  # valid; no exec_cmd
    opts["postprocessors"] = pp
    return opts

def _resolve_outpath(ydl: yt_dlp.YoutubeDL, entry: dict, fallback_exts=("mp3","m4a","webm","opus")) -> str:
    if "requested_downloads" in entry and entry["requested_downloads"]:
        return entry["requested_downloads"][0]["filepath"]
    base = ydl.prepare_filename(entry)
    root, _ = os.path.splitext(base)
    for ext in fallback_exts:
        candidate = f"{root}.{ext}"
        if os.path.exists(candidate): return candidate
    return f"{root}.mp3"

def _maybe_thumb_path_from_media_path(media_path: str) -> Optional[str]:
    root, _ = os.path.splitext(media_path)
    jpg = root + ".jpg"
    return jpg if os.path.exists(jpg) else None

async def probe_playlist_count(link: str) -> Optional[int]:
    """Try to get playlist length without downloading."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True, "extract_flat": True, "yesplaylist": True}) as ydl:
            info = ydl.extract_info(link, download=False)
            if isinstance(info, dict) and "entries" in info and info["entries"] is not None:
                return len([e for e in info["entries"] if e])
            return None
    except Exception:
        return None

async def download_all_to_mp3(link: str, tmpdir: str, *, include_thumbs: bool) -> Tuple[List[Tuple[str, str]], str, bool, List[Dict], Optional[str]]:
    """
    Download single or playlist.
    Returns:
      items: [(filepath, display_name)]
      collection_title: str
      is_playlist: bool
      meta: [{'index': int|None, 'title': str, 'thumb': Optional[str]}]
      cover_path: Optional[str]
    """
    items: List[Tuple[str, str]] = []
    meta: List[Dict] = []
    cover_path: Optional[str] = None

    with yt_dlp.YoutubeDL(ydl_opts(tmpdir, include_thumbs)) as ydl:
        info = ydl.extract_info(link, download=True)

        if "entries" in info and info["entries"]:
            title = info.get("title") or "playlist"
            for ent in info["entries"]:
                if not ent: continue
                path = _resolve_outpath(ydl, ent)
                track_title = ent.get("title") or "audio"
                root, _ = os.path.splitext(path)
                mp3 = root + ".mp3"
                final_path = mp3 if os.path.exists(mp3) else path
                items.append((final_path, f"{track_title}.mp3"))

                idx = ent.get("playlist_index")
                thumb = _maybe_thumb_path_from_media_path(final_path) if include_thumbs else None
                if not cover_path and thumb: cover_path = thumb
                meta.append({"index": idx, "title": track_title, "thumb": thumb})
            return items, title, True, meta, cover_path
        else:
            title = info.get("title") or "audio"
            path = _resolve_outpath(ydl, info)
            root, _ = os.path.splitext(path)
            mp3 = root + ".mp3"
            final_path = mp3 if os.path.exists(mp3) else path
            items.append((final_path, f"{title}.mp3"))

            thumb = _maybe_thumb_path_from_media_path(final_path) if include_thumbs else None
            if thumb: cover_path = thumb
            meta.append({"index": None, "title": title, "thumb": thumb})
            return items, title, False, meta, cover_path

def _safe_base(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "playlist"

def make_single_zip(files: List[Tuple[str, str]], out_zip_path: str, *, track_meta: List[Dict], cover_path: Optional[str]):
    """Create one zip at out_zip_path, including tracks, tracklist.txt, and optional artwork image."""
    ordered = sorted(
        enumerate(track_meta),
        key=lambda t: (t[1].get("index") is None, t[1].get("index") or (t[0] + 1))
    )
    tracklist_txt = "\n".join(
        [f"{(m.get('index') if m.get('index') is not None else i+1):02d}. {m.get('title','')}"
         for i, m in [(i, m) for i, m in ordered]]
    )

    with zipfile.ZipFile(out_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p, display_name in files:
            try: zf.write(p, arcname=display_name)
            except FileNotFoundError: continue
        zf.writestr("tracklist.txt", tracklist_txt)
        if cover_path and os.path.exists(cover_path):
            ext = pathlib.Path(cover_path).suffix.lower()
            # include as artwork file
            zf.write(cover_path, arcname=f"artwork{ext}")

def build_attribution(ctx: commands.Context, link: str) -> Optional[str]:
    """Server-only: 'ripped by: Display(Name)(Username) from [Provider](link)'."""
    if ctx.guild is None: return None
    display = ctx.author.display_name
    uname = ctx.author.name
    provider_pretty, canonical = provider_of(link)
    return f"ripped by: {display}({uname}) from [{provider_pretty}]({canonical})"

async def send_single_file_with_banner(
    ctx: commands.Context,
    filepath: str,
    filename: str,
    *,
    to_dm: bool,
    attribution: Optional[str],
):
    """Send exactly one attachment with optional attribution line."""
    content = attribution if attribution else None
    try:
        if to_dm:
            dm = await ctx.author.create_dm()
            await dm.send(content=content, file=discord.File(filepath, filename=filename))
        else:
            await ctx.send(content=content, file=discord.File(filepath, filename=filename))
    except discord.HTTPException as e:
        hint = ""
        try:
            size = os.path.getsize(filepath)
            hint = f" (size ~{size/1024/1024:.1f} MiB; server cap may be lower)"
        except Exception:
            pass
        err = await ctx.reply(f"❌ Failed to upload file{hint}. Discord likely rejected it due to file size limits.", mention_author=False)
        await countdown_delete_message(err, 5)
        raise e

async def send_many_try_one_message_then_fallback(
    ctx: commands.Context,
    files: List[Tuple[str, str]],
    *,
    to_dm: bool,
    attribution: Optional[str]
):
    """For small playlists (≤5): try to send all tracks in one message; if rejected, fall back to one per message."""
    try:
        payload = [discord.File(p, filename=n) for (p, n) in files]
        content = attribution if attribution else None
        if to_dm:
            dm = await ctx.author.create_dm()
            await dm.send(content=content, files=payload)
        else:
            await ctx.send(content=content, files=payload)
    except discord.HTTPException:
        for p, n in files:
            await send_single_file_with_banner(ctx, p, n, to_dm=to_dm, attribution=attribution)

async def delete_invoke_safely(ctx: commands.Context):
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

# ---------- Artwork choice ----------
class ArtChoiceView(discord.ui.View):
    """Ask whether to include Artwork (applies to singles and playlists)."""
    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.include_art: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This prompt isn’t for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Include Artwork", style=discord.ButtonStyle.primary)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.include_art = True
        for c in self.children: c.disabled = True
        await interaction.response.edit_message(content="🖼️ Including Artwork…", view=self)
        self.stop()

    @discord.ui.button(label="No Artwork", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.include_art = False
        for c in self.children: c.disabled = True
        await interaction.response.edit_message(content="🎵 Audio only…", view=self)
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
        for t in range(countdown - 1, -1, -1):
            await asyncio.sleep(1)
            await msg.edit(content=f"{HELP_TEXT}\n\n[This message will go away in {t} seconds]")
    except discord.HTTPException:
        pass
    finally:
        try: await msg.delete()
        except discord.HTTPException: pass
        await delete_invoke_safely(ctx)

async def maybe_notify_zip(ctx: commands.Context, link: str) -> None:
    """If playlist length > 5, show a 5s 'will be zipped' notice then delete it."""
    is_pl, provider = detect_playlist(link)
    if not is_pl:
        return
    if provider == "spotify":
        warn = await ctx.reply("⚠️ Spotify playlists aren’t supported.", mention_author=False)
        await countdown_delete_message(warn, 5)
        return
    count = await probe_playlist_count(link)
    if count is not None and count > 5:
        prov_name = "YouTube" if provider == "youtube" else ("SoundCloud" if provider == "soundcloud" else "Bandcamp")
        notice = await ctx.reply(
            f"⚠️ Detected a **{prov_name}** playlist with **{count} tracks**.\n"
            f"It exceeds 5 tracks, so I’ll **zip** the audio.",
            mention_author=False
        )
        await countdown_delete_message(notice, 5)

async def ask_artwork(ctx: commands.Context) -> Tuple[bool, Optional[discord.Message]]:
    """Ask once per rip whether to include Artwork."""
    view = ArtChoiceView(ctx.author.id, timeout=30)
    prompt = await ctx.reply(
        "Do you want to include **Artwork**? (If yes, I’ll ZIP audio + artwork together.)",
        view=view, mention_author=False
    )
    await view.wait()
    include = bool(view.include_art)
    try:
        await prompt.edit(content=("🖼️ Artwork will be included." if include else "🎵 Audio only."), view=None)
    except discord.HTTPException:
        pass
    return include, prompt

@bot.command(name="rip")
async def rip(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        return await ctx.reply("Usage: `*rip <link>`", mention_author=False)
    if not ok_domain(link):
        is_pl, provider = detect_playlist(link)
        if provider == "spotify":
            warn = await ctx.reply("⚠️ Spotify playlists aren’t supported.", mention_author=False)
            await countdown_delete_message(warn, 5)
            return
        err = await ctx.reply("Unsupported link. Try YouTube, SoundCloud, or Bandcamp.", mention_author=False)
        await countdown_delete_message(err, 5)
        return

    # Playlist notice (if >5)
    await maybe_notify_zip(ctx, link)

    include_art, art_prompt = await ask_artwork(ctx)

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        items, title, is_pl, meta, cover = await download_all_to_mp3(link, tmpdir, include_thumbs=include_art)
        attribution = build_attribution(ctx, link)

        if include_art:
            await status.edit(content=f"🗜️ Zipping audio + artwork…")
            safe = _safe_base(title)
            zip_path = os.path.join(tmpdir, f"{safe}.zip")
            make_single_zip(items, zip_path, track_meta=meta, cover_path=cover)
            try:
                if os.path.getsize(zip_path) > MAX_FILE_BYTES_HINT:
                    await status.edit(content="🗜️ Zipping… (note: zip may exceed this server's upload cap)")
            except Exception:
                pass
            await status.edit(content="📤 Uploading zip…")
            await send_single_file_with_banner(ctx, zip_path, os.path.basename(zip_path), to_dm=False, attribution=attribution)

        else:
            if is_pl and len(items) > 5:
                await status.edit(content=f"🗜️ Zipping… ({len(items)} tracks)")
                safe = _safe_base(title)
                zip_path = os.path.join(tmpdir, f"{safe}.zip")
                make_single_zip(items, zip_path, track_meta=meta, cover_path=None)
                try:
                    if os.path.getsize(zip_path) > MAX_FILE_BYTES_HINT:
                        await status.edit(content="🗜️ Zipping… (note: zip may exceed this server's upload cap)")
                except Exception:
                    pass
                await status.edit(content="📤 Uploading zip…")
                await send_single_file_with_banner(ctx, zip_path, os.path.basename(zip_path), to_dm=False, attribution=attribution)
            else:
                await status.edit(content="📤 Uploading…")
                if len(items) == 1:
                    p, n = items[0]
                    await send_single_file_with_banner(ctx, p, n, to_dm=False, attribution=attribution)
                else:
                    await send_many_try_one_message_then_fallback(ctx, items, to_dm=False, attribution=attribution)

        # Clean up prompts after upload
        if art_prompt:
            try: await art_prompt.delete()
            except discord.HTTPException: pass

        await delete_invoke_safely(ctx)
        try: await status.delete()
        except discord.HTTPException: pass

    except Exception as e:
        try: await status.delete()
        except discord.HTTPException: pass
        err = await ctx.reply(f"❌ Rip failed: `{e}`\n", mention_author=False)
        await countdown_delete_message(err, 5)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@bot.command(name="ripdm")
async def ripdm(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        return await ctx.reply("Usage: `*ripdm <link>`", mention_author=False)
    if not ok_domain(link):
        is_pl, provider = detect_playlist(link)
        if provider == "spotify":
            warn = await ctx.reply("⚠️ Spotify playlists aren’t supported.", mention_author=False)
            await countdown_delete_message(warn, 5)
            return
        err = await ctx.reply("Unsupported link. Try YouTube, SoundCloud, or Bandcamp.", mention_author=False)
        await countdown_delete_message(err, 5)
        return

    await maybe_notify_zip(ctx, link)
    include_art, art_prompt = await ask_artwork(ctx)

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    status = await ctx.reply("⏳ Ripping…", mention_author=False)
    try:
        items, title, is_pl, meta, cover = await download_all_to_mp3(link, tmpdir, include_thumbs=include_art)

        if include_art or (is_pl and len(items) > 5):
            await status.edit(content="🗜️ Zipping…")
            safe = _safe_base(title)
            zip_path = os.path.join(tmpdir, f"{safe}.zip")
            make_single_zip(items, zip_path, track_meta=meta, cover_path=(cover if include_art else None))
            await status.edit(content="📤 Uploading to DM…")
            await send_single_file_with_banner(ctx, zip_path, os.path.basename(zip_path), to_dm=True, attribution=None)
        else:
            await status.edit(content="📤 Uploading to DM…")
            if len(items) == 1:
                p, n = items[0]
                await send_single_file_with_banner(ctx, p, n, to_dm=True, attribution=None)
            else:
                await send_many_try_one_message_then_fallback(ctx, items, to_dm=True, attribution=None)

        if art_prompt:
            try: await art_prompt.delete()
            except discord.HTTPException: pass

        await delete_invoke_safely(ctx)
        try: await status.delete()
        except discord.HTTPException: pass

    except Exception as e:
        try: await status.delete()
        except discord.HTTPException: pass
        err = await ctx.reply(f"❌ Rip failed: `{e}`\n", mention_author=False)
        await countdown_delete_message(err, 5)
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
