import discord, asyncio, os, tempfile, yt_dlp, time, zipfile
from ui_components import ArtChoice, ZipChoice
from utils import validate_link, clean_dir
from config import ALLOWED_DOMAINS

TARGET_ABR = 192  # kbps for FFmpegExtractAudio

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

# -------------------- Discord-safe ASCII progress (monospace) --------------------
def render_progress_block(title: str, percent01: float, eta_s: int | None, abr_kbps: int | None) -> str:
    p = max(0.0, min(1.0, float(percent01)))
    pct = int(round(p * 100))
    length = 22
    filled = int(round(length * p))
    bar = "#" * filled + "-" * (length - filled)
    eta_part = f"  ETA {int(eta_s)}s" if eta_s and eta_s > 0 else ""
    abr_part = f"  @{abr_kbps} kbps" if abr_kbps else ""
    return (
        f"🎶 **{title}**\n"
        f"```"
        f"[{bar}]  {pct}%{eta_part}{abr_part}"
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

    # ---- public notice ----
    public_msg = await interaction.channel.send(f"{interaction.user.mention} is ripping audio... 🎧")

    # ---- temp working folder ----
    session_dir = tempfile.mkdtemp(prefix="ripperroo_")

    # ---- capture main loop for cross-thread updates + throttle ----
    loop = asyncio.get_running_loop()
    last_update = {"t": 0.0}
    def progress_hook(d):
        now = time.time()
        if now - last_update["t"] < 0.5:  # 2 updates/sec
            return
        last_update["t"] = now
        asyncio.run_coroutine_threadsafe(on_progress(d, ephemeral), loop)

    # ---- yt-dlp options ----
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
        "logger": _QuietLogger(),
        "noprogress": True,
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
        upload_limit = int(interaction.guild.filesize_limit)  # auto: 8 MB, 50/100 MB on boosted servers
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

    # ---- upload each part as an attachment ----
    for i, zp in enumerate(parts, start=1):
        try:
            await interaction.followup.send(
                content=f"📦 Part {i}/{len(parts)}",
                file=discord.File(zp),
                ephemeral=False
            )
        except Exception:
            # keep going even if one fails
            pass
        await asyncio.sleep(0.3)

    # ---- finalize/public summary ----
    elapsed = int(time.monotonic() - start_ts)
    mins, secs = divmod(elapsed, 60)
    elapsed_text = f"{mins:02d}:{secs:02d}"

    await ephemeral.edit(content="✅ Done! Cleaning up…")
    clean_dir(session_dir)

    await public_msg.edit(
        content=f"{interaction.user.mention} ripped 🎶 **{total_done} track(s)** — {elapsed_text} @{TARGET_ABR} kbps ✅"
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

        # If a single file is bigger than the part limit, we can't include it anywhere.
        # Bail out and let caller notify the user.
        if size > part_limit_bytes and len(files) == 1:
            return []

        # If adding this file would bust the current part size, flush and start new.
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

    # In the extreme case where every single file individually exceeds part_limit_bytes,
    # 'parts' will be empty and caller handles the message.
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
    Live progress renderer. ASCII bar inside a code block so it always
    displays correctly in Discord.
    """
    if d.get("status") != "downloading":
        return

    filename = d.get("filename") or "unknown"
    title = os.path.splitext(os.path.basename(filename))[0]

    # Get percent robustly
    pct01 = None
    if "_percent_str" in d:
        try:
            pct01 = float(d["_percent_str"].replace("%", "")) / 100.0
        except Exception:
            pct01 = None
    if pct01 is None:
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes") or 0
        pct01 = (downloaded / total) if total else 0.0

    eta = d.get("eta")
    abr = d.get("abr", TARGET_ABR)

    try:
        await ephemeral_msg.edit(content=render_progress_block(title, pct01, eta, abr))
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
