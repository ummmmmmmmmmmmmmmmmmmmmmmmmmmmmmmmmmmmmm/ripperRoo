# ytdlp_wrapper.py
import os, yt_dlp, time
from typing import Callable, Optional, Dict, Any, List
from constants import YTDLP_FORMAT, OUT_FILENAME_TEMPLATE

class QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): print(msg)

def build_ydl_opts(
    out_dir: str,
    include_art: bool,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
):
    hooks = [progress_hook] if progress_hook else []
    return {
        "format": YTDLP_FORMAT,
        "outtmpl": os.path.join(out_dir, OUT_FILENAME_TEMPLATE),
        "logger": QuietLogger(),
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "ignore_no_formats_error": True,
        "extractor_retries": 3,
        "skip_unavailable_fragments": True,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
        "buffersize": 128 * 1024,
        "socket_timeout": 10,
        "noplaylist": False,            # allows playlist ripping
        "writethumbnail": include_art,  # fetch best thumbnail(s)
        "skip_download": False,
        "progress_hooks": hooks,
        # Helps YouTube when desktop formats are funky
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }

def extract_info(url: str, out_dir: str, include_art: bool) -> Optional[dict]:
    with yt_dlp.YoutubeDL(build_ydl_opts(out_dir, include_art)) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except Exception:
            return None

def download_all(url: str, out_dir: str, include_art: bool,
                 progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
    with yt_dlp.YoutubeDL(build_ydl_opts(out_dir, include_art, progress_hook)) as ydl:
        ydl.download([url])
