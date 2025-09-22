# config.py
import os

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # required
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")
MAX_FILE_BYTES_HINT = int(os.getenv("MAX_FILE_BYTES_HINT", str(25 * 1024 * 1024)))

# Put your server IDs here for INSTANT slash commands (recommended while developing)
APP_GUILD_IDS = [123456789012345678]  # <-- replace with your guild id(s); or [] for global
