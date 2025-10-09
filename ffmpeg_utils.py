# ffmpeg_utils.py
import shutil, subprocess, os
from constants import TARGET_ABR_KBPS

def ensure_ffmpeg_available() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not available on PATH. Install ffmpeg first.")

def transcode_to_mp3(src_path: str, dst_path: str, abr_kbps: int = TARGET_ABR_KBPS) -> None:
    """
    Transcode (or re-mux) any audio file to constant-bitrate MP3 using ffmpeg.
    Overwrites dst_path if exists.
    """
    ensure_ffmpeg_available()
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", src_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", f"{abr_kbps}k",
        dst_path
    ]
    subprocess.run(cmd, check=True)

def embed_art_in_mp3(mp3_path: str, art_path: str) -> None:
    """Embed album art into the MP3 as APIC (front cover)."""
    ensure_ffmpeg_available()
    tmp = mp3_path + ".arttmp.mp3"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", mp3_path, "-i", art_path,
        "-map", "0:a", "-map", "1:v",
        "-c:a", "copy",
        "-c:v", "mjpeg",
        "-metadata:s:v", "title=Album cover",
        "-metadata:s:v", "comment=Cover (front)",
        tmp
    ]
    subprocess.run(cmd, check=True)
    os.replace(tmp, mp3_path)
