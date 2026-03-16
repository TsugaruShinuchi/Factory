import os
import logging
from datetime import datetime

import discord
from discord.ext import commands
from discord import app_commands

log = logging.getLogger(__name__)

JOIN_LOG_CHANNEL_ID = 1482816742001475727
EMBED_COLOR = discord.Color.from_rgb(114, 47, 55)  # wine redっぽい色


class InviteTrackerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> {invite_code: uses}
        self.invite_cache: dict[int, dict[str, int]] = {}
        self.cache_initialized = False

    async def cog_load(self):
        # setup_hook中に wait_until_ready 系へ入ると詰まりやすいので何もしない
        pass

    async def build_invite_cache(self):
        for guild in self.bot.guilds:
            await self.refresh_guild_invites(guild)
        self.cache_initialized = True

    async def refresh_guild_invites(self, guild: discord.Guild):
        try:
            invites = await guild.invites()
            self.invite_cache[guild.id] = {
                invite.code: (invite.uses or 0)
                for invite in invites
            }
            log.info("招待キャッシュ更新: guild_id=%s invites=%s", guild.id, len(invites))
        except discord.Forbidden:
            log.warning("招待一覧取得権限なし: guild_id=%s", guild.id)
        except Exception:
            log.exception("招待キャッシュ更新失敗: guild_id=%s", guild.id)

    def format_dt(self, dt: datetime) -> str:
        unix = int(dt.timestamp())
        return f"<t:{unix}:F>"

    def build_join_embed(
        self,
        member: discord.Member,
        inviter: discord.User | discord.Member | None = None,
    ) -> discord.Embed:
        member_name = member.display_name
        joined_text = self.format_dt(member.joined_at or discord.utils.utcnow())

        if inviter is not None:
            inviter_name = getattr(inviter, "display_name", inviter.name)
            description = (
                f"{inviter.mention}（{inviter_name}）▷ "
                f"{member.mention}（{member_name}）"
            )
        else:
            description = (
                f"招待者不明 ▷ {member.mention}（{member_name}）\n"
                f"※ 使用された招待URLを特定できませんでした。"
            )

        embed = discord.Embed(
            title="新規参加者が入場したよ！",
            description=description,
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="参加日時",
            value=joined_text,
            inline=False,
        )

        avatar = member.display_avatar
        embed.set_thumbnail(url=avatar.url)

        return embed

    async def send_join_log(
        self,
        guild: discord.Guild,
        member: discord.Member,
        inviter: discord.User | discord.Member | None = None,
    ):
        channel = guild.get_channel(JOIN_LOG_CHANNEL_ID)
        if channel is None:
            log.warning("JOIN_LOG_CHANNEL_ID のチャンネルが見つかりません: %s", JOIN_LOG_CHANNEL_ID)
            return

        embed = self.build_join_embed(member=member, inviter=inviter)
        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready は複数回呼ばれうるので一度だけ初期化
        if not self.cache_initialized:
            await self.build_invite_cache()
            log.info("初回招待キャッシュ構築完了")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.refresh_guild_invites(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild = invite.guild
        if guild is None:
            return

        self.invite_cache.setdefault(guild.id, {})
        self.invite_cache[guild.id][invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        guild = invite.guild
        if guild is None:
            return

        if guild.id in self.invite_cache:
            self.invite_cache[guild.id].pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        before_cache = self.invite_cache.get(guild.id, {}).copy()

        try:
            invites = await guild.invites()
        except discord.Forbidden:
            log.warning("on_member_join: 招待一覧取得権限なし guild_id=%s", guild.id)
            try:
                await self.send_join_log(guild, member, inviter=None)
            except Exception:
                log.exception("参加通知の送信に失敗（権限不足フォールバック）")
            return
        except Exception:
            log.exception("on_member_join: 招待一覧取得失敗 guild_id=%s", guild.id)
            try:
                await self.send_join_log(guild, member, inviter=None)
            except Exception:
                log.exception("参加通知の送信に失敗（例外フォールバック）")
            return

        used_invite: discord.Invite | None = None
        current_cache: dict[str, int] = {}

        for invite in invites:
            current_uses = invite.uses or 0
            current_cache[invite.code] = current_uses

            old_uses = before_cache.get(invite.code, 0)
            if current_uses > old_uses:
                used_invite = invite

        self.invite_cache[guild.id] = current_cache
        inviter = used_invite.inviter if used_invite else None

        try:
            await self.send_join_log(guild, member, inviter=inviter)
        except Exception:
            log.exception("参加通知の送信に失敗")

    @app_commands.command(name="招待追跡確認", description="招待追跡の現在キャッシュを更新します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def refresh_invites_command(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        await self.refresh_guild_invites(interaction.guild)
        await interaction.response.send_message(
            "招待キャッシュを更新しました。",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteTrackerCog(bot))