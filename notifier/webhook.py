"""
Discord Webhook 通知モジュール

開示情報をリッチEmbed形式でDiscordに送信する。
カテゴリ別に色分けし、証券コード・会社名・開示タイトル・リンクを含む。
"""

import logging
import time
from typing import Optional

import requests

from fetchers import Disclosure

logger = logging.getLogger(__name__)

# Discord Webhook のレート制限対応
RATE_LIMIT_WAIT = 1.0  # 送信間隔（秒）
MAX_EMBEDS_PER_MESSAGE = 10  # 1メッセージあたりの最大Embed数


class DiscordNotifier:
    """Discord Webhook を使った通知送信"""

    def __init__(self, webhook_url: str, username: str = "適時開示Bot",
                 category_colors: Optional[dict] = None):
        self.webhook_url = webhook_url
        self.username = username
        self.category_colors = category_colors or {}
        self.session = requests.Session()

    def _get_color(self, disclosure: Disclosure) -> int:
        """開示内容に応じた Embed カラーを決定"""
        title = disclosure.title + disclosure.category
        for keyword, color in self.category_colors.items():
            if keyword in title:
                return color
        return self.category_colors.get("default", 0x95a5a6)

    def _get_emoji(self, disclosure: Disclosure) -> str:
        """開示内容に応じた絵文字を返す"""
        title = disclosure.title
        if "決算短信" in title:
            return "\U0001f4ca"  # 棒グラフ
        if "業績予想" in title or "業績修正" in title:
            return "\U0001f4c8" if "上方" in title else "\U0001f4c9"  # 上昇/下降グラフ
        if "配当" in title:
            return "\U0001f4b0"  # お金
        if "自己株式" in title or "自社株" in title:
            return "\U0001f3e6"  # 銀行
        if "MBO" in title or "TOB" in title or "公開買付" in title:
            return "\U0001f4a5"  # 爆発
        if "株式分割" in title:
            return "\u2702\ufe0f"  # ハサミ
        if "上場廃止" in title or "民事再生" in title:
            return "\u26a0\ufe0f"  # 警告
        if "合併" in title or "株式交換" in title or "株式移転" in title:
            return "\U0001f91d"  # 握手
        if "代表取締役" in title:
            return "\U0001f464"  # 人物
        if "特別損失" in title:
            return "\U0001f534"  # 赤丸
        if "特別利益" in title:
            return "\U0001f7e2"  # 緑丸
        return "\U0001f4e2"  # メガホン

    def _build_embed(self, disclosure: Disclosure) -> dict:
        """Disclosure から Discord Embed を構築"""
        emoji = self._get_emoji(disclosure)
        color = self._get_color(disclosure)

        # タイトル（証券コード + 会社名）
        header = f"{emoji} [{disclosure.code}] {disclosure.company_name}"
        if disclosure.exchange:
            header += f" ({disclosure.exchange})"

        # 説明文
        description = disclosure.title
        if disclosure.url:
            description = f"[{disclosure.title}]({disclosure.url})"

        # フィールド
        fields = []
        if disclosure.category:
            fields.append({
                "name": "カテゴリ",
                "value": disclosure.category,
                "inline": True,
            })
        fields.append({
            "name": "ソース",
            "value": disclosure.source.upper(),
            "inline": True,
        })

        embed = {
            "title": header[:256],  # Discord制限
            "description": description[:4096],
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"開示日時: {disclosure.disclosed_at}",
            },
        }

        return embed

    def send(self, disclosures: list[Disclosure]) -> bool:
        """開示情報リストを Discord に送信"""
        if not disclosures:
            return True

        if not self.webhook_url or self.webhook_url == "YOUR_DISCORD_WEBHOOK_URL_HERE":
            logger.error("Discord Webhook URL が設定されていません")
            return False

        # Embed を構築してバッチ送信
        embeds = [self._build_embed(d) for d in disclosures]

        success = True
        for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
            batch = embeds[i:i + MAX_EMBEDS_PER_MESSAGE]
            payload = {
                "username": self.username,
                "embeds": batch,
            }

            try:
                resp = self.session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=15,
                )

                if resp.status_code == 429:
                    # レート制限
                    retry_after = resp.json().get("retry_after", 5)
                    logger.warning("Discord レート制限: %s秒待機", retry_after)
                    time.sleep(retry_after)
                    resp = self.session.post(
                        self.webhook_url, json=payload, timeout=15,
                    )

                resp.raise_for_status()
                logger.info("Discord 送信成功: %d 件", len(batch))

            except requests.RequestException as e:
                logger.error("Discord 送信エラー: %s", e)
                success = False

            # レート制限対策で間隔を空ける
            if i + MAX_EMBEDS_PER_MESSAGE < len(embeds):
                time.sleep(RATE_LIMIT_WAIT)

        return success

    def send_summary(self, total: int, filtered: int, new: int) -> bool:
        """巡回サマリーを送信（通知が0件のときは送信しない）"""
        if new == 0:
            return True

        payload = {
            "username": self.username,
            "content": (
                f"**巡回完了** | "
                f"取得: {total}件 | "
                f"フィルタ通過: {filtered}件 | "
                f"新規通知: {new}件"
            ),
        }

        try:
            resp = self.session.post(self.webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error("サマリー送信エラー: %s", e)
            return False

    def send_error(self, message: str) -> bool:
        """エラー通知を送信"""
        payload = {
            "username": self.username,
            "embeds": [{
                "title": "\u26a0\ufe0f システムエラー",
                "description": message,
                "color": 0xe74c3c,
            }],
        }

        try:
            resp = self.session.post(self.webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
            return True
        except requests.RequestException:
            return False
