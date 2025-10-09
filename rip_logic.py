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

def derive_zip_basename(info: dict) -> str:
    """
    Prefer: Artist - Album
    Else:   Artist - Title
    Else:   Title
    Fallback: rip
    """
    entry = info
    if info and isinstance(info.get("entries"), list) and info["entries"]:
        entry = info["entries"][0] or {}

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
    return (
        f"🎶 **{title}**\n"
        f"```"
        f"[{bar}]  {pct}%{eta_part}{abr_part}"
        f"```"
    )

def render_ffmpeg_block(title: str) -> str:
    chunk = FILLED_CHAR * (BAR_LEN // 3)
    bar = chunk + (EMPTY_CHAR * (BAR_LEN - len(chunk)))
    return (
        f"🎶 **{title}**\n"
        f"```"
        f"[{bar}]  converting…  @{TARGET_ABR} kbps"
        f"```"
    )

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

def normalize_entries(info: dict) -> list[dict]:
    """Return a list of extractor entries (single becomes [info])."""
    if not info:
        return []
    if isinstance(info.get("entries"), list) and info["entries"]:
        return [e or {} for e in info["entries"]]
    return [info]

def build_track_docs(session_dir: str, info: dict, ripped_files: list[str]) -> list[str]:
    """
    Create TRACKLIST.txt, metadata.json, and playlist.m3u8 in session_dir.
    Returns the list of created file paths (to be bundled into ZIP Part 1).
    """
    entries = normalize_entries(info)

    # Build a canonical per-track list with best-effort fields
    tracks = []
    for i, e in enumerate(entries, start=1):
        title  = e.get("track") or e.get("title") or ""
        artist = e.get("artist") or e.get("uploader") or e.get("channel") or ""
        album  = e.get("album") or info.get("playlist_title") or info.get("playlist") or ""
        idx    = e.get("playlist_index") or e.get("track_number") or i
        dur    = e.get("duration")  # seconds
        # Find the actual ripped filename that likely matches this entry (best effort by index)
        filename = None
        if ripped_files:
            if 0 < i <= len(ripped_files):
                filename = os.path.basename(ripped_files[i-1])
            else:
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

    # TRACKLIST.txt
    tl_lines = []
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
        tl_lines.append(line)
    tl_path = os.path.join(session_dir, "TRACKLIST.txt")
    with open(tl_path, "w", encoding="utf-8") as f:
        f.write("\n".join(tl_lines) + "\n")

    # metadata.json
    meta = {
        "zip_basename": derive_zip_basename(info),
        "count": len(tracks),
        "tracks": tracks,
    }
    meta_path = os.path.join(session_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # playlist.m3u8
    m3u_lines = ["#EXTM3U"]
    for t in tracks:
        artist = t["artist"] or "Unknown Artist"
        title  = t["title"]  or (t["filename"] or "Unknown Title")
        dur    = int(t["duration"]) if t["duration"] else -1
        fn     = t["filename"] or title
        m3u_lines.append(f"#EXTINF:{dur},{artist} - {title}")
        m3u_lines.append(fn)
    m3u_path = os.path.join(session_dir, "playlist.m3u8")
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u_lines) + "\n")

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

    # Immediately switch to "Initializing..." on the SAME ephemeral message
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

    # ---- public notice (to be replaced with the final kangaroo summary) ----
    public_msg = await interaction.channel.send(f"{interaction.user.mention} is ripping audio... 🎧")

    # ---- temp working folder ----
    session_dir = tempfile.mkdtemp(prefix="ripperroo_")

    # ---- progress state + background animator ----
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

    # ---- progress hook (authoritative) ----
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

        asyncio.run_coroutine_threadsafe(asyncio.to_thread(upd), loop)

    # ---- yt-dlp options ----
    # NOTE: We add FFmpegMetadata to write ID3 tags into MP3s.
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
        "logger": _QuietLogger(),
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "noplaylist": False,
        "writethumbnail": include_art,    # may be disabled for playlists below
        "skip_download": False,
        # speed/robustness
        "concurrent_fragment_downloads": 5,
        "retries": 5,
        "fragment_retries": 5,
        "buffersize": 128 * 1024,
        "continuedl": True,
        "socket_timeout": 10,
        # progress
        "progress_hooks": [progress_hook],
        # audio extraction + metadata tags
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(TARGET_ABR)},
            {"key": "FFmpegMetadata"},  # <- write ID3 tags (title/artist/album/track)
        ],
    }

    # ---- fetch info (playlist size & shared art) ----
    info = await asyncio.to_thread(extract_info, link, ydl_opts)

    # stop "Initializing..."
    init_active = False
    try:
        await init_task
    except Exception:
        pass

    entries = normalize_entries(info)
    total = len(entries)
    await ephemeral.edit(content=f"🎧 Ripping {total or 'unknown'} track(s)...")

    # base name for zip parts
    zip_base = derive_zip_basename(info)

    # ---- album art optimization (once for sets) ----
    if include_art and total and total > 1:
        # We avoid per-track thumbnails; if you ever want embedded art per track,
        # you'd need to fetch/embed per-file. Here we just keep a shared image.
        ydl_opts["writethumbnail"] = False
        thumb = (info.get("entries") or [{}])[0].get("thumbnail")
        if thumb:
            await ephemeral.edit(content="🎨 Downloading shared album art...")
            art_path = os.path.join(session_dir, "album_art.jpg")
            try:
                await asyncio.to_thread(download_file, thumb, art_path)
            except Exception:
                pass
            await ephemeral.edit(content="🎨 Shared album art saved. Continuing rip...")

    # ---- run rip + start animator ----
    anim_task = asyncio.create_task(animator())
    await asyncio.to_thread(run_ytdlp, link, ydl_opts)
    await asyncio.sleep(0.4)  # let handles close

    state["active"] = False
    try:
        await anim_task
    except Exception:
        pass

    # ---- collect output files ----
    files = [os.path.join(session_dir, f) for f in os.listdir(session_dir) if f.lower().endswith(".mp3")]
    files.sort()
    total_done = len(files)
    if total_done == 0:
        await ephemeral.edit(content="❌ No audio files were downloaded.")
        clean_dir(session_dir)
        return

    # ---- create track docs (TRACKLIST.txt, metadata.json, playlist.m3u8) ----
    extra_docs = build_track_docs(session_dir, info, files)

    # ---- detect server upload limit (bytes) ----
    upload_limit = int(getattr(interaction.guild, "filesize_limit", 8 * 1024 * 1024))
    target_part_size = max(1, upload_limit - 256 * 1024)  # headroom

    # ---- zip into parts under limit, using the derived base name (docs go in Part 1) ----
    await ephemeral.edit(content="📦 Packaging tracks for Discord…")
    parts = build_zip_parts(files, session_dir, target_part_size, zip_base, extra_first=extra_docs)
    if not parts:
        too_big = max((os.path.getsize(f), f) for f in files)[1]
        mb = round(os.path.getsize(too_big) / (1024 * 1024), 2)
        await ephemeral.edit(content=f"⚠️ A track is {mb} MB and exceeds this server’s upload limit. Unable to send.")
        clean_dir(session_dir)
        return

    # ---- delete the original public notice and post summary WITH first part attached ----
    try:
        await public_msg.delete()
    except Exception:
        pass

    elapsed = int(time.monotonic() - start_ts)
    mins, secs = divmod(elapsed, 60)
    elapsed_text = f"{mins:02d}:{secs:02d}"
    source_md = f"[Source]({link})"

    first = parts[0]
    summary_msg = None
    try:
        summary_msg = await interaction.channel.send(
            content=(
                f"{interaction.user.mention} ripped 🎶 **{total_done} track(s)** "
                f"for {elapsed_text} @ {TARGET_ABR} kbps · {source_md} 🦘"
            ),
            file=discord.File(first),
        )
    except Exception:
        summary_msg = await interaction.channel.send(
            content=(
                f"{interaction.user.mention} ripped 🎶 **{total_done} track(s)** "
                f"for {elapsed_text} @ {TARGET_ABR} kbps · {source_md} 🦘"
            )
        )

    # ---- reply with remaining parts under the summary ----
    for i, zp in enumerate(parts[1:], start=2):
        try:
            await interaction.channel.send(
                content=f"📦 {zip_base} — Part {i}/{len(parts)}",
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
    Uses ZIP_STORED (no compression) so size ~ sum(mp3 sizes) + tiny header.
    Names like: 'Artist - Album_part_01.zip'
    Ensures extra_first files (tracklist, metadata, m3u8) go into Part 1.
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

    # Start Part 1 with docs if they fit
    idx = 1
    for doc in extra_first:
        size = os.path.getsize(doc)
        if total_in_bundle and (total_in_bundle + size) > part_limit_bytes:
            zp = flush_bundle(idx); 
            if zp: parts.append(zp); idx += 1
            bundle = []; total_in_bundle = 0
        bundle.append(doc); total_in_bundle += size

    # Add audio files into parts
    for fp in files:
        size = os.path.getsize(fp)
        if size > part_limit_bytes and len(files) == 1:
            return []  # a single huge file can't be sent
        if total_in_bundle and (total_in_bundle + size) > part_limit_bytes:
            zp = flush_bundle(idx)
            if zp:
                parts.append(zp); idx += 1
            bundle = []; total_in_bundle = 0
        bundle.append(fp); total_in_bundle += size

    # flush last bundle
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
        return ydl.extract_info(link, download=False)

# simple HTTP fetcher (for shared album art)
def download_file(url: str, path: str):
    import requests
    r = requests.get(url, stream=True, timeout=10)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(1024):
            f.write(chunk)
