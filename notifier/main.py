#!/usr/bin/env python3
"""
適時開示 Discord 通知システム

公開情報（TDnet / EDINET）を定期的に取得し、
キーワード・銘柄コードでフィルタリングした上で
Discord Webhook に通知を送る。

使い方:
  1. config.yaml の webhook_url を設定
  2. pip install -r requirements.txt
  3. python main.py

オプション:
  --once    1回だけ実行して終了（cron等で使う場合）
  --dry-run 通知を送らずにフィルタ結果を表示
  --config  設定ファイルパス（デフォルト: config.yaml）
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import date
from pathlib import Path

import schedule
import yaml

from fetchers import Disclosure, TDnetFetcher, EDINETFetcher
from webhook import DiscordNotifier

# ============================================================
#  設定読み込み
# ============================================================

def load_config(config_path: str = "config.yaml") -> dict:
    """YAML 設定ファイルを読み込む"""
    path = Path(config_path)
    if not path.exists():
        print(f"設定ファイルが見つかりません: {config_path}")
        print("config.yaml を作成してください。")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============================================================
#  重複排除ストア
# ============================================================

class SeenStore:
    """通知済みアイテムを JSON ファイルで管理"""

    def __init__(self, filepath: str = "seen_items.json", max_items: int = 10000):
        self.filepath = Path(filepath)
        self.max_items = max_items
        self.seen: set[str] = set()
        self._load()

    def _load(self):
        if self.filepath.exists():
            try:
                with open(self.filepath, encoding="utf-8") as f:
                    data = json.load(f)
                    self.seen = set(data.get("seen", []))
            except (json.JSONDecodeError, KeyError):
                self.seen = set()

    def _save(self):
        # 古いアイテムを削除（FIFO）
        items = list(self.seen)
        if len(items) > self.max_items:
            items = items[-self.max_items:]
            self.seen = set(items)

        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump({"seen": items}, f, ensure_ascii=False)

    def is_seen(self, uid: str) -> bool:
        return uid in self.seen

    def mark_seen(self, uid: str):
        self.seen.add(uid)

    def mark_many(self, uids: list[str]):
        self.seen.update(uids)
        self._save()


# ============================================================
#  フィルタリング
# ============================================================

def filter_disclosures(
    disclosures: list[Disclosure],
    keywords: list[str],
    watch_codes: list[str],
) -> list[Disclosure]:
    """キーワードと銘柄コードでフィルタリング"""
    filtered = []

    for disc in disclosures:
        # 銘柄コードフィルタ（設定されている場合のみ）
        if watch_codes and disc.code not in watch_codes:
            continue

        # キーワードフィルタ
        title_text = disc.title + disc.category
        if not any(kw in title_text for kw in keywords):
            continue

        filtered.append(disc)

    return filtered


# ============================================================
#  メインジョブ
# ============================================================

class NotificationJob:
    """通知ジョブ: 取得 → フィルタ → 通知"""

    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run

        # データソース
        self.fetchers = []
        sources = config.get("sources", {})
        if sources.get("tdnet", {}).get("enabled", True):
            self.fetchers.append(TDnetFetcher())
        if sources.get("edinet", {}).get("enabled", True):
            self.fetchers.append(EDINETFetcher())

        # Discord 通知
        discord_cfg = config.get("discord", {})
        self.notifier = DiscordNotifier(
            webhook_url=discord_cfg.get("webhook_url", ""),
            username=discord_cfg.get("username", "適時開示Bot"),
            category_colors=config.get("category_colors", {}),
        )

        # 重複排除
        self.seen = SeenStore(config.get("seen_file", "seen_items.json"))

        # フィルタ設定
        self.keywords = config.get("keywords", [])
        self.watch_codes = config.get("watch_codes", [])

    def run(self):
        """1回分のジョブを実行"""
        logger = logging.getLogger(__name__)
        logger.info("=" * 50)
        logger.info("巡回開始: %s", date.today().isoformat())

        # 1. 全ソースからデータ取得
        all_disclosures: list[Disclosure] = []
        for fetcher in self.fetchers:
            try:
                items = fetcher.fetch()
                all_disclosures.extend(items)
            except Exception as e:
                logger.error("取得エラー (%s): %s", type(fetcher).__name__, e)
                if not self.dry_run:
                    self.notifier.send_error(
                        f"データ取得エラー: {type(fetcher).__name__}\n{e}"
                    )

        total = len(all_disclosures)
        logger.info("合計取得: %d 件", total)

        # 2. フィルタリング
        filtered = filter_disclosures(
            all_disclosures, self.keywords, self.watch_codes,
        )
        logger.info("フィルタ通過: %d 件", len(filtered))

        # 3. 重複排除
        new_items = [d for d in filtered if not self.seen.is_seen(d.uid)]
        logger.info("新規: %d 件", len(new_items))

        if not new_items:
            logger.info("新規通知なし")
            return

        # 4. 通知送信
        if self.dry_run:
            logger.info("--- DRY RUN ---")
            for item in new_items:
                logger.info(
                    "  [%s] %s - %s | %s",
                    item.code, item.company_name, item.title, item.source,
                )
        else:
            success = self.notifier.send(new_items)
            if success:
                # 通知成功したアイテムを記録
                self.seen.mark_many([d.uid for d in new_items])
                self.notifier.send_summary(total, len(filtered), len(new_items))
            else:
                logger.error("通知送信に失敗しました")

        logger.info("巡回完了")


# ============================================================
#  エントリーポイント
# ============================================================

def setup_logging(level: str = "INFO"):
    """ロギング設定"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(
        description="適時開示 Discord 通知システム",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="1回だけ実行して終了",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="通知を送らずにフィルタ結果を表示",
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="設定ファイルパス（デフォルト: config.yaml）",
    )
    args = parser.parse_args()

    # 設定読み込み
    config = load_config(args.config)
    setup_logging(config.get("log_level", "INFO"))

    logger = logging.getLogger(__name__)
    logger.info("適時開示 Discord 通知システム 起動")

    # ジョブ作成
    job = NotificationJob(config, dry_run=args.dry_run)

    if args.once:
        # 1回実行モード
        job.run()
        return

    # スケジューラモード
    interval = config.get("interval", 300)
    logger.info("巡回間隔: %d 秒", interval)

    # 起動時に1回実行
    job.run()

    # 定期実行スケジュール
    schedule.every(interval).seconds.do(job.run)

    # シグナルハンドラ（graceful shutdown）
    running = True

    def shutdown(signum, frame):
        nonlocal running
        logger.info("シャットダウン要求を受信 (signal=%s)", signum)
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("スケジューラ開始（Ctrl+C で停止）")

    while running:
        schedule.run_pending()
        time.sleep(1)

    logger.info("システム停止")


if __name__ == "__main__":
    main()
