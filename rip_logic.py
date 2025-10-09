import discord, asyncio, os, tempfile, yt_dlp, time, zipfile
from ui_components import ArtChoice, ZipChoice
from utils import validate_link, clean_dir
from config import ALLOWED_DOMAINS

TARGET_ABR = 192  # kbps for FFmpegExtractAudio
BAR_LEN     = 50  # fixed progress bar width
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

# -------------------- Discord-safe progress blocks --------------------
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
    bar = FILLED_CHAR * (BAR_LEN // 3) + EMPTY_CHAR * (BAR_LEN - BAR_LEN // 3)
    return (
        f"🎶 **{title}**\n"
        f"```"
        f"[{bar}]  converting…  @{TARGET_ABR} kbps"
        f"```"
    )

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
    await ephemeral.edit(
        content=f"{'✅' if include_art else '🚫'} Album art will be {'included' if include_art else 'excluded'}."
    )

    # ---- public notice (we’ll later edit this with Source + Download) ----
    public_msg = await interaction.channel.send(f"{interaction.user.mention} is ripping audio... 🎧")

    # ---- temp working folder ----
    session_dir = tempfile.mkdtemp(prefix="ripperroo_")

    # ---- capture main loop for cross-thread updates + throttle ----
    loop = asyncio.get_running_loop()
    last_update = {"t": 0.0}

    def progress_hook(d):
        # yt-dlp runs in a worker thread; schedule coroutine on main loop
        now = time.time()
        if now - last_update["t"] < 0.2:  # 5 updates/sec
            return
        last_update["t"] = now
        asyncio.run_coroutine_threadsafe(on_progress(d, ephemeral), loop)

    # ---- yt-dlp options ----
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
        "logger": _QuietLogger(),
        "noprogress": True,      # silence terminal progress
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "noplaylist": False,
        "writethumbnail": include_art,
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
        # audio extraction
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(TARGET_ABR)},
        ],
    }

    # ---- fetch info (playlist size & shared art) ----
    await ephemeral.edit(content="🎵 Fetching playlist info...")
    info = await asyncio.to_thread(extract_info, link, ydl_opts)
    entries = info.get("entries", [info]) if info else []
    total = len(entries)
    await ephemeral.edit(content=f"🎧 Ripping {total or 'unknown'} track(s)...")

    # ---- album art optimization: download once for sets ----
    if include_art and total and total > 1:
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

    # ---- run rip (real downloads) ----
    await asyncio.to_thread(run_ytdlp, link, ydl_opts)
    await asyncio.sleep(0.5)  # allow handles to close

    # ---- collect output files ----
    files = [os.path.join(session_dir, f) for f in os.listdir(session_dir) if f.lower().endswith(".mp3")]
    files.sort()
    total_done = len(files)
    if total_done == 0:
        await ephemeral.edit(content="❌ No audio files were downloaded.")
        clean_dir(session_dir)
        return

    # ---- detect server upload limit (bytes) ----
    if interaction.guild is not None and hasattr(interaction.guild, "filesize_limit"):
        upload_limit = int(interaction.guild.filesize_limit)  # true per-guild cap
    else:
        upload_limit = 8 * 1024 * 1024  # fallback

    # keep some headroom for zip headers/etc.
    target_part_size = max(1, upload_limit - 256 * 1024)

    # ---- always deliver as ZIP parts (never individual tracks) ----
    await ephemeral.edit(content="📦 Packaging tracks for Discord…")
    parts = build_zip_parts(files, session_dir, target_part_size)

    if not parts:
        # Handle rare case: a single MP3 > server limit (cannot be sent)
        too_big = max((os.path.getsize(f), f) for f in files)[1]
        mb = round(os.path.getsize(too_big) / (1024 * 1024), 2)
        await ephemeral.edit(
            content=f"⚠️ A single track is {mb} MB which exceeds this server’s upload limit. Unable to send."
        )
        clean_dir(session_dir)
        return

    # ---- upload each part; remember the first attachment link for summary ----
    first_part_url = None
    for i, zp in enumerate(parts, start=1):
        try:
            msg = await interaction.followup.send(
                content=f"📦 Part {i}/{len(parts)}",
                file=discord.File(zp),
                ephemeral=False
            )
            if first_part_url is None:
                first_part_url = msg.jump_url
        except Exception:
            pass
        await asyncio.sleep(0.3)

    # ---- finalize/public summary (with Source + Download jump link) ----
    elapsed = int(time.monotonic() - start_ts)
    mins, secs = divmod(elapsed, 60)
    elapsed_text = f"{mins:02d}:{secs:02d}"

    source_md = f"[Source]({link})"
    download_md = f"[Download]({first_part_url})" if first_part_url else "Download posted below"

    await ephemeral.edit(content="✅ Done! Cleaning up…")
    clean_dir(session_dir)

    await public_msg.edit(
        content=(
            f"{interaction.user.mention} ripped 🎶 **{total_done} track(s)** — "
            f"{elapsed_text} @{TARGET_ABR} kbps · {source_md} · {download_md} ✅"
        )
    )
    await asyncio.sleep(1.2)
    try:
        await ephemeral.delete()
    except Exception:
        pass

# ==============================================================
# ZIP PARTITIONING (stay under guild upload limit)
# ==============================================================

def build_zip_parts(files: list[str], session_dir: str, part_limit_bytes: int) -> list[str]:
    """
    Create multiple ZIP files, each <= part_limit_bytes.
    Uses ZIP_STORED (no compression) so size ~ sum(mp3 sizes) + tiny header.
    """
    if not files:
        return []

    parts: list[str] = []
    bundle: list[str] = []
    total_in_bundle = 0

    def flush_bundle(index: int):
        if not bundle:
            return None
        zip_name = os.path.join(session_dir, f"rip_part_{index:02d}.zip")
        with zipfile.ZipFile(zip_name, "w", compression=zipfile.ZIP_STORED) as zf:
            for fp in bundle:
                arcname = os.path.basename(fp)
                zf.write(fp, arcname)
        return zip_name

    idx = 1
    for fp in files:
        size = os.path.getsize(fp)

        # If a single file is bigger than the part limit and it's the only file, bail out.
        if size > part_limit_bytes and len(files) == 1:
            return []

        if total_in_bundle and (total_in_bundle + size) > part_limit_bytes:
            zp = flush_bundle(idx)
            if zp:
                parts.append(zp)
                idx += 1
            bundle = []
            total_in_bundle = 0

        bundle.append(fp)
        total_in_bundle += size

    # flush last bundle
    zp = flush_bundle(idx)
    if zp:
        parts.append(zp)

    return parts

# ==============================================================
# yt-dlp helpers + progress
# ==============================================================

def run_ytdlp(link, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([link])

def extract_info(link, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(link, download=False)

async def on_progress(d, ephemeral_msg):
    """
    Rock-solid progress:
      - Prefer bytes-based progress (downloaded/total)
      - Else use fragment_index/fragment_count
      - Else 'finished' => 100%
      - 'postprocessing' => show converting block
    """
    status = d.get("status")
    filename = d.get("filename") or "unknown"
    title = os.path.splitext(os.path.basename(filename))[0]

    if status == "postprocessing":
        try:
            await ephemeral_msg.edit(content=render_ffmpeg_block(title))
        except Exception:
            pass
        return

    if status == "finished":
        try:
            await ephemeral_msg.edit(content=render_progress_block(title, 1.0, 0, d.get("abr", TARGET_ABR)))
        except Exception:
            pass
        return

    if status != "downloading":
        return

    # ----- compute progress robustly -----
    # 1) bytes if available
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    downloaded = d.get("downloaded_bytes")
    p01 = None
    if total and downloaded:
        try:
            p01 = max(0.0, min(1.0, downloaded / total))
        except Exception:
            p01 = None

    # 2) fragment ratio fallback
    if p01 is None:
        frag_count = d.get("fragment_count") or d.get("n_fragments")
        frag_index = d.get("fragment_index")
        if frag_count:
            # frag_index is 0-based; add 1 for user-facing progress across fragments
            try:
                p01 = max(0.0, min(1.0, ((frag_index or 0) + 1) / float(frag_count)))
            except Exception:
                p01 = None

    # 3) percent string last (often flaky)
    if p01 is None and "_percent_str" in d:
        try:
            p01 = float(d["_percent_str"].replace("%", "")) / 100.0
        except Exception:
            p01 = 0.0

    eta = d.get("eta")
    abr = d.get("abr", TARGET_ABR)

    try:
        await ephemeral_msg.edit(content=render_progress_block(title, p01 or 0.0, eta, abr))
    except Exception:
        pass

# simple HTTP fetcher (for shared album art)
def download_file(url: str, path: str):
    import requests
    r = requests.get(url, stream=True, timeout=10)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(1024):
            f.write(chunk)
