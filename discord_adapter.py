# discord_adapter.py
import os, asyncio, time, tempfile
import discord
from rip_core import rip_to_zips
from constants import TARGET_ABR_KBPS
from ui_components import ArtChoice
from utils import validate_link, clean_dir
from config import ALLOWED_DOMAINS

HEADROOM = 2 * 1024 * 1024  # 2 MiB under guild limit to avoid 413

# ---------- Progress UI helpers ----------
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

async def _send_with_files_guaranteed(channel: discord.TextChannel, content: str, paths: list[str]):
    """Always returns a message that has at least one attachment from `paths`."""
    paths = [p for p in paths if p and os.path.isfile(p)]
    if not paths: raise RuntimeError("No zip files to send.")

    # Try batches down to 1
    max_batch = min(len(paths), 10)
    for n in range(max_batch, 0, -1):
        try:
            files = [discord.File(p) for p in paths[:n]]
            summary = await channel.send(content=content, files=files)
            # overflow as replies
            for p in paths[n:]:
                try: await channel.send(file=discord.File(p), reference=summary)
                except Exception: pass
                await asyncio.sleep(0.2)
            return summary
        except Exception:
            continue

    # File-only then edit summary
    try:
        m = await channel.send(file=discord.File(paths[0]))
        try: await m.edit(content=content)
        except Exception: pass
        for p in paths[1:]:
            try: await channel.send(file=discord.File(p), reference=m)
            except Exception: pass
            await asyncio.sleep(0.2)
        return m
    except Exception as e:
        raise RuntimeError(f"Unable to attach ZIP: {e}")

async def handle_rip(interaction: discord.Interaction, link: str):
    started = time.monotonic()

    await interaction.response.defer(ephemeral=True, thinking=True)
    eph = await interaction.followup.send("✅ Received. Checking link…", ephemeral=True)

    if not validate_link(link, ALLOWED_DOMAINS):
        await eph.edit(content="❌ Unsupported or invalid link.")
        return

    # Ask album art
    view = ArtChoice()
    await eph.edit(content="🎨 Include album art?", view=view)
    await view.wait()
    include_art = view.choice or False
    await eph.edit(content="Thank you for using Ripper Roo, your download will begin momentarily…", view=None)

    # Public lightweight status
    pub, pub_state, pub_task = await _animated_public(interaction)

    # Ephemeral progress state + animator
    loop = asyncio.get_running_loop()
    prog = {"title":"…","p01":0.0,"eta":None,"abr":TARGET_ABR_KBPS,"status":"idle","active":True,"last":time.monotonic()}
    async def animator():
        while prog["active"]:
            try:
                await eph.edit(content=_render_bar(prog["p01"], prog["eta"], prog["abr"], prog["title"]))
            except Exception: pass
            await asyncio.sleep(0.15)
    anim_task = asyncio.create_task(animator())

    # Thread-safe progress callback for rip_core/yt-dlp
    def progress_cb(d: dict):
        def upd():
            status = d.get("status")
            fn = d.get("filename") or "unknown"
            title = os.path.splitext(os.path.basename(fn))[0]
            prog["title"] = title
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                dl    = d.get("downloaded_bytes")
                speed = d.get("speed") or 0
                fragc = d.get("fragment_count") or d.get("n_fragments")
                fragi = d.get("fragment_index")
                if total and dl is not None:
                    prog["p01"] = max(0.0, min(1.0, dl/total))
                elif fragc:
                    prog["p01"] = max(0.0, min(1.0, ((fragi or 0)+1)/float(fragc)))
                prog["eta"] = d.get("eta")
                prog["abr"] = TARGET_ABR_KBPS
            elif status == "postprocessing":
                prog["status"] = "postprocessing"
                prog["eta"] = None
            elif status == "finished":
                prog["p01"] = 1.0; prog["eta"] = 0
                pub_state["done"] += 1
        loop.call_soon_threadsafe(upd)

    # Compute safe per-file size
    guild_limit = int(getattr(interaction.guild, "filesize_limit", 8 * 1024 * 1024))
    part_limit = max(1, guild_limit - HEADROOM)

    # Rip in a worker thread (progress_cb is used by yt-dlp hooks)
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

    # Close progress animations
    prog["active"] = False
    try: await anim_task
    except Exception: pass
    pub_state["tot"] = res["count"]; pub_state["done"] = res["count"]
    pub_state["run"] = False
    try: await pub_task
    except Exception: pass
    try: await pub.delete()
    except Exception: pass

    # Final summary + guaranteed attachments (same message)
    elapsed = int(time.monotonic() - started)
    mm, ss = divmod(elapsed, 60)
    elapsed_txt = f"{mm:02d}:{ss:02d}"
    source_md = f"[Source](<{link}>)"  # suppress embed so files are visible

    summary = (f"{interaction.user.mention} ripped 🎶 **{res['count']} track(s)** "
               f"for {elapsed_txt} @ {TARGET_ABR_KBPS} kbps · {source_md} — **Download below ⤵️**")

    try:
        summary_msg = await _send_with_files_guaranteed(interaction.channel, summary, res["zips"])
    except Exception as e:
        await eph.edit(content=f"❌ Failed to attach ZIP: `{e}`")
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
