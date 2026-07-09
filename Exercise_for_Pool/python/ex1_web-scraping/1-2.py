"""楽天ぐるなびの「名古屋駅・居酒屋」検索結果をSeleniumで収集する。"""

import re
import time
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


SEARCH_URL = "https://r.gnavi.co.jp/area/aream4102/izakaya/rs/"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / "1-2.csv"
DRIVER_PATH = BASE_DIR / "chromedriver.exe"
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


def create_driver() -> webdriver.Chrome:
    """同じディレクトリに配置したChromeDriverを使ってブラウザを起動する。"""
    if not DRIVER_PATH.exists():
        raise FileNotFoundError(f"ChromeDriverがありません: {DRIVER_PATH}")
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1200")
    options.add_argument(f"--user-agent={USER_AGENT}")
    return webdriver.Chrome(service=Service(str(DRIVER_PATH)), options=options)


def open_page(driver: webdriver.Chrome, url: str) -> None:
    """サーバー負荷を抑えるため、必ず3秒待ってからページを開く。"""
    time.sleep(WAIT_SECONDS)
    driver.get(url)


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
    number_match = re.match(r"^(?P<town>.*?)(?P<number>\d.*)$", rest)
    if number_match:
        return (
            match.group("pref"),
            match.group("city") + number_match.group("town"),
            number_match.group("number"),
        )
    return match.group("pref"), match.group("city") + rest, ""


def collect_urls(driver: webdriver.Chrome) -> list[str]:
    """ページ下部の「次」ボタンをクリックしながら50店舗のURLを集める。"""
    urls: list[str] = []
    open_page(driver, SEARCH_URL)
    while len(urls) < MAX_RECORDS:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href^="https://r.gnavi.co.jp/"]'))
        )
        links = driver.find_elements(
            By.CSS_SELECTOR, 'a[href^="https://r.gnavi.co.jp/"]'
        )
        for link in links:
            href = link.get_attribute("href") or ""
            restaurant_id = href.rstrip("/").rsplit("/", 1)[-1]
            if re.fullmatch(r"[a-zA-Z0-9_-]+", restaurant_id) and re.search(r"\d", restaurant_id):
                normalized = href.rstrip("/") + "/"
                if normalized not in urls:
                    urls.append(normalized)
        # 2ページ目以降では店舗URLがNext.jsのJSON内だけに入る場合がある。
        for href in re.findall(
            r"https://r\.gnavi\.co\.jp/[a-zA-Z0-9_-]*\d[a-zA-Z0-9_-]*/?",
            driver.page_source,
        ):
            normalized = href.rstrip("/") + "/"
            if normalized not in urls:
                urls.append(normalized)
        if len(urls) >= MAX_RECORDS:
            break

        # 課題指定どおり、ページ下部の「>」に相当する次ページボタンをクリックする。
        next_button = driver.find_element(
            By.XPATH, '//img[contains(@alt, "次（") and contains(@alt, "ページを表示")]/parent::a'
        )
        current_url = driver.current_url
        time.sleep(WAIT_SECONDS)
        driver.execute_script("arguments[0].click();", next_button)
        # Reactの画面遷移では要素が再利用されるため、URLの変化を待つ。
        WebDriverWait(driver, 20).until(lambda d: d.current_url != current_url)
    return urls[:MAX_RECORDS]


def text_or_empty(driver: webdriver.Chrome, selector: str) -> str:
    """要素がない項目は課題指定どおり空文字にする。"""
    try:
        return driver.find_element(By.CSS_SELECTOR, selector).text.strip()
    except NoSuchElementException:
        return ""


def scrape_restaurant(driver: webdriver.Chrome, url: str) -> dict[str, object]:
    """店舗詳細ページから提出フォーマット1行分の情報を取得する。"""
    open_page(driver, url)
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
    )
    body = driver.find_element(By.CSS_SELECTOR, "body")
    name = (body.get_attribute("data-rstname") or "").strip()
    if not name:
        name = text_or_empty(driver, "h1")

    address = text_or_empty(driver, "p.adr .region")
    prefecture, municipality, street_number = split_address(address)
    try:
        email = driver.find_element(By.CSS_SELECTOR, 'a[href^="mailto:"]').get_attribute("href")
        email = email.removeprefix("mailto:")
    except NoSuchElementException:
        email = ""

    official_url = ""
    try:
        official = driver.find_element(
            By.CSS_SELECTOR,
            'a[title="オフィシャルページ"], a[title="お店のホームページ"]',
        )
        original_window = driver.current_window_handle
        # 外部公式サイトへのアクセス前にも3秒待機する。
        time.sleep(WAIT_SECONDS)
        driver.execute_script("arguments[0].click();", official)
        WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > 1)
        driver.switch_to.window([h for h in driver.window_handles if h != original_window][0])
        WebDriverWait(driver, 20).until(lambda d: d.current_url != "about:blank")
        official_url = driver.current_url
        driver.close()
        driver.switch_to.window(original_window)
    except (NoSuchElementException, TimeoutException):
        # 公式サイトが掲載されていない店舗は空欄にする。
        if len(driver.window_handles) > 1:
            driver.close()
            driver.switch_to.window(driver.window_handles[0])

    return {
        "店舗名": name,
        "電話番号": text_or_empty(driver, "#info-phone .number"),
        "メールアドレス": email,
        "都道府県": prefecture,
        "市区町村": municipality,
        "番地": street_number,
        "建物名": text_or_empty(driver, "p.adr .locality"),
        "URL": official_url,
        # Seleniumは証明書エラーを無視しないため、HTTPSで表示できた場合をTrueとする。
        "SSL": urlparse(official_url).scheme.lower() == "https",
    }


def main() -> None:
    """50店舗を収集し、Excel対応のCSVへ保存する。"""
    driver = create_driver()
    try:
        urls = collect_urls(driver)
        rows = []
        for index, url in enumerate(urls, 1):
            print(f"[{index}/{MAX_RECORDS}] {url}")
            rows.append(scrape_restaurant(driver, url))
        # Excelで文字化けしないよう、BOM付きUTF-8で出力する。
        pd.DataFrame(rows, columns=COLUMNS).to_csv(
            OUTPUT_PATH, index=False, encoding="utf-8-sig"
        )
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
