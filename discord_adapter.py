# discord_adapter.py
import os, asyncio, time, tempfile
import discord
from rip_core import rip_to_zips
from constants import TARGET_ABR_KBPS
from ui_components import ArtChoice
from utils import validate_link, clean_dir
from config import ALLOWED_DOMAINS

async def handle_rip(interaction: discord.Interaction, link: str):
    started = time.monotonic()

    # 1) Acknowledge + quick validation
    await interaction.response.defer(ephemeral=True, thinking=True)
    eph = await interaction.followup.send("✅ Received. Checking link…", ephemeral=True)

    if not validate_link(link, ALLOWED_DOMAINS):
        await eph.edit(content="❌ Unsupported or invalid link.")
        return

    # 2) Ask album art once (ephemeral)
    art_view = ArtChoice()
    await eph.edit(content="🎨 Include album art?", view=art_view)
    await art_view.wait()
    include_art = art_view.choice or False

    # Small handoff message
    await eph.edit(content="Thank you for using Ripper Roo, your download will begin momentarily…", view=None)

    # 3) Public lightweight status (no progress bar)
    pub = await interaction.channel.send(f"{interaction.user.mention} is ripping audio…")
    anim = {"dots": 0, "done": 0, "tot": None, "run": True}

    async def ticker():
        while anim["run"]:
            dots = "." * (1 + anim["dots"] % 3)
            total = anim["tot"]
            status = (f"{interaction.user.mention} is ripping audio{dots}"
                      if total is None
                      else f"{interaction.user.mention} is ripping audio{dots} ({anim['done']}/{total})")
            try:
                await pub.edit(content=status)
            except Exception:
                pass
            anim["dots"] += 1
            await asyncio.sleep(0.9)
    tick_task = asyncio.create_task(ticker())

    # 4) Determine upload limit (guild-specific)
    upload_limit = int(getattr(interaction.guild, "filesize_limit", 8 * 1024 * 1024))
    part_limit = max(1, upload_limit - 256 * 1024)  # headroom

    # 5) Run the ripping core (playlist-safe) — this uses yt-dlp + ffmpeg and zips
    try:
        res = await asyncio.to_thread(
            rip_to_zips,
            link,
            include_art,
            part_limit,
        )
    except Exception as e:
        anim["run"] = False
        try: await tick_task
        except Exception: pass
        await eph.edit(content=f"❌ Rip failed: `{e}`")
        return

    # allow final ticker refresh to reflect total items
    anim["tot"] = res["count"]
    anim["done"] = res["count"]

    # 6) Final summary (NO kangaroo; attach zips on the SAME message)
    elapsed = int(time.monotonic() - started)
    mm, ss = divmod(elapsed, 60)
    elapsed_txt = f"{mm:02d}:{ss:02d}"
    source_md = f"[Source](<{link}>)"  # <…> suppresses rich preview so files stay visible
    content = (f"{interaction.user.mention} ripped 🎶 **{res['count']} track(s)** "
               f"for {elapsed_txt} @ {TARGET_ABR_KBPS} kbps · {source_md} — **Download below ⤵️**")

    zips = res["zips"]

    # Try to send ALL parts (Discord limit 10 files/msg). We must attach at least 1 on THIS message.
    summary_msg = None
    sent = False
    try:
        files = [discord.File(p) for p in zips[:10]]
        summary_msg = await interaction.channel.send(content=content, files=files)
        sent = True
    except Exception:
        pass

    if not sent:
        # hard fallback: send file-only first, then edit in-place with the summary text
        try:
            summary_msg = await interaction.channel.send(files=[discord.File(zips[0])])
            await summary_msg.edit(content=content)
            sent = True
        except Exception:
            # absolute last-ditch: text then reply with first file
            summary_msg = await interaction.channel.send(content=content)
            try:
                await interaction.channel.send(file=discord.File(zips[0]), reference=summary_msg)
            except Exception:
                pass

    # Overflow parts (if >10) as replies to keep the set grouped
    if len(zips) > 10:
        for zp in zips[10:]:
            try:
                await interaction.channel.send(file=discord.File(zp), reference=summary_msg)
            except Exception:
                pass
            await asyncio.sleep(0.25)

    # 7) Clean up UI
    anim["run"] = False
    try: await tick_task
    except Exception: pass
    try: await pub.delete()
    except Exception: pass

    await eph.edit(content="✅ Done! Cleaning up…")
    # keep the temp dir for a beat then remove
    try:
        await asyncio.sleep(0.8)
        clean_dir(res.get("work_dir") or tempfile.gettempdir())
    except Exception:
        pass
    try:
        await asyncio.sleep(0.8)
        await eph.delete()
    except Exception:
        pass
