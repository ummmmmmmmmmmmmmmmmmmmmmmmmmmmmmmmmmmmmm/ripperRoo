# rip_logic.py
import discord, asyncio, os, tempfile, yt_dlp, time, zipfile, re, json
from ui_components import ArtChoice, ZipChoice
from utils import validate_link, clean_dir
from config import ALLOWED_DOMAINS

TARGET_ABR = 192   # kbps
BAR_LEN     = 50   # progress bar width
FILLED_CHAR = "♪"
EMPTY_CHAR  = "-"

# -------------------- Quiet logger for yt-dlp (no terminal progress) --------------------
class _QuietLogger:
    def debug(self, msg):  # swallow progress lines
        pass
    def info(self, msg):
        pass
    def warning(self, msg):
        pass
    def error(self, msg):
        print(msg)

# -------------------- helpers: safe names & rendering --------------------
SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9 \-_.]+")

def safe_name(s: str, maxlen: int = 80) -> str:
    s = SAFE_CHARS_RE.sub("_", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen]).strip() or "rip"

def derive_zip_basename(info: dict | None) -> str:
    if not info:
        return "rip"
    entry = info
    if isinstance(info.get("entries"), list) and info["entries"]:
        entry = next((e for e in info["entries"] if e), {}) or {}
    artist = (
        entry.get("artist")
        or entry.get("uploader")
        or entry.get("channel")
        or info.get("uploader")
        or info.get("channel")
        or ""
    )
    album = entry.get("album") or info.get("playlist_title") or info.get("playlist") or ""
    title = entry.get("track") or entry.get("title") or info.get("title") or ""
    if artist and album:
        return safe_name(f"{artist} - {album}")
    if artist and title:
        return safe_name(f"{artist} - {title}")
    if title:
        return safe_name(title)
    return "rip"

def render_progress_block(title: str, p01: float, eta_s: int | None, abr_kbps: int | None) -> str:
    p = max(0.0, min(1.0, float(p01)))
    pct = int(round(p * 100))
    filled = int(round(BAR_LEN * p))
    bar = (FILLED_CHAR * filled) + (EMPTY_CHAR * (BAR_LEN - filled))
    eta_part = f"  ETA {int(eta_s)}s" if eta_s and eta_s > 0 else ""
    abr_part = f"  @{abr_kbps} kbps" if abr_kbps else ""
    return f"🎶 **{title}**\n```[{bar}]  {pct}%{eta_part}{abr_part}```"

def render_ffmpeg_block(title: str) -> str:
    chunk = FILLED_CHAR * (BAR_LEN // 3)
    bar = chunk + (EMPTY_CHAR * (BAR_LEN - len(chunk)))
    return f"🎶 **{title}**\n```[{bar}]  converting…  @{TARGET_ABR} kbps```"

def render_initializing_frame(dot_count: int) -> str:
    dots = "." * (1 + (dot_count % 3))
    return f"🛠️ Initializing{dots}"

# -------------------- metadata helpers --------------------
def _seconds_to_hmmss(sec: int | float | None) -> str:
    if not sec:
        return "--:--"
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"

def normalize_entries(info: dict | None) -> list[dict]:
    if not info:
        return []
    entries = info.get("entries")
    if isinstance(entries, list) and entries:
        return [e or {} for e in entries if e is not None]
    return [info]

def first_thumbnail_url(obj: dict | None) -> str | None:
    if not obj or not isinstance(obj, dict):
        return None
    t = obj.get("thumbnail")
    if isinstance(t, str) and t:
        return t
    thumbs = obj.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        first = thumbs[0] or {}
        return first.get("url") or first.get("id")
    return None

def find_shared_art_url(info: dict | None) -> str | None:
    if not info:
        return None
    t = first_thumbnail_url(info)
    if t:
        return t
    entries = info.get("entries")
    if isinstance(entries, list):
        for e in entries:
            t = first_thumbnail_url(e)
            if t:
                return t
    return None

def build_track_docs(session_dir: str, info: dict | None, ripped_files: list[str]) -> list[str]:
    entries = normalize_entries(info)
    tracks = []
    for i, e in enumerate(entries, start=1):
        title  = e.get("track") or e.get("title") or ""
        artist = e.get("artist") or e.get("uploader") or e.get("channel") or ""
        album  = e.get("album") or (info.get("playlist_title") if info else "") or (info.get("playlist") if info else "") or ""
        idx    = e.get("playlist_index") or e.get("track_number") or i
        dur    = e.get("duration")
        filename = None
        if ripped_files:
            filename = os.path.basename(ripped_files[min(len(ripped_files)-1, i-1)])
        tracks.append({
            "index": idx,
            "title": title,
            "artist": artist,
            "album": album,
            "duration": dur,
            "duration_hmmss": _seconds_to_hmmss(dur),
            "filename": filename,
        })

    tl_path = os.path.join(session_dir, "TRACKLIST.txt")
    with open(tl_path, "w", encoding="utf-8") as f:
        for t in tracks:
            idx = f"{int(t['index']):02d}" if isinstance(t["index"], int) else "--"
            artist = t["artist"] or "Unknown Artist"
            title = t["title"] or (t["filename"] or "Unknown Title")
            album = t["album"] or ""
            dur = t["duration_hmmss"]
            line = f"{idx}. {artist} — {title}"
            if album:
                line += f"  ({album})"
            line += f"  [{dur}]"
            f.write(line + "\n")

    meta = {"zip_basename": derive_zip_basename(info), "count": len(tracks), "tracks": tracks}
    meta_path = os.path.join(session_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    m3u_path = os.path.join(session_dir, "playlist.m3u8")
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for t in tracks:
            artist = t["artist"] or "Unknown Artist"
            title  = t["title"]  or (t["filename"] or "Unknown Title")
            dur    = int(t["duration"]) if t["duration"] else -1
            fn     = t["filename"] or title
            f.write(f"#EXTINF:{dur},{artist} - {title}\n{fn}\n")
    return [tl_path, meta_path, m3u_path]

# ==============================================================
# MAIN RIP COMMAND
# ==============================================================

async def handle_rip(interaction: discord.Interaction, link: str):
    start_ts = time.monotonic()

    await interaction.response.defer(ephemeral=True, thinking=True)
    ephemeral = await interaction.followup.send("✅ Received. Checking link...", ephemeral=True)

    # ---- validate domain ----
    if not validate_link(link, ALLOWED_DOMAINS):
        await ephemeral.edit(content="❌ Unsupported or invalid link.")
        return

    # ---- ask for album art ----
    art_view = ArtChoice()
    await ephemeral.edit(content="🎨 Include album art?", view=art_view)
    await art_view.wait()
    include_art = art_view.choice or False

    # Thank-you → then Initializing…
    await ephemeral.edit(content="Thank you for using Ripper Roo, your download will begin momentarily... 🦘", view=None)
    await asyncio.sleep(0.8)

    init_active = True
    async def init_anim():
        i = 0
        while init_active:
            try:
                await ephemeral.edit(content=render_initializing_frame(i))
            except Exception:
                pass
            i += 1
            await asyncio.sleep(0.35)
    init_task = asyncio.create_task(init_anim())

    # ---- public animated header (jumping kangaroo + counter) ----
    public_msg = await interaction.channel.send(f"{interaction.user.mention} is ripping audio… 🦘")
    public_state = {"dots": 0, "completed": 0, "total": None, "active": True, "runner_pos": 0, "frame": 0}
    TRACK_LEN = 18
    def make_runner_line():
        pos = public_state["runner_pos"] % TRACK_LEN
        jump = (public_state["frame"] % 2 == 1)
        ground = "-" * TRACK_LEN
        sprite = "🦘" if not jump else "ᴗ🦘"
        line = list(ground)
        # place sprite bounded
        if 0 <= pos < TRACK_LEN:
            line[pos] = " "
        runner = "".join(line)
        # show as code line for monospacing
        return f"`{runner[:pos]}{sprite}{runner[pos+1:]}`"

    async def public_anim():
        while public_state["active"]:
            dots = "." * (1 + (public_state["dots"] % 3))
            total = public_state["total"]
            runner = make_runner_line()
            if total is None:
                text = f"{interaction.user.mention} is ripping audio{dots} {runner} 🦘"
            else:
                text = f"{interaction.user.mention} is ripping audio{dots} ({public_state['completed']}/{total}) {runner} 🦘"
            try:
                await public_msg.edit(content=text)
            except Exception:
                pass
            public_state["dots"] += 1
            public_state["runner_pos"] += 1
            public_state["frame"] += 1
            await asyncio.sleep(0.95)
    public_task = asyncio.create_task(public_anim())

    # ---- temp working folder ----
    session_dir = tempfile.mkdtemp(prefix="ripperroo_")

    # ---- progress state + background animator (ephemeral card) ----
    loop = asyncio.get_running_loop()
    state = {
        "title": "…",
        "downloaded": 0,
        "total": None,
        "eta": None,
        "abr": TARGET_ABR,
        "speed": None,        # bytes/sec
        "p01": 0.0,           # 0..1
        "status": "idle",     # downloading | postprocessing | finished
        "active": True,
        "last_ts": time.monotonic(),
    }
    download_counters = {"ok": 0, "skipped": 0}

    async def animator():
        tick = 0.13  # ~7–8 fps
        while state["active"]:
            try:
                p = state["p01"]
                if state["status"] == "downloading" and state["total"] and state["speed"]:
                    now = time.monotonic()
                    dt = now - state["last_ts"]
                    state["last_ts"] = now
                    est_bytes = state["downloaded"] + state["speed"] * dt
                    p = min(1.0, max(0.0, float(est_bytes) / float(state["total"])))
                if state["status"] == "postprocessing":
                    await ephemeral.edit(content=render_ffmpeg_block(state["title"]))
                else:
                    await ephemeral.edit(content=render_progress_block(state["title"], p, state["eta"], state["abr"]))
            except Exception:
                pass
            await asyncio.sleep(tick)

    def progress_hook(d):
        def upd():
            status = d.get("status")
            filename = d.get("filename") or "unknown"
            state["title"] = os.path.splitext(os.path.basename(filename))[0]
            state["abr"] = d.get("abr", TARGET_ABR)

            if status == "downloading":
                state["status"] = "downloading"
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                dl    = d.get("downloaded_bytes")
                speed = d.get("speed")
                frag_count = d.get("fragment_count") or d.get("n_fragments")
                frag_idx   = d.get("fragment_index")
                p01 = None
                if total and dl is not None:
                    state["total"] = total
                    state["downloaded"] = dl
                    p01 = max(0.0, min(1.0, dl / total))
                elif frag_count:
                    p01 = max(0.0, min(1.0, ((frag_idx or 0) + 1) / float(frag_count)))
                elif "_percent_str" in d:
                    try:
                        p01 = float(d["_percent_str"].replace("%", "")) / 100.0
                    except Exception:
                        p01 = None
                if speed:
                    state["speed"] = speed
                state["eta"] = d.get("eta")
                if p01 is not None:
                    state["p01"] = p01
                state["last_ts"] = time.monotonic()
            elif status == "postprocessing":
                state["status"] = "postprocessing"
                state["eta"] = None
            elif status == "finished":
                state["status"] = "finished"
                state["p01"] = 1.0
                state["eta"] = 0
                public_state["completed"] += 1
                download_counters["ok"] += 1
            elif status == "error":
                download_counters["skipped"] += 1
        loop.call_soon_threadsafe(upd)

    # ---- yt-dlp options (robust for YT playlists) ----
    ydl_opts = {
        "format": "ba[ext=m4a]/ba[acodec^=mp4a]/ba[ext=webm]/ba/bestaudio/best",
        "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
        "logger": _QuietLogger(),
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "ignore_no_formats_error": True,
        "extractor_retries": 3,
        "skip_unavailable_fragments": True,
        "retries": 5,
        "fragment_retries": 5,
        "buffersize": 128 * 1024,
        "continuedl": True,
        "socket_timeout": 10,
        "noplaylist": False,
        "writethumbnail": include_art,
        "skip_download": False,
        "progress_hooks": [progress_hook],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(TARGET_ABR)},
            {"key": "FFmpegMetadata"},
        ],
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }

    # ---- fetch info (playlist size & shared art) ----
    info = await asyncio.to_thread(extract_info, link, ydl_opts)  # may be None

    # stop "Initializing..."
    init_active = False
    try:
        await init_task
    except Exception:
        pass

    entries = normalize_entries(info)
    total = len(entries) if entries else None
    public_state["total"] = total
    await ephemeral.edit(content=f"🎧 Ripping {total if total is not None else 'unknown'} track(s)...")

    zip_base = derive_zip_basename(info)

    # shared album art (once for sets)
    if include_art and (total or 0) > 1:
        thumb_url = find_shared_art_url(info)
        if thumb_url:
            await ephemeral.edit(content="🎨 Downloading shared album art...")
            art_path = os.path.join(session_dir, "album_art.jpg")
            try:
                await asyncio.to_thread(download_file, thumb_url, art_path)
                await ephemeral.edit(content="🎨 Shared album art saved. Continuing rip...")
            except Exception:
                await ephemeral.edit(content="🎨 Continuing rip...")

    # ---- run rip + start animator ----
    anim_task = asyncio.create_task(animator())
    await asyncio.to_thread(run_ytdlp, link, ydl_opts)
    await asyncio.sleep(0.4)

    state["active"] = False
    try:
        await anim_task
    except Exception:
        pass

    # ---- collect output files ----
    files = [os.path.join(session_dir, f) for f in os.listdir(session_dir) if f.lower().endswith(".mp3")]
    files.sort()
    total_done = len(files)

    # Info for skipped items
    if total and total_done < total:
        skipped = total - total_done
        try:
            await ephemeral.edit(content=f"ℹ️ Some items had no playable audio and were skipped ({skipped}/{total}). Continuing…")
            await asyncio.sleep(0.6)
        except Exception:
            pass

    if total_done == 0:
        public_state["active"] = False
        try:
            await public_task
        except Exception:
            pass
        await ephemeral.edit(content="❌ No audio files were downloaded.")
        clean_dir(session_dir)
        return

    # ---- build docs ----
    extra_docs = build_track_docs(session_dir, info, files)

    # ---- detect server upload limit (bytes) ----
    upload_limit = int(getattr(interaction.guild, "filesize_limit", 8 * 1024 * 1024))
    target_part_size = max(1, upload_limit - 256 * 1024)

    # ---- zip into parts (docs in Part 1) ----
    await ephemeral.edit(content="📦 Packaging tracks for Discord…")
    parts = build_zip_parts(files, session_dir, target_part_size, zip_base, extra_first=extra_docs)
    if not parts:
        public_state["active"] = False
        try:
            await public_task
        except Exception:
            pass
        too_big = max((os.path.getsize(f), f) for f in files)[1]
        mb = round(os.path.getsize(too_big) / (1024 * 1024), 2)
        await ephemeral.edit(content=f"⚠️ A track is {mb} MB and exceeds this server’s upload limit. Unable to send.")
        clean_dir(session_dir)
        return

    # ---- close public header animator ----
    public_state["active"] = False
    try:
        await public_task
    except Exception:
        pass
    try:
        await public_msg.delete()
    except Exception:
        pass

    elapsed = int(time.monotonic() - start_ts)
    mins, secs = divmod(elapsed, 60)
    elapsed_text = f"{mins:02d}:{secs:02d}"
    source_md = f"[Source]({link})"

    # ---- ALWAYS attach zips on the SAME final message ----
    summary_content = (
        f"{interaction.user.mention} ripped 🎶 **{total_done} track(s)** "
        f"for {elapsed_text} @ {TARGET_ABR} kbps · {source_md} 🦘 — **Download below ⤵️**"
    )

    async def send_summary_with_files():
        # try to attach ALL parts first (Discord allows up to 10 files/message)
        for k in range(len(parts), 0, -1):
            try:
                files_to_send = [discord.File(p) for p in parts[:k]]
                return await interaction.channel.send(content=summary_content, files=files_to_send)
            except Exception:
                continue
        # absolute fallback: attach at least Part 1 (fresh handle) or send text
        try:
            return await interaction.channel.send(content=summary_content, file=discord.File(parts[0]))
        except Exception:
            return await interaction.channel.send(content=summary_content)

    summary_msg = await send_summary_with_files()

    # If not all parts fit in one message, post the remainder as a reply (rare)
    if summary_msg and len(parts) > 10:
        for zp in parts[10:]:
            try:
                await interaction.channel.send(
                    content=None,
                    file=discord.File(zp),
                    reference=summary_msg
                )
            except Exception:
                pass
            await asyncio.sleep(0.25)

    await ephemeral.edit(content="✅ Done! Cleaning up…")
    clean_dir(session_dir)
    try:
        await asyncio.sleep(1.0)
        await ephemeral.delete()
    except Exception:
        pass

# ==============================================================
# ZIP PARTITIONING
# ==============================================================

def build_zip_parts(
    files: list[str],
    session_dir: str,
    part_limit_bytes: int,
    base_name: str,
    extra_first: list[str] | None = None
) -> list[str]:
    """
    Create multiple ZIP files, each <= part_limit_bytes.
    ZIP_STORED (no compression). Docs (extra_first) are added to Part 1.
    Names like: 'Artist - Album_part_01.zip'
    """
    extra_first = extra_first or []
    parts: list[str] = []
    bundle: list[str] = []
    total_in_bundle = 0

    def flush_bundle(index: int):
        if not bundle:
            return None
        zip_name = os.path.join(session_dir, f"{base_name}_part_{index:02d}.zip")
        with zipfile.ZipFile(zip_name, "w", compression=zipfile.ZIP_STORED) as zf:
            for fp in bundle:
                arcname = os.path.basename(fp)
                zf.write(fp, arcname)
        return zip_name

    idx = 1
    # Put docs first into Part 1
    for doc in extra_first:
        size = os.path.getsize(doc)
        if total_in_bundle and (total_in_bundle + size) > part_limit_bytes:
            zp = flush_bundle(idx)
            if zp: parts.append(zp); idx += 1
            bundle = []; total_in_bundle = 0
        bundle.append(doc); total_in_bundle += size

    # Add audio files
    for fp in files:
        size = os.path.getsize(fp)
        if size > part_limit_bytes and len(files) == 1:
            return []
        if total_in_bundle and (total_in_bundle + size) > part_limit_bytes:
            zp = flush_bundle(idx)
            if zp:
                parts.append(zp); idx += 1
            bundle = []; total_in_bundle = 0
        bundle.append(fp); total_in_bundle += size

    zp = flush_bundle(idx)
    if zp:
        parts.append(zp)

    return parts

# ==============================================================
# yt-dlp helpers
# ==============================================================

def run_ytdlp(link, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([link])

def extract_info(link, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            return ydl.extract_info(link, download=False)
        except Exception:
            return None

def download_file(url: str, path: str):
    import requests
    r = requests.get(url, stream=True, timeout=10)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(1024):
            f.write(chunk)
