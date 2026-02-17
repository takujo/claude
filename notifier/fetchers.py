"""
適時開示データ取得モジュール

データソース:
  1. TDnet（適時開示情報閲覧サービス）- 決算短信、業績修正、自社株買い等
  2. EDINET API v2 - 有価証券報告書、大量保有報告書等
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TDNET_BASE = "https://www.release.tdnet.info/inbs"
EDINET_API = "https://api.edinet-fsa.go.jp/api/v2/documents.json"

# TDnet のページは Shift_JIS の場合があるため対応
TDNET_ENCODING = "utf-8"


@dataclass
class Disclosure:
    """開示情報を表すデータクラス"""
    source: str              # "tdnet" or "edinet"
    uid: str                 # 一意識別子（重複排除用）
    title: str               # 開示タイトル
    company_name: str        # 会社名
    code: str                # 証券コード
    disclosed_at: str        # 開示日時（文字列）
    url: str                 # 開示資料URL
    category: str = ""       # カテゴリ（決算短信、業績修正等）
    exchange: str = ""       # 取引所
    extra: dict = field(default_factory=dict)


class TDnetFetcher:
    """TDnet 適時開示一覧をスクレイピングして取得"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })

    def fetch(self, target_date: Optional[date] = None) -> list[Disclosure]:
        """指定日の開示一覧を取得する"""
        if target_date is None:
            target_date = date.today()

        date_str = target_date.strftime("%Y%m%d")
        url = f"{TDNET_BASE}/I_list_001_{date_str}.html"
        logger.info("TDnet 取得中: %s", url)

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("TDnet 取得エラー: %s", e)
            return []

        # エンコーディング自動検出
        if resp.encoding and "shift" in resp.encoding.lower():
            resp.encoding = "shift_jis"
        elif resp.apparent_encoding:
            resp.encoding = resp.apparent_encoding

        return self._parse_html(resp.text, target_date)

    def _parse_html(self, html: str, target_date: date) -> list[Disclosure]:
        """TDnet の HTML をパースして開示リストを返す"""
        soup = BeautifulSoup(html, "lxml")
        disclosures = []

        # TDnet のテーブル行を探索
        # 複数のセレクタを試行（ページ構造変更に備える）
        rows = (
            soup.select("table.mainlist tr")
            or soup.select("table tr")
            or soup.find_all("tr")
        )

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            try:
                disc = self._parse_row(cells, target_date)
                if disc:
                    disclosures.append(disc)
            except Exception as e:
                logger.debug("行パースエラー: %s", e)
                continue

        logger.info("TDnet: %d 件取得", len(disclosures))
        return disclosures

    def _parse_row(self, cells, target_date: date) -> Optional[Disclosure]:
        """テーブル行から Disclosure を構築"""
        texts = [c.get_text(strip=True) for c in cells]

        # 証券コードを探す（4桁数字）
        code = ""
        code_idx = -1
        for i, t in enumerate(texts):
            match = re.search(r"(\d{4})", t)
            if match and len(t) <= 6:
                code = match.group(1)
                code_idx = i
                break

        if not code:
            return None

        # 時刻を探す（HH:MM 形式）
        time_str = ""
        for t in texts:
            time_match = re.search(r"(\d{1,2}:\d{2})", t)
            if time_match:
                time_str = time_match.group(1)
                break

        # タイトルとリンクを探す
        title = ""
        doc_url = ""
        for cell in cells:
            link = cell.find("a")
            if link and link.get("href"):
                href = link["href"]
                link_text = link.get_text(strip=True)
                # PDF や開示文書へのリンクを優先
                if link_text and len(link_text) > 5:
                    title = link_text
                    if href.startswith("http"):
                        doc_url = href
                    else:
                        doc_url = f"{TDNET_BASE}/{href}"
                    break

        if not title:
            # リンクがなくても最長テキストをタイトルとして採用
            longest = max(texts, key=len)
            if len(longest) > 5:
                title = longest
            else:
                return None

        # 会社名を推定（コードの次のセル）
        company_name = ""
        if code_idx >= 0 and code_idx + 1 < len(texts):
            company_name = texts[code_idx + 1]
        if not company_name:
            company_name = texts[1] if len(texts) > 1 else ""

        # 取引所
        exchange = ""
        for t in texts:
            if t in ("東", "名", "福", "札", "東証", "名証", "グロース", "プライム", "スタンダード"):
                exchange = t
                break

        disclosed_at = f"{target_date.isoformat()} {time_str}".strip()
        uid = f"tdnet-{code}-{target_date.strftime('%Y%m%d')}-{hash(title) & 0xFFFFFFFF:08x}"

        return Disclosure(
            source="tdnet",
            uid=uid,
            title=title,
            company_name=company_name,
            code=code,
            disclosed_at=disclosed_at,
            url=doc_url,
            exchange=exchange,
        )


class EDINETFetcher:
    """EDINET API v2 から開示書類一覧を取得"""

    # EDINET 書類種別コード
    DOC_TYPES = {
        "120": "有価証券報告書",
        "130": "四半期報告書",
        "140": "半期報告書",
        "150": "臨時報告書",
        "160": "有価証券届出書",
        "170": "発行登録書",
        "350": "大量保有報告書",
        "360": "変更報告書",
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "DisclosureNotifier/1.0",
        })

    def fetch(self, target_date: Optional[date] = None) -> list[Disclosure]:
        """指定日の EDINET 提出書類一覧を取得"""
        if target_date is None:
            target_date = date.today()

        date_str = target_date.strftime("%Y-%m-%d")
        params = {
            "date": date_str,
            "type": 2,  # 2=メタデータ + 書類一覧
        }

        logger.info("EDINET 取得中: date=%s", date_str)

        try:
            resp = self.session.get(EDINET_API, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error("EDINET 取得エラー: %s", e)
            return []
        except ValueError as e:
            logger.error("EDINET JSONパースエラー: %s", e)
            return []

        results = data.get("results", [])
        if not results:
            logger.info("EDINET: 0 件")
            return []

        disclosures = []
        for doc in results:
            disc = self._parse_document(doc, target_date)
            if disc:
                disclosures.append(disc)

        logger.info("EDINET: %d 件取得", len(disclosures))
        return disclosures

    def _parse_document(self, doc: dict, target_date: date) -> Optional[Disclosure]:
        """EDINET API レスポンスの1件を Disclosure に変換"""
        doc_id = doc.get("docID", "")
        doc_type_code = doc.get("docTypeCode", "")
        title = doc.get("docDescription", "")
        company_name = doc.get("filerName", "")
        sec_code = doc.get("secCode", "")
        submit_date = doc.get("submitDateTime", "")

        if not doc_id or not title:
            return None

        # 証券コードは5桁の場合がある（末尾0を除去して4桁に）
        code = ""
        if sec_code:
            code = sec_code.rstrip("0")
            if len(code) < 4:
                code = sec_code[:4]

        doc_type_name = self.DOC_TYPES.get(doc_type_code, "")
        doc_url = f"https://disclosure.edinet-fsa.go.jp/E01EW/BLMainController.jsp?uji.verb=W0EZA226CXP001003Action&uji.bean=ee.bean.W0EZA226.EEW0EZA226CXP001003Bean&PID=currentPage&lgKbn=2&dflg=0&iflg=0&dispKbn=1&cal=1&ESession=&monession=&edession=&session=&sessionkey=&downloadFileName={doc_id}"

        uid = f"edinet-{doc_id}"
        disclosed_at = submit_date or target_date.isoformat()

        return Disclosure(
            source="edinet",
            uid=uid,
            title=title,
            company_name=company_name,
            code=code,
            disclosed_at=disclosed_at,
            url=doc_url,
            category=doc_type_name,
            extra={"doc_type_code": doc_type_code, "doc_id": doc_id},
        )
