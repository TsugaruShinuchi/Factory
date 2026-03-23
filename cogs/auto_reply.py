import random
import re
import logging

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# =========================
# 自動返信ルール
# =========================
# 1要素 = 1ルール
# match:
#   exact    -> 完全一致
#   contains -> 部分一致
AUTO_REPLY_RULES = [
    {
        "name": "にわちゃん系",
        "match": "exact",
        "triggers": [
            "せのくるみ",
            "にわちゃん",
            "にわ",
        ],
        "replies": [
            "kawaii！！",
            "美人！！",
            "小さい！！",
        ],
    },
    {
        "name": "さくちゃん系",
        "match": "exact",
        "triggers": [
            "さくちゃん",
        ],
        "replies": [
            "画伯！！",
            "元ヤン！！",
            "バカ！！",
        ],
    },
    {
        "name": "だっち系",
        "match": "exact",
        "triggers": [
            "だっち",
        ],
        "replies": [
            "クズ",
            "ゴミ！！",
            "レジアイス！！",
        ],
    },
]


class AutoReplyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def normalize_text(self, text: str) -> str:
        text = text.strip().lower()
        text = re.sub(r"\s+", " ", text)
        return text

    def get_reply_for_message(self, content: str) -> str | None:
        normalized = self.normalize_text(content)

        for rule in AUTO_REPLY_RULES:
            match_type = rule.get("match", "exact")
            triggers = [self.normalize_text(t) for t in rule.get("triggers", [])]
            replies = rule.get("replies", [])

            if not triggers or not replies:
                continue

            matched = False

            if match_type == "exact":
                matched = normalized in triggers
            elif match_type == "contains":
                matched = any(trigger in normalized for trigger in triggers)

            if matched:
                return random.choice(replies)

        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Botの発言は無視
        if message.author.bot:
            return

        # DMは無視
        if message.guild is None:
            return

        # 本文なしは無視
        if not message.content:
            return

        reply_text = self.get_reply_for_message(message.content)
        if reply_text is None:
            return

        try:
            await message.reply(reply_text, mention_author=False)
        except discord.Forbidden:
            log.warning(
                "自動返信の送信権限がありません guild_id=%s channel_id=%s",
                message.guild.id,
                message.channel.id,
            )
        except Exception:
            log.exception(
                "自動返信中にエラー guild_id=%s channel_id=%s message_id=%s",
                message.guild.id,
                message.channel.id,
                message.id,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoReplyCog(bot))