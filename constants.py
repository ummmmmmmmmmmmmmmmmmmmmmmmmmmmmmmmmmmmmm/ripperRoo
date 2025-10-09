# constants.py
TARGET_ABR_KBPS = 192  # final MP3 bitrate
ALLOWED_DOMAINS = {"youtube.com", "youtu.be", "soundcloud.com", "vimeo.com", "dailymotion.com"}
DEFAULT_ZIP_PART_MB = 45  # default local/test limit; Discord detects its own
OUT_FILENAME_TEMPLATE = "%(title)s.%(ext)s"

# yt-dlp format fallback chain: prefer m4a, then webm/opus, then any audio
YTDLP_FORMAT_PRIMARY = "ba[ext=m4a]/ba[acodec^=mp4a]/ba[ext=webm]/ba/bestaudio/best"
# broader retry if nothing downloaded on the first pass
YTDLP_FORMAT_FALLBACK = "bestaudio/best"

# Optional cookies file (export with a browser extension). Leave as None to disable.
COOKIES_FILE = "cookies.txt"  # or None
