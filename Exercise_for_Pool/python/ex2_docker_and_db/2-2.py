"""楽天ぐるなびの店舗情報をスクレイピングし、MySQLのex2_2テーブルへ保存する。"""

from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import quote_plus, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag
from sqlalchemy import Boolean, String, Text, create_engine, text
from sqlalchemy.engine import Engine


SEARCH_URL = "https://r.gnavi.co.jp/area/aream4102/izakaya/rs/"
MAX_RECORDS = 50
WAIT_SECONDS = 3
TABLE_NAME = "ex2_2"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0 Safari/537.36"
)
COLUMNS = [
    "店舗名",
    "電話番号",
    "メールアドレス",
    "都道府県",
    "市区町村",
    "番地",
    "建物名",
    "URL",
    "SSL",
]


def get_page(session: requests.Session, url: str) -> requests.Response:
    """サーバー負荷を抑えるため、必ず3秒待ってからページを取得する。"""
    last_error: requests.RequestException | None = None
    for attempt in range(3):
        time.sleep(WAIT_SECONDS)
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            # HTTPヘッダーの文字コード誤判定を避けるため、UTF-8を明示する。
            response.encoding = "utf-8"
            return response
        except requests.RequestException as error:
            last_error = error
            print(f"取得を再試行します ({attempt + 1}/3): {url}")
    assert last_error is not None
    raise last_error


def get_restaurant_urls(soup: BeautifulSoup) -> list[str]:
    """検索結果ページから店舗詳細ページのURLを重複なしで抽出する。"""
    urls: list[str] = []

    # 構造化データがあるページでは、ItemListから店舗URLを取得する。
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

    # 構造化データがない場合に備えて、通常リンクからも店舗URLを拾う。
    for link in soup.select('a[href^="https://r.gnavi.co.jp/"]'):
        url = link.get("href", "")
        restaurant_id = url.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"[a-zA-Z0-9_-]+", restaurant_id) and re.search(r"\d", restaurant_id):
            urls.append(url.rstrip("/") + "/")

    # Next.jsのJSON内だけに店舗URLが入るケースを補完する。
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
    # 最初に現れる数字以降を番地とし、その前の町域は市区町村列へ含める。
    number_match = re.match(r"^(?P<town>.*?)(?P<number>\d.*)$", rest)
    if number_match:
        city = match.group("city") + number_match.group("town")
        number = number_match.group("number")
    else:
        city, number = match.group("city") + rest, ""
    return match.group("pref"), city, number


def resolve_official_url(session: requests.Session, soup: BeautifulSoup) -> tuple[str, bool]:
    """公式サイトの最終URLと、HTTPSで証明書検証に成功したかを返す。"""
    link = soup.select_one(
        'a[title="オフィシャルページ"], a[title="お店のホームページ"]'
    )
    if not link or not link.get("href"):
        return "", False

    target = urljoin(SEARCH_URL, link["href"])
    try:
        response = get_page(session, target)
        final_url = response.url
        return final_url, urlparse(final_url).scheme.lower() == "https"
    except requests.RequestException:
        # 接続や証明書検証に失敗した場合、SSLはFalseとしてURLだけ残す。
        return target, False


def extract_email(email_link: Tag | None) -> str:
    """mailtoリンクからメールアドレス部分だけを取り出す。"""
    if not email_link:
        return ""
    href = email_link.get("href", "")
    return href[7:] if href.startswith("mailto:") else href


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
        "メールアドレス": extract_email(email),
        "都道府県": prefecture,
        "市区町村": municipality,
        "番地": street_number,
        "建物名": building.get_text(" ", strip=True) if building else "",
        "URL": official_url,
        "SSL": ssl_enabled,
    }


def collect_rows() -> list[dict[str, object]]:
    """検索結果を順番に巡回し、50店舗分の行データを作成する。"""
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
    return rows


def create_mysql_engine() -> Engine:
    """環境変数からMySQL接続情報を読み取り、SQLAlchemyエンジンを作成する。"""
    host = os.getenv("DB_HOST", "mysql")
    port = os.getenv("DB_PORT", "3306")
    user = os.getenv("DB_USER", "ex2_user")
    password = quote_plus(os.getenv("DB_PASSWORD", "ex2_password"))
    database = os.getenv("DB_NAME", "ex2")
    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4"
    return create_engine(url, pool_pre_ping=True)


def wait_for_database(engine: Engine) -> None:
    """MySQLコンテナの起動完了を待ってから処理を続行する。"""
    last_error: Exception | None = None
    for _ in range(30):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except Exception as error:
            last_error = error
            time.sleep(2)
    raise RuntimeError("MySQLへ接続できませんでした。") from last_error


def save_to_mysql(rows: list[dict[str, object]], engine: Engine) -> None:
    """取得したデータをMySQLのex2_2テーブルへ保存する。"""
    df = pd.DataFrame(rows, columns=COLUMNS)
    dtype = {
        "店舗名": Text(),
        "電話番号": String(64),
        "メールアドレス": Text(),
        "都道府県": String(16),
        "市区町村": Text(),
        "番地": Text(),
        "建物名": Text(),
        "URL": Text(),
        "SSL": Boolean(),
    }
    df.to_sql(TABLE_NAME, con=engine, if_exists="replace", index=False, dtype=dtype)


def main() -> None:
    """スクレイピングからMySQL保存までを実行する。"""
    engine = create_mysql_engine()
    wait_for_database(engine)
    rows = collect_rows()
    save_to_mysql(rows, engine)
    print(f"{len(rows)}件をMySQLの{TABLE_NAME}テーブルへ保存しました。")


if __name__ == "__main__":
    main()
