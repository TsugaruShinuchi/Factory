import os
import logging
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

PROF_TC_ID = 1481378900100
MEN_ROLE_ID = 1477396194018984062
WOMEN_ROLE_ID = 1477396319139008646
PROFILE_HISTORY_LIMIT = 10  # 起動後にキャッシュが空だった場合、過去メッセージをどこまで遡るか

MEN_COLOR = discord.Color.from_rgb(173, 216, 230)      # 薄水色
WOMEN_COLOR = discord.Color.from_rgb(255, 182, 193)    # 薄ピンク
DEFAULT_COLOR = discord.Color.light_grey()


class VCProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # (guild_id, user_id) -> profile_message_id
        self.profile_message_cache: dict[tuple[int, int], int] = {}

        # (guild_id, user_id) -> {"channel_id": int, "message_id": int}
        self.active_vc_profile_messages: dict[tuple[int, int], dict[str, int]] = {}

        self.cache_initialized = False

    # =========================
    # 初期化
    # =========================
    async def build_profile_cache(self):
        for guild in self.bot.guilds:
            profile_ch = guild.get_channel(PROF_TC_ID)
            if not isinstance(profile_ch, discord.TextChannel):
                log.warning("PROF_TC_ID が TextChannel ではありません: %s", PROF_TC_ID)
                continue

            # 新しいメッセージから見る
            async for message in profile_ch.history(limit=PROFILE_HISTORY_LIMIT):
                if message.author.bot:
                    continue

                key = (guild.id, message.author.id)
                if key not in self.profile_message_cache:
                    self.profile_message_cache[key] = message.id

        self.cache_initialized = True
        log.info("プロフィールキャッシュ構築完了: %s 件", len(self.profile_message_cache))

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.cache_initialized:
            await self.build_profile_cache()

    # =========================
    # ヘルパー
    # =========================
    def get_profile_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        ch = guild.get_channel(PROF_TC_ID)
        return ch if isinstance(ch, discord.TextChannel) else None

    def get_embed_color(self, member: discord.Member) -> discord.Color:
        if member.get_role(MEN_ROLE_ID):
            return MEN_COLOR
        if member.get_role(WOMEN_ROLE_ID):
            return WOMEN_COLOR
        return DEFAULT_COLOR

    async def get_profile_message(self, member: discord.Member) -> Optional[discord.Message]:
        guild = member.guild
        profile_ch = self.get_profile_channel(guild)
        if profile_ch is None:
            return None

        key = (guild.id, member.id)
        cached_message_id = self.profile_message_cache.get(key)

        # キャッシュがあればまず取得
        if cached_message_id:
            try:
                return await profile_ch.fetch_message(cached_message_id)
            except discord.NotFound:
                self.profile_message_cache.pop(key, None)
            except discord.Forbidden:
                log.warning("プロフメッセージ取得権限なし: guild_id=%s", guild.id)
                return None
            except Exception:
                log.exception("キャッシュ済みプロフメッセージ取得失敗")

        # キャッシュが無い or 消えていたら履歴から探す
        try:
            async for message in profile_ch.history(limit=PROFILE_HISTORY_LIMIT):
                if message.author.id == member.id and not message.author.bot:
                    self.profile_message_cache[key] = message.id
                    return message
        except discord.Forbidden:
            log.warning("プロフチャンネル履歴取得権限なし: guild_id=%s", guild.id)
        except Exception:
            log.exception("プロフ履歴検索失敗")

        return None

    def build_profile_embed(
        self,
        member: discord.Member,
        profile_message: Optional[discord.Message],
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{member.display_name} のプロフィール",
            color=self.get_embed_color(member),
        )

        if profile_message is None:
            embed.description = "プロフィール未登録です。"
        else:
            content = profile_message.content.strip() if profile_message.content else ""
            if not content and profile_message.attachments:
                content = "\n".join(att.url for att in profile_message.attachments)
            if not content:
                content = "（本文なし）"

            embed.description = content

        embed.set_thumbnail(url=member.display_avatar.url)

        # 最下部にリンクを置きたいので field を最後に追加
        if profile_message is not None:
            embed.add_field(
                name="プロフィール元メッセージ",
                value=f"[ここを押すと移動]({profile_message.jump_url})",
                inline=False,
            )

        return embed

    async def send_profile_to_vc_chat(
        self,
        member: discord.Member,
        voice_channel: discord.VoiceChannel,
    ):
        profile_message = await self.get_profile_message(member)
        embed = self.build_profile_embed(member, profile_message)

        sent_message = await voice_channel.send(embed=embed)

        key = (member.guild.id, member.id)
        self.active_vc_profile_messages[key] = {
            "channel_id": voice_channel.id,
            "message_id": sent_message.id,
        }

    async def delete_profile_from_vc_chat(
        self,
        guild: discord.Guild,
        member_id: int,
        fallback_channel: Optional[discord.abc.GuildChannel] = None,
    ):
        key = (guild.id, member_id)
        data = self.active_vc_profile_messages.pop(key, None)
        if not data:
            return

        channel = guild.get_channel(data["channel_id"])
        if channel is None:
            channel = fallback_channel

        if not isinstance(channel, discord.VoiceChannel):
            return

        try:
            partial = channel.get_partial_message(data["message_id"])
            await partial.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning(
                "VCプロフィール埋め込み削除権限なし: guild_id=%s channel_id=%s",
                guild.id,
                channel.id,
            )
        except Exception:
            log.exception("VCプロフィール埋め込み削除失敗")

    # =========================
    # プロフ更新監視
    # =========================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.guild is None:
            return

        if message.channel.id != PROF_TC_ID:
            return

        key = (message.guild.id, message.author.id)
        self.profile_message_cache[key] = message.id

    # =========================
    # VC入退室監視
    # =========================
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        # ミュート変更などは無視
        if before.channel == after.channel:
            return

        # 退室 or 移動前
        if before.channel is not None:
            await self.delete_profile_from_vc_chat(
                guild=member.guild,
                member_id=member.id,
                fallback_channel=before.channel,
            )

        # 入室 or 移動後
        if after.channel is not None and isinstance(after.channel, discord.VoiceChannel):
            try:
                await self.send_profile_to_vc_chat(member, after.channel)
            except discord.Forbidden:
                log.warning(
                    "VCチャット送信権限なし: guild_id=%s channel_id=%s",
                    member.guild.id,
                    after.channel.id,
                )
            except Exception:
                log.exception("VCプロフィール送信失敗")


async def setup(bot: commands.Bot):
    await bot.add_cog(VCProfileCog(bot))