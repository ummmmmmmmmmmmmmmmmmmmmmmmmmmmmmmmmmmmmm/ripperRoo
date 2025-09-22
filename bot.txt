# bot.py — pure slash client (discord.Client + app_commands.CommandTree)
import os, re, stat, time, random, zipfile, shutil, tempfile, asyncio, pathlib, datetime as dt
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlparse, parse_qs
from pathlib import Path

import discord
from discord import app_commands
import yt_dlp

# ========= Config =========
TOKEN = os.getenv("DISCORD_TOKEN")  # set in your environment
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")
MAX_FILE_BYTES_HINT = int(os.getenv("MAX_FILE_BYTES_HINT", str(25 * 1024 * 1024)))  # e.g., 25 MiB

# OPTIONAL: fill with your server IDs for INSTANT command availability
APP_GUILD_IDS: List[int] = []  # example: [123456789012345678]
# =========================

HELP_TEXT = (
    "**ripperRoo — a Discord mp3 ripper created by d-rod**\n"
    "• `/rip <link>` — rip YouTube, SoundCloud, or Bandcamp audio and post it here\n"
    "• `/ripdm <link>` — rip and DM you the file\n"
    "• `/abort` — abort **your** current rip\n"
    "• `/help` — show this help\n"
    "• `/sync` — owner-only; re-sync slash commands in this server\n"
)

# One active rip per user id
ACTIVE_RIPS: Dict[int, Dict] = {}

# ---------- small utils ----------
def human_mb(n: Optional[float]) -> str:
    try: return f"{(n or 0)/1024/1024:.1f} MB"
    except Exception: return "0 MB"

def clamp(v, lo, hi): return max(lo, min(hi, v))

def _safe_base(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "playlist"

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

def build_attribution(inter: discord.Interaction, link: str) -> Optional[str]:
    if inter.guild is None: return None
    disp = inter.user.display_name
    uname = inter.user.name
    provider, url = provider_of(link)
    return f"ripped by: {disp}({uname}) from [{provider}]({url})"

# ---------- startup temp cleanup ----------
def _safe_rmtree(p: Path):
    def onerr(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWUSR)
            func(path)
        except Exception:
            pass
    shutil.rmtree(p, ignore_errors=False, onerror=onerr)

def clean_stale_tmp(prefix: str = "rip-", max_age_hours: int = 36):
    tmp = Path(tempfile.gettempdir())
    cutoff = dt.datetime.now().timestamp() - max_age_hours * 3600
    for child in tmp.iterdir():
        try:
            if child.is_dir() and child.name.startswith(prefix):
                if child.stat().st_mtime < cutoff:
                    _safe_rmtree(child)
        except Exception:
            pass

# ---------- ephemeral helper ----------
async def eph_send(inter: discord.Interaction, content: str = "\u200b", *,
                   view: discord.ui.View | None = None) -> discord.Message:
    """Send or follow up with an ephemeral message and return it so we can edit later."""
    if not inter.response.is_done():
        await inter.response.send_message(content=content, view=view, ephemeral=True)
        return await inter.original_response()
    else:
        return await inter.followup.send(content=content, view=view, ephemeral=True, wait=True)

# ---------- progress bar (music notes) ----------
NOTE_GLYPHS = ["𝆕","𝅘𝅥","𝅗𝅥","𝅘𝅥𝅮","♩","♪","♫","♬"]

class ProgressReporter:
    def __init__(self, inter: discord.Interaction):
        self.inter = inter
        self.msg: Optional[discord.Message] = None
        self._last = 0.0
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
            self.msg = await eph_send(self.inter, "Process starting…")

    def _bar(self) -> str:
        filled = clamp(int(round(self.percent/100 * self.bar_width)), 0, self.bar_width)
        notes = "".join(random.choice(NOTE_GLYPHS) for _ in range(filled))
        rest  = "-" * (self.bar_width - filled)
        return f"[{notes}{rest}]"

    async def update(self, force: bool=False):
        if not self.msg: return
        now = time.time()
        if not force and now - self._last < 0.45: return
        self._last = now
        title = (self.current_title or "")
        title = title if len(title) <= 70 else title[:67]+"…"
        header = f"**[{self.current_index}/{self.total_tracks}] {title}**"
        size = f"{human_mb(self.d_bytes)}" + (f" / {human_mb(self.t_bytes)}" if self.t_bytes else "")
        content = f"{header}\n{self._bar()}  {self.percent:>3d}% ({size})"
        try: await self.msg.edit(content=content)
        except discord.HTTPException: pass

    async def replace(self, text: str):
        if not self.msg:
            self.msg = await eph_send(self.inter, text)
        else:
            try: await self.msg.edit(content=text)
            except discord.HTTPException: pass

# ---------- yt-dlp plumbing ----------
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
        "cachedir": False,
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
    loop = asyncio.get_running_loop()
    caplog = CaptureLogger()
    def do_download():
        with yt_dlp.YoutubeDL(ydl_opts(tmpdir, include_thumbs, pr, loop, caplog)) as ydl:
            info = ydl.extract_info(link, download=True)
            return info, ydl
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

# ---------- zipping / size ----------
def _filesize(path: str) -> int:
    try: return os.path.getsize(path)
    except Exception: return 0

def make_zip(path_out: str, files: List[Tuple[str,str]], *, track_meta: List[Dict],
             cover_path: Optional[str], include_cover: bool):
    ordered = sorted(enumerate(track_meta), key=lambda t: (t[1].get("index") is None, t[1].get("index") or (t[0]+1)))
    tracklist_txt = "\n".join([
        f"{(m.get('index') if m.get('index') is not None else i+1):02d}. {m.get('title','')}"
        for i, m in [(i, m) for i, m in ordered] if m.get("title")
    ])
    with zipfile.ZipFile(path_out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p,n in files:
            if os.path.exists(p): zf.write(p, arcname=n)
        zf.writestr("tracklist.txt", tracklist_txt)
        if include_cover and cover_path and os.path.exists(cover_path):
            ext = pathlib.Path(cover_path).suffix.lower()
            zf.write(cover_path, arcname=f"artwork{ext}")

def pack_into_zip_parts(base_name: str, tmpdir: str, files: List[Tuple[str,str]], *,
                        track_meta: List[Dict], cover_path: Optional[str], size_limit: int) -> List[str]:
    parts: List[List[Tuple[str,str]]] = [[]]; sizes: List[int] = [0]; overhead = 4096
    for fpath,fname in files:
        fsz = _filesize(fpath) + overhead
        if sizes[-1] + fsz > size_limit and parts[-1]:
            parts.append([(fpath,fname)]); sizes.append(fsz)
        else:
            parts[-1].append((fpath,fname)); sizes[-1] += fsz
    out: List[str] = []
    for i,bundle in enumerate(parts, start=1):
        suffix = f"_part{i}.zip" if len(parts) > 1 else ".zip"
        outzip = os.path.join(tmpdir, f"{base_name}{suffix}")
        make_zip(outzip, bundle, track_meta=track_meta, cover_path=cover_path, include_cover=(i==1))
        out.append(outzip)
    return out

def make_single_zip(files: List[Tuple[str,str]], out_zip: str, *, track_meta: List[Dict], cover_path: Optional[str]):
    make_zip(out_zip, files, track_meta=track_meta, cover_path=cover_path, include_cover=True)

# ---------- Views (Artwork / Geo / Abort) ----------
class ArtChoiceView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout); self.author_id = author_id; self.include_art: Optional[bool] = None
    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message("This prompt isn’t for you.", ephemeral=True); return False
        return True
    @discord.ui.button(label="Include", style=discord.ButtonStyle.primary)
    async def yes(self, itx: discord.Interaction, _: discord.ui.Button):
        self.include_art = True; [setattr(c,"disabled",True) for c in self.children]
        await itx.response.edit_message(content="Artwork will be included.", view=self); self.stop()
    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, itx: discord.Interaction, _: discord.ui.Button):
        self.include_art = False; [setattr(c,"disabled",True) for c in self.children]
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
        self.choice = "continue"; [setattr(c,"disabled",True) for c in self.children]
        await itx.response.edit_message(content="Continuing without the blocked tracks…", view=self); self.stop()
    @discord.ui.button(label="Abort", style=discord.ButtonStyle.danger)
    async def abort(self, itx: discord.Interaction, _: discord.ui.Button):
        self.choice = "abort"; [setattr(c,"disabled",True) for c in self.children]
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
        self.confirmed = True; [setattr(c,"disabled",True) for c in self.children]
        await itx.response.edit_message(content="Aborting…", view=self); self.stop()
    @discord.ui.button(label="N", style=discord.ButtonStyle.secondary)
    async def no(self, itx: discord.Interaction, _: discord.ui.Button):
        self.confirmed = False; [setattr(c,"disabled",True) for c in self.children]
        await itx.response.edit_message(content="Abort cancelled.", view=self); self.stop()

# ---------- Client / Tree ----------
intents = discord.Intents.default()  # no message_content needed
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def guild_objs() -> List[discord.Object]:
    return [discord.Object(id=i) for i in APP_GUILD_IDS]

async def sync_all():
    if APP_GUILD_IDS:
        for gid in APP_GUILD_IDS:
            await tree.sync(guild=discord.Object(id=gid))
        print(f"Slash commands synced to guilds: {APP_GUILD_IDS}")
    else:
        synced = await tree.sync()
        print(f"Global slash commands synced: {len(synced)}")

@client.event
async def on_ready():
    try: clean_stale_tmp()
    except Exception: pass
    try: await sync_all()
    except Exception as e: print(f"Slash sync error: {e}")
    print(f"Logged in as {client.user} (id={client.user.id})")
    print("Ready.")

@client.event
async def on_guild_join(guild: discord.Guild):
    try:
        await tree.sync(guild=guild)
        print(f"Synced commands for new guild: {guild.id}")
    except Exception as e:
        print(f"Guild sync error ({guild.id}): {e}")
    chan = guild.system_channel
    if not chan or not chan.permissions_for(guild.me).send_messages:
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages: chan = c; break
    if chan:
        try: await chan.send("Thanks for adding me! Use `/help` for commands.")
        except discord.HTTPException: pass

# ---------- helpers used in flow ----------
async def maybe_notify_zip(inter: discord.Interaction, link: str) -> Tuple[bool, Optional[int]]:
    is_pl, provider = detect_playlist(link)
    if not is_pl: return False, None
    if provider == "spotify":
        await eph_send(inter, "Spotify playlists aren’t supported."); return False, None
    count = await probe_playlist_count(link)
    if count is not None and count > 5:
        prov = "YouTube" if provider == "youtube" else ("SoundCloud" if provider == "soundcloud" else "Bandcamp")
        await eph_send(inter, f"Detected a {prov} playlist with {count} tracks.\nIt exceeds 5 tracks, so I will zip the audio.")
        return True, count
    return False, count

async def ask_artwork(inter: discord.Interaction, *, big_zip: bool) -> bool:
    v = ArtChoiceView(inter.user.id, timeout=30)
    prompt = "Include Artwork in the .zip?" if big_zip else "Include Artwork? (Yes will zip the file.)"
    m = await eph_send(inter, prompt, view=v)
    await v.wait()
    try: await m.edit(view=None)
    except discord.HTTPException: pass
    return bool(v.include_art)

def user_has_active(uid: int) -> bool: return uid in ACTIVE_RIPS
def clear_active(uid: int): ACTIVE_RIPS.pop(uid, None)

# ---------- ripping flow ----------
async def run_rip(inter: discord.Interaction, link: str, *, to_dm: bool):
    big_zip, _ = await maybe_notify_zip(inter, link)
    include_art = await ask_artwork(inter, big_zip=big_zip)

    pr = ProgressReporter(inter); await pr.start()
    est_count = await probe_playlist_count(link)
    if est_count and est_count > 0: pr.total_tracks = est_count

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    ACTIVE_RIPS[inter.user.id] = {"pr": pr, "tmpdir": tmpdir}

    try:
        items, title, is_pl, meta, cover, caplog = await download_all_to_mp3(link, tmpdir, include_thumbs=include_art, pr=pr)

        # Geo-restriction handling
        geo_lines = [ln for ln in (caplog.errors + caplog.warnings)
                     if "geo" in ln.lower() or "available from your location" in ln.lower()]
        skipped = sum(1 for m in meta if not m.get("title"))
        if geo_lines and is_pl and skipped > 0 and not pr.cancelled:
            v = GeoDecisionView(inter.user.id)
            prompt = await eph_send(inter, f"Some tracks appear geo-restricted ({skipped} skipped). Continue without them or abort?", view=v)
            await v.wait()
            try: await prompt.edit(view=None)
            except discord.HTTPException: pass
            if v.choice != "continue":
                await eph_send(inter, "Aborted.")
                raise yt_dlp.utils.DownloadError("User aborted after geo prompt")
            items = [it for it in items if os.path.exists(it[0])]
            meta  = [m for m in meta  if m.get("title")]

        if pr.cancelled:
            raise yt_dlp.utils.DownloadError("User aborted")

        await pr.replace("Preparing files…")
        attribution = build_attribution(inter, link)

        # Upload decisions
        if include_art:
            safe = _safe_base(title); zip_path = os.path.join(tmpdir, f"{safe}.zip")
            make_single_zip(items, zip_path, track_meta=meta, cover_path=cover)
            try:
                if os.path.getsize(zip_path) > MAX_FILE_BYTES_HINT:
                    await pr.replace("Preparing files… (note: zip may exceed this server's upload cap)")
            except Exception: pass
            await pr.replace("Uploading zip…")
            try:
                if to_dm:
                    dm = await inter.user.create_dm()
                    await dm.send(content=attribution, file=discord.File(zip_path, filename=os.path.basename(zip_path)))
                else:
                    await inter.channel.send(content=attribution, file=discord.File(zip_path, filename=os.path.basename(zip_path)))
            except discord.HTTPException:
                await pr.replace("Zip too large. Splitting into parts…")
                parts = pack_into_zip_parts(_safe_base(safe), tmpdir, items, track_meta=meta, cover_path=cover, size_limit=MAX_FILE_BYTES_HINT)
                payload = [discord.File(p, filename=os.path.basename(p)) for p in parts]
                if to_dm:
                    dm = await inter.user.create_dm(); await dm.send(content=attribution, files=payload)
                else:
                    await inter.channel.send(content=attribution, files=payload)
        else:
            if is_pl and len(items) > 5:
                safe = _safe_base(title); zip_path = os.path.join(tmpdir, f"{safe}.zip")
                make_single_zip(items, zip_path, track_meta=meta, cover_path=None)
                await pr.replace("Uploading zip…")
                try:
                    if to_dm:
                        dm = await inter.user.create_dm()
                        await dm.send(content=attribution, file=discord.File(zip_path, filename=os.path.basename(zip_path)))
                    else:
                        await inter.channel.send(content=attribution, file=discord.File(zip_path, filename=os.path.basename(zip_path)))
                except discord.HTTPException:
                    await pr.replace("Zip too large. Splitting into parts…")
                    parts = pack_into_zip_parts(_safe_base(safe), tmpdir, items, track_meta=meta, cover_path=None, size_limit=MAX_FILE_BYTES_HINT)
                    payload = [discord.File(p, filename=os.path.basename(p)) for p in parts]
                    if to_dm:
                        dm = await inter.user.create_dm(); await dm.send(content=attribution, files=payload)
                    else:
                        await inter.channel.send(content=attribution, files=payload)
            else:
                await pr.replace("Uploading…")
                if len(items) == 1:
                    p,n = items[0]
                    if to_dm:
                        dm = await inter.user.create_dm()
                        await dm.send(content=attribution, file=discord.File(p, filename=n))
                    else:
                        await inter.channel.send(content=attribution, file=discord.File(p, filename=n))
                else:
                    payload = [discord.File(p, filename=n) for p,n in items]
                    try:
                        if to_dm:
                            dm = await inter.user.create_dm(); await dm.send(content=attribution, files=payload)
                        else:
                            await inter.channel.send(content=attribution, files=payload)
                    except discord.HTTPException:
                        # emergency zip-split
                        base = _safe_base("bundle")
                        parts = pack_into_zip_parts(base, tmpdir, items,
                            track_meta=[{"index":i+1,"title":n} for i,(_,n) in enumerate(items)],
                            cover_path=None, size_limit=MAX_FILE_BYTES_HINT)
                        payload = [discord.File(p, filename=os.path.basename(p)) for p in parts]
                        if to_dm:
                            dm = await inter.user.create_dm(); await dm.send(content=attribution, files=payload)
                        else:
                            await inter.channel.send(content=attribution, files=payload)

        await pr.replace("Done.")
    except yt_dlp.utils.DownloadError as e:
        await eph_send(inter, "Aborted." if "User aborted" in str(e) else f"Rip failed: `{e}`")
    except Exception as e:
        await eph_send(inter, f"Rip failed: `{e}`")
    finally:
        clear_active(inter.user.id)
        shutil.rmtree(tmpdir, ignore_errors=True)

# ---------- Slash Commands ----------
def scope_decorator():
    return app_commands.guilds(*guild_objs()) if APP_GUILD_IDS else (lambda f: f)

@scope_decorator()
@tree.command(name="help", description="Show commands")
async def help_cmd(inter: discord.Interaction):
    await eph_send(inter, HELP_TEXT)

@scope_decorator()
@tree.command(name="abort", description="Abort your current rip")
async def abort_cmd(inter: discord.Interaction):
    job = ACTIVE_RIPS.get(inter.user.id)
    if not job:
        await eph_send(inter, "You don't have an active rip.")
        return
    view = AbortConfirmView(inter.user.id)
    confirm = await eph_send(inter, "Download currently in process, are you SURE? [Y/N]", view=view)
    await view.wait()
    try: await confirm.edit(view=None)
    except discord.HTTPException: pass
    if not view.confirmed:
        await eph_send(inter, "Abort cancelled.")
        return
    pr: ProgressReporter = job["pr"]
    pr.cancelled = True
    await eph_send(inter, "Aborted.")

@scope_decorator()
@tree.command(name="rip", description="Rip audio and post to this channel")
@app_commands.describe(link="YouTube / SoundCloud / Bandcamp link")
async def rip_cmd(inter: discord.Interaction, link: str):
    if not ok_domain(link):
        is_pl, prov = detect_playlist(link)
        if prov == "spotify":
            await eph_send(inter, "Spotify playlists aren’t supported."); return
        await eph_send(inter, "Unsupported link. Try YouTube, SoundCloud, or Bandcamp."); return
    if user_has_active(inter.user.id):
        await eph_send(inter, "You already have a rip running. Use `/abort` to cancel it."); return
    await inter.response.defer(ephemeral=True)
    await run_rip(inter, link, to_dm=False)

@scope_decorator()
@tree.command(name="ripdm", description="Rip audio and DM it to you")
@app_commands.describe(link="YouTube / SoundCloud / Bandcamp link")
async def ripdm_cmd(inter: discord.Interaction, link: str):
    if not ok_domain(link):
        is_pl, prov = detect_playlist(link)
        if prov == "spotify":
            await eph_send(inter, "Spotify playlists aren’t supported."); return
        await eph_send(inter, "Unsupported link. Try YouTube, SoundCloud, or Bandcamp."); return
    if user_has_active(inter.user.id):
        await eph_send(inter, "You already have a rip running. Use `/abort` to cancel it."); return
    await inter.response.defer(ephemeral=True)
    await run_rip(inter, link, to_dm=True)

@scope_decorator()
@tree.command(name="sync", description="(Owner) Force re-sync of slash commands here")
async def sync_here(inter: discord.Interaction):
    app = await client.application_info()
    if inter.user.id != app.owner.id:
        await inter.response.send_message("Only the bot owner can run this.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    try:
        res = await tree.sync(guild=inter.guild)
        await inter.followup.send(f"Synced {len(res)} command(s) to this guild.", ephemeral=True)
    except Exception as e:
        await inter.followup.send(f"Sync failed: `{e}`", ephemeral=True)

# ---------- main ----------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment.")
    client.run(TOKEN)
