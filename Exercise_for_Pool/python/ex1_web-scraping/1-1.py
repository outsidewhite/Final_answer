"""楽天ぐるなびの「名古屋駅・居酒屋」検索結果をrequestsで収集する。"""

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup


SEARCH_URL = "https://r.gnavi.co.jp/area/aream4102/izakaya/rs/"
OUTPUT_PATH = Path(__file__).with_name("1-1.csv")
MAX_RECORDS = 50
WAIT_SECONDS = 3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0 Safari/537.36"
)
COLUMNS = [
    "店舗名", "電話番号", "メールアドレス", "都道府県", "市区町村",
    "番地", "建物名", "URL", "SSL",
]


def get_page(session: requests.Session, url: str) -> requests.Response:
    """サーバー負荷を抑えるため、必ず3秒待ってからページを取得する。"""
    last_error: requests.RequestException | None = None
    for attempt in range(3):
        time.sleep(WAIT_SECONDS)
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            # HTTPヘッダーによる文字コード誤判定を避けるためUTF-8を明示する。
            response.encoding = "utf-8"
            return response
        except requests.RequestException as error:
            last_error = error
            print(f"取得を再試行します ({attempt + 1}/3): {url}")
    assert last_error is not None
    raise last_error


def get_restaurant_urls(soup: BeautifulSoup) -> list[str]:
    """検索結果の構造化データから店舗ページURLだけを抽出する。"""
    urls: list[str] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "ItemList":
            continue
        for item in data.get("itemListElement", []):
            url = item.get("url", "")
            restaurant_id = url.rstrip("/").rsplit("/", 1)[-1]
            if re.fullmatch(r"[a-zA-Z0-9_-]+", restaurant_id) and re.search(r"\d", restaurant_id):
                urls.append(url.rstrip("/") + "/")
    # ページによって構造化データがない場合に備え、店舗カードのリンクも確認する。
    for link in soup.select('a[href^="https://r.gnavi.co.jp/"]'):
        url = link.get("href", "")
        restaurant_id = url.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"[a-zA-Z0-9_-]+", restaurant_id) and re.search(r"\d", restaurant_id):
            urls.append(url.rstrip("/") + "/")
    # 2ページ目以降では店舗URLがNext.jsのJSON内だけに入る場合がある。
    for url in re.findall(
        r"https://r\.gnavi\.co\.jp/[a-zA-Z0-9_-]*\d[a-zA-Z0-9_-]*/?",
        str(soup),
    ):
        urls.append(url.rstrip("/") + "/")
    return list(dict.fromkeys(urls))


def split_address(address: str) -> tuple[str, str, str]:
    """住所を正規表現で都道府県、市区町村、番地に分割する。"""
    normalized = re.sub(r"[ 　]", "", address)
    match = re.match(
        r"^(?P<pref>東京都|北海道|(?:京都|大阪)府|.{2,3}県)"
        r"(?P<city>.+?市.+?区|.+?郡.+?[町村]|.+?[市区町村])"
        r"(?P<rest>.*)$",
        normalized,
    )
    if not match:
        return "", "", normalized

    rest = match.group("rest")
    # 最初に現れる数字以降を番地とし、その前の町域は市区町村列に含める。
    number_match = re.match(r"^(?P<town>.*?)(?P<number>\d.*)$", rest)
    if number_match:
        city = match.group("city") + number_match.group("town")
        number = number_match.group("number")
    else:
        city, number = match.group("city") + rest, ""
    return match.group("pref"), city, number


def resolve_official_url(
    session: requests.Session, soup: BeautifulSoup
) -> tuple[str, bool]:
    """公式リンクの最終URLと、証明書検証済みかどうかを返す。"""
    link = soup.select_one(
        'a[title="オフィシャルページ"], a[title="お店のホームページ"]'
    )
    if not link or not link.get("href"):
        return "", False
    target = urljoin(SEARCH_URL, link["href"])
    try:
        response = get_page(session, target)
        return response.url, urlparse(response.url).scheme.lower() == "https"
    except requests.RequestException:
        # 証明書検証を含む接続に失敗した場合、SSLはFalseとする。
        return target, False


def scrape_restaurant(session: requests.Session, url: str) -> dict[str, object]:
    """店舗詳細ページから提出フォーマット1行分の情報を取得する。"""
    soup = BeautifulSoup(get_page(session, url).text, "html.parser")
    body = soup.body
    name = (body.get("data-rstname", "") if body else "").strip()
    if not name:
        heading = soup.select_one("h1")
        name = heading.get_text(" ", strip=True) if heading else ""

    phone = soup.select_one("#info-phone .number")
    region = soup.select_one("p.adr .region")
    building = soup.select_one("p.adr .locality")
    email = soup.select_one('a[href^="mailto:"]')
    prefecture, municipality, street_number = split_address(
        region.get_text(" ", strip=True) if region else ""
    )
    official_url, ssl_enabled = resolve_official_url(session, soup)

    return {
        "店舗名": name,
        "電話番号": phone.get_text(strip=True) if phone else "",
        "メールアドレス": email["href"].removeprefix("mailto:") if email else "",
        "都道府県": prefecture,
        "市区町村": municipality,
        "番地": street_number,
        "建物名": building.get_text(" ", strip=True) if building else "",
        "URL": official_url,
        "SSL": ssl_enabled,
    }


def main() -> None:
    """検索結果を順番に巡回し、50店舗をCSVへ保存する。"""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    restaurant_urls: list[str] = []

    page = 1
    while len(restaurant_urls) < MAX_RECORDS:
        page_url = SEARCH_URL if page == 1 else f"{SEARCH_URL}?p={page}"
        soup = BeautifulSoup(get_page(session, page_url).text, "html.parser")
        found = get_restaurant_urls(soup)
        if not found:
            raise RuntimeError(f"店舗URLを取得できませんでした: {page_url}")
        restaurant_urls.extend(url for url in found if url not in restaurant_urls)
        page += 1

    rows = []
    for index, url in enumerate(restaurant_urls[:MAX_RECORDS], 1):
        print(f"[{index}/{MAX_RECORDS}] {url}")
        rows.append(scrape_restaurant(session, url))

    # Excelで文字化けしないよう、BOM付きUTF-8で出力する。
    pd.DataFrame(rows, columns=COLUMNS).to_csv(
        OUTPUT_PATH, index=False, encoding="utf-8-sig"
    )


if __name__ == "__main__":
    main()
