# constants.py
TARGET_ABR_KBPS = 192  # final MP3 bitrate
ALLOWED_DOMAINS = {"youtube.com", "youtu.be", "soundcloud.com", "vimeo.com", "dailymotion.com"}
DEFAULT_ZIP_PART_MB = 45  # local/test; Discord limit is read at runtime
OUT_FILENAME_TEMPLATE = "%(title)s.%(ext)s"

# Primary, then fallback if nothing downloads
YTDLP_FORMAT_PRIMARY = "ba[ext=m4a]/ba[acodec^=mp4a]/ba[ext=webm]/ba/bestaudio/best"
YTDLP_FORMAT_FALLBACK = "bestaudio/best"

# OPTIONAL: export your browser cookies and put the file in project root.
# Use a “cookies.txt” extension (Netscape format).
COOKIES_FILE = "cookies.txt"  # set to None to disable
