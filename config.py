import os

# ===== BOT CONFIG =====
TOKEN = os.getenv("DISCORD_TOKEN")  # Set this in PowerShell: $env:DISCORD_TOKEN='YOUR_TOKEN'
FFMPEG_BIN = os.getenv("FFMPEG_BIN", r"C:\ffmpeg\bin")  # adjust path if needed

# Allowed domains
ALLOWED_DOMAINS = {"youtube.com", "youtu.be", "music.youtube.com", "soundcloud.com", "bandcamp.com"}
