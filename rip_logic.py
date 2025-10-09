import asyncio
from utils import progress_bar, validate_link
from ui_components import ArtChoice, ZipChoice
from config import ALLOWED_DOMAINS

async def handle_rip(interaction, link: str):
    await interaction.response.defer(ephemeral=True, thinking=True)

    ephemeral_msg = await interaction.followup.send("✅ Command received. Checking link...", ephemeral=True)
    await asyncio.sleep(1)

    if not validate_link(link, ALLOWED_DOMAINS):
        await ephemeral_msg.edit(content="❌ Invalid link or unsupported domain.")
        return

    await ephemeral_msg.edit(content="🎨 Would you like to include album art?")
    view = ArtChoice()
    await ephemeral_msg.edit(view=view)
    await view.wait()
    include_art = view.choice or False

    public_msg = await interaction.channel.send(f"{interaction.user.mention} is ripping audio... 🎧")

    # Simulate a playlist
    total_tracks = 12
    for i in range(total_tracks):
        await ephemeral_msg.edit(content=f"Ripping track {i+1}/{total_tracks} {progress_bar((i+1)/total_tracks)}")
        await asyncio.sleep(1)

    # Zip logic
    if total_tracks > 10:
        zip_mode = True
    elif total_tracks > 5:
        zip_view = ZipChoice()
        await ephemeral_msg.edit(content="You have more than 5 tracks. Would you like them zipped?", view=zip_view)
        await zip_view.wait()
        zip_mode = zip_view.choice or False
    else:
        zip_mode = False

    await asyncio.sleep(1)
    await ephemeral_msg.edit(content="📦 Finalizing files...")

    # Mock output path
    download_url = "https://example.com/download/fake.zip" if zip_mode else "https://example.com/download/track1.mp3"

    await asyncio.sleep(1)
    await ephemeral_msg.edit(content="✅ Done! Your rip is ready.")

    await public_msg.edit(
        content=f"{interaction.user.mention} ripped 🎶 **Album Title** — [Download]({download_url}) "
                f"(Time: 00:{total_tracks:02}s)"
    )
