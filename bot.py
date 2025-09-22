# bot.py
import os, re, tempfile, asyncio, shutil, zipfile, pathlib, time, random
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands
import yt_dlp

# ===================== Config =====================
TOKEN = os.getenv("DISCORD_TOKEN")  # set in your env
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

# One active rip per user. We also track messages to delete on abort.
ACTIVE_RIPS: Dict[int, Dict] = {}

# ---------- utilities ----------
def human_mb(n: Optional[float]) -> str:
    try: return f"{(n or 0)/1024/1024:.1f} MB"
    except Exception: return "0 MB"

def clamp(v, lo, hi): return max(lo, min(hi, v))

def provider_of(link: str) -> Tuple[str, str]:
    try:
        u = urlparse(link); host = (u.hostname or "").lower()
        if host.startswith("www."): host = host[4:]
        if "soundcloud.com" in host: return "SoundCloud", link
        if "bandcamp.com"   in host: return "Bandcamp", link
        if host in {"youtube.com","music.youtube.com","m.youtube.com","youtu.be"}: return "YouTube", link
        if host == "open.spotify.com": return "Spotify", link
        return "Source", link
    except Exception:
        return "Source", link

def detect_playlist(link: str) -> Tuple[bool, str]:
    try:
        u = urlparse(link); host = (u.hostname or "").lower() if u.hostname else ""
        if host.startswith("www."): host = host[4:]
        if host in {"youtube.com","music.youtube.com","m.youtube.com"}:
            qs = parse_qs(u.query or "")
            if "list" in qs or (u.path or "").startswith("/playlist"): return True,"youtube"
        if host == "youtu.be":
            if "list" in parse_qs(u.query or ""): return True,"youtube"
        if "soundcloud.com" in host and "/sets/"  in (u.path or ""): return True,"soundcloud"
        if "bandcamp.com"   in host and "/album/" in (u.path or ""): return True,"bandcamp"
        if host == "open.spotify.com" and "/playlist/" in (u.path or ""): return True,"spotify"
        return False,"unknown"
    except Exception:
        return False,"unknown"

def ok_domain(link: str) -> bool:
    try:
        host = (urlparse(link).hostname or "").lower()
        return (host.endswith("youtube.com") or host == "youtu.be" or
                host.endswith("soundcloud.com") or host.endswith("bandcamp.com"))
    except Exception:
        return False

def _safe_base(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "playlist"

async def countdown_delete_message(msg: discord.Message, seconds: int = 5):
    try:
        for t in range(seconds, 0, -1):
            try: await msg.edit(content=f"{msg.content}\n[This message will be deleted in {t}]")
            except discord.HTTPException: pass
            await asyncio.sleep(1)
    finally:
        try: await msg.delete()
        except discord.HTTPException: pass

def record_msg(ctx: commands.Context, msg: discord.Message):
    job = ACTIVE_RIPS.get(ctx.author.id)
    if job is not None:
        job.setdefault("msgs", []).append(msg)

async def delete_recorded_msgs(ctx: commands.Context):
    job = ACTIVE_RIPS.get(ctx.author.id)
    if not job: return
    for m in job.get("msgs", []):
        try: await m.delete()
        except discord.HTTPException: pass
    job["msgs"] = []

# ---------- musical progress bar ----------
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
        self.bar_width = 30
        self.cancelled = False

    async def start(self):
        if not self.msg:
            self.msg = await self.ctx.reply("Process starting…", mention_author=False)
            record_msg(self.ctx, self.msg)

    def _music_bar(self) -> str:
        filled = clamp(int(round(self.percent/100 * self.bar_width)), 0, self.bar_width)
        notes = "".join(random.choice(NOTE_GLYPHS) for _ in range(filled))
        rest  = "-" * (self.bar_width - filled)
        return f"[{notes}{rest}]"

    async def update(self, force: bool=False):
        if not self.msg: return
        now = time.time()
        if not force and now - self._last_edit < 0.45: return
        self._last_edit = now

        title = (self.current_title or "").strip()
        title = title if len(title) <= 70 else title[:67]+"…"
        header = f"**[{self.current_index}/{self.total_tracks}] {title}**"
        size = f"{human_mb(self.d_bytes)}" + (f" / {human_mb(self.t_bytes)}" if self.t_bytes else "")
        content = f"{header}\n{self._music_bar()}  {self.percent:>3d}% ({size})"
        try: await self.msg.edit(content=content)
        except discord.HTTPException: pass

    async def replace(self, text: str):
        if not self.msg:
            self.msg = await self.ctx.reply(text, mention_author=False)
            record_msg(self.ctx, self.msg)
        else:
            try: await self.msg.edit(content=text)
            except discord.HTTPException: pass

# ---------- yt_dlp helpers & geo capture ----------
class CaptureLogger:
    def __init__(self):
        self.errors: List[str] = []; self.warnings: List[str] = []
    def debug(self, msg): pass
    def info(self, msg):  pass
    def warning(self, msg): self.warnings.append(str(msg))
    def error(self, msg):   self.errors.append(str(msg))

def make_hook(pr: ProgressReporter, loop: asyncio.AbstractEventLoop):
    def hook(d):
        try:
            if pr.cancelled: raise yt_dlp.utils.DownloadError("User aborted")
            if d.get("status") in ("downloading","finished"):
                info = d.get("info_dict") or {}
                if info.get("title"): pr.current_title = info["title"]
                if info.get("playlist_index"): pr.current_index = int(info["playlist_index"])
                pr.d_bytes = int(d.get("downloaded_bytes") or 0)
                tbytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pr.t_bytes = int(tbytes or 0)
                pr.percent = clamp(int(pr.d_bytes * 100 / pr.t_bytes), 0, 100) if pr.t_bytes else min(99, pr.percent + 1)
                if d.get("status") == "finished": pr.percent = 100
                loop.call_soon_threadsafe(asyncio.create_task, pr.update())
        except Exception:
            raise
    return hook

def ydl_opts(tmpdir: str, include_thumbs: bool, pr: ProgressReporter,
             loop: asyncio.AbstractEventLoop, caplog: CaptureLogger) -> dict:
    pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    o = {
        "outtmpl": os.path.join(tmpdir, "%(playlist_index)03d - %(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "noprogress": True,
        "quiet": True,
        "ffmpeg_location": FFMPEG_BIN or None,
        "yesplaylist": True,
        "ignoreerrors": "only_download",
        "progress_hooks": [make_hook(pr, loop)],
        "logger": caplog,
    }
    if include_thumbs:
        o["writethumbnail"] = True
        pp.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
    o["postprocessors"] = pp
    return o

def _resolve_outpath(ydl: yt_dlp.YoutubeDL, entry: dict, fallback=("mp3","m4a","webm","opus")) -> str:
    if entry.get("requested_downloads"): return entry["requested_downloads"][0]["filepath"]
    base = ydl.prepare_filename(entry); root,_ = os.path.splitext(base)
    for ext in fallback:
        p = f"{root}.{ext}"
        if os.path.exists(p): return p
    return f"{root}.mp3"

def _maybe_thumb_path_from_media_path(media_path: str) -> Optional[str]:
    root,_ = os.path.splitext(media_path); jpg = root + ".jpg"
    return jpg if os.path.exists(jpg) else None

async def probe_playlist_count(link: str) -> Optional[int]:
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True, "extract_flat": True, "yesplaylist": True}) as ydl:
            info = ydl.extract_info(link, download=False)
            if isinstance(info, dict) and info.get("entries") is not None:
                return len([e for e in info["entries"] if e])
    except Exception: pass
    return None

async def download_all_to_mp3(link: str, tmpdir: str, *, include_thumbs: bool, pr: ProgressReporter):
    main_loop = asyncio.get_running_loop()
    caplog = CaptureLogger()
    def do_download():
        with yt_dlp.YoutubeDL(ydl_opts(tmpdir, include_thumbs, pr, main_loop, caplog)) as ydl:
            info = ydl.extract_info(link, download=True); return info, ydl
    info, ydl = await asyncio.to_thread(do_download)

    items: List[Tuple[str,str]] = []; meta: List[Dict] = []; cover: Optional[str] = None
    if "entries" in info and info["entries"] is not None:
        title = info.get("title") or "playlist"
        for ent in info["entries"]:
            if not ent:
                meta.append({"index": None, "title": None, "thumb": None}); continue
            path = _resolve_outpath(ydl, ent)
            ttitle = ent.get("title") or "audio"
            root,_ = os.path.splitext(path); mp3 = root + ".mp3"
            final = mp3 if os.path.exists(mp3) else path
            items.append((final, f"{ttitle}.mp3"))
            idx = ent.get("playlist_index")
            thumb = _maybe_thumb_path_from_media_path(final) if include_thumbs else None
            if not cover and thumb: cover = thumb
            meta.append({"index": idx, "title": ttitle, "thumb": thumb})
        return items, title, True, meta, cover, caplog
    else:
        title = info.get("title") or "audio"
        path = _resolve_outpath(ydl, info)
        root,_ = os.path.splitext(path); mp3 = root + ".mp3"
        final = mp3 if os.path.exists(mp3) else path
        items.append((final, f"{title}.mp3"))
        thumb = _maybe_thumb_path_from_media_path(final) if include_thumbs else None
        if thumb: cover = thumb
        meta.append({"index": None, "title": title, "thumb": thumb})
        return items, title, False, meta, cover, caplog

# ---------- zipping / attribution ----------
def make_single_zip(files: List[Tuple[str,str]], out_zip: str, *, track_meta: List[Dict], cover_path: Optional[str]):
    ordered = sorted(enumerate(track_meta), key=lambda t: (t[1].get("index") is None, t[1].get("index") or (t[0]+1)))
    tracklist_txt = "\n".join([
        f"{(m.get('index') if m.get('index') is not None else i+1):02d}. {m.get('title','')}"
        for i, m in [(i, m) for i, m in ordered] if m.get("title")
    ])
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p,n in files:
            try: zf.write(p, arcname=n)
            except FileNotFoundError: pass
        zf.writestr("tracklist.txt", tracklist_txt)
        if cover_path and os.path.exists(cover_path):
            ext = pathlib.Path(cover_path).suffix.lower()
            zf.write(cover_path, arcname=f"artwork{ext}")

def build_attribution(ctx: commands.Context, link: str) -> Optional[str]:
    if ctx.guild is None: return None
    disp = ctx.author.display_name; uname = ctx.author.name
    provider, url = provider_of(link)
    return f"ripped by: {disp}({uname}) from [{provider}]({url})"

async def send_single_file_with_banner(ctx: commands.Context, path: str, name: str, *, to_dm: bool, attribution: Optional[str]):
    content = attribution if (attribution and not to_dm) else None
    try:
        if to_dm:
            dm = await ctx.author.create_dm()
            m = await dm.send(content=content, file=discord.File(path, filename=name))
        else:
            m = await ctx.send(content=content, file=discord.File(path, filename=name))
        record_msg(ctx, m)
    except discord.HTTPException as e:
        hint = ""
        try: hint = f" (size ~{os.path.getsize(path)/1024/1024:.1f} MB)"
        except Exception: pass
        msg = await ctx.reply(f"Failed to upload file{hint}. Discord may have rejected it due to size.", mention_author=False)
        record_msg(ctx, msg); await countdown_delete_message(msg, 5); raise e

async def send_many_try_one_message_then_fallback(ctx: commands.Context, files: List[Tuple[str,str]], *, to_dm: bool, attribution: Optional[str]):
    try:
        payload = [discord.File(p, filename=n) for p,n in files]
        content = attribution if (attribution and not to_dm) else None
        if to_dm:
            dm = await ctx.author.create_dm(); m = await dm.send(content=content, files=payload)
        else:
            m = await ctx.send(content=content, files=payload)
        record_msg(ctx, m)
    except discord.HTTPException:
        for p,n in files:
            await send_single_file_with_banner(ctx, p, n, to_dm=to_dm, attribution=attribution)

async def delete_invoke_safely(ctx: commands.Context):
    try: await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException): pass

# ---------- Views (Artwork, Geo, Abort confirm) ----------
class ArtChoiceView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout); self.author_id = author_id; self.include_art: Optional[bool] = None
    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message("This prompt isn’t for you.", ephemeral=True); return False
        return True
    @discord.ui.button(label="Include", style=discord.ButtonStyle.primary)
    async def yes(self, itx: discord.Interaction, _: discord.ui.Button):
        self.include_art = True
        for c in self.children: c.disabled = True
        await itx.response.edit_message(content="Artwork will be included.", view=self); self.stop()
    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, itx: discord.Interaction, _: discord.ui.Button):
        self.include_art = False
        for c in self.children: c.disabled = True
        await itx.response.edit_message(content="Audio only.", view=self); self.stop()

class GeoDecisionView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 45.0):
        super().__init__(timeout=timeout); self.author_id = author_id; self.choice: Optional[str] = None
    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message("This prompt isn’t for you.", ephemeral=True); return False
        return True
    @discord.ui.button(label="Ignore & Continue", style=discord.ButtonStyle.primary)
    async def cont(self, itx: discord.Interaction, _: discord.ui.Button):
        self.choice = "continue"; [setattr(c, "disabled", True) for c in self.children]
        await itx.response.edit_message(content="Continuing without the blocked tracks…", view=self); self.stop()
    @discord.ui.button(label="Abort", style=discord.ButtonStyle.danger)
    async def abort(self, itx: discord.Interaction, _: discord.ui.Button):
        self.choice = "abort"; [setattr(c, "disabled", True) for c in self.children]
        await itx.response.edit_message(content="Cancelling…", view=self); self.stop()

class AbortConfirmView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout); self.author_id = author_id; self.confirmed = False
    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message("This prompt isn’t for you.", ephemeral=True); return False
        return True
    @discord.ui.button(label="Y", style=discord.ButtonStyle.danger)
    async def yes(self, itx: discord.Interaction, _: discord.ui.Button):
        self.confirmed = True; [setattr(c, "disabled", True) for c in self.children]
        await itx.response.edit_message(content="Aborting…", view=self); self.stop()
    @discord.ui.button(label="N", style=discord.ButtonStyle.secondary)
    async def no(self, itx: discord.Interaction, _: discord.ui.Button):
        self.confirmed = False; [setattr(c, "disabled", True) for c in self.children]
        await itx.response.edit_message(content="Abort cancelled.", view=self); self.stop()

# ---------- events ----------
@bot.event
async def on_guild_join(guild: discord.Guild):
    chan = None
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        chan = guild.system_channel
    else:
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages: chan = c; break
    if chan:
        try: await chan.send("Thanks for adding me! Please type `*help` for a list of commands.")
        except discord.HTTPException: pass

# ---------- help ----------
@bot.command(name="help")
async def _help(ctx: commands.Context):
    msg = await ctx.reply(f"{HELP_TEXT}", mention_author=False); await countdown_delete_message(msg, 5); await delete_invoke_safely(ctx)

# ---------- prompts ----------
async def maybe_notify_zip(ctx: commands.Context, link: str) -> Tuple[bool, Optional[int], Optional[discord.Message]]:
    is_pl, provider = detect_playlist(link)
    if not is_pl: return False, None, None
    if provider == "spotify":
        warn = await ctx.reply("Spotify playlists aren’t supported.", mention_author=False); record_msg(ctx, warn)
        await countdown_delete_message(warn, 5); return False, None, warn
    count = await probe_playlist_count(link)
    if count is not None and count > 5:
        prov = "YouTube" if provider == "youtube" else ("SoundCloud" if provider == "soundcloud" else "Bandcamp")
        note = await ctx.reply(f"Detected a {prov} playlist with {count} tracks.\nIt exceeds 5 tracks, so I will zip the audio.", mention_author=False)
        record_msg(ctx, note); await countdown_delete_message(note, 5)
        return True, count, note
    return False, count, None

async def ask_artwork(ctx: commands.Context, *, big_zip: bool) -> Tuple[bool, Optional[discord.Message]]:
    view = ArtChoiceView(ctx.author.id, timeout=30)
    text = ("Include Artwork in the .zip?"
            if big_zip else
            "Include Artwork? (Yes will zip the file.)")
    prompt = await ctx.reply(text, view=view, mention_author=False)
    record_msg(ctx, prompt)
    await view.wait()
    # We delete this prompt when ripping begins, so no lingering status message.
    return bool(view.include_art), prompt

# ---------- abort helpers ----------
def user_has_active(uid: int) -> bool: return uid in ACTIVE_RIPS
def clear_active(uid: int): ACTIVE_RIPS.pop(uid, None)

@bot.command(name="abort")
async def abort(ctx: commands.Context):
    job = ACTIVE_RIPS.get(ctx.author.id)
    if not job:
        msg = await ctx.reply("You don't have an active rip.", mention_author=False)
        await countdown_delete_message(msg, 5); return
    # Confirm Y/N
    view = AbortConfirmView(ctx.author.id)
    m = await ctx.reply("Download currently in process, are you SURE? [Y/N]", view=view, mention_author=False)
    await view.wait()
    try: await m.edit(view=None)
    except discord.HTTPException: pass
    if not view.confirmed:
        msg = await ctx.reply("Abort cancelled.", mention_author=False); await countdown_delete_message(msg, 5); return
    # Signal cancel
    pr: ProgressReporter = job["pr"]; pr.cancelled = True
    # Clean up previous messages
    await delete_recorded_msgs(ctx)
    msg = await ctx.reply("Aborted.", mention_author=False); await countdown_delete_message(msg, 5)

# ---------- core ripping flow ----------
async def run_rip(ctx: commands.Context, link: str, *, to_dm: bool):
    big_zip, _, _ = await maybe_notify_zip(ctx, link)
    include_art, art_prompt = await ask_artwork(ctx, big_zip=big_zip)

    pr = ProgressReporter(ctx); await pr.start()
    est_count = await probe_playlist_count(link)
    if est_count and est_count > 0: pr.total_tracks = est_count

    # remove the artwork prompt right as we begin ripping
    if art_prompt:
        try: await art_prompt.delete()
        except discord.HTTPException: pass

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    ACTIVE_RIPS[ctx.author.id] = {"pr": pr, "tmpdir": tmpdir, "msgs": [pr.msg]}

    try:
        items, title, is_pl, meta, cover, caplog = await download_all_to_mp3(link, tmpdir, include_thumbs=include_art, pr=pr)

        # Geo-restriction prompt
        geo_lines = [ln for ln in (caplog.errors + caplog.warnings) if "geo" in ln.lower() or "available from your location" in ln.lower()]
        skipped = sum(1 for m in meta if not m.get("title"))
        if geo_lines and is_pl and skipped > 0 and not pr.cancelled:
            view = GeoDecisionView(ctx.author.id)
            prompt = await ctx.reply(f"Some tracks appear geo-restricted ({skipped} skipped). Continue without them or abort?",
                                     view=view, mention_author=False)
            record_msg(ctx, prompt)
            await view.wait()
            try: await prompt.edit(view=None)
            except discord.HTTPException: pass
            if view.choice != "continue":
                await delete_recorded_msgs(ctx)
                m = await ctx.reply("Aborted.", mention_author=False); await countdown_delete_message(m, 5)
                raise yt_dlp.utils.DownloadError("User aborted after geo prompt")
            items = [it for it in items if os.path.exists(it[0])]
            meta  = [m for m in meta  if m.get("title")]

        if pr.cancelled: raise yt_dlp.utils.DownloadError("User aborted")

        await pr.replace("Preparing files…")

        attribution = build_attribution(ctx, link)

        if include_art:
            safe = _safe_base(title); zip_path = os.path.join(tmpdir, f"{safe}.zip")
            make_single_zip(items, zip_path, track_meta=meta, cover_path=cover)
            try:
                if os.path.getsize(zip_path) > MAX_FILE_BYTES_HINT:
                    await pr.replace("Preparing files… (note: zip may exceed this server's upload cap)")
            except Exception: pass
            await pr.replace("Uploading zip…")
            await send_single_file_with_banner(ctx, zip_path, os.path.basename(zip_path), to_dm=to_dm, attribution=attribution)
        else:
            if is_pl and len(items) > 5:
                safe = _safe_base(title); zip_path = os.path.join(tmpdir, f"{safe}.zip")
                make_single_zip(items, zip_path, track_meta=meta, cover_path=None)
                await pr.replace("Uploading zip…")
                await send_single_file_with_banner(ctx, zip_path, os.path.basename(zip_path), to_dm=to_dm, attribution=attribution)
            else:
                await pr.replace("Uploading…")
                if len(items) == 1:
                    p,n = items[0]; await send_single_file_with_banner(ctx, p, n, to_dm=to_dm, attribution=attribution)
                else:
                    await send_many_try_one_message_then_fallback(ctx, items, to_dm=to_dm, attribution=attribution)

        # tidy
        await delete_recorded_msgs(ctx)  # progress + prompts
        await delete_invoke_safely(ctx)

    except yt_dlp.utils.DownloadError as e:
        await delete_recorded_msgs(ctx)
        err = await ctx.reply("Aborted." if "User aborted" in str(e) else f"Rip failed: `{e}`", mention_author=False)
        await countdown_delete_message(err, 5)
    except Exception as e:
        await delete_recorded_msgs(ctx)
        err = await ctx.reply(f"Rip failed: `{e}`", mention_author=False); await countdown_delete_message(err, 5)
    finally:
        clear_active(ctx.author.id)
        shutil.rmtree(tmpdir, ignore_errors=True)

# ---------- commands ----------
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
            warn = await ctx.reply("Spotify playlists aren’t supported.", mention_author=False); await countdown_delete_message(warn, 5); return
        msg = await ctx.reply("Unsupported link. Try YouTube, SoundCloud, or Bandcamp.", mention_author=False); await countdown_delete_message(msg, 5); return
    await run_rip(ctx, link, to_dm=False)

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
            warn = await ctx.reply("Spotify playlists aren’t supported.", mention_author=False); await countdown_delete_message(warn, 5); return
        msg = await ctx.reply("Unsupported link. Try YouTube, SoundCloud, or Bandcamp.", mention_author=False); await countdown_delete_message(msg, 5); return
    await run_rip(ctx, link, to_dm=True)

# ---------- startup ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    print("Ready.")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment.")
    bot.run(TOKEN)
