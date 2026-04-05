import logging
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

log = logging.getLogger(__name__)

MEN_COLOR = discord.Color.from_rgb(173, 216, 230)      # 薄水色
WOMEN_COLOR = discord.Color.from_rgb(255, 182, 193)    # 薄ピンク
DEFAULT_COLOR = discord.Color.light_grey()


class VCProfileDBCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings_cache: dict[int, dict] = {}
        self.startup_sync_done = False

    async def cog_load(self):
        await self.ensure_tables()

    # =========================
    # DB 初期化
    # =========================
    async def ensure_tables(self):
        sql = """
        CREATE TABLE IF NOT EXISTS guild_profile_settings (
            guild_id        BIGINT PRIMARY KEY,
            prof_tc_id      BIGINT NOT NULL,
            men_role_id     BIGINT NOT NULL,
            women_role_id   BIGINT NOT NULL,
            enabled         BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS member_profiles (
            guild_id            BIGINT NOT NULL,
            user_id             BIGINT NOT NULL,
            profile_channel_id  BIGINT NOT NULL,
            profile_message_id  BIGINT NOT NULL,
            profile_content     TEXT,
            profile_jump_url    TEXT NOT NULL,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS active_vc_profile_posts (
            guild_id            BIGINT NOT NULL,
            user_id             BIGINT NOT NULL,
            voice_channel_id    BIGINT NOT NULL,
            sent_message_id     BIGINT NOT NULL,
            profile_message_id  BIGINT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_member_profiles_guild_updated
            ON member_profiles (guild_id, updated_at DESC);

        CREATE INDEX IF NOT EXISTS idx_active_vc_profile_posts_guild_voice
            ON active_vc_profile_posts (guild_id, voice_channel_id);
        """
        async with self.bot.db.acquire() as conn:
            await conn.execute(sql)

    # =========================
    # 設定
    # =========================
    async def get_settings(self, guild_id: int, refresh: bool = False) -> Optional[dict]:
        if not refresh and guild_id in self.settings_cache:
            return self.settings_cache[guild_id]

        sql = """
        SELECT guild_id, prof_tc_id, men_role_id, women_role_id, enabled
        FROM guild_profile_settings
        WHERE guild_id = $1
        """
        async with self.bot.db.acquire() as conn:
            row = await conn.fetchrow(sql, guild_id)

        if row is None:
            self.settings_cache.pop(guild_id, None)
            return None

        data = dict(row)
        self.settings_cache[guild_id] = data
        return data

    async def upsert_settings(
        self,
        guild_id: int,
        prof_tc_id: int,
        men_role_id: int,
        women_role_id: int,
        enabled: bool = True,
    ):
        sql = """
        INSERT INTO guild_profile_settings (
            guild_id, prof_tc_id, men_role_id, women_role_id, enabled, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, NOW())
        ON CONFLICT (guild_id)
        DO UPDATE SET
            prof_tc_id = EXCLUDED.prof_tc_id,
            men_role_id = EXCLUDED.men_role_id,
            women_role_id = EXCLUDED.women_role_id,
            enabled = EXCLUDED.enabled,
            updated_at = NOW()
        """
        async with self.bot.db.acquire() as conn:
            await conn.execute(sql, guild_id, prof_tc_id, men_role_id, women_role_id, enabled)

        await self.get_settings(guild_id, refresh=True)

    async def disable_settings(self, guild_id: int):
        sql = """
        UPDATE guild_profile_settings
        SET enabled = FALSE,
            updated_at = NOW()
        WHERE guild_id = $1
        """
        async with self.bot.db.acquire() as conn:
            await conn.execute(sql, guild_id)

        await self.get_settings(guild_id, refresh=True)

    # =========================
    # プロフィール保存
    # =========================
    def extract_profile_content(self, message: discord.Message) -> str:
        parts: list[str] = []

        if message.content:
            parts.append(message.content.strip())

        if message.attachments:
            parts.extend(att.url for att in message.attachments)

        text = "\n".join(p for p in parts if p).strip()
        return text or "（本文なし）"

    async def upsert_profile_from_message(self, message: discord.Message):
        content = self.extract_profile_content(message)

        sql = """
        INSERT INTO member_profiles (
            guild_id,
            user_id,
            profile_channel_id,
            profile_message_id,
            profile_content,
            profile_jump_url,
            updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET
            profile_channel_id = EXCLUDED.profile_channel_id,
            profile_message_id = EXCLUDED.profile_message_id,
            profile_content = EXCLUDED.profile_content,
            profile_jump_url = EXCLUDED.profile_jump_url,
            updated_at = NOW()
        """
        async with self.bot.db.acquire() as conn:
            await conn.execute(
                sql,
                message.guild.id,
                message.author.id,
                message.channel.id,
                message.id,
                content,
                message.jump_url,
            )

    async def update_profile_if_current_message(self, message: discord.Message):
        content = self.extract_profile_content(message)

        sql = """
        UPDATE member_profiles
        SET profile_content = $4,
            profile_jump_url = $5,
            updated_at = NOW()
        WHERE guild_id = $1
          AND user_id = $2
          AND profile_message_id = $3
        """
        async with self.bot.db.acquire() as conn:
            await conn.execute(
                sql,
                message.guild.id,
                message.author.id,
                message.id,
                content,
                message.jump_url,
            )

    async def delete_profile_if_current_message(self, message: discord.Message):
        sql = """
        DELETE FROM member_profiles
        WHERE guild_id = $1
          AND user_id = $2
          AND profile_message_id = $3
        """
        async with self.bot.db.acquire() as conn:
            await conn.execute(sql, message.guild.id, message.author.id, message.id)

    async def get_profile(self, guild_id: int, user_id: int) -> Optional[dict]:
        sql = """
        SELECT guild_id, user_id, profile_channel_id, profile_message_id,
               profile_content, profile_jump_url, updated_at
        FROM member_profiles
        WHERE guild_id = $1
          AND user_id = $2
        """
        async with self.bot.db.acquire() as conn:
            row = await conn.fetchrow(sql, guild_id, user_id)

        return dict(row) if row else None

    # =========================
    # VC投稿管理
    # =========================
    async def upsert_active_post(
        self,
        guild_id: int,
        user_id: int,
        voice_channel_id: int,
        sent_message_id: int,
        profile_message_id: Optional[int],
    ):
        sql = """
        INSERT INTO active_vc_profile_posts (
            guild_id, user_id, voice_channel_id, sent_message_id, profile_message_id, created_at
        )
        VALUES ($1, $2, $3, $4, $5, NOW())
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET
            voice_channel_id = EXCLUDED.voice_channel_id,
            sent_message_id = EXCLUDED.sent_message_id,
            profile_message_id = EXCLUDED.profile_message_id,
            created_at = NOW()
        """
        async with self.bot.db.acquire() as conn:
            await conn.execute(sql, guild_id, user_id, voice_channel_id, sent_message_id, profile_message_id)

    async def get_active_post(self, guild_id: int, user_id: int) -> Optional[dict]:
        sql = """
        SELECT guild_id, user_id, voice_channel_id, sent_message_id, profile_message_id, created_at
        FROM active_vc_profile_posts
        WHERE guild_id = $1
          AND user_id = $2
        """
        async with self.bot.db.acquire() as conn:
            row = await conn.fetchrow(sql, guild_id, user_id)

        return dict(row) if row else None

    async def delete_active_post_row(self, guild_id: int, user_id: int):
        sql = """
        DELETE FROM active_vc_profile_posts
        WHERE guild_id = $1
          AND user_id = $2
        """
        async with self.bot.db.acquire() as conn:
            await conn.execute(sql, guild_id, user_id)

    async def get_all_active_posts_in_guild(self, guild_id: int) -> list[dict]:
        sql = """
        SELECT guild_id, user_id, voice_channel_id, sent_message_id, profile_message_id, created_at
        FROM active_vc_profile_posts
        WHERE guild_id = $1
        """
        async with self.bot.db.acquire() as conn:
            rows = await conn.fetch(sql, guild_id)

        return [dict(r) for r in rows]

    async def delete_post_message_by_ids(self, guild_id: int, channel_id: int, message_id: int):
        try:
            messageable = self.bot.get_partial_messageable(channel_id, guild_id=guild_id)
            partial = messageable.get_partial_message(message_id)
            await partial.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning(
                "VCプロフィール投稿の削除権限がありません guild_id=%s channel_id=%s message_id=%s",
                guild_id, channel_id, message_id
            )
        except Exception:
            log.exception(
                "VCプロフィール投稿削除失敗 guild_id=%s channel_id=%s message_id=%s",
                guild_id, channel_id, message_id
            )

    async def remove_active_post(self, guild: discord.Guild, user_id: int):
        row = await self.get_active_post(guild.id, user_id)
        if row is None:
            return

        await self.delete_post_message_by_ids(
            guild_id=guild.id,
            channel_id=row["voice_channel_id"],
            message_id=row["sent_message_id"],
        )
        await self.delete_active_post_row(guild.id, user_id)

    # =========================
    # Embed
    # =========================
    def get_embed_color(self, member: discord.Member, settings: dict) -> discord.Color:
        if member.get_role(settings["men_role_id"]):
            return MEN_COLOR
        if member.get_role(settings["women_role_id"]):
            return WOMEN_COLOR
        return DEFAULT_COLOR

    def trim_for_embed(self, text: str, limit: int = 4000) -> str:
        text = text.strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def build_profile_embed(
        self,
        member: discord.Member,
        settings: dict,
        profile: Optional[dict],
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{member.display_name} のプロフィール",
            color=self.get_embed_color(member, settings),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if profile is None:
            embed.description = "プロフィール未登録です。"
            return embed

        embed.description = self.trim_for_embed(profile["profile_content"] or "（本文なし）")

        embed.add_field(
            name="プロフィール元メッセージ",
            value=f"[ここを押すと移動]({profile['profile_jump_url']})",
            inline=False,
        )
        return embed

    async def send_profile_post(self, member: discord.Member, channel: discord.VoiceChannel):
        settings = await self.get_settings(member.guild.id)
        if not settings or not settings["enabled"]:
            return

        profile = await self.get_profile(member.guild.id, member.id)
        embed = self.build_profile_embed(member, settings, profile)

        sent = await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await self.upsert_active_post(
            guild_id=member.guild.id,
            user_id=member.id,
            voice_channel_id=channel.id,
            sent_message_id=sent.id,
            profile_message_id=(profile["profile_message_id"] if profile else None),
        )

    # =========================
    # 起動時再同期
    # =========================
    async def reconcile_guild(self, guild: discord.Guild):
        settings = await self.get_settings(guild.id)
        if not settings or not settings["enabled"]:
            return

        current_voice_members: dict[int, discord.VoiceChannel] = {}
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                current_voice_members[member.id] = vc

        rows = await self.get_all_active_posts_in_guild(guild.id)
        valid_user_ids: set[int] = set()

        for row in rows:
            user_id = row["user_id"]
            current_channel = current_voice_members.get(user_id)

            if current_channel is None or current_channel.id != row["voice_channel_id"]:
                await self.delete_post_message_by_ids(
                    guild_id=guild.id,
                    channel_id=row["voice_channel_id"],
                    message_id=row["sent_message_id"],
                )
                await self.delete_active_post_row(guild.id, user_id)
            else:
                valid_user_ids.add(user_id)

        for user_id, vc in current_voice_members.items():
            if user_id in valid_user_ids:
                continue
            member = guild.get_member(user_id)
            if member is None:
                continue
            await self.send_profile_post(member, vc)

    async def force_resync_guild(self, guild: discord.Guild):
        rows = await self.get_all_active_posts_in_guild(guild.id)
        for row in rows:
            await self.delete_post_message_by_ids(
                guild_id=guild.id,
                channel_id=row["voice_channel_id"],
                message_id=row["sent_message_id"],
            )
            await self.delete_active_post_row(guild.id, row["user_id"])

        settings = await self.get_settings(guild.id)
        if not settings or not settings["enabled"]:
            return

        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                await self.send_profile_post(member, vc)

    # =========================
    # イベント
    # =========================
    @commands.Cog.listener()
    async def on_ready(self):
        if self.startup_sync_done:
            return

        self.startup_sync_done = True
        for guild in self.bot.guilds:
            try:
                await self.reconcile_guild(guild)
            except Exception:
                log.exception("起動時再同期失敗 guild_id=%s", guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        settings = await self.get_settings(message.guild.id)
        if not settings or not settings["enabled"]:
            return

        if message.channel.id != settings["prof_tc_id"]:
            return

        await self.upsert_profile_from_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or after.guild is None:
            return

        settings = await self.get_settings(after.guild.id)
        if not settings or not settings["enabled"]:
            return

        if after.channel.id != settings["prof_tc_id"]:
            return

        await self.update_profile_if_current_message(after)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        settings = await self.get_settings(message.guild.id)
        if not settings or not settings["enabled"]:
            return

        if message.channel.id != settings["prof_tc_id"]:
            return

        await self.delete_profile_if_current_message(message)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        if before.channel == after.channel:
            return

        settings = await self.get_settings(member.guild.id)
        if not settings or not settings["enabled"]:
            return

        if before.channel is not None:
            await self.remove_active_post(member.guild, member.id)

        if after.channel is not None and isinstance(after.channel, discord.VoiceChannel):
            try:
                await self.send_profile_post(member, after.channel)
            except discord.Forbidden:
                log.warning(
                    "VCチャットへの送信権限がありません guild_id=%s channel_id=%s",
                    member.guild.id, after.channel.id
                )
            except Exception:
                log.exception(
                    "VCプロフィール送信失敗 guild_id=%s user_id=%s channel_id=%s",
                    member.guild.id, member.id, after.channel.id
                )

    # =========================
    # 管理者コマンド
    # =========================
    @app_commands.command(name="vcプロフ設定", description="このサーバーのVCプロフィール設定を保存します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        prof_tc="プロフィールを書かせるチャンネル",
        men_role="男性ロール",
        women_role="女性ロール",
    )
    async def vc_profile_set(
        self,
        interaction: discord.Interaction,
        prof_tc: discord.TextChannel,
        men_role: discord.Role,
        women_role: discord.Role,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        await self.upsert_settings(
            guild_id=interaction.guild.id,
            prof_tc_id=prof_tc.id,
            men_role_id=men_role.id,
            women_role_id=women_role.id,
            enabled=True,
        )

        await interaction.response.send_message(
            (
                "VCプロフィール設定を保存しました。\n"
                f"プロフチャンネル: {prof_tc.mention}\n"
                f"男性ロール: {men_role.mention}\n"
                f"女性ロール: {women_role.mention}"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="vcプロフ状態", description="このサーバーのVCプロフィール設定状況を表示します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def vc_profile_status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.get_settings(interaction.guild.id, refresh=True)
        if not settings:
            await interaction.response.send_message("まだ設定されていません。", ephemeral=True)
            return

        prof_ch = interaction.guild.get_channel(settings["prof_tc_id"])
        men_role = interaction.guild.get_role(settings["men_role_id"])
        women_role = interaction.guild.get_role(settings["women_role_id"])

        embed = discord.Embed(
            title="VCプロフィール設定状況",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="有効状態",
            value="有効" if settings["enabled"] else "無効",
            inline=False,
        )
        embed.add_field(
            name="プロフチャンネル",
            value=prof_ch.mention if prof_ch else f"`{settings['prof_tc_id']}`",
            inline=False,
        )
        embed.add_field(
            name="男性ロール",
            value=men_role.mention if men_role else f"`{settings['men_role_id']}`",
            inline=False,
        )
        embed.add_field(
            name="女性ロール",
            value=women_role.mention if women_role else f"`{settings['women_role_id']}`",
            inline=False,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="vcプロフ無効", description="このサーバーのVCプロフィール機能を無効化します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def vc_profile_disable(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        await self.disable_settings(interaction.guild.id)
        await interaction.response.send_message("VCプロフィール機能を無効化しました。", ephemeral=True)

    @app_commands.command(name="vcプロフ再同期", description="今VCにいるユーザー分のプロフィール埋め込みを再同期します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def vc_profile_resync(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.force_resync_guild(interaction.guild)
        await interaction.followup.send("VCプロフィールを再同期しました。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VCProfileDBCog(bot))