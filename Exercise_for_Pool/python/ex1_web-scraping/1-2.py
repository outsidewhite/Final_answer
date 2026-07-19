"""楽天ぐるなびの「名古屋駅・居酒屋」検索結果をSeleniumで収集する。"""

import json
import re
import time
from collections import Counter
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup, Tag
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
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
EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9._%+\-])"
)
ASSET_FILE_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "avif",
    "svg",
    "css",
    "js",
}
PHONE_PATTERN = re.compile(r"0\d{1,4}-\d{1,4}-\d{3,4}|0\d{9,10}")
CFEMAIL_PATTERN = re.compile(r"^[0-9a-fA-F]{4,}$")
OFFICIAL_SITE_LABEL_KEYWORDS = ("お店のホームページ",)
GORP_OFFICIAL_LABEL_KEYWORDS = ("オフィシャルページ",)
SKIP_LINK_KEYWORDS = ("このページのURL", "スマートフォン", "印刷", "予約", "地図", "クーポン")
GNAVI_INTERMEDIATE_DOMAINS = ("gnavi.co.jp", "gurunavi.com")
GNAVI_OFFICIAL_DOMAINS = ("gorp.jp",)
NON_OFFICIAL_DOMAINS = (
    "line.me",
    "liff.line.me",
    "ebica.jp",
    "booking.ebica.jp",
    "notion.site",
    "instagram.com",
    "facebook.com",
    "x.com",
    "twitter.com",
    "google.com",
    "goo.gl",
    "hotpepper.jp",
    "rakuten.co.jp",
    "rakuten.com",
    "tabelog.com",
    "youtube.com",
    "youtu.be",
)
CAPTCHA_URL_KEYWORDS = (
    "captcha",
    "recaptcha",
    "challenge",
    "botdetect",
    "bot-detect",
    "/cdn-cgi/",
    "/sorry/",
    "security-check",
    "access_denied",
)
EMAIL_PAGE_KEYWORDS = (
    "contact",
    "inquiry",
    "mail",
    "form",
    "about",
    "company",
    "info",
    "toiawase",
    "otoiawase",
    "お問い合わせ",
    "問合せ",
    "お問合わせ",
    "問い合わせ",
    "メール",
    "連絡",
    "会社",
    "店舗",
    "アクセス",
)
MAX_EMAIL_PAGES = 10
DEFAULT_EMAIL_PAGE_PATHS = (
    "/",
    "/contact/",
    "/contact/form/",
    "/inquiry/",
    "/inquiry/form/",
    "/toiawase/",
    "/otoiawase/",
    "/company/",
)
JSON_SCRIPT_TYPES = ("application/ld+json", "application/json")
EXPAND_KEYWORDS = ("もっと見る", "すべて見る", "続きを見る", "詳細", "開く", "表示")


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


def restore_original_window(driver: webdriver.Chrome, original_window: str) -> None:
    """外部サイト用に開いた別タブを閉じ、店舗詳細ページのタブへ戻す。"""
    try:
        handles = list(driver.window_handles)
    except WebDriverException:
        return

    for handle in handles:
        if handle == original_window:
            continue
        try:
            driver.switch_to.window(handle)
            driver.close()
        except WebDriverException:
            continue

    try:
        handles = list(driver.window_handles)
        if original_window in handles:
            driver.switch_to.window(original_window)
        elif handles:
            driver.switch_to.window(handles[0])
    except WebDriverException:
        return


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


def decode_escaped_text(value: str) -> str:
    """HTML/URL/JavaScriptエスケープを通常文字列に戻す。"""
    text = unquote(unescape(value))
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), text)
    text = re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match.group(1), 16)), text)
    return text


def normalize_email_text(value: str) -> str:
    """全角記号や難読化されたat/dot表記をメール検出しやすい形に寄せる。"""
    text = decode_escaped_text(value).replace("＠", "@").replace("．", ".").replace("。", ".")
    text = re.sub(r"\s*(?:\[at\]|\(at\)|\{at\}| at |★|☆)\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(?:\[dot\]|\(dot\)|\{dot\}| dot )\s*", ".", text, flags=re.IGNORECASE)
    # 公式サイト側でよくある日本語の難読化と、記号周辺の空白をメール検出用に寄せる。
    text = re.sub(r"\s*(?:\[アット\]|\(アット\)|（アット）|アットマーク| atmark )\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(?:\[ドット\]|\(ドット\)|（ドット）)\s*", ".", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*@\s*", "@", text)
    text = re.sub(r"(?<=[A-Za-z0-9])\s*\.\s*(?=[A-Za-z0-9])", ".", text)
    return text


def find_email_in_text(value: str) -> str:
    """通常表記と難読化表記の両方からメールアドレスを探す。"""
    text = normalize_email_text(value)
    for match in EMAIL_PATTERN.finditer(text):
        candidate = match.group(0)
        if is_valid_email_candidate(candidate):
            return candidate
    return ""


def is_valid_email_candidate(candidate: str) -> bool:
    """画像ファイル名などを除外し、メールアドレス候補として妥当か判定する。"""
    local_part, domain = candidate.rsplit("@", 1)
    if not local_part or local_part.startswith(".") or local_part.endswith("."):
        return False

    domain_labels = domain.split(".")
    if len(domain_labels) < 2:
        return False
    if domain_labels[-1].lower() in ASSET_FILE_EXTENSIONS:
        return False
    return all(
        label and not label.startswith("-") and not label.endswith("-")
        for label in domain_labels
    )


def decode_cfemail(value: str) -> str:
    """Cloudflareのdata-cfemailで保護されたメールアドレスを復号する。"""
    encoded = value.strip()
    if len(encoded) < 4 or len(encoded) % 2 or not CFEMAIL_PATTERN.fullmatch(encoded):
        return ""
    key = int(encoded[:2], 16)
    chars = [
        chr(int(encoded[index : index + 2], 16) ^ key)
        for index in range(2, len(encoded), 2)
    ]
    return "".join(chars)


def clean_email(value: str) -> str:
    """mailtoや各種エスケープを取り除き、メールアドレス部分だけを返す。"""
    text = normalize_email_text(value).strip()
    if text.lower().startswith("mailto:"):
        text = text[7:]
    text = text.split("?", 1)[0].strip()
    return find_email_in_text(text)


def has_email_related_link(soup: BeautifulSoup) -> bool:
    """メールが別形式や問い合わせ導線として持たれている可能性を確認する。"""
    keyword_pattern = re.compile(
        r"contact|inquiry|form|mail|restmail|toiawase|otoiawase|お問い合わせ|問合せ|メール",
        re.IGNORECASE,
    )
    for link in soup.select("a[href]"):
        label = " ".join(
            filter(
                None,
                [
                    link.get_text(" ", strip=True),
                    link.get("title", ""),
                    link.get("aria-label", ""),
                    link.get("href", ""),
                ],
            )
        )
        if keyword_pattern.search(label):
            return True
    return False


def extract_email_with_checks(
    soup: BeautifulSoup, checked_after_expand: bool = False
) -> tuple[str, tuple[str, ...]]:
    """メール取得経路を分けて確認し、未取得時は確認済み項目を返す。"""
    email = find_email_in_text(soup.get_text(" ", strip=True))
    if email:
        return email, ("ページ上に表示",)

    for link in soup.select('a[href^="mailto:"]'):
        email = clean_email(link.get("href", ""))
        if email:
            return email, ("HTML内のmailto",)

    for protected in soup.select("[data-cfemail]"):
        email = clean_email(decode_cfemail(protected.get("data-cfemail", "")))
        if email:
            return email, ("HTML内のCloudflare難読化",)

    for link in soup.select('a[href*="/cdn-cgi/l/email-protection#"]'):
        encoded = link.get("href", "").rsplit("#", 1)[-1]
        email = clean_email(decode_cfemail(encoded))
        if email:
            return email, ("HTML内のCloudflare難読化",)

    email = find_email_in_text(str(soup))
    if email:
        return email, ("HTML内の埋め込み",)

    checks = [
        "ページ上にメール表示なし",
        "HTML内のメール埋め込みなし",
        "クリック・展開後もメール表示なし" if checked_after_expand else "クリック・展開後は未確認",
        "問い合わせフォーム等の別形式候補あり"
        if has_email_related_link(soup)
        else "別形式データ候補なし",
    ]
    return "", tuple(checks)


def normalize_url(raw_url: str, base_url: str) -> str:
    """相対URLやエスケープ済みURLを、比較しやすい絶対URLに整える。"""
    url = unquote(unescape(raw_url)).strip()
    if not url or url.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""
    return urljoin(base_url, url)


def get_gnavi_data_url(link: Tag, base_url: str) -> str:
    """ぐるなびのdata-o属性に分割保存された外部URLを取り出す。"""
    data_o = link.get("data-o", "")
    if not data_o:
        return ""
    try:
        data = json.loads(data_o)
    except json.JSONDecodeError:
        return ""

    target = str(data.get("a", "")).strip()
    if not target:
        return ""
    if target.startswith(("http://", "https://")):
        return normalize_url(target, base_url)

    scheme = str(data.get("b", "https")).strip().lower() or "https"
    if scheme not in {"http", "https"}:
        scheme = "https"
    return normalize_url(f"{scheme}://{target.lstrip('/')}", base_url)


def extract_urls_from_attribute(value: object, base_url: str) -> list[str]:
    """HTML属性内に埋め込まれたURL文字列を候補として取り出す。"""
    if not isinstance(value, str):
        return []
    text = decode_escaped_text(value).replace("\\/", "/")
    urls = []
    for match in re.findall(r"https?://[^\s\"'<>]+", text):
        url = normalize_url(match.rstrip("),;]"), base_url)
        if url:
            urls.append(url)
    return urls


def link_candidate_urls(link: Tag, base_url: str) -> list[str]:
    """href、data-o、その他属性から公式サイト候補URLを重複なしで集める。"""
    urls: list[str] = []
    for url in (get_gnavi_data_url(link, base_url), normalize_url(link.get("href", ""), base_url)):
        if url:
            urls.append(url)
    for value in link.attrs.values():
        urls.extend(extract_urls_from_attribute(value, base_url))
    return list(dict.fromkeys(urls))


def unwrap_redirect_url(url: str) -> str:
    """ぐるなび等の中継URLに埋め込まれた遷移先URLを取り出す。"""
    parsed = urlparse(url)
    for _, value in parse_qsl(parsed.query):
        decoded = unquote(unescape(value)).strip()
        if decoded.startswith(("http://", "https://")):
            return decoded
    return url


def matches_domain(url: str, domains: tuple[str, ...]) -> bool:
    """URLのホストが指定ドメイン配下かどうかを判定する。"""
    host = urlparse(url).netloc.lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def is_gnavi_intermediate_url(url: str) -> bool:
    """ぐるなび側の中間ページURLかどうかを判定する。"""
    return matches_domain(url, GNAVI_INTERMEDIATE_DOMAINS)


def is_gnavi_official_page_url(url: str) -> bool:
    """ぐるなびのオフィシャルページURLかどうかを判定する。"""
    return matches_domain(url, GNAVI_OFFICIAL_DOMAINS)


def is_non_official_service_url(url: str) -> bool:
    """予約・SNS・地図など、店舗公式サイトとして保存しない外部サービスを判定する。"""
    return matches_domain(url, NON_OFFICIAL_DOMAINS)


def is_captcha_url(url: str) -> bool:
    """CAPTCHAやbot判定ページに飛ばされたURLかどうかを判定する。"""
    parsed = urlparse(url)
    target = f"{parsed.netloc}{parsed.path}?{parsed.query}".lower()
    return any(keyword in target for keyword in CAPTCHA_URL_KEYWORDS)


def is_asset_url(url: str) -> bool:
    """画像・CSS・JSなど、公式サイトではなく素材ファイルへのURLを除外する。"""
    path = urlparse(url).path.lower().rsplit("/", 1)[-1]
    return "." in path and path.rsplit(".", 1)[-1] in ASSET_FILE_EXTENSIONS


def is_valid_official_url(url: str) -> bool:
    """CSVに保存してよい公式URLかどうかを判定する。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return False
    return (
        not is_gnavi_intermediate_url(url)
        and not is_non_official_service_url(url)
        and not is_captcha_url(url)
        and not is_asset_url(url)
    )


def homepage_priority(url: str) -> tuple[int, str]:
    """外部公式サイトを優先し、gorpのオフィシャルページは最後の候補にする。"""
    if is_gnavi_intermediate_url(url):
        return (2, url)
    if is_gnavi_official_page_url(url):
        return (1, url)
    return (0, url)


def is_official_site_label(label: str) -> bool:
    """店舗情報のお店のホームページ欄かどうかを判定する。"""
    return any(keyword in label for keyword in OFFICIAL_SITE_LABEL_KEYWORDS)


def is_gorp_official_label(label: str) -> bool:
    """サービス一覧のオフィシャルページ欄かどうかを判定する。"""
    return any(keyword in label for keyword in GORP_OFFICIAL_LABEL_KEYWORDS)


def link_label(link: Tag) -> str:
    """リンク本文と補助属性をまとめ、ホームページ系リンクの判定に使う。"""
    return " ".join(
        filter(
            None,
            [
                link.get_text(" ", strip=True),
                link.get("title", ""),
                link.get("aria-label", ""),
            ],
        )
    )


def collect_homepage_candidates(
    soup: BeautifulSoup, base_url: str
) -> list[str]:
    """お店のホームページ欄の外部URLと、オフィシャルページ欄のgorp.jpだけを候補にする。"""
    candidates: dict[str, int] = {}

    def add_candidate(url: str, label: str) -> None:
        """お店のホームページ欄の外部URLを最優先し、gorp.jpはオフィシャルページ欄だけを残す。"""
        candidate = unwrap_redirect_url(url)
        if not candidate or is_gnavi_intermediate_url(candidate):
            return
        if is_gnavi_official_page_url(candidate):
            if is_gorp_official_label(label):
                candidates.setdefault(candidate, 1)
            return
        if is_valid_official_url(candidate) and is_official_site_label(label):
            candidates[candidate] = 0

    for link in soup.select("a[href], a[data-o]"):
        label = link_label(link)
        should_skip = (
            any(keyword in label for keyword in SKIP_LINK_KEYWORDS)
            and not is_official_site_label(label)
            and not is_gorp_official_label(label)
        )
        if not should_skip:
            for href in link_candidate_urls(link, base_url):
                add_candidate(href, label)

    for header in soup.find_all(string=re.compile(r"お店のホームページ|オフィシャルページ")):
        if any(keyword in str(header) for keyword in SKIP_LINK_KEYWORDS):
            continue
        parent = header.parent
        if not parent:
            continue
        containers = [parent, parent.find_parent(["tr", "dl", "li", "p", "div"])]
        for container in containers:
            if not container:
                continue
            for link in container.select("a[href], a[data-o]"):
                for href in link_candidate_urls(link, base_url):
                    add_candidate(href, f"{header} {link_label(link)}")

    return sorted(
        candidates,
        key=lambda url: (homepage_priority(url)[0], candidates[url], homepage_priority(url)[1]),
    )


def same_site_url(url: str, base_url: str) -> bool:
    """メール探索を実店舗サイト内に限定するため、同一ホストか判定する。"""
    return urlparse(url).netloc.lower() == urlparse(base_url).netloc.lower()


def collect_default_email_page_urls(base_url: str) -> list[str]:
    """公式URLのパスが無効でも、同一ドメインの定番問い合わせページを確認する。"""
    parsed = urlparse(base_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    urls = [urljoin(origin, path) for path in DEFAULT_EMAIL_PAGE_PATHS]
    return [url for url in urls if is_valid_official_url(url)]


def collect_email_page_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    """実店舗サイト内の問い合わせ系ページをメール探索候補にする。"""
    urls = [base_url]
    for link in soup.select("a[href]"):
        label = link_label(link)
        href = normalize_url(link.get("href", ""), base_url)
        if not href or not same_site_url(href, base_url) or not is_valid_official_url(href):
            continue
        target_text = f"{label} {href}".lower()
        if any(keyword.lower() in target_text for keyword in EMAIL_PAGE_KEYWORDS):
            urls.append(href)
    urls.extend(collect_default_email_page_urls(base_url))
    return list(dict.fromkeys(urls))[:MAX_EMAIL_PAGES]


def extract_email_from_official_site_with_checks(
    driver: webdriver.Chrome, official_url: str
) -> tuple[str, tuple[str, ...]]:
    """公式サイトと問い合わせ候補ページでメール有無を確認する。"""
    if not is_valid_official_url(official_url):
        return "", ("公式サイトURLなし",)

    email_page_urls = [official_url, *collect_default_email_page_urls(official_url)]
    checked_urls: set[str] = set()
    all_checks: list[str] = []
    while email_page_urls and len(checked_urls) < MAX_EMAIL_PAGES:
        email_page_url = email_page_urls.pop(0)
        if email_page_url in checked_urls:
            continue
        checked_urls.add(email_page_url)
        try:
            open_page(driver, email_page_url)
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
            )
        except (TimeoutException, WebDriverException):
            all_checks.append("問い合わせ候補ページ取得失敗")
            continue
        try:
            current_url = driver.current_url
            page_source = driver.page_source
        except WebDriverException:
            all_checks.append("問い合わせ候補ページ取得失敗")
            continue
        if is_gnavi_official_page_url(official_url) and is_captcha_url(current_url):
            # gorp.jp確認時にCAPTCHAへ転送された場合は、原因を分けてログに残す。
            return "", ("CAPTCHAによりgorp.jpの取得失敗",)
        if not is_valid_official_url(current_url):
            all_checks.append("公式サイトが取得対象外")
            continue

        soup = BeautifulSoup(page_source, "html.parser")
        try:
            email, page_checks = extract_email_with_checks(
                soup, checked_after_expand=False
            )
        except WebDriverException:
            all_checks.append("問い合わせ候補ページ取得失敗")
            continue
        if email:
            return email, page_checks
        all_checks.extend(page_checks)
        email_page_urls.extend(collect_email_page_urls(soup, current_url)[1:])
    return "", tuple(dict.fromkeys(all_checks))


def extract_email_from_official_sites_with_checks(
    driver: webdriver.Chrome, official_urls: list[str]
) -> tuple[str, tuple[str, ...]]:
    """公式サイト欄とgorp.jpの両方を順番に確認し、最初に見つかったメールを返す。"""
    all_checks: list[str] = []
    for official_url in dict.fromkeys(official_urls):
        email, checks = extract_email_from_official_site_with_checks(
            driver, official_url
        )
        if email:
            return email, checks
        all_checks.extend(checks)
    return "", tuple(dict.fromkeys(all_checks)) or ("公式サイトURLなし",)


def iter_json_objects(value):
    """HTML内のJSONを再帰的にたどり、辞書データだけを順番に返す。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def iter_embedded_json_objects(soup: BeautifulSoup):
    """構造化データやNext.jsのJSONから、店舗情報候補を探せる形で取り出す。"""
    for script in soup.select("script"):
        script_type = (script.get("type") or "").lower().split(";", 1)[0].strip()
        text = (script.string or script.get_text() or "").strip()
        if not text:
            continue
        if script_type not in JSON_SCRIPT_TYPES and not text.startswith(("{", "[")):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        yield from iter_json_objects(data)


def extract_embedded_address(soup: BeautifulSoup) -> str:
    """画面上の住所が空欄の場合に、JSON-LDなど別形式の住所を確認する。"""
    for data in iter_embedded_json_objects(soup):
        address = data.get("address")
        if isinstance(address, dict):
            parts = [
                address.get("addressRegion", ""),
                address.get("addressLocality", ""),
                address.get("streetAddress", ""),
            ]
            full_address = "".join(str(part).strip() for part in parts if part)
            if full_address:
                return full_address
        elif isinstance(address, str) and address.strip():
            return address.strip()
    return ""


def extract_embedded_value(soup: BeautifulSoup, field: str) -> str:
    """画面表示で取れない値をHTML属性・構造化データ・本文全体から補完する。"""
    if field == "店舗名":
        meta = soup.select_one('meta[property="og:title"], meta[name="title"]')
        if meta and meta.get("content"):
            return meta["content"].split("（", 1)[0].strip()

    for data in iter_embedded_json_objects(soup):
        if field == "店舗名" and data.get("name"):
            return str(data["name"]).strip()
        if field == "電話番号" and data.get("telephone"):
            return str(data["telephone"]).strip()

    if field == "電話番号":
        match = PHONE_PATTERN.search(soup.get_text(" ", strip=True))
        if match:
            return match.group(0)
    return ""


def fill_blank_fields_from_embedded_data(
    row: dict[str, object], soup: BeautifulSoup
) -> None:
    """空欄項目について、表示以外のHTML埋め込み情報を確認して補完する。"""
    for field in ("店舗名", "電話番号"):
        if str(row.get(field, "")).strip():
            continue
        value = extract_embedded_value(soup, field)
        if value:
            row[field] = value

    if row.get("URL") and not row.get("SSL"):
        row["SSL"] = urlparse(str(row["URL"])).scheme.lower() == "https"

    address_fields = ("都道府県", "市区町村", "番地")
    if any(not str(row.get(field, "")).strip() for field in address_fields):
        embedded_address = extract_embedded_address(soup)
        if embedded_address:
            prefecture, municipality, street_number = split_address(embedded_address)
            row["都道府県"] = row["都道府県"] or prefecture
            row["市区町村"] = row["市区町村"] or municipality
            row["番地"] = row["番地"] or street_number


def can_click_without_navigation(element, current_url: str) -> bool:
    """展開操作だけを対象にするため、別ページへ移動するリンクを除外する。"""
    if element is None:
        return True
    href = element.get_attribute("href") or ""
    if not href or href.startswith(("javascript:", "#")):
        return True
    current_base = current_url.split("#", 1)[0].rstrip("/")
    href_base = href.split("#", 1)[0].rstrip("/")
    return href_base == current_base


def expand_hidden_sections(driver: webdriver.Chrome) -> None:
    """クリック後に表示される情報がないか、同一ページ内の展開ボタンだけ確認する。"""
    current_url = driver.current_url
    xpath = (
        "//*[self::button or self::a]"
        "[contains(@aria-expanded, 'false') or "
        "contains(., 'もっと見る') or contains(., 'すべて見る') or "
        "contains(., '続きを見る') or contains(., '詳細') or contains(., '表示')]"
    )
    for element in driver.find_elements(By.XPATH, xpath)[:8]:
        try:
            label = " ".join(
                filter(
                    None,
                    [
                        element.text,
                        element.get_attribute("title"),
                        element.get_attribute("aria-label"),
                    ],
                )
            )
            if label and not any(keyword in label for keyword in EXPAND_KEYWORDS):
                continue
            if not can_click_without_navigation(element, current_url):
                continue
            driver.execute_script("arguments[0].click();", element)
            time.sleep(1)
            if driver.current_url.split("#", 1)[0].rstrip("/") != current_url.split("#", 1)[0].rstrip("/"):
                driver.back()
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
                )
        except WebDriverException:
            continue


def blank_columns(row: dict[str, object]) -> list[str]:
    """SSLのFalseは空欄ではないため、文字列項目だけを空欄判定する。"""
    return [
        column
        for column in COLUMNS
        if column != "SSL" and not str(row.get(column, "")).strip()
    ]


def log_blank_fields(row: dict[str, object]) -> tuple[str, ...]:
    """どうしても空欄だった項目を、指定された文言でログ出力する。"""
    blanks = tuple(blank_columns(row))
    store_name = str(row.get("店舗名") or "店舗名不明")
    for column in blanks:
        if column == "メールアドレス":
            # メール未取得は店舗名を出さず、終了時の件数集計だけにする。
            continue
        print(f"{store_name}の{column}に空欄がありました")
    return blanks


def log_blank_summary(patterns: Counter[tuple[str, ...]]) -> None:
    """終了時に、空欄だった項目ごとの店舗数を集計して出力する。"""
    labels = {"URL": "公式サイト未取得"}
    totals: Counter[str] = Counter()
    for columns, count in patterns.items():
        for column in columns:
            if column == "メールアドレス":
                continue
            totals[labels.get(column, f"{column}未取得")] += count

    for label, count in totals.items():
        print(f"{label}:{count}店舗")


def log_email_summary(patterns: Counter[tuple[str, ...]]) -> None:
    """終了時に、メールアドレスを取得できなかった店舗数だけを出力する。"""
    total = sum(count for checks, count in patterns.items() if checks)
    if total:
        print(f"メアド未取得:{total}店舗")


def open_homepage_candidate(driver: webdriver.Chrome, target: str) -> str:
    """候補URLを別タブで開き、ブラウザが到達したURLを返す。"""
    original_window = driver.current_window_handle
    original_handles = set(driver.window_handles)
    try:
        time.sleep(WAIT_SECONDS)
        driver.execute_script("window.open(arguments[0], '_blank');", target)
        WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > len(original_handles))
        new_window = [handle for handle in driver.window_handles if handle not in original_handles][0]
        driver.switch_to.window(new_window)
        WebDriverWait(driver, 20).until(lambda d: d.current_url != "about:blank")
        final_url = unwrap_redirect_url(driver.current_url)
        return final_url if is_valid_official_url(final_url) else ""
    finally:
        restore_original_window(driver, original_window)


def resolve_official_url(
    driver: webdriver.Chrome, soup: BeautifulSoup, page_url: str
) -> tuple[str, bool, list[str]]:
    """保存用URLを選び、メール探索用には外部公式URLとgorp.jpの両方を返す。"""
    candidates = collect_homepage_candidates(soup, page_url)
    if not candidates:
        return "", False, []

    original_window = driver.current_window_handle
    gorp_fallback = ""
    unreachable_official_fallback = ""
    saved_url = ""
    ssl_enabled = False
    email_search_urls: list[str] = []
    for candidate in candidates:
        candidate = unwrap_redirect_url(candidate)
        if is_gnavi_intermediate_url(candidate):
            # 候補収集後に中継URLが混入しても、アクセス対象にはしない。
            continue
        if is_gnavi_official_page_url(candidate):
            # gorp.jpは保存URLの予備にしつつ、メール探索対象にも残す。
            gorp_fallback = gorp_fallback or candidate
            email_search_urls.append(candidate)
            continue

        try:
            final_url = open_homepage_candidate(driver, candidate)
        except (TimeoutException, WebDriverException):
            # 接続確認できない外部公式URLは、gorp.jpより優先するため一旦保持する。
            restore_original_window(driver, original_window)
            if is_valid_official_url(candidate):
                unreachable_official_fallback = unreachable_official_fallback or candidate
            continue

        if is_valid_official_url(final_url):
            email_search_urls.append(final_url)
            if is_gnavi_official_page_url(final_url):
                gorp_fallback = gorp_fallback or final_url
                continue
            if not saved_url:
                # 外部の公式サイトは保存URLとしてgorp.jpより優先する。
                saved_url = final_url
                ssl_enabled = urlparse(final_url).scheme.lower() == "https"

    if saved_url:
        return saved_url, ssl_enabled, list(dict.fromkeys(email_search_urls))
    if unreachable_official_fallback:
        ssl_enabled = urlparse(unreachable_official_fallback).scheme.lower() == "https"
        return (
            unreachable_official_fallback,
            ssl_enabled,
            list(dict.fromkeys(email_search_urls)),
        )
    if gorp_fallback:
        return (
            gorp_fallback,
            urlparse(gorp_fallback).scheme.lower() == "https",
            list(dict.fromkeys(email_search_urls)),
        )
    return "", False, list(dict.fromkeys(email_search_urls))


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
        return normalize_csv_text(driver.find_element(By.CSS_SELECTOR, selector).text)
    except NoSuchElementException:
        return ""


def normalize_csv_text(value: object) -> object:
    """CSVの1セル内に改行や連続空白が残らないように整える。"""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


def normalize_output_row(row: dict[str, object]) -> dict[str, object]:
    """出力対象列の文字列をCSV向けに正規化する。"""
    for column in COLUMNS:
        row[column] = normalize_csv_text(row.get(column, ""))
    return row


def scrape_restaurant(driver: webdriver.Chrome, url: str) -> dict[str, object]:
    """店舗詳細ページから提出フォーマット1行分の情報を取得する。"""
    open_page(driver, url)
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
    )
    expand_hidden_sections(driver)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    body = driver.find_element(By.CSS_SELECTOR, "body")
    name = normalize_csv_text(body.get_attribute("data-rstname") or "")
    if not name:
        name = text_or_empty(driver, "h1")

    address = text_or_empty(driver, "p.adr .region")
    prefecture, municipality, street_number = split_address(address)
    phone = text_or_empty(driver, "#info-phone .number")
    building = text_or_empty(driver, "p.adr .locality")
    official_url, ssl_enabled, email_search_urls = resolve_official_url(driver, soup, url)
    official_email, official_email_checks = extract_email_from_official_sites_with_checks(
        driver, email_search_urls
    )

    row = {
        "店舗名": name,
        "電話番号": phone,
        "メールアドレス": official_email,
        "都道府県": prefecture,
        "市区町村": municipality,
        "番地": street_number,
        "建物名": building,
        "URL": official_url,
        # Seleniumは証明書エラーを無視しないため、HTTPSで表示できた場合をTrueとする。
        "SSL": ssl_enabled,
    }
    fill_blank_fields_from_embedded_data(row, soup)
    normalize_output_row(row)
    if row["メールアドレス"]:
        row["__email_checks"] = ()
    else:
        row["__email_checks"] = official_email_checks
    return row


def main() -> None:
    """50店舗を収集し、Excel対応のCSVへ保存する。"""
    driver = create_driver()
    try:
        urls = collect_urls(driver)
        rows = []
        blank_patterns: Counter[tuple[str, ...]] = Counter()
        email_patterns: Counter[tuple[str, ...]] = Counter()
        for index, url in enumerate(urls, 1):
            print(f"[{index}/{MAX_RECORDS}] {url}")
            row = scrape_restaurant(driver, url)
            blank_patterns[log_blank_fields(row)] += 1
            email_patterns[tuple(row.get("__email_checks", ()))] += 1
            rows.append(row)
        log_email_summary(email_patterns)
        log_blank_summary(blank_patterns)
        # Excelで文字化けしないよう、BOM付きUTF-8で出力する。
        pd.DataFrame(rows, columns=COLUMNS).to_csv(
            OUTPUT_PATH, index=False, encoding="utf-8-sig"
        )
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
