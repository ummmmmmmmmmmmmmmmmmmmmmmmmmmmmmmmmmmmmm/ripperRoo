# utils.py
import os, re, stat, shutil, tempfile, zipfile, pathlib, datetime as dt
from pathlib import Path
from typing import Tuple, Optional, List, Dict
from urllib.parse import urlparse, parse_qs

import discord

# ---------- small shared utils ----------
def human_mb(n: Optional[float]) -> str:
    try:
        return f"{(n or 0)/1024/1024:.1f} MB"
    except Exception:
        return "0 MB"

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def _safe_rmtree(p: Path):
    def onerr(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWUSR)
            func(path)
        except Exception:
            pass
    shutil.rmtree(p, ignore_errors=False, onerror=onerr)

def clean_stale_tmp(prefix: str = "rip-", max_age_hours: int = 36):
    tmp = Path(tempfile.gettempdir())
    cutoff = dt.datetime.now().timestamp() - max_age_hours * 3600
    for child in tmp.iterdir():
        try:
            if child.is_dir() and child.name.startswith(prefix):
                if child.stat().st_mtime < cutoff:
                    _safe_rmtree(child)
        except Exception:
            pass

async def eph_send(inter: discord.Interaction, content: str = "\u200b",
                   *, view: discord.ui.View | None = None) -> discord.Message:
    """Send/follow-up ephemeral message and return it for later edits."""
    if not inter.response.is_done():
        await inter.response.send_message(content=content, view=view, ephemeral=True)
        return await inter.original_response()
    else:
        return await inter.followup.send(content=content, view=view, ephemeral=True, wait=True)

# ---------- domain helpers ----------
def provider_of(link: str) -> Tuple[str, str]:
    try:
        u = urlparse(link)
        host = (u.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if "soundcloud.com" in host:
            return "SoundCloud", link
        if "bandcamp.com" in host:
            return "Bandcamp", link
        if host in {"youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}:
            return "YouTube", link
        if host == "open.spotify.com":
            return "Spotify", link
        return "Source", link
    except Exception:
        return "Source", link

def detect_playlist(link: str) -> Tuple[bool, str]:
    try:
        u = urlparse(link)
        host = (u.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host in {"youtube.com", "music.youtube.com", "m.youtube.com"}:
            qs = parse_qs(u.query or "")
            if "list" in qs or (u.path or "").startswith("/playlist"):
                return True, "youtube"
        if host == "youtu.be":
            if "list" in parse_qs(u.query or ""):
                return True, "youtube"
        if "soundcloud.com" in host and "/sets/" in (u.path or ""):
            return True, "soundcloud"
        if "bandcamp.com" in host and "/album/" in (u.path or ""):
            return True, "bandcamp"
        if host == "open.spotify.com" and "/playlist/" in (u.path or ""):
            return True, "spotify"
        return False, "unknown"
    except Exception:
        return False, "unknown"

def ok_domain(link: str) -> bool:
    try:
        host = (urlparse(link).hostname or "").lower()
        return (
            host.endswith("youtube.com")
            or host == "youtu.be"
            or host.endswith("soundcloud.com")
            or host.endswith("bandcamp.com")
        )
    except Exception:
        return False

def _safe_base(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "playlist"

def build_attribution(inter: discord.Interaction, link: str) -> Optional[str]:
    if inter.guild is None:
        return None
    disp = inter.user.display_name
    uname = inter.user.name
    provider, url = provider_of(link)
    return f"ripped by: {disp}({uname}) from [{provider}]({url})"

# ---------- zipping ----------
def _filesize(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0

def make_zip(path_out: str, files: List[Tuple[str, str]], *,
             track_meta: List[Dict], cover_path: Optional[str], include_cover: bool):
    ordered = sorted(
        enumerate(track_meta),
        key=lambda t: (t[1].get("index") is None, t[1].get("index") or (t[0] + 1)),
    )
    tracklist_txt = "\n".join(
        [
            f"{(m.get('index') if m.get('index') is not None else i+1):02d}. {m.get('title','')}"
            for i, m in [(i, m) for i, m in ordered]
            if m.get("title")
        ]
    )
    with zipfile.ZipFile(path_out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p, n in files:
            if os.path.exists(p):
                zf.write(p, arcname=n)
        zf.writestr("tracklist.txt", tracklist_txt)
        if include_cover and cover_path and os.path.exists(cover_path):
            ext = pathlib.Path(cover_path).suffix.lower()
            zf.write(cover_path, arcname=f"artwork{ext}")

def pack_into_zip_parts(base_name: str, tmpdir: str, files: List[Tuple[str, str]], *,
                        track_meta: List[Dict], cover_path: Optional[str], size_limit: int) -> List[str]:
    parts: List[List[Tuple[str, str]]] = [[]]
    sizes: List[int] = [0]
    overhead = 4096
    for fpath, fname in files:
        fsz = _filesize(fpath) + overhead
        if sizes[-1] + fsz > size_limit and parts[-1]:
            parts.append([(fpath, fname)])
            sizes.append(fsz)
        else:
            parts[-1].append((fpath, fname))
            sizes[-1] += fsz

    out: List[str] = []
    for i, bundle in enumerate(parts, start=1):
        suffix = f"_part{i}.zip" if len(parts) > 1 else ".zip"
        outzip = os.path.join(tmpdir, f"{base_name}{suffix}")
        make_zip(outzip, bundle, track_meta=track_meta, cover_path=cover_path, include_cover=(i == 1))
        out.append(outzip)
    return out

def make_single_zip(files: List[Tuple[str, str]], out_zip: str, *,
                    track_meta: List[Dict], cover_path: Optional[str]):
    make_zip(out_zip, files, track_meta=track_meta, cover_path=cover_path, include_cover=True)
