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
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"❌ Sync failed: {e}")

@bot.tree.command(name="rip", description="Rip audio from YouTube, SoundCloud, Bandcamp, etc.")
@app_commands.describe(link="Provide the media link")
async def rip(interaction: discord.Interaction, link: str):
    await handle_rip(interaction, link)

bot.run(TOKEN)
