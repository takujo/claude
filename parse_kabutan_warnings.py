#!/usr/bin/env python3
"""
株探(kabutan.jp) 株価注意報スクレイパー

mode=2_1: 株価上昇率ランキング（上位30銘柄）
mode=2_2: 株価下落率ランキング（下位30銘柄）

各銘柄について以下を取得:
  - 銘柄名
  - 騰落率
  - 理由詳細（ソースと発表日込み）
  - PBR
  - PER
  - 利回り

Usage:
    python3 parse_kabutan_warnings.py
    python3 parse_kabutan_warnings.py --markdown   # Markdown形式で出力
    python3 parse_kabutan_warnings.py --csv         # CSV形式で出力
"""

import argparse
import csv
import io
import sys
import time
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from tabulate import tabulate


BASE_URL = "https://kabutan.jp/warning/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Referer": "https://kabutan.jp/",
}
TARGET_COUNT = 30  # 上位/下位30銘柄


def fetch_page(mode: str, page: int = 1) -> str:
    """指定されたモードとページのHTMLを取得する"""
    params = {
        "mode": mode,
        "market": "0",
        "capitalization": "-1",
        "dispmode": "normal",
        "stc": "",
        "stm": "0",
    }
    if page > 1:
        params["page"] = str(page)

    url = f"{BASE_URL}?{urlencode(params)}"
    print(f"  Fetching: {url}", file=sys.stderr)

    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def parse_warning_table(html: str) -> list[dict]:
    """
    株価注意報テーブルをパースして銘柄データのリストを返す。

    株探の注意報テーブル構造:
    - 各銘柄は2行構成（メイン行 + 詳細行）
    - メイン行: コード, 銘柄名, 市場, 株価, 前日比, 騰落率, PER, PBR, 利回り, etc.
    - 詳細行: 理由詳細テキスト（カタリスト情報）
    """
    soup = BeautifulSoup(html, "lxml")
    stocks = []

    # テーブルを探す
    table = soup.select_one("table.stock_table") or soup.select_one(
        "#stock_kabuka_table"
    )
    if not table:
        # フォールバック: 最初の大きなテーブルを探す
        tables = soup.find_all("table")
        for t in tables:
            rows = t.find_all("tr")
            if len(rows) > 5:
                table = t
                break

    if not table:
        print("  Warning: テーブルが見つかりませんでした", file=sys.stderr)
        return stocks

    rows = table.find_all("tr")

    i = 0
    while i < len(rows):
        row = rows[i]
        cells = row.find_all(["td", "th"])

        # ヘッダー行やデータが少ない行をスキップ
        if len(cells) < 5 or row.find("th"):
            i += 1
            continue

        try:
            stock = _parse_main_row(cells)
        except (IndexError, ValueError):
            i += 1
            continue

        # 次の行が詳細行（理由）かチェック
        if i + 1 < len(rows):
            detail_row = rows[i + 1]
            detail_cells = detail_row.find_all("td")
            # 詳細行は通常セル数が少なく、colspan付きの長いテキストを含む
            if detail_cells and (
                len(detail_cells) <= 3
                or detail_cells[0].get("colspan")
                or (len(detail_cells) < len(cells) and len(detail_cells[0].get_text(strip=True)) > 10)
            ):
                reason_text = detail_cells[-1].get_text(strip=True)
                # ソースと日付情報を抽出
                source_links = detail_cells[-1].find_all("a")
                sources = [a.get_text(strip=True) for a in source_links if a.get_text(strip=True)]
                stock["理由詳細"] = reason_text
                if sources:
                    stock["ソース"] = ", ".join(sources)
                i += 2
                stocks.append(stock)
                continue

        stock["理由詳細"] = ""
        stocks.append(stock)
        i += 1

    return stocks


def _parse_main_row(cells: list) -> dict:
    """メイン行のセルから銘柄データを抽出する"""
    texts = [c.get_text(strip=True) for c in cells]

    # 株探のテーブル列構成を特定する
    # 一般的な列順: コード, 銘柄名, 市場, 株価, 前日比, 前日比(%), PER, PBR, 利回り
    # ただし表示モードにより列が変わることがある

    stock = {
        "コード": "",
        "銘柄名": "",
        "騰落率": "",
        "理由詳細": "",
        "ソース": "",
        "PBR": "",
        "PER": "",
        "利回り": "",
    }

    # コードは数字4桁
    for idx, text in enumerate(texts):
        if len(text) == 4 and text.isdigit():
            stock["コード"] = text
            # コードの次が銘柄名
            if idx + 1 < len(texts):
                stock["銘柄名"] = texts[idx + 1]
            break

    # 騰落率は %を含むか、+/- 付きの数値
    for text in texts:
        if "%" in text or (
            text and text[0] in "+-" and any(c.isdigit() for c in text)
        ):
            # 前日比(絶対値)と前日比(%)の両方がある場合、%付きを優先
            if "%" in text:
                stock["騰落率"] = text
                break
            elif not stock["騰落率"]:
                stock["騰落率"] = text

    # PER, PBR, 利回り - 通常テーブルの後半列
    # 数値列を後ろから探す
    numeric_cols = []
    for idx, text in enumerate(texts):
        cleaned = text.replace(",", "").replace("%", "").replace("倍", "").replace("-", "").replace("―", "")
        if cleaned.replace(".", "").isdigit() and idx > 2:
            numeric_cols.append((idx, text))

    # 典型的に最後の3つの数値列がPER, PBR, 利回り
    if len(numeric_cols) >= 3:
        stock["PER"] = numeric_cols[-3][1]
        stock["PBR"] = numeric_cols[-2][1]
        stock["利回り"] = numeric_cols[-1][1]
    elif len(numeric_cols) == 2:
        stock["PBR"] = numeric_cols[-2][1]
        stock["利回り"] = numeric_cols[-1][1]

    if not stock["コード"]:
        raise ValueError("コードが見つかりません")

    return stock


def collect_stocks(mode: str, target: int = TARGET_COUNT) -> list[dict]:
    """複数ページからtarget件の銘柄を収集する"""
    all_stocks = []
    page = 1
    max_pages = 5

    while len(all_stocks) < target and page <= max_pages:
        try:
            html = fetch_page(mode, page)
            stocks = parse_warning_table(html)
            if not stocks:
                break
            all_stocks.extend(stocks)
            page += 1
            time.sleep(1)  # サーバー負荷軽減
        except requests.RequestException as e:
            print(f"  Error fetching page {page}: {e}", file=sys.stderr)
            break

    return all_stocks[:target]


def format_table(stocks: list[dict], title: str, fmt: str = "grid") -> str:
    """銘柄データをテーブル形式にフォーマットする"""
    headers = ["#", "コード", "銘柄名", "騰落率", "理由詳細", "PER", "PBR", "利回り"]
    table_data = []

    for i, s in enumerate(stocks, 1):
        reason = s.get("理由詳細", "")
        source = s.get("ソース", "")
        if source:
            reason = f"{reason} [出典: {source}]"
        # 理由が長い場合は短縮
        if len(reason) > 80:
            reason = reason[:77] + "..."

        table_data.append([
            i,
            s.get("コード", ""),
            s.get("銘柄名", ""),
            s.get("騰落率", ""),
            reason,
            s.get("PER", ""),
            s.get("PBR", ""),
            s.get("利回り", ""),
        ])

    tablefmt = "pipe" if fmt == "markdown" else "grid"
    result = f"\n{'=' * 60}\n{title}\n{'=' * 60}\n\n"
    result += tabulate(table_data, headers=headers, tablefmt=tablefmt, stralign="left")
    result += f"\n\n(全{len(stocks)}銘柄)\n"
    return result


def output_csv(stocks_up: list[dict], stocks_down: list[dict]) -> str:
    """CSV形式で出力する"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["区分", "順位", "コード", "銘柄名", "騰落率", "理由詳細", "PER", "PBR", "利回り"])

    for i, s in enumerate(stocks_up, 1):
        reason = s.get("理由詳細", "")
        source = s.get("ソース", "")
        if source:
            reason = f"{reason} [出典: {source}]"
        writer.writerow([
            "上昇", i, s.get("コード", ""), s.get("銘柄名", ""),
            s.get("騰落率", ""), reason,
            s.get("PER", ""), s.get("PBR", ""), s.get("利回り", ""),
        ])

    for i, s in enumerate(stocks_down, 1):
        reason = s.get("理由詳細", "")
        source = s.get("ソース", "")
        if source:
            reason = f"{reason} [出典: {source}]"
        writer.writerow([
            "下落", i, s.get("コード", ""), s.get("銘柄名", ""),
            s.get("騰落率", ""), reason,
            s.get("PER", ""), s.get("PBR", ""), s.get("利回り", ""),
        ])

    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(
        description="株探(kabutan.jp) 株価注意報 上昇/下落率ランキング スクレイパー"
    )
    parser.add_argument("--markdown", action="store_true", help="Markdown形式で出力")
    parser.add_argument("--csv", action="store_true", help="CSV形式で出力")
    parser.add_argument(
        "--count", type=int, default=TARGET_COUNT,
        help=f"取得する銘柄数 (デフォルト: {TARGET_COUNT})"
    )
    args = parser.parse_args()

    fmt = "markdown" if args.markdown else "grid"

    print("株探 株価注意報 データ取得中...\n", file=sys.stderr)

    # 上昇率ランキング
    print("[1/2] 株価上昇率ランキング (mode=2_1) を取得中...", file=sys.stderr)
    stocks_up = collect_stocks("2_1", args.count)
    print(f"  → {len(stocks_up)}銘柄取得\n", file=sys.stderr)

    # 下落率ランキング
    print("[2/2] 株価下落率ランキング (mode=2_2) を取得中...", file=sys.stderr)
    stocks_down = collect_stocks("2_2", args.count)
    print(f"  → {len(stocks_down)}銘柄取得\n", file=sys.stderr)

    # 出力
    if args.csv:
        print(output_csv(stocks_up, stocks_down))
    else:
        print(format_table(stocks_up, "📈 株価上昇率ランキング 上位30銘柄 (mode=2_1)", fmt))
        print(format_table(stocks_down, "📉 株価下落率ランキング 下位30銘柄 (mode=2_2)", fmt))

    print("データソース: https://kabutan.jp/warning/", file=sys.stderr)
    print("※ 市場が開いている時間帯はリアルタイムで更新されます", file=sys.stderr)


if __name__ == "__main__":
    main()
