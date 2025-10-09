# ytdlp_wrapper.py
import os, yt_dlp
from typing import Callable, Optional, Dict, Any
from constants import OUT_FILENAME_TEMPLATE, YTDLP_FORMAT_PRIMARY, COOKIES_FILE

class QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): print(msg)

def build_ydl_opts(
    out_dir: str,
    include_art: bool,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
    format_str: Optional[str] = None,
    use_pp_mp3: bool = False,
    abr_kbps: int = 192,
):
    hooks = [progress_hook] if progress_hook else []
    fmt = format_str or YTDLP_FORMAT_PRIMARY
    opts = {
        "format": fmt,
        "outtmpl": os.path.join(out_dir, OUT_FILENAME_TEMPLATE),
        "logger": QuietLogger(),
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,

        # robustness
        "ignoreerrors": "only_download",   # skip broken entries, keep ripping
        "ignore_no_formats_error": True,
        "extractor_retries": 3,
        "skip_unavailable_fragments": True,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
        "buffersize": 128 * 1024,
        "socket_timeout": 10,
        "geo_bypass": True,

        # playlists & art
        "noplaylist": False,
        "writethumbnail": include_art,
        "skip_download": False,

        "progress_hooks": hooks,

        # Often helps YouTube when desktop player formats are odd
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }

    # Optional cookies (fix age/region blocks)
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    # Let yt-dlp run ffmpeg to MP3 directly
    if use_pp_mp3:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(abr_kbps)},
            {"key": "FFmpegMetadata"},
        ]
        opts["keepvideo"] = False

    return opts

def extract_info(url: str, out_dir: str, include_art: bool, format_str: Optional[str] = None) -> Optional[dict]:
    with yt_dlp.YoutubeDL(build_ydl_opts(out_dir, include_art, format_str=format_str)) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except Exception:
            return None

def download_all(url: str, out_dir: str, include_art: bool,
                 progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
                 format_str: Optional[str] = None,
                 use_pp_mp3: bool = False,
                 abr_kbps: int = 192) -> None:
    with yt_dlp.YoutubeDL(build_ydl_opts(out_dir, include_art, progress_hook, format_str, use_pp_mp3, abr_kbps)) as ydl:
        ydl.download([url])
