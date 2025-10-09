import os

TOKEN = os.getenv("DISCORD_TOKEN")

ALLOWED_DOMAINS = {
    "youtube.com", "youtu.be",
    "soundcloud.com",
    "vimeo.com",
    "dailymotion.com"
}

# temp download path
DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
