import discord, asyncio, os, tempfile, yt_dlp, time, math
from datetime import datetime
from ui_components import ArtChoice, ZipChoice
from utils import progress_bar, validate_link, zip_folder, clean_dir
from config import ALLOWED_DOMAINS

DISCORD_MAX = 8 * 1024 * 1024  # 8 MB

# -------------------- Quiet logger for yt-dlp (no terminal spam) --------------------
class _QuietLogger:
    def debug(self, msg):  # swallow progress lines
        pass
    def info(self, msg):
        pass
    def warning(self, msg):
        pass
    def error(self, msg):
        print(msg)

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

    # ---- capture main loop for cross-thread updates ----
    loop = asyncio.get_running_loop()
    last_update = {"t": 0.0}

    # ---- progress hook (throttled) ----
    def progress_hook(d):
        now = time.time()
        if now - last_update["t"] < 0.5:
            return
        last_update["t"] = now
        asyncio.run_coroutine_threadsafe(on_progress(d, ephemeral), loop)

    # ---- yt-dlp options ----
    target_bitrate_kbps = 192
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
        "logger": _QuietLogger(),
        "noprogress": True,                 # do not print progress in terminal
        "quiet": True,
        "no_warnings": True,
        "progress_with_newline": False,
        "ignoreerrors": True,
        "noplaylist": False,
        "writethumbnail": include_art,
        "skip_download": False,
        "concurrent_fragment_downloads": 5, # faster
        "retries": 5,
        "fragment_retries": 5,
        "buffersize": 128 * 1024,
        "continuedl": True,
        "socket_timeout": 10,
        "progress_hooks": [progress_hook],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(target_bitrate_kbps)},
        ],
    }

    # ---- fetch info (for playlist size & shared art) ----
    await ephemeral.edit(content="🎵 Fetching playlist info...")
    info = await asyncio.to_thread(extract_info, link, ydl_opts)
    entries = info.get("entries", [info]) if info else []
    total = len(entries)
    await ephemeral.edit(content=f"🎧 Ripping {total or 'unknown'} track(s)...")

    # ---- album art optimization: download once for sets ----
    if include_art and total and total > 1:
        ydl_opts["writethumbnail"] = False    # avoid per-track art
        first_thumb = (info.get("entries") or [{}])[0].get("thumbnail")
        if first_thumb:
            await ephemeral.edit(content="🎨 Downloading shared album art...")
            art_path = os.path.join(session_dir, "album_art.jpg")
            try:
                await asyncio.to_thread(download_file, first_thumb, art_path)
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

    # ---- zipping choice/logic ----
    zip_mode = total_done > 10
    if 5 < total_done <= 10:
        zip_view = ZipChoice()
        await ephemeral.edit(content=f"Found {total_done} tracks. Zip them?", view=zip_view)
        await zip_view.wait()
        zip_mode = zip_view.choice or False

    # ---- upload to Discord (always try to deliver in chat) ----
    delivered = 0

    if zip_mode:
        await ephemeral.edit(content="📦 Zipping files...")
        zip_path = zip_folder(session_dir)
        if os.path.getsize(zip_path) <= DISCORD_MAX:
            await interaction.followup.send(file=discord.File(zip_path), ephemeral=False)
            delivered = 1
            try: os.remove(zip_path)
            except: pass
        else:
            # fallback: send individually (so user still gets files)
            await ephemeral.edit(content="⚠️ ZIP > 8 MB. Sending tracks individually instead…")
            delivered += await _send_tracks_individually(interaction, files)
    else:
        delivered += await _send_tracks_individually(interaction, files)

    # ---- finalize/public summary ----
    elapsed = time.monotonic() - start_ts
    mins, secs = divmod(int(elapsed), 60)
    elapsed_text = f"{mins:02d}:{secs:02d}"

    await ephemeral.edit(content="✅ Done! Cleaning up…")
    clean_dir(session_dir)

    await public_msg.edit(
        content=f"{interaction.user.mention} ripped 🎶 **{total_done} track(s)** — {elapsed_text} @{target_bitrate_kbps} kbps ✅"
    )
    await asyncio.sleep(1.5)
    try:
        await ephemeral.delete()
    except Exception:
        pass


# ==============================================================
# HELPERS
# ==============================================================

async def _send_tracks_individually(interaction: discord.Interaction, files: list[str]) -> int:
    sent = 0
    for idx, fp in enumerate(files, 1):
        try:
            size = os.path.getsize(fp)
        except FileNotFoundError:
            continue
        if size > DISCORD_MAX:
            # can't send file, let the user know ephemerally but keep going
            mb = round(size / (1024 * 1024), 2)
            try:
                await interaction.followup.send(
                    content=f"⚠️ `{os.path.basename(fp)}` is {mb} MB — too large for Discord (skipped).",
                    ephemeral=True,
                )
            except Exception:
                pass
            continue

        try:
            await interaction.followup.send(
                content=f"🎶 `{idx}/{len(files)}`",
                file=discord.File(fp),
                ephemeral=False
            )
            sent += 1
            await asyncio.sleep(0.25)
        except Exception:
            # keep going even if one upload fails
            continue
    return sent


def run_ytdlp(link, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([link])

def extract_info(link, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(link, download=False)

async def on_progress(d, ephemeral_msg):
    """Live progress: title + Discord-safe bar + percent + ETA + bitrate."""
    if d.get("status") != "downloading":
        return

    filename = d.get("filename", "unknown")
    title = os.path.splitext(os.path.basename(filename))[0]

    # derive percent robustly
    pct = None
    if "_percent_str" in d:
        try:
            pct = float(d["_percent_str"].replace("%", ""))
        except Exception:
            pct = None
    if pct is None:
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes") or 0
        pct = (downloaded / total * 100.0) if total else 0.0

    progress = max(0.0, min(1.0, pct / 100.0))
    eta = d.get("eta")
    abr = d.get("abr", 192)

    eta_text = f"⏱️ ETA {int(eta)}s" if eta else ""
    bar = progress_bar(progress)  # uses Discord-safe characters

    text = (
        f"🎶 **{title}**\n"
        f"{bar}  `{int(progress*100)}%`  {eta_text}  `{abr} kbps`"
    )
    try:
        await ephemeral_msg.edit(content=text)
    except Exception:
        pass

# simple img/file fetch (for shared album art)
def download_file(url: str, path: str):
    import requests
    r = requests.get(url, stream=True, timeout=10)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(1024):
            f.write(chunk)
