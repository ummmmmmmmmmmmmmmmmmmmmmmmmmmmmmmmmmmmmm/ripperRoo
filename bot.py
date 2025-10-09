import discord
from discord import app_commands
from discord.ext import commands
from config import TOKEN
from discord_adapter import handle_rip
from utils import auto_clean_temp

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="*", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    auto_clean_temp()  # 🧹 Clean old temp folders on startup
    await bot.tree.sync()
    print("✅ Slash commands synced and temp cleaned.")

@bot.tree.command(name="rip", description="Rip audio from supported sites")
@app_commands.describe(link="Provide a YouTube, SoundCloud, Vimeo, or Dailymotion link")
async def rip(interaction: discord.Interaction, link: str):
    await handle_rip(interaction, link)

bot.run(TOKEN)
