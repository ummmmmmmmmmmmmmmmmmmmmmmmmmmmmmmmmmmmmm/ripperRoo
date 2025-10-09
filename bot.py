import discord
from discord import app_commands
from discord.ext import commands
from config import TOKEN
from rip_logic import handle_rip

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="*", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    await bot.tree.sync()
    print("✅ Slash commands synced.")

@bot.tree.command(name="rip", description="Rip audio from supported sites")
@app_commands.describe(link="Paste a YouTube, SoundCloud, Vimeo, or Dailymotion link")
async def rip(interaction: discord.Interaction, link: str):
    await handle_rip(interaction, link)

bot.run(TOKEN)
