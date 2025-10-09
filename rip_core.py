# rip_core.py
import os, tempfile, time, json
from typing import Dict, Any, List, Optional
from constants import (
    TARGET_ABR_KBPS, DEFAULT_ZIP_PART_MB,
    YTDLP_FORMAT_FALLBACK,
)
from ytdlp_wrapper import extract_info, download_all
from packager import build_zip_parts

def _hmmss(sec: int | float | None) -> str:
    if not sec: return "--:--"
    sec = int(sec); m, s = divmod(sec, 60); return f"{m:02d}:{s:02d}"

def _normalize_entries(info: Optional[dict]) -> list[dict]:
    if not info: return []
    entries = info.get("entries")
    if isinstance(entries, list) and entries:
        return [e or {} for e in entries if e is not None]
    return [info]

def _derive_zip_basename(info: Optional[dict]) -> str:
    def safe(t: str) -> str:
        import re
        t = re.sub(r"[^A-Za-z0-9 \-_.]+", "_", t or "").strip()
        t = re.sub(r"\s+", " ", t)
        return (t[:80]).strip() or "rip"
    if not info: return "rip"
    entry = info
    if isinstance(info.get("entries"), list) and info["entries"]:
        entry = next((e for e in info["entries"] if e), {}) or {}
    artist = entry.get("artist") or entry.get("uploader") or entry.get("channel") or info.get("uploader") or info.get("channel") or ""
    album  = entry.get("album") or info.get("playlist_title") or info.get("playlist") or ""
    title  = entry.get("track") or entry.get("title") or info.get("title") or ""
    if artist and album: return safe(f"{artist} - {album}")
    if artist and title: return safe(f"{artist} - {title}")
    if title: return safe(title)
    return "rip"

def _collect_audio_files(dirpath: str) -> List[str]:
    exts = {".mp3", ".m4a", ".opus", ".webm", ".aac", ".flac", ".wav", ".ogg"}
    out = []
    for name in os.listdir(dirpath):
        if os.path.splitext(name)[1].lower() in exts:
            out.append(os.path.join(dirpath, name))
    out.sort()
    return out

def _write_docs(session_dir: str, info: Optional[dict], audio_files: List[str]) -> List[str]:
    entries = _normalize_entries(info)
    tracks = []
    for i, e in enumerate(entries, start=1):
        title  = e.get("track") or e.get("title") or ""
        artist = e.get("artist") or e.get("uploader") or e.get("channel") or ""
        album  = e.get("album") or (info.get("playlist_title") if info else "") or (info.get("playlist") if info else "") or ""
        idx    = e.get("playlist_index") or e.get("track_number") or i
        dur    = e.get("duration")
        filename = os.path.basename(audio_files[min(len(audio_files)-1, i-1)]) if audio_files else None
        tracks.append({
            "index": idx, "title": title, "artist": artist, "album": album,
            "duration": dur, "duration_hmmss": _hmmss(dur), "filename": filename,
        })

    tl = os.path.join(session_dir, "TRACKLIST.txt")
    with open(tl, "w", encoding="utf-8") as f:
        for t in tracks:
            idx = f"{int(t['index']):02d}" if isinstance(t["index"], int) else "--"
            artist = t["artist"] or "Unknown Artist"
            title  = t["title"]  or (t["filename"] or "Unknown Title")
            album  = t["album"]  or ""
            dur    = t["duration_hmmss"]
            line = f"{idx}. {artist} — {title}"
            if album: line += f"  ({album})"
            line += f"  [{dur}]"
            f.write(line + "\n")

    meta = {"zip_basename": _derive_zip_basename(info), "count": len(tracks), "tracks": tracks}
    meta_path = os.path.join(session_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    m3u = os.path.join(session_dir, "playlist.m3u8")
    with open(m3u, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for t in tracks:
            dur = int(t["duration"]) if t["duration"] else -1
            artist = t["artist"] or "Unknown Artist"
            title  = t["title"]  or (t["filename"] or "Unknown Title")
            fn     = t["filename"] or title
            f.write(f"#EXTINF:{dur},{artist} - {title}\n{fn}\n")
    return [tl, meta_path, m3u]

def rip_to_zips(
    url: str,
    include_art: bool,
    zip_part_limit_bytes: int = DEFAULT_ZIP_PART_MB * 1024 * 1024,
) -> Dict[str, Any]:
    """
    Downloads (playlist-safe) with yt-dlp, converts to MP3 (yt-dlp ffmpeg pp),
    writes docs, zips into parts <= zip_part_limit_bytes.
    """
    session_dir = tempfile.mkdtemp(prefix="ripperroo_")

    # Probe info for naming/tracklist (non-fatal)
    info = extract_info(url, session_dir, include_art)

    # PASS 1: strict format chain + yt-dlp postprocessor → MP3
    download_all(url, session_dir, include_art,
                 progress_hook=None,
                 format_str=None,
                 use_pp_mp3=True,
                 abr_kbps=TARGET_ABR_KBPS)

    files = _collect_audio_files(session_dir)

    # PASS 2: if nothing was created, retry with a very loose format
    if not files:
        download_all(url, session_dir, include_art,
                     progress_hook=None,
                     format_str=YTDLP_FORMAT_FALLBACK,
                     use_pp_mp3=True,
                     abr_kbps=TARGET_ABR_KBPS)
        files = _collect_audio_files(session_dir)

    if not files:
        raise RuntimeError("No audio files were downloaded (all items unavailable?).")

    # Docs + playlist
    docs = _write_docs(session_dir, info, files)

    # Zip parts
    zip_base = _derive_zip_basename(info)
    zips = build_zip_parts(files, session_dir, zip_base, zip_part_limit_bytes, extra_first=docs)
    if not zips:
        biggest = max((os.path.getsize(f), f) for f in files)[1]
        raise RuntimeError(f"Track too large for part limit: {os.path.basename(biggest)}")

    # Duration (best-effort: sum entry durations)
    total_sec = 0
    for e in _normalize_entries(info):
        if e and e.get("duration"):
            total_sec += int(e["duration"])
    dur_hmmss = _hmmss(total_sec if total_sec > 0 else None)

    return {
        "zips": zips,
        "count": len(files),
        "duration_hmmss": dur_hmmss,
        "bitrate": TARGET_ABR_KBPS,
        "zip_base": zip_base,
        "work_dir": session_dir,
    }
