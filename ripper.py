# ripper.py
import discord
from discord import app_commands
from config import DISCORD_TOKEN, APP_GUILD_IDS
import commands as cmdmod

intents = discord.Intents.default()  # pure slash; no message_content
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def _guild_objs():
    return [discord.Object(id=g) for g in APP_GUILD_IDS]

async def sync_all():
    if APP_GUILD_IDS:
        for g in APP_GUILD_IDS:
            await tree.sync(guild=discord.Object(id=g))
        print(f"Slash commands synced to guilds: {APP_GUILD_IDS}")
    else:
        synced = await tree.sync()
        print(f"Global slash commands synced: {len(synced)}")

@client.event
async def on_ready():
    await cmdmod.on_startup()            # cleanup temp, etc.
    await sync_all()
    print(f"Logged in as {client.user} (id={client.user.id})")
    print("Ready.")

@client.event
async def on_guild_join(guild: discord.Guild):
    # ensure commands show up immediately when added to a new server
    try:
        await tree.sync(guild=guild)
        print(f"Synced for guild {guild.id}")
    except Exception as e:
        print("Guild sync error:", e)
    await cmdmod.thank_new_guild(guild)

# Register slash commands into the tree (scoped per guild if set)
cmdmod.register_commands(tree, _guild_objs())

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment.")
    client.run(DISCORD_TOKEN)
