# rip_logic.py
import os, tempfile, asyncio
from typing import List, Tuple, Dict, Optional

import discord
import yt_dlp

from config import FFMPEG_BIN, MAX_FILE_BYTES_HINT
from progress import ProgressReporter
from views import ArtChoiceView, GeoDecisionView
from utils import (
    eph_send, detect_playlist, ok_domain, build_attribution, _safe_base,
    pack_into_zip_parts, make_single_zip
)

# Public help text (imported by commands.py)
HELP_TEXT = (
    "**ripperRoo — a Discord mp3 ripper created by d-rod**\n"
    "• `/rip <link>` — rip YouTube, SoundCloud, or Bandcamp audio and post it here\n"
    "• `/ripdm <link>` — rip and DM you the file\n"
    "• `/abort` — abort **your** current rip\n"
    "• `/help` — show this help\n"
    "• `/sync` — owner-only; re-sync slash commands in this server\n"
)

# Track one active rip per user
ACTIVE_RIPS: Dict[int, Dict] = {}

def user_has_active(uid: int) -> bool:
    return uid in ACTIVE_RIPS

def clear_active(uid: int):
    ACTIVE_RIPS.pop(uid, None)

# ----- yt-dlp helpers -----
class _CapLog:
    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []
    def debug(self, msg): pass
    def info(self, msg):  pass
    def warning(self, msg): self.warnings.append(str(msg))
    def error(self, msg):   self.errors.append(str(msg))

def _make_hook(pr: ProgressReporter, loop: asyncio.AbstractEventLoop):
    def hook(d):
        if pr.cancelled:
            raise yt_dlp.utils.DownloadError("User aborted")
        if d.get("status") in ("downloading", "finished"):
            info = d.get("info_dict") or {}
            if info.get("title"):
                pr.current_title = info["title"]
            if info.get("playlist_index"):
                pr.current_index = int(info["playlist_index"])
            pr.d_bytes = int(d.get("downloaded_bytes") or 0)
            tbytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pr.t_bytes = int(tbytes or 0)
            if pr.t_bytes:
                pr.percent = max(0, min(100, int(pr.d_bytes * 100 / pr.t_bytes)))
            else:
                pr.percent = min(99, pr.percent + 1)
            if d.get("status") == "finished":
                pr.percent = 100
            loop.call_soon_threadsafe(asyncio.create_task, pr.update())
    return hook

def _ydl_opts(tmpdir: str, include_thumbs: bool, pr: ProgressReporter,
              loop: asyncio.AbstractEventLoop, caplog: _CapLog) -> dict:
    pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    opts = {
        "outtmpl": os.path.join(tmpdir, "%(playlist_index)03d - %(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "noprogress": True,
        "quiet": True,
        "ffmpeg_location": FFMPEG_BIN or None,
        "yesplaylist": True,
        "ignoreerrors": "only_download",
        "cachedir": False,
        "progress_hooks": [_make_hook(pr, loop)],
        "logger": caplog,
    }
    if include_thumbs:
        opts["writethumbnail"] = True
        pp.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
    opts["postprocessors"] = pp
    return opts

def _resolve_outpath(ydl: yt_dlp.YoutubeDL, entry: dict,
                     fallback=("mp3", "m4a", "webm", "opus")) -> str:
    if entry.get("requested_downloads"):
        return entry["requested_downloads"][0]["filepath"]
    base = ydl.prepare_filename(entry)
    root, _ = os.path.splitext(base)
    for ext in fallback:
        p = f"{root}.{ext}"
        if os.path.exists(p):
            return p
    return f"{root}.mp3"

def _maybe_thumb(media_path: str) -> Optional[str]:
    root, _ = os.path.splitext(media_path)
    cand = root + ".jpg"
    return cand if os.path.exists(cand) else None

async def _probe_count(link: str) -> Optional[int]:
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True, "extract_flat": True, "yesplaylist": True}) as ydl:
            info = ydl.extract_info(link, download=False)
            if isinstance(info, dict) and info.get("entries") is not None:
                return len([e for e in info["entries"] if e])
    except Exception:
        pass
    return None

async def _download_all(link: str, tmpdir: str, *, include_thumbs: bool, pr: ProgressReporter):
    loop = asyncio.get_running_loop()
    caplog = _CapLog()

    def do_download():
        with yt_dlp.YoutubeDL(_ydl_opts(tmpdir, include_thumbs, pr, loop, caplog)) as ydl:
            info = ydl.extract_info(link, download=True)
            return info, ydl

    info, ydl = await asyncio.to_thread(do_download)

    items: List[Tuple[str, str]] = []
    meta: List[Dict] = []
    cover: Optional[str] = None

    if "entries" in info and info["entries"] is not None:
        title = info.get("title") or "playlist"
        for ent in info["entries"]:
            if not ent:
                meta.append({"index": None, "title": None, "thumb": None})
                continue
            path = _resolve_outpath(ydl, ent)
            ttitle = ent.get("title") or "audio"
            root, _ = os.path.splitext(path)
            mp3 = root + ".mp3"
            final = mp3 if os.path.exists(mp3) else path
            items.append((final, f"{ttitle}.mp3"))
            idx = ent.get("playlist_index")
            thumb = _maybe_thumb(final) if include_thumbs else None
            if not cover and thumb:
                cover = thumb
            meta.append({"index": idx, "title": ttitle, "thumb": thumb})
        return items, title, True, meta, cover, caplog

    # single
    title = info.get("title") or "audio"
    path = _resolve_outpath(ydl, info)
    root, _ = os.path.splitext(path)
    mp3 = root + ".mp3"
    final = mp3 if os.path.exists(mp3) else path
    items.append((final, f"{title}.mp3"))
    thumb = _maybe_thumb(final) if include_thumbs else None
    if thumb:
        cover = thumb
    meta.append({"index": None, "title": title, "thumb": thumb})
    return items, title, False, meta, cover, caplog

# ----- prompts -----
async def _maybe_notify_zip(inter: discord.Interaction, link: str) -> Tuple[bool, Optional[int]]:
    is_pl, provider = detect_playlist(link)
    if not is_pl:
        return False, None
    if provider == "spotify":
        await eph_send(inter, "Spotify playlists aren’t supported.")
        return False, None
    count = await _probe_count(link)
    if count is not None and count > 5:
        prov = "YouTube" if provider == "youtube" else ("SoundCloud" if provider == "soundcloud" else "Bandcamp")
        await eph_send(inter, f"Detected a {prov} playlist with {count} tracks.\nIt exceeds 5 tracks, so I will zip the audio.")
        return True, count
    return False, count

async def _ask_artwork(inter: discord.Interaction, *, big_zip: bool) -> bool:
    view = ArtChoiceView(inter.user.id, timeout=30)
    prompt = "Include Artwork in the .zip?" if big_zip else "Include Artwork? (Yes will zip the file.)"
    msg = await eph_send(inter, prompt, view=view)
    await view.wait()
    try:
        await msg.edit(view=None)
    except discord.HTTPException:
        pass
    return bool(view.include_art)

# ----- main public entry -----
async def run_rip(inter: discord.Interaction, link: str, *, to_dm: bool):
    if not ok_domain(link):
        await eph_send(inter, "Unsupported link. Try YouTube, SoundCloud, or Bandcamp.")
        return

    big_zip, _ = await _maybe_notify_zip(inter, link)
    include_art = await _ask_artwork(inter, big_zip=big_zip)

    pr = ProgressReporter(inter)
    await pr.start()

    est_count = await _probe_count(link)
    if est_count and est_count > 0:
        pr.total_tracks = est_count

    tmpdir = tempfile.mkdtemp(prefix="rip-")
    ACTIVE_RIPS[inter.user.id] = {"pr": pr, "tmpdir": tmpdir}

    try:
        items, title, is_pl, meta, cover, caplog = await _download_all(
            link, tmpdir, include_thumbs=include_art, pr=pr
        )

        # Geo restriction pathway
        geo_lines = [ln for ln in (caplog.errors + caplog.warnings)
                     if "geo" in ln.lower() or "available from your location" in ln.lower()]
        skipped = sum(1 for m in meta if not m.get("title"))
        if geo_lines and is_pl and skipped > 0 and not pr.cancelled:
            gv = GeoDecisionView(inter.user.id)
            prompt = await eph_send(
                inter, f"Some tracks appear geo-restricted ({skipped} skipped). Continue without them or abort?",
                view=gv
            )
            await gv.wait()
            try:
                await prompt.edit(view=None)
            except discord.HTTPException:
                pass
            if gv.choice != "continue":
                await eph_send(inter, "Aborted.")
                raise yt_dlp.utils.DownloadError("User aborted after geo prompt")
            # prune missing entries
            items = [it for it in items if os.path.exists(it[0])]
            meta = [m for m in meta if m.get("title")]

        if pr.cancelled:
            raise yt_dlp.utils.DownloadError("User aborted")

        await pr.replace("Preparing files…")
        attribution = build_attribution(inter, link)

        # Decide how to deliver
        if include_art:
            safe = _safe_base(title)
            zip_path = os.path.join(tmpdir, f"{safe}.zip")
            make_single_zip(items, zip_path, track_meta=meta, cover_path=cover)
            try:
                if os.path.getsize(zip_path) > MAX_FILE_BYTES_HINT:
                    await pr.replace("Preparing files… (note: zip may exceed this server's upload cap)")
            except Exception:
                pass

            await pr.replace("Uploading zip…")
            try:
                if to_dm:
                    dm = await inter.user.create_dm()
                    await dm.send(content=attribution, file=discord.File(zip_path, filename=os.path.basename(zip_path)))
                else:
                    await inter.channel.send(content=attribution, file=discord.File(zip_path, filename=os.path.basename(zip_path)))
            except discord.HTTPException:
                await pr.replace("Zip too large. Splitting into parts…")
                parts = pack_into_zip_parts(
                    _safe_base(safe), tmpdir, items, track_meta=meta, cover_path=cover, size_limit=MAX_FILE_BYTES_HINT
                )
                payload = [discord.File(p, filename=os.path.basename(p)) for p in parts]
                if to_dm:
                    dm = await inter.user.create_dm()
                    await dm.send(content=attribution, files=payload)
                else:
                    await inter.channel.send(content=attribution, files=payload)

        else:
            if is_pl and len(items) > 5:
                safe = _safe_base(title)
                zip_path = os.path.join(tmpdir, f"{safe}.zip")
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
                    parts = pack_into_zip_parts(
                        _safe_base(safe), tmpdir, items, track_meta=meta, cover_path=None, size_limit=MAX_FILE_BYTES_HINT
                    )
                    payload = [discord.File(p, filename=os.path.basename(p)) for p in parts]
                    if to_dm:
                        dm = await inter.user.create_dm()
                        await dm.send(content=attribution, files=payload)
                    else:
                        await inter.channel.send(content=attribution, files=payload)
            else:
                await pr.replace("Uploading…")
                if len(items) == 1:
                    p, n = items[0]
                    if to_dm:
                        dm = await inter.user.create_dm()
                        await dm.send(content=attribution, file=discord.File(p, filename=n))
                    else:
                        await inter.channel.send(content=attribution, file=discord.File(p, filename=n))
                else:
                    payload = [discord.File(p, filename=n) for p, n in items]
                    try:
                        if to_dm:
                            dm = await inter.user.create_dm()
                            await dm.send(content=attribution, files=payload)
                        else:
                            await inter.channel.send(content=attribution, files=payload)
                    except discord.HTTPException:
                        # Fallback: split into zip parts
                        base = _safe_base("bundle")
                        parts = pack_into_zip_parts(
                            base, tmpdir, items,
                            track_meta=[{"index": i + 1, "title": n} for i, (_, n) in enumerate(items)],
                            cover_path=None, size_limit=MAX_FILE_BYTES_HINT
                        )
                        payload = [discord.File(p, filename=os.path.basename(p)) for p in parts]
                        if to_dm:
                            dm = await inter.user.create_dm()
                            await dm.send(content=attribution, files=payload)
                        else:
                            await inter.channel.send(content=attribution, files=payload)

        await pr.replace("Done.")

    except yt_dlp.utils.DownloadError as e:
        await eph_send(inter, "Aborted." if "User aborted" in str(e) else f"Rip failed: `{e}`")
    except Exception as e:
        await eph_send(inter, f"Rip failed: `{e}`")
    finally:
        clear_active(inter.user.id)
        # clean temp
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
