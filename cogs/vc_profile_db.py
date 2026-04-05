import logging
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

log = logging.getLogger(__name__)

MEN_COLOR = discord.Color.from_rgb(173, 216, 230)      # 薄水色
WOMEN_COLOR = discord.Color.from_rgb(255, 182, 193)    # 薄ピンク
DEFAULT_COLOR = discord.Color.light_grey()

IMPORT_DEFAULT_LIMIT = 5000
REBUILD_LIMIT_PER_CHANNEL = 3000


class VCProfileDBCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings_cache: dict[int, dict] = {}
        self.profile_channels_cache: dict[int, list[int]] = {}
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

        CREATE TABLE IF NOT EXISTS guild_profile_channels (
            guild_id    BIGINT NOT NULL,
            channel_id  BIGINT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (guild_id, channel_id)
        );

        CREATE TABLE IF NOT EXISTS member_profiles (
            guild_id            BIGINT NOT NULL,
            user_id             BIGINT NOT NULL,
            profile_channel_id  BIGINT NOT NULL,
            profile_message_id  BIGINT NOT NULL,
            profile_content     TEXT,
            profile_jump_url    TEXT NOT NULL,
            source_created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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

            # 旧テーブルからの移行対策
            await conn.execute("""
                ALTER TABLE member_profiles
                ADD COLUMN IF NOT EXISTS source_created_at TIMESTAMPTZ
            """)
            await conn.execute("""
                UPDATE member_profiles
                SET source_created_at = updated_at
                WHERE source_created_at IS NULL
            """)

    # =========================
    # 設定 / チャンネル
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

    async def get_profile_channel_ids(self, guild_id: int, refresh: bool = False) -> list[int]:
        if not refresh and guild_id in self.profile_channels_cache:
            return self.profile_channels_cache[guild_id]

        settings = await self.get_settings(guild_id, refresh=refresh)
        channel_ids: set[int] = set()

        if settings:
            channel_ids.add(settings["prof_tc_id"])

        sql = """
        SELECT channel_id
        FROM guild_profile_channels
        WHERE guild_id = $1
        """
        async with self.bot.db.acquire() as conn:
            rows = await conn.fetch(sql, guild_id)

        for row in rows:
            channel_ids.add(row["channel_id"])

        result = sorted(channel_ids)
        self.profile_channels_cache[guild_id] = result
        return result

    async def is_profile_channel(self, guild_id: int, channel_id: int) -> bool:
        return channel_id in await self.get_profile_channel_ids(guild_id)

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
        await self.get_profile_channel_ids(guild_id, refresh=True)

    async def add_profile_channel(self, guild_id: int, channel_id: int):
        sql = """
        INSERT INTO guild_profile_channels (guild_id, channel_id)
        VALUES ($1, $2)
        ON CONFLICT (guild_id, channel_id) DO NOTHING
        """
        async with self.bot.db.acquire() as conn:
            await conn.execute(sql, guild_id, channel_id)

        await self.get_profile_channel_ids(guild_id, refresh=True)

    async def remove_profile_channel(self, guild_id: int, channel_id: int) -> tuple[bool, str]:
        settings = await self.get_settings(guild_id, refresh=True)
        if settings is None:
            return False, "先に /vcプロフ設定 を実行してください。"

        primary_channel_id = settings["prof_tc_id"]
        all_channels = await self.get_profile_channel_ids(guild_id, refresh=True)
        extra_channels = [cid for cid in all_channels if cid != primary_channel_id]

        async with self.bot.db.acquire() as conn:
            if channel_id == primary_channel_id:
                if not extra_channels:
                    return False, "最後のプロフィールチャンネルは削除できません。先に別チャンネルを追加してください。"

                promoted = extra_channels[0]

                await conn.execute("""
                    UPDATE guild_profile_settings
                    SET prof_tc_id = $2,
                        updated_at = NOW()
                    WHERE guild_id = $1
                """, guild_id, promoted)

                await conn.execute("""
                    DELETE FROM guild_profile_channels
                    WHERE guild_id = $1 AND channel_id = $2
                """, guild_id, promoted)

            await conn.execute("""
                DELETE FROM guild_profile_channels
                WHERE guild_id = $1 AND channel_id = $2
            """, guild_id, channel_id)

        await self.get_settings(guild_id, refresh=True)
        await self.get_profile_channel_ids(guild_id, refresh=True)
        return True, "プロフィールチャンネルを削除しました。"

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
            source_created_at,
            updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET
            profile_channel_id = EXCLUDED.profile_channel_id,
            profile_message_id = EXCLUDED.profile_message_id,
            profile_content = EXCLUDED.profile_content,
            profile_jump_url = EXCLUDED.profile_jump_url,
            source_created_at = EXCLUDED.source_created_at,
            updated_at = NOW()
        WHERE member_profiles.source_created_at IS NULL
           OR EXCLUDED.source_created_at >= member_profiles.source_created_at
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
                message.created_at,
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

    async def get_profile(self, guild_id: int, user_id: int) -> Optional[dict]:
        sql = """
        SELECT guild_id, user_id, profile_channel_id, profile_message_id,
               profile_content, profile_jump_url, source_created_at, updated_at
        FROM member_profiles
        WHERE guild_id = $1
          AND user_id = $2
        """
        async with self.bot.db.acquire() as conn:
            row = await conn.fetchrow(sql, guild_id, user_id)

        return dict(row) if row else None

    async def delete_profile_by_message_id(self, guild_id: int, message_id: int) -> Optional[int]:
        sql_select = """
        SELECT user_id
        FROM member_profiles
        WHERE guild_id = $1
          AND profile_message_id = $2
        """
        sql_delete = """
        DELETE FROM member_profiles
        WHERE guild_id = $1
          AND profile_message_id = $2
        """
        async with self.bot.db.acquire() as conn:
            row = await conn.fetchrow(sql_select, guild_id, message_id)
            if row is None:
                return None
            await conn.execute(sql_delete, guild_id, message_id)
            return row["user_id"]

    async def rebuild_latest_profile_for_user(self, guild: discord.Guild, user_id: int):
        channel_ids = await self.get_profile_channel_ids(guild.id)
        latest_message: Optional[discord.Message] = None

        for channel_id in channel_ids:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                async for msg in channel.history(
                    limit=REBUILD_LIMIT_PER_CHANNEL if REBUILD_LIMIT_PER_CHANNEL > 0 else None
                ):
                    if msg.author.bot:
                        continue
                    if msg.author.id != user_id:
                        continue

                    if latest_message is None or msg.created_at > latest_message.created_at:
                        latest_message = msg
                    break
            except discord.Forbidden:
                log.warning("プロフィール再構築時に履歴取得権限なし guild_id=%s channel_id=%s", guild.id, channel_id)
            except Exception:
                log.exception("プロフィール再構築失敗 guild_id=%s channel_id=%s", guild.id, channel_id)

        if latest_message is not None:
            await self.upsert_profile_from_message(latest_message)

    async def import_existing_messages(
        self,
        guild: discord.Guild,
        limit_per_channel: int,
    ) -> tuple[int, int]:
        channel_ids = await self.get_profile_channel_ids(guild.id)
        processed = 0
        users: set[int] = set()

        for channel_id in channel_ids:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                async for msg in channel.history(
                    limit=None if limit_per_channel == 0 else limit_per_channel,
                    oldest_first=False,
                ):
                    if msg.author.bot:
                        continue

                    await self.upsert_profile_from_message(msg)
                    processed += 1
                    users.add(msg.author.id)
            except discord.Forbidden:
                log.warning("既存取込で履歴取得権限なし guild_id=%s channel_id=%s", guild.id, channel_id)
            except Exception:
                log.exception("既存取込失敗 guild_id=%s channel_id=%s", guild.id, channel_id)

        return processed, len(users)

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

        if not await self.is_profile_channel(message.guild.id, message.channel.id):
            return

        await self.upsert_profile_from_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or after.guild is None:
            return

        settings = await self.get_settings(after.guild.id)
        if not settings or not settings["enabled"]:
            return

        if not await self.is_profile_channel(after.guild.id, after.channel.id):
            return

        await self.update_profile_if_current_message(after)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None:
            return

        settings = await self.get_settings(payload.guild_id)
        if not settings or not settings["enabled"]:
            return

        if not await self.is_profile_channel(payload.guild_id, payload.channel_id):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        user_id = await self.delete_profile_by_message_id(payload.guild_id, payload.message_id)
        if user_id is not None:
            await self.rebuild_latest_profile_for_user(guild, user_id)

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
    @app_commands.command(name="vcプロフ設定", description="VCプロフィール設定を保存します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
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
                f"メインチャンネル: {prof_tc.mention}\n"
                f"男性ロール: {men_role.mention}\n"
                f"女性ロール: {women_role.mention}"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="vcプロフチャンネル追加", description="追加のプロフィールチャンネルを登録します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def vc_profile_add_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.get_settings(interaction.guild.id)
        if settings is None:
            await interaction.response.send_message("先に /vcプロフ設定 を実行してください。", ephemeral=True)
            return

        await self.add_profile_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"{channel.mention} を追加プロフィールチャンネルとして登録しました。",
            ephemeral=True,
        )

    @app_commands.command(name="vcプロフチャンネル削除", description="プロフィールチャンネルを削除します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def vc_profile_remove_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        ok, msg = await self.remove_profile_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="vcプロフチャンネル一覧", description="プロフィールチャンネル一覧を表示します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def vc_profile_channels(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.get_settings(interaction.guild.id, refresh=True)
        if settings is None:
            await interaction.response.send_message("まだ設定されていません。", ephemeral=True)
            return

        channel_ids = await self.get_profile_channel_ids(interaction.guild.id, refresh=True)
        lines = []

        for cid in channel_ids:
            ch = interaction.guild.get_channel(cid)
            label = ch.mention if ch else f"`{cid}`"
            if cid == settings["prof_tc_id"]:
                lines.append(f"メイン: {label}")
            else:
                lines.append(f"追加: {label}")

        embed = discord.Embed(
            title="プロフィールチャンネル一覧",
            description="\n".join(lines) if lines else "なし",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="vcプロフ既存取込", description="既存のプロフィール投稿をDBへ取り込みます")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def vc_profile_import(
        self,
        interaction: discord.Interaction,
        limit_per_channel: app_commands.Range[int, 0, 100000] = IMPORT_DEFAULT_LIMIT,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.get_settings(interaction.guild.id)
        if settings is None:
            await interaction.response.send_message("先に /vcプロフ設定 を実行してください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        processed, users = await self.import_existing_messages(interaction.guild, limit_per_channel)

        await interaction.followup.send(
            (
                "既存プロフィールを取り込みました。\n"
                f"処理メッセージ数: {processed}\n"
                f"対象ユーザー数: {users}\n"
                f"探索件数/チャンネル: {'全件' if limit_per_channel == 0 else limit_per_channel}"
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
        channels = await self.get_profile_channel_ids(interaction.guild.id, refresh=True)

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
            name="メインチャンネル",
            value=prof_ch.mention if prof_ch else f"`{settings['prof_tc_id']}`",
            inline=False,
        )
        embed.add_field(
            name="プロフィールチャンネル数",
            value=str(len(channels)),
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