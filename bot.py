# bot.py
import os, re, tempfile, asyncio, shutil, zipfile, pathlib, time, random
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands
import yt_dlp

# ===================== Config =====================
TOKEN = os.getenv("DISCORD_TOKEN")  # PowerShell: $env:DISCORD_TOKEN='...'
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")
MAX_FILE_BYTES_HINT = int(os.getenv("MAX_FILE_BYTES_HINT", str(25 * 1024 * 1024)))  # ~25 MiB
# ===================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="*", intents=intents, help_command=None)

HELP_TEXT = (
    "**ripperRoo — a Discord mp3 ripper created by d-rod**\n"
    "• `*rip <link>` — rip YouTube, SoundCloud, or Bandcamp audio and post it here\n"
    "• `*ripdm <link>` — rip and DM you the file\n"
    "• `*abort` — abort **your** current rip\n\n"
    "_Tip: to auto-delete your command after sending files, give my role **Manage Messages** in this channel._"
)

# Track the currently running rip per user (one at a time).
ACTIVE_RIPS: Dict[int, Dict] = {}

# ---------- small utils ----------
def human_mb(n: Optional[float]) -> str:
    try:
        return f"{(n or 0)/1024/1024:.1f} MB"
    except Exception:
        return "0 MB"

def clamp(v, lo, hi): return max(lo, min(hi, v))

def provider_of(link: str) -> Tuple[str, str]:
    try:
        u = urlparse(link)
        host = (u.hostname or "").lower()
        if host.startswith("www."): host = host[4:]
        if "soundcloud.com" in host: return "SoundCloud", link
        if "bandcamp.com" in host:   return "Bandcamp", link
        if host in {"youtube.com","music.youtube.com","m.youtube.com","youtu.be"}: return "YouTube", link
        if host == "open.spotify.com": return "Spotify", link
        return "Source", link
    except Exception:
        return "Source", link

def detect_playlist(link: str) -> Tuple[bool, str]:
    try:
        u = urlparse(link)
        host = (u.hostname or "").lower() if u.hostname else ""
        if host.startswith("www."): host = host[4:]
        if host in {"youtube.com","music.youtube.com","m.youtube.com"}:
            qs = parse_qs(u.query or "")
            if "list" in qs or (u.path or "").startswith("/playlist"): return True,"youtube"
        if host == "youtu.be":
            qs = parse_qs(u.query or "")
            if "list" in qs: return True,"youtube"
        if "soundcloud.com" in host and "/sets/" in (u.path or ""): return True,"soundcloud"
        if "bandcamp.com" in host and "/album/" in (u.path or ""): return True,"bandcamp"
        if host == "open.spotify.com" and "/playlist/" in (u.path or ""): return True,"spotify"
        return False,"unknown"
    except Exception:
        return False,"unknown"

def ok_domain(link: str) -> bool:
    try:
        host = (urlparse(link).hostname or "").lower()
        return (
            host.endswith("youtube.com") or host == "youtu.be" or
            host.endswith("soundcloud.com") or
            host.endswith("bandcamp.com")
        )
    except Exception:
        return False

def _safe_base(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "playlist"

# ---------- countdown that preserves message text ----------
async def countdown_delete_message(msg: discord.Message, seconds: int = 5):
    """Append a live countdown line and then delete."""
    try:
        for t in range(seconds, 0, -1):
            try:
                await msg.edit(content=f"{msg.content}\n[This message will be deleted in {t}]")
            except discord.HTTPException:
                pass
            await asyncio.sleep(1)
    finally:
        try: await msg.delete()
        except discord.HTTPException: pass

# ---------- progress reporting with musical bar ----------
NOTE_GLYPHS = ["𝆕","𝅘𝅥","𝅗𝅥","𝅘𝅥𝅮","♩","♪","♫","♬"]

class ProgressReporter:
    def __init__(self, ctx: commands.Context):
        self.ctx = ctx
        self.msg: Optional[discord.Message] = None
        self._last_edit = 0.0

        self.total_tracks = 1
        self.current_index = 1
        self.current_title = ""
        self.percent = 0
        self.d_bytes = 0
        self.t_bytes = 0

        self.bar_width = 30  # inside the [ ... ] brackets
        self.cancelled = False

    async def start(self):
        if not self.msg:
            self.msg = await self.ctx.reply("⏳ Ripping…", mention_author=False)

    def _music_bar(self) -> str:
        filled = clamp(int(round(self.percent/100 * self.bar_width)), 0, self.bar_width)
        notes = "".join(random.choice(NOTE_GLYPHS) for _ in range(filled))
        rest  = "-" * (self.bar_width - filled)
        return f"[{notes}{rest}]"

    async def update(self, force: bool=False):
        if not self.msg: return
        now = time.time()
        if not force and now - self._last_edit < 0.45:
            return
        self._last_edit = now

        title = (self.current_title or "").strip()
        title = title if len(title) <= 70 else title[:67]+"…"
        header = f"[{self.current_index}/{self.total_tracks}] {title}"

        if self.t_bytes:
            size = f"{human_mb(self.d_bytes)} / {human_mb(self.t_bytes)}"
        else:
            size = f"{human_mb(self.d_bytes)}"

        bar = self._music_bar()
        content = f"{header}\nRIPPING {bar} {self.percent:>3d}% ({size})"
        try:
            await self.msg.edit(content=content)
        except discord.HTTPException:
            pass

    async def replace(self, text: str):
        if not self.msg:
            self.msg = await self.ctx.reply(text, mention_author=False)
        else:
            try: await self.msg.edit(content=text)
            except discord.HTTPException: pass

# ---------- yt_dlp helpers ----------
def ydl_opts(tmpdir: str, include_thumbs: bool, pr: ProgressReporter, loop: asyncio.AbstractEventLoop) -> dict:
    pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    opts = {
        "outtmpl": os.path.join(tmpdir, "%(playlist_index)03d - %(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "noprogress": True,
        "quiet": True,
        "ffmpeg_location": FFMPEG_BIN if FFMPEG_BIN else None,
        "yesplaylist": True,
        "progress_hooks": [make_hook(pr, loop)],
    }
    if include_thumbs:
        opts["writethumbnail"] = True
        pp.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
    opts["postprocessors"] = pp
    return opts

def make_hook(pr: ProgressReporter, loop: asyncio.AbstractEventLoop):
    # 'loop' is from the main thread; this hook runs in worker thread.
    def hook(d):
        try:
            if pr.cancelled:
                # Abort download by raising; caught upstream as a user cancel.
                raise yt_dlp.utils.DownloadError("User aborted")
            if d.get("status") in ("downloading","finished"):
                info = d.get("info_dict") or {}
                t = info.get("title")
                if t: pr.current_title = t
                idx = info.get("playlist_index")
                if idx: pr.current_index = int(idx)

                pr.d_bytes = int(d.get("downloaded_bytes") or 0)
                tbytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pr.t_bytes = int(tbytes or 0)
                if pr.t_bytes:
                    pr.percent = clamp(int(pr.d_bytes * 100 / pr.t_bytes), 0, 100)
                else:
                    pr.percent = min(99, pr.percent + 1)

                if d.get("status") == "finished":
                    pr.percent = 100

                try:
                    loop.call_soon_threadsafe(asyncio.create_task, pr.update())
                except RuntimeError:
                    pass
        except Exception as _:
            # Re-raise to stop yt_dlp
            raise
    return hook

def _resolve_outpath(ydl: yt_dlp.YoutubeDL, entry: dict, fallback_exts=("mp3","m4a","webm","opus")) -> str:
    if "requested_downloads" in entry and entry["requested_downloads"]:
        return entry["requested_downloads"][0]["filepath"]
    base = ydl.prepare_filename(entry)
    root, _ = os.path.splitext(base)
    for ext in fallback_exts:
        p = f"{root}.{ext}"
        if os.path.exists(p): return p
    return f"{root}.mp3"

def _maybe_thumb_path_from_media_path(media_path: str) -> Optional[str]:
    root, _ = os.path.splitext(media_path)
    jpg = root + ".jpg"
    return jpg if os.path.exists(jpg) else None

async def probe_playlist_count(link: str) -> Optional[int]:
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True, "extract_flat": True, "yesplaylist": True}) as ydl:
            info = ydl.extract_info(link, download=False)
            if isinstance(info, dict) and info.get("entries") is not None:
                return len([e for e in info["entries"] if e])
    except Exception:
        pass
    return None

async def download_all_to_mp3(link: str, tmpdir: str, *, include_thumbs: bool, pr: ProgressReporter) -> Tuple[List[Tuple[str,str]], str, bool, List[Dict], Optional[str]]:
    """Run yt_dlp in a worker thread so our progress bar can update live."""
    main_loop = asyncio.get_running_loop()

    def do_download():
        with yt_dlp.YoutubeDL(ydl_opts(tmpdir, include_thumbs, pr, main_loop)) as ydl:
            info = ydl.extract_info(link, download=True)
            return info, ydl

    info, ydl = await asyncio.to_thread(do_download)

    items: List[Tuple[str,str]] = []
    meta: List[Dict] = []
    cover: Optional[str] = None

    if "entries" in info and info["entries"]:
        title = info.get("title") or "playlist"
        for ent in info["entries"]:
            if not ent: continue
            path = _resolve_outpath(ydl, ent)
            ttitle = ent.get("title") or "audio"
            root,_ = os.path.splitext(path)
            mp3 = root + ".mp3"
            final = mp3 if os.path.exists(mp3) else path
            items.append((final, f"{ttitle}.mp3"))
            idx = ent.get("playlist_index")
            thumb = _maybe_thumb_path_from_media_path(final) if include_thumbs else None
            if not cover and thumb: cover = thumb
            meta.append({"index": idx, "title": ttitle, "thumb": thumb})
        return items, title, True, meta, cover
    else:
        title = info.get("title") or "audio"
        path = _resolve_outpath(ydl, info)
        root,_ = os.path.splitext(path)
        mp3 = root + ".mp3"
        final = mp3 if os.path.exists(mp3) else path
        items.append((final, f"{title}.mp3"))
        thumb = _maybe_thumb_path_from_media_path(final) if include_thumbs else None
        if thumb: cover = thumb
        meta.append({"index": None, "title": title, "thumb": thumb})
        return items, title, False, meta, cover

# ---------- zipping / attribution ----------
def make_single_zip(files: List[Tuple[str,str]], out_zip: str, *, track_meta: List[Dict], cover_path: Optional[str]):
    ordered = sorted(enumerate(track_meta), key=lambda t: (t[1].get("index") is None, t[1].get("index") or (t[0]+1)))
    tracklist_txt = "\n".join([f"{(m.get('index') if m.get('index') is not None else i+1):02d}. {m.get('title','')}" for i,m in [(i,m) for i,m in ordered]])
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p,name in files:
            try: zf.write(p, arcname=name)
            except FileNotFoundError: pass
        zf.writestr("tracklist.txt", tracklist_txt)
        if cover_path and os.path.exists(cover_path):
            ext = pathlib.Path(cover_path).suffix.lower()
            zf.write(cover_path, arcname=f"artwork{ext}")

def build_attribution(ctx: commands.Context, link: str) -> Optional[str]:
    if ctx.guild is None: return None
    display = ctx.author.display_name
    uname = ctx.author.name
    provider, url = provider_of(link)
    return f"ripped by: {display}({uname}) from [{provider}]({url})"

async def send_single_file_with_banner(ctx: commands.Context, path: str, name: str, *, to_dm: bool, attribution: Optional[str]):
    content = attribution if attribution else None
    try:
        if to_dm:
            dm = await ctx.author.create_dm()
            await dm.send(content=content, file=discord.File(path, filename=name))
        else:
            await ctx.send(content=content, file=discord.File(path, filename=name))
    except discord.HTTPException as e:
        hint = ""
        try: hint = f" (size ~{os.path.getsize(path)/1024/1024:.1f} MB)"
        except Exception: pass
        msg = await ctx.reply(f"❌ Failed to upload file{hint}. Discord likely rejected it due to size.", mention_author=False)
        await countdown_delete_message(msg, 5)
        raise e

async def send_many_try_one_message_then_fallback(ctx: commands.Context, files: List[Tuple[str,str]], *, to_dm: bool, attribution: Optional[str]):
    try:
        payload = [discord.File(p, filename=n) for p,n in files]
        content = attribution if attribution else None
        if to_dm:
            dm = await ctx.author.create_dm()
            await dm.send(content=content, files=payload)
        else:
            await ctx.send(content=content, files=payload)
    except discord.HTTPException:
        for p,n in files:
            await send_single_file_with_banner(ctx, p, n, to_dm=to_dm, attribution=attribution)

async def delete_invoke_safely(ctx: commands.Context):
    try: await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException): pass

# ---------- Artwork choice ----------
class ArtChoiceView(discord.ui.View):
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
        await interaction.response.edit_message(content="🖼️ Artwork will be included.", view=self); self.stop()
    @discord.ui.button(label="No Artwork", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.include_art = False
        for c in self.children: c.disabled = True
        await interaction.response.edit_message(content="🎵 Audio only.", view=self); self.stop()

# ---------- commands & events ----------
@bot.event
async def on_guild_join(guild: discord.Guild):
    text_chan = None
    # Prefer system channel, else first text channel bot can send in
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        text_chan = guild.system_channel
    else:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                text_chan = ch
                break
    if text_chan:
        try:
            await text_chan.send("Thanks for adding me! Please type `*help` for a list of commands.")
        except discord.HTTPException:
            pass

@bot.command(name="help")
async def _help(ctx: commands.Context):
    msg = await ctx.reply(f"{HELP_TEXT}", mention_author=False)
    await countdown_delete_message(msg, 5)
    await delete_invoke_safely(ctx)

async def maybe_notify_zip(ctx: commands.Context, link: str) -> Tuple[bool, Optional[int]]:
    """
    If playlist length > 5, show a 5s 'will be zipped' notice then delete it.
    Returns (big_zip, count_or_None).
    """
    is_pl, provider = detect_playlist(link)
    if not is_pl: return False, None
    if provider == "spotify":
        warn = await ctx.reply("⚠️ Spotify playlists aren’t supported.", mention_author=False)
        await countdown_delete_message(warn, 5)
        return False, None
    count = await probe_playlist_count(link)
    if count is not None and count > 5:
        prov = "YouTube" if provider == "youtube" else ("SoundCloud" if provider == "soundcloud" else "Bandcamp")
        note = await ctx.reply(f"⚠️ Detected a **{prov}** playlist with **{count} tracks**.\nIt exceeds 5 tracks, so I’ll **zip** the audio.", mention_author=False)
        await countdown_delete_message(note, 5)
        return True, count
    return False, count

async def ask_artwork(ctx: commands.Context, *, big_zip: bool) -> Tuple[bool, Optional[discord.Message]]:
    """
    Ask once per rip whether to include Artwork.
    If big_zip=True (playlist >5): wording becomes "include the Artwork in the .zip?"
    """
    view = ArtChoiceView(ctx.author.id, timeout=30)
    prompt_text = (
        "This source exceeds **5 tracks** and will be **zipped**.\n"
        "Would you like to include the **Artwork** in the **.zip**?"
        if big_zip else
        "Do you want to include **Artwork**? (If yes, I’ll ZIP audio + artwork together.)"
    )
    prompt = await ctx.reply(prompt_text, view=view, mention_author=False)
    await view.wait()
    include = bool(view.include_art)
    try:
        await prompt.edit(
            content=("🖼️ Artwork will be included." if include else "🎵 Audio only."),
            view=None
        )
    except discord.HTTPException:
        pass
    return include, prompt

def user_has_active(uid: int) -> bool:
    return uid in ACTIVE_RIPS

def clear_active(uid: int):
    ACTIVE_RIPS.pop(uid, None)

@bot.command(name="abort")
async def abort(ctx: commands.Context):
    job = ACTIVE_RIPS.get(ctx.author.id)
    if not job:
        msg = await ctx.reply("You don't have an active rip.", mention_author=False)
        await countdown_delete_message(msg, 5)
        return
    # Signal cancel
    pr: ProgressReporter = job["pr"]
    pr.cancelled = True
    msg = await ctx.reply("🛑 Aborting…", mention_author=False)
    await countdown_delete_message(msg, 5)

@bot.command(name="rip")
async def rip(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        msg = await ctx.reply("Usage: `*rip <link>`", mention_author=False); await countdown_delete_message(msg, 5); return
    if user_has_active(ctx.author.id):
        m = await ctx.reply("You already have a rip running. Use `*abort` to cancel it.", mention_author=False)
        await countdown_delete_message(m, 5); return
    if not ok_domain(link):
        is_pl, prov = detect_playlist(link)
        if prov == "spotify":
            warn = await ctx.reply("⚠️ Spotify playlists aren’t supported.", mention_author=False); await countdown_delete_message(warn, 5); return
        msg = await ctx.reply("Unsupported link. Try YouTube, SoundCloud, or Bandcamp.", mention_author=False); await countdown_delete_message(msg, 5); return

    big_zip, _ = await maybe_notify_zip(ctx, link)
    include_art, art_prompt = await ask_artwork(ctx, big_zip=big_zip)

    pr = ProgressReporter(ctx); await pr.start()
    est_count = await probe_playlist_count(link)
    if est_count and est_count > 0: pr.total_tracks = est_count

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    ACTIVE_RIPS[ctx.author.id] = {"pr": pr, "tmpdir": tmpdir}

    try:
        items, title, is_pl, meta, cover = await download_all_to_mp3(link, tmpdir, include_thumbs=include_art, pr=pr)

        if pr.cancelled:
            # user aborted during or just after download
            raise yt_dlp.utils.DownloadError("User aborted")

        await pr.replace("🗜️ Preparing files…")

        attribution = build_attribution(ctx, link)

        if include_art:
            safe = _safe_base(title)
            zip_path = os.path.join(tmpdir, f"{safe}.zip")
            make_single_zip(items, zip_path, track_meta=meta, cover_path=cover)
            try:
                if os.path.getsize(zip_path) > MAX_FILE_BYTES_HINT:
                    await pr.replace("🗜️ Preparing files… (note: zip may exceed this server's upload cap)")
            except Exception:
                pass
            await pr.replace("📤 Uploading zip…")
            await send_single_file_with_banner(ctx, zip_path, os.path.basename(zip_path), to_dm=False, attribution=attribution)
        else:
            if is_pl and len(items) > 5:
                safe = _safe_base(title)
                zip_path = os.path.join(tmpdir, f"{safe}.zip")
                make_single_zip(items, zip_path, track_meta=meta, cover_path=None)
                await pr.replace("📤 Uploading zip…")
                await send_single_file_with_banner(ctx, zip_path, os.path.basename(zip_path), to_dm=False, attribution=attribution)
            else:
                await pr.replace("📤 Uploading…")
                if len(items) == 1:
                    p,n = items[0]
                    await send_single_file_with_banner(ctx, p, n, to_dm=False, attribution=attribution)
                else:
                    await send_many_try_one_message_then_fallback(ctx, items, to_dm=False, attribution=attribution)

        if art_prompt:
            try: await art_prompt.delete()
            except discord.HTTPException: pass
        try: await pr.msg.delete()
        except discord.HTTPException: pass
        await delete_invoke_safely(ctx)

    except yt_dlp.utils.DownloadError as e:
        # Detect explicit user abort
        if "User aborted" in str(e):
            try:
                if pr.msg: await pr.msg.delete()
            except discord.HTTPException: pass
            m = await ctx.reply("🛑 Aborted.", mention_author=False)
            await countdown_delete_message(m, 5)
        else:
            try:
                if pr.msg: await pr.msg.delete()
            except discord.HTTPException: pass
            err = await ctx.reply(f"❌ Rip failed: `{e}`", mention_author=False)
            await countdown_delete_message(err, 5)
    except Exception as e:
        try:
            if pr.msg: await pr.msg.delete()
        except discord.HTTPException: pass
        err = await ctx.reply(f"❌ Rip failed: `{e}`", mention_author=False)
        await countdown_delete_message(err, 5)
    finally:
        clear_active(ctx.author.id)
        shutil.rmtree(tmpdir, ignore_errors=True)

@bot.command(name="ripdm")
async def ripdm(ctx: commands.Context, link: Optional[str] = None):
    if not link:
        msg = await ctx.reply("Usage: `*ripdm <link>`", mention_author=False); await countdown_delete_message(msg, 5); return
    if user_has_active(ctx.author.id):
        m = await ctx.reply("You already have a rip running. Use `*abort` to cancel it.", mention_author=False)
        await countdown_delete_message(m, 5); return
    if not ok_domain(link):
        is_pl, prov = detect_playlist(link)
        if prov == "spotify":
            warn = await ctx.reply("⚠️ Spotify playlists aren’t supported.", mention_author=False); await countdown_delete_message(warn, 5); return
        msg = await ctx.reply("Unsupported link. Try YouTube, SoundCloud, or Bandcamp.", mention_author=False); await countdown_delete_message(msg, 5); return

    big_zip, _ = await maybe_notify_zip(ctx, link)
    include_art, art_prompt = await ask_artwork(ctx, big_zip=big_zip)

    pr = ProgressReporter(ctx); await pr.start()
    est_count = await probe_playlist_count(link)
    if est_count and est_count > 0: pr.total_tracks = est_count

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    ACTIVE_RIPS[ctx.author.id] = {"pr": pr, "tmpdir": tmpdir}

    try:
        items, title, is_pl, meta, cover = await download_all_to_mp3(link, tmpdir, include_thumbs=include_art, pr=pr)

        if pr.cancelled:
            raise yt_dlp.utils.DownloadError("User aborted")

        await pr.replace("🗜️ Preparing files…")

        if include_art or (is_pl and len(items) > 5):
            safe = _safe_base(title)
            zip_path = os.path.join(tmpdir, f"{safe}.zip")
            make_single_zip(items, zip_path, track_meta=meta, cover_path=(cover if include_art else None))
            await pr.replace("📤 Uploading to DM…")
            await send_single_file_with_banner(ctx, zip_path, os.path.basename(zip_path), to_dm=True, attribution=None)
        else:
            await pr.replace("📤 Uploading to DM…")
            if len(items) == 1:
                p,n = items[0]
                await send_single_file_with_banner(ctx, p, n, to_dm=True, attribution=None)
            else:
                await send_many_try_one_message_then_fallback(ctx, items, to_dm=True, attribution=None)

        if art_prompt:
            try: await art_prompt.delete()
            except discord.HTTPException: pass
        try: await pr.msg.delete()
        except discord.HTTPException: pass
        await delete_invoke_safely(ctx)

    except yt_dlp.utils.DownloadError as e:
        if "User aborted" in str(e):
            try:
                if pr.msg: await pr.msg.delete()
            except discord.HTTPException: pass
            m = await ctx.reply("🛑 Aborted.", mention_author=False)
            await countdown_delete_message(m, 5)
        else:
            try:
                if pr.msg: await pr.msg.delete()
            except discord.HTTPException: pass
            err = await ctx.reply(f"❌ Rip failed: `{e}`", mention_author=False)
            await countdown_delete_message(err, 5)
    except Exception as e:
        try:
            if pr.msg: await pr.msg.delete()
        except discord.HTTPException: pass
        err = await ctx.reply(f"❌ Rip failed: `{e}`", mention_author=False)
        await countdown_delete_message(err, 5)
    finally:
        clear_active(ctx.author.id)
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
