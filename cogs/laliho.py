import discord
from discord.ext import commands
from discord import app_commands
import os

GUILD_ID = int(os.getenv("GUILD_ID"))
EMBED_COLOR = discord.Color.from_str("#bce2e8")
LOG_CHANNEL_ID = 1482847880577548469

class LalihoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ラリホー", description="寝落ちしたメンバーを切断します。")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def laliho(self, interaction: discord.Interaction):
        await interaction.response.send_message("ラリホーを起動しました。", ephemeral=True)
        embed = discord.Embed(
            title="💤ラリホー😪",
            description="寝落ちしたメンバーを切断出来ます。",
            color=EMBED_COLOR
        )
        await interaction.channel.send(embed=embed, view=InitialView())


class InitialView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="▷ラリホー", style=discord.ButtonStyle.secondary, custom_id="laliho_button")
    async def laliho_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_vc = interaction.user.voice.channel if interaction.user.voice else None
        if not user_vc:
            await interaction.response.send_message("VC接続時のみ利用できます。", ephemeral=True)
            return

        members = [m for m in user_vc.members if not m.bot]
        if not members:
            await interaction.response.send_message("VC内に切断対象がいません。", ephemeral=True)
            return

        embed = discord.Embed(description="誰をラリホーしますか？", color=EMBED_COLOR)
        await interaction.response.send_message(embed=embed, view=MemberSelectView(members), ephemeral=True)


class MemberSelectView(discord.ui.View):
    def __init__(self, members: list[discord.Member]):
        super().__init__(timeout=None)
        for m in members[:25]:
            self.add_item(MemberButton(m))


class MemberButton(discord.ui.Button):
    def __init__(self, member: discord.Member):
        super().__init__(
            label=member.display_name,
            style=discord.ButtonStyle.primary,
            custom_id=f"laliho_member_{member.id}"
        )
        self.member = member

    async def callback(self, interaction: discord.Interaction):
        embed = discord.Embed(
            description=f"{self.member.display_name} をラリホーしますか？",
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed, view=ConfirmView(self.member), ephemeral=True)


class ConfirmView(discord.ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=None)
        self.member = member

    @discord.ui.button(label="唱える", style=discord.ButtonStyle.danger, custom_id="laliho_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_channel = self.member.voice.channel if self.member.voice else None

        if voice_channel:
            try:
                await self.member.move_to(None)
                await interaction.response.send_message(f"{self.member.display_name} をラリホーしました！", ephemeral=True)

                # ログ送信
                log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
                if log_channel:
                    embed = discord.Embed(
                        color=EMBED_COLOR,
                        title="ラリホー😪",
                        description=f'おお…「{self.member.mention}」よ！死んでしまうとは情けない！'
                    )
                    embed.add_field(name="唱えた人",value=interaction.user.mention, inline=True)
                    embed.add_field(name="唱えられた人", value=self.member.mention,inline=True)
                    
                    await log_channel.send(embed=embed)
            except discord.Forbidden:
                await interaction.followup.send("権限が足りません。", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"エラー: {e}", ephemeral=True)
        else:
            await interaction.response.send_message("既にVCから退出しています。", ephemeral=True)

        self.stop()



    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary, custom_id="laliho_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)
        self.stop()

# ✅ 永続ビューを登録する専用関数
def register_persistent_views(bot: commands.Bot):
    bot.add_view(InitialView())

async def setup(bot: commands.Bot):
    await bot.add_cog(LalihoCog(bot))
