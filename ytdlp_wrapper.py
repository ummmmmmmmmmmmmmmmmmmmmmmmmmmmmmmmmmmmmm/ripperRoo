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
        "ignoreerrors": "only_download",   # skip bad entries but continue playlist
        "ignore_no_formats_error": True,
        "extractor_retries": 3,
        "skip_unavailable_fragments": True,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
        "buffersize": 128 * 1024,
        "socket_timeout": 10,
        "geo_bypass": True,

        # playlists and art
        "noplaylist": False,
        "writethumbnail": include_art,
        "skip_download": False,

        "progress_hooks": hooks,

        # Often helps YouTube when desktop player lists are odd
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }

    # Optional cookies (fixes age/region blocks)
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    return opts

def extract_info(url: str, out_dir: str, include_art: bool, format_str: Optional[str] = None) -> Optional[dict]:
    with yt_dlp.YoutubeDL(build_ydl_opts(out_dir, include_art, format_str=format_str)) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except Exception:
            return None

def download_all(url: str, out_dir: str, include_art: bool,
                 progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
                 format_str: Optional[str] = None) -> None:
    with yt_dlp.YoutubeDL(build_ydl_opts(out_dir, include_art, progress_hook, format_str=format_str)) as ydl:
        ydl.download([url])
