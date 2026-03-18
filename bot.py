import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
from db.database import init_db_pool

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN が見つかりません")

guild_id_str = os.getenv("GUILD_ID")
if not guild_id_str:
    raise RuntimeError("環境変数 GUILD_ID が見つかりません")
GUILD_ID = int(guild_id_str)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


class MyBot(commands.Bot):
    async def setup_hook(self):
        self.db = await init_db_pool()

        await self.load_extension("cogs.invite_tracker")
        await self.load_extension("cogs.laliho")

        await self.load_extension("cogs.invite_tracker")
        await self.load_extension("cogs.auto_reply")

        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        print("✅ ギルド同期完了")
        print(self.tree.get_commands(guild=guild))


bot = MyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ ログインしました: {bot.user} ({bot.user.id})")


bot.run(TOKEN)