import discord, asyncio, os, tempfile, yt_dlp
from datetime import datetime
from ui_components import ArtChoice, ZipChoice
from utils import progress_bar, validate_link, zip_folder, clean_dir
from config import ALLOWED_DOMAINS


# ==============================================================
# MAIN RIP COMMAND
# ==============================================================

async def handle_rip(interaction: discord.Interaction, link: str):
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

    # ---- global notice ----
    public_msg = await interaction.channel.send(f"{interaction.user.mention} is ripping audio... 🎧")

    # ---- create isolated temp folder ----
    session_dir = tempfile.mkdtemp(prefix="ripperroo_")

    # ---- capture main event loop ----
    loop = asyncio.get_running_loop()

    # ---- progress hook closure ----
    def progress_hook(d):
        asyncio.run_coroutine_threadsafe(on_progress(d, ephemeral), loop)

    # ---- yt-dlp configuration ----
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
        "quiet": True,
        "ignoreerrors": True,
        "noplaylist": False,
        "writethumbnail": include_art,
        "skip_download": False,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ],
        "progress_hooks": [progress_hook],
    }

    await ephemeral.edit(content="🎵 Fetching playlist info...")
    info = await asyncio.to_thread(extract_info, link, ydl_opts)
    entries = info.get("entries", [info]) if info else []
    total = len(entries)
    await ephemeral.edit(content=f"🎧 Ripping {total or 'unknown'} track(s)...")

    # ---- run yt-dlp ----
    await asyncio.to_thread(run_ytdlp, link, ydl_opts)
    await asyncio.sleep(0.5)  # allow yt-dlp handles to close

    # ---- gather downloaded files ----
    files = [os.path.join(session_dir, f) for f in os.listdir(session_dir) if f.endswith(".mp3")]
    total_done = len(files)
    if total_done == 0:
        await ephemeral.edit(content="❌ No audio files were downloaded.")
        clean_dir(session_dir)
        return

    # ---- zip logic ----
    zip_mode = total_done > 10
    if 5 < total_done <= 10:
        zip_view = ZipChoice()
        await ephemeral.edit(content=f"Found {total_done} tracks. Zip them?", view=zip_view)
        await zip_view.wait()
        zip_mode = zip_view.choice or False

    # ---- upload logic ----
    if zip_mode:
        await ephemeral.edit(content="📦 Zipping files...")
        zip_path = zip_folder(session_dir)

        file_size = os.path.getsize(zip_path)
        max_size = 8 * 1024 * 1024  # 8 MB limit
        if file_size > max_size:
            mb = round(file_size / (1024 * 1024), 2)
            await ephemeral.edit(
                content=f"⚠️ File is {mb} MB — too large to send on Discord.\n"
                        f"I’ll skip uploading it to avoid the 8 MB limit."
            )
        else:
            await interaction.followup.send(file=discord.File(zip_path), ephemeral=False)
            os.remove(zip_path)

    else:
        await ephemeral.edit(content="📤 Uploading ripped tracks...")
        max_size = 8 * 1024 * 1024
        for idx, file in enumerate(files, 1):
            size = os.path.getsize(file)
            if size > max_size:
                mb = round(size / (1024 * 1024), 2)
                await ephemeral.edit(content=f"⚠️ `{os.path.basename(file)}` is {mb} MB — too large to send.")
                continue
            await interaction.followup.send(
                f"🎶 `{idx}/{total_done}`", file=discord.File(file), ephemeral=False
            )
            await asyncio.sleep(0.5)

    await ephemeral.edit(content="✅ Done! Cleaning up...")
    clean_dir(session_dir)

    await public_msg.edit(content=f"{interaction.user.mention} ripped 🎶 **{total_done} track(s)** ✅")
    await asyncio.sleep(2)
    await ephemeral.delete()


# ==============================================================
# SUPPORT FUNCTIONS
# ==============================================================

def run_ytdlp(link, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([link])

def extract_info(link, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(link, download=False)

async def on_progress(d, ephemeral_msg):
    """Update ephemeral message live with title, progress, ETA, bitrate."""
    if d["status"] == "downloading":
        filename = d.get("filename", "unknown")
        title = os.path.splitext(os.path.basename(filename))[0]

        # percentage
        percent = d.get("_percent_str", "0.0%").replace("%", "")
        try:
            progress = float(percent) / 100
        except ValueError:
            progress = 0.0

        # ETA (seconds remaining)
        eta = d.get("eta", 0)
        eta_text = f"⏱️ ETA {int(eta)} s" if eta else ""

        # Bitrate (kbps)
        abr = d.get("abr", 192)
        bitrate_text = f"{abr} kbps" if abr else ""

        # Build progress line
        bar = progress_bar(progress)
        text = f"🎶 Ripping **{title}**\n{bar} {percent}%  {eta_text}  {bitrate_text}"

        try:
            await ephemeral_msg.edit(content=text)
        except Exception:
            pass
