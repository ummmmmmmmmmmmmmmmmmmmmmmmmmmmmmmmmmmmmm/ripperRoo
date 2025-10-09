import discord, asyncio, os, yt_dlp
from datetime import datetime
from ui_components import ArtChoice, ZipChoice
from utils import progress_bar, validate_link, zip_folder, clean_dir
from config import ALLOWED_DOMAINS, DOWNLOAD_DIR

async def handle_rip(interaction: discord.Interaction, link: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    ephemeral = await interaction.followup.send("✅ Received. Checking link...", ephemeral=True)

    if not validate_link(link, ALLOWED_DOMAINS):
        await ephemeral.edit(content="❌ Unsupported or invalid link.")
        return

    # Ask for album art
    art_view = ArtChoice()
    await ephemeral.edit(content="🎨 Include album art?", view=art_view)
    await art_view.wait()
    include_art = art_view.choice or False
    await ephemeral.edit(content=f"{'✅' if include_art else '🚫'} Album art will be {'included' if include_art else 'excluded'}.")

    public_msg = await interaction.channel.send(f"{interaction.user.mention} is ripping audio... 🎧")

    # temp folder
    timestamp = datetime.now().strftime("%H%M%S")
    session_dir = os.path.join(DOWNLOAD_DIR, f"rip_{timestamp}")
    os.makedirs(session_dir, exist_ok=True)

    # yt-dlp options
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
        "quiet": True,
        "noplaylist": False,
        "writethumbnail": include_art,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    # Run yt-dlp asynchronously
    await ephemeral.edit(content="🎵 Downloading audio...")
    try:
        proc = await asyncio.to_thread(run_ytdlp, link, ydl_opts)
    except Exception as e:
        await ephemeral.edit(content=f"❌ Download failed: {e}")
        clean_dir(session_dir)
        return

    files = [os.path.join(session_dir, f) for f in os.listdir(session_dir) if f.endswith(".mp3")]
    total = len(files)
    if total == 0:
        await ephemeral.edit(content="❌ No audio files were downloaded.")
        clean_dir(session_dir)
        return

    # zip logic
    zip_mode = total > 10
    if 5 < total <= 10:
        zip_view = ZipChoice()
        await ephemeral.edit(content=f"Found {total} tracks. Zip them?", view=zip_view)
        await zip_view.wait()
        zip_mode = zip_view.choice or False

    if zip_mode:
        await ephemeral.edit(content="📦 Zipping files...")
        zip_path = zip_folder(session_dir)
        await interaction.followup.send(file=discord.File(zip_path), ephemeral=False)
        os.remove(zip_path)
    else:
        await ephemeral.edit(content="📤 Uploading ripped files...")
        for idx, file in enumerate(files, 1):
            await interaction.followup.send(f"Track {idx}/{total}", file=discord.File(file), ephemeral=False)
            await asyncio.sleep(0.5)

    await ephemeral.edit(content="✅ Rip complete. Cleaning up...")
    clean_dir(session_dir)

    await public_msg.edit(content=f"{interaction.user.mention} ripped 🎶 **Audio Set** ({total} track{'s' if total>1 else ''}) ✅")
    await asyncio.sleep(2)
    await ephemeral.delete()

def run_ytdlp(link, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([link])
