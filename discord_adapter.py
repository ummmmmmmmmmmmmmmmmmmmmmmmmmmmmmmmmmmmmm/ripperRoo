# discord_adapter.py
import os, asyncio, time, tempfile
import discord
from rip_core import rip_to_zips
from constants import TARGET_ABR_KBPS
from ui_components import ArtChoice
from utils import validate_link, clean_dir
from config import ALLOWED_DOMAINS

# Keep parts comfortably under the guild limit to avoid 413s.
HEADROOM = 5 * 1024 * 1024  # 5 MiB safety margin

# ---------- Progress UI ----------
BAR_LEN = 50
FILLED, EMPTY = "♪", "-"

def _render_bar(p01: float, eta: int | None, abr: int | None, title: str) -> str:
    p = max(0.0, min(1.0, float(p01)))
    pct = int(round(p * 100))
    filled = int(round(BAR_LEN * p))
    bar = (FILLED * filled) + (EMPTY * (BAR_LEN - filled))
    eta_part = f"  ETA {int(eta)}s" if eta and eta > 0 else ""
    abr_part = f"  @{abr} kbps" if abr else ""
    return f"🎶 **{title}**\n```[{bar}]  {pct}%{eta_part}{abr_part}```"

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

async def _send_zips_as_replies(channel: discord.TextChannel, summary_msg: discord.Message, zips: list[str]):
    """Always send each zip as its own message reply to summary_msg."""
    total = len(zips)
    for i, zp in enumerate(zips, start=1):
        label = f"Part {i}/{total}" if total > 1 else "Download"
        try:
            await channel.send(content=f"📦 {label}", file=discord.File(zp), reference=summary_msg)
        except Exception:
            # try without reference if thread linking fails
            try:
                await channel.send(content=f"📦 {label}", file=discord.File(zp))
            except Exception:
                pass
        await asyncio.sleep(0.2)

async def _send_with_files_best_effort(channel: discord.TextChannel, content: str, zips: list[str]):
    """
    Try to send summary + attachments in ONE message (<=10 files). If that fails,
    post the summary text-only, then follow-up with each ZIP as its own message.
    """
    zips = [p for p in zips if p and os.path.isfile(p)]
    if not zips:
        raise RuntimeError("No zip files to send.")

    # Attempt single message with as many as possible (down to 1)
    max_batch = min(len(zips), 10)
    for n in range(max_batch, 0, -1):
        try:
            files = [discord.File(p) for p in zips[:n]]
            msg = await channel.send(content=content, files=files)
            # overflow parts as replies
            if len(zips) > n:
                await _send_zips_as_replies(channel, msg, zips[n:])
            return msg
        except discord.HTTPException as e:
            # If it's a 413 or similar, fall through to text+followups quickly
            if getattr(e, "status", None) == 413 or "Payload Too Large" in str(e):
                break
            continue
        except Exception:
            continue

    # Fallback: summary text-only, then follow up each ZIP so users still get downloads
    msg = await channel.send(content=content)
    await _send_zips_as_replies(channel, msg, zips)
    return msg

async def handle_rip(interaction: discord.Interaction, link: str):
    started = time.monotonic()

    await interaction.response.defer(ephemeral=True, thinking=True)
    eph = await interaction.followup.send("✅ Received. Checking link…", ephemeral=True)

    if not validate_link(link, ALLOWED_DOMAINS):
        await eph.edit(content="❌ Unsupported or invalid link.")
        return

    # Album art choice (ephemeral)
    view = ArtChoice()
    await eph.edit(content="🎨 Include album art?", view=view)
    await view.wait()
    include_art = view.choice or False
    await eph.edit(content="Thank you for using Ripper Roo, your download will begin momentarily…", view=None)

    # Public ticker
    pub, pub_state, pub_task = await _animated_public(interaction)

    # Ephemeral progress with smoothing
    loop = asyncio.get_running_loop()
    prog = {
        "title": "…",
        "p01_target": 0.0,   # true % from hooks
        "p01_smooth": 0.0,   # eased % for UI
        "eta": None,
        "abr": TARGET_ABR_KBPS,
        "active": True,
    }

    async def animator():
        # smooth using exponential moving average toward target
        alpha = 0.20  # smoothing factor; higher = faster
        tick = 0.16   # ~6 fps
        while prog["active"]:
            # ease toward target
            t = prog["p01_target"]
            s = prog["p01_smooth"]
            prog["p01_smooth"] = s + alpha * (t - s)
            try:
                await eph.edit(content=_render_bar(prog["p01_smooth"], prog["eta"], prog["abr"], prog["title"]))
            except Exception:
                pass
            await asyncio.sleep(tick)
    anim_task = asyncio.create_task(animator())

    # Hook: update target % from yt-dlp; animator eases the bar
    def progress_cb(d: dict):
        def upd():
            status = d.get("status")
            fn = d.get("filename") or "unknown"
            prog["title"] = os.path.splitext(os.path.basename(fn))[0]
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                dl    = d.get("downloaded_bytes")
                fragc = d.get("fragment_count") or d.get("n_fragments")
                fragi = d.get("fragment_index")
                if total and dl is not None:
                    prog["p01_target"] = max(0.0, min(1.0, dl/total))
                elif fragc:
                    prog["p01_target"] = max(0.0, min(1.0, ((fragi or 0)+1)/float(fragc)))
                prog["eta"] = d.get("eta")
            elif status == "postprocessing":
                prog["eta"] = None
            elif status == "finished":
                prog["p01_target"] = 1.0
                prog["eta"] = 0
                pub_state["done"] += 1
        loop.call_soon_threadsafe(upd)

    # Determine safe per-file size
    guild_limit = int(getattr(interaction.guild, "filesize_limit", 8 * 1024 * 1024))
    part_limit = max(1, guild_limit - HEADROOM)

    # Run rip (yt-dlp+ffmpeg) with progress callback
    try:
        res = await asyncio.to_thread(rip_to_zips, link, include_art, part_limit, progress_cb)
    except Exception as e:
        prog["active"] = False
        try: await anim_task
        except Exception: pass
        pub_state["run"] = False
        try: await pub_task
        except Exception: pass
        await eph.edit(content=f"❌ Rip failed: `{e}`")
        try: await pub.delete()
        except Exception: pass
        return

    # Close UI animations
    prog["active"] = False
    try: await anim_task
    except Exception: pass
    pub_state["tot"] = res["count"]; pub_state["done"] = res["count"]
    pub_state["run"] = False
    try: await pub_task
    except Exception: pass
    try: await pub.delete()
    except Exception: pass

    # Final summary (suppress rich preview so files aren’t hidden by embeds)
    elapsed = int(time.monotonic() - started)
    mm, ss = divmod(elapsed, 60)
    elapsed_txt = f"{mm:02d}:{ss:02d}"
    source_md = f"[Source](<{link}>)"
    summary = (f"{interaction.user.mention} ripped 🎶 **{res['count']} track(s)** "
               f"for {elapsed_txt} @ {TARGET_ABR_KBPS} kbps · {source_md} — **Download below ⤵️**")

    # Try best effort: single message with attachments; if not, summary then follow-up ZIP posts
    try:
        await _send_with_files_best_effort(interaction.channel, summary, res["zips"])
    except Exception as e:
        await eph.edit(content=f"❌ Failed to attach ZIP(s): `{e}`")
        clean_dir(res.get("work_dir") or tempfile.gettempdir())
        return

    await eph.edit(content="✅ Done! Cleaning up…")
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
