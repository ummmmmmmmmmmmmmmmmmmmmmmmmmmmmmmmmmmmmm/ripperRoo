# discord_adapter.py
import os, asyncio, time, tempfile
import discord
from rip_core import rip_to_zips
from constants import TARGET_ABR_KBPS
from ui_components import ArtChoice
from utils import validate_link, clean_dir
from config import ALLOWED_DOMAINS

HEADROOM = 512 * 1024  # 512 KiB safety under guild limit

async def _animated_public(interaction: discord.Interaction):
    msg = await interaction.channel.send(f"{interaction.user.mention} is ripping audio…")
    state = {"dots": 0, "done": 0, "tot": None, "run": True}
    async def ticker():
        while state["run"]:
            dots = "." * (1 + (state["dots"] % 3))
            total = state["tot"]
            text = (f"{interaction.user.mention} is ripping audio{dots}"
                    if total is None else
                    f"{interaction.user.mention} is ripping audio{dots} ({state['done']}/{total})")
            try: await msg.edit(content=text)
            except Exception: pass
            state["dots"] += 1
            await asyncio.sleep(0.9)
    task = asyncio.create_task(ticker())
    return msg, state, task

async def _send_with_files_guaranteed(channel: discord.TextChannel, content: str, paths: list[str]):
    """
    ALWAYS returns a message that has at least one attachment from `paths`.
    Strategy:
      1) Try to send as many as possible (cap 10). If it fails, bisect down to 1.
      2) If even 1 fails, send a file-only message (fresh handle), then edit with content.
      3) Overflow (>10) are replied to the summary message.
    """
    # Ensure paths exist
    paths = [p for p in paths if p and os.path.isfile(p)]
    if not paths:
        raise RuntimeError("No zip files to send.")

    # Helper: try N files
    async def try_send_n(n: int):
        files = [discord.File(p) for p in paths[:n]]
        return await channel.send(content=content, files=files)

    # Try decreasing batch sizes down to 1
    max_batch = min(len(paths), 10)
    for n in range(max_batch, 0, -1):
        try:
            summary = await try_send_n(n)
            # overflow as replies
            if len(paths) > n:
                for p in paths[n:]:
                    try:
                        await channel.send(file=discord.File(p), reference=summary)
                    except Exception:
                        pass
                    await asyncio.sleep(0.2)
            return summary
        except Exception:
            continue

    # If we get here, even 1 file + content failed. Try file only, then edit text.
    try:
        file_only = await channel.send(file=discord.File(paths[0]))
        try:
            await file_only.edit(content=content)
        except Exception:
            pass
        # overflow as replies
        for p in paths[1:]:
            try:
                await channel.send(file=discord.File(p), reference=file_only)
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return file_only
    except Exception as e:
        raise RuntimeError(f"Unable to attach ZIP: {e}")

async def handle_rip(interaction: discord.Interaction, link: str):
    started = time.monotonic()

    # 1) Acknowledge + quick validation
    await interaction.response.defer(ephemeral=True, thinking=True)
    eph = await interaction.followup.send("✅ Received. Checking link…", ephemeral=True)

    if not validate_link(link, ALLOWED_DOMAINS):
        await eph.edit(content="❌ Unsupported or invalid link.")
        return

    # 2) Album art choice
    view = ArtChoice()
    await eph.edit(content="🎨 Include album art?", view=view)
    await view.wait()
    include_art = view.choice or False
    await eph.edit(content="Thank you for using Ripper Roo, your download will begin momentarily…", view=None)

    # 3) Public lightweight status (no progress bar)
    pub, pub_state, pub_task = await _animated_public(interaction)

    # 4) Guild upload limit → zip part limit
    guild_limit = int(getattr(interaction.guild, "filesize_limit", 8 * 1024 * 1024))
    part_limit = max(1, guild_limit - HEADROOM)

    # 5) Rip → Zips (yt-dlp + ffmpeg inside rip_core)
    try:
        res = await asyncio.to_thread(rip_to_zips, link, include_art, part_limit)
    except Exception as e:
        pub_state["run"] = False
        try: await pub_task
        except Exception: pass
        await eph.edit(content=f"❌ Rip failed: `{e}`")
        try: await pub.delete()
        except Exception: pass
        return

    pub_state["tot"]  = res["count"]
    pub_state["done"] = res["count"]

    # 6) Final summary text (suppress embeds so attachments are front/center)
    elapsed = int(time.monotonic() - started)
    mm, ss = divmod(elapsed, 60)
    elapsed_txt = f"{mm:02d}:{ss:02d}"
    source_md = f"[Source](<{link}>)"

    summary_text = (
        f"{interaction.user.mention} ripped 🎶 **{res['count']} track(s)** "
        f"for {elapsed_txt} @ {TARGET_ABR_KBPS} kbps · {source_md} — **Download below ⤵️**"
    )

    # 7) ALWAYS send the zips on the same message
    try:
        summary_msg = await _send_with_files_guaranteed(interaction.channel, summary_text, res["zips"])
    except Exception as e:
        pub_state["run"] = False
        try: await pub_task
        except Exception: pass
        await eph.edit(content=f"❌ Failed to attach ZIP: `{e}`")
        try: await pub.delete()
        except Exception: pass
        return

    # 8) Cleanup UI & temp
    pub_state["run"] = False
    try: await pub_task
    except Exception: pass
    try: await pub.delete()
    except Exception: pass

    await eph.edit(content="✅ Done! Cleaning up…")
    # Keep the working dir for a moment, then remove
    try:
        await asyncio.sleep(1.0)
        clean_dir(res.get("work_dir") or tempfile.gettempdir())
    except Exception:
        pass
    try:
        await asyncio.sleep(0.6)
        await eph.delete()
    except Exception:
        pass
