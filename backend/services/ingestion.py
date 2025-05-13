import requests
from bs4 import BeautifulSoup, NavigableString, Tag
import tiktoken
import PyPDF2
from docx import Document
import time
import re
import os
import traceback
from urllib.parse import urljoin

# Selenium関連
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

import json
import base64
import platform
from backend.models import db, GoogleCookie  # GoogleCookie: user_id PK, cookie_json_encrypted column
from backend.extensions import get_google_cookies

# --- ロガー設定 ---
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# === シンプルな requests 取得（まずはこちらで試し、失敗したら Selenium） ===
def fetch_text_simple(url: str, user_id: int | None = None, timeout_sec=15) -> str | None:
    """requests だけで取得できるページはここで済ませる"""
    # --- optional Google Sites cookie injection ---------------------------
    cookies_to_add = None
    if "sites.google.com" in url and user_id is not None:
        cookies_to_add = get_google_cookies(user_id)

    # --- Build requests session (always use a Session object) -------------
    sess = requests.Session()

    if cookies_to_add:  # Google Sites private pages
        # requests.Session expects a {name: value} map; ignore other fields
        try:
            cookie_map = {ck["name"]: ck["value"] for ck in cookies_to_add if ck.get("name")}
            sess.cookies.update(cookie_map)
            logger.info("Injected %d Google cookies into session", len(cookie_map))
        except Exception as ck_err:
            logger.warning("Failed to inject cookies: %s", ck_err)
    try:
        r = sess.get(url,
                     timeout=timeout_sec,
                     allow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0 (compatible; AiQlyBot/1.0)"})
        if r.status_code // 100 != 2:
            logger.warning("Non‑2xx status %s for %s", r.status_code, url)
            return None

        if "text" not in r.headers.get("content-type", ""):
            logger.warning("Non‑text content‑type %s for %s", r.headers.get("content-type"), url)
            return None

        html_text = r.text.strip()
        # --- Google "Sign in" page detection → force Selenium fallback ----
        if "Use your Google Account" in html_text and "Sign in" in html_text and "accounts.google.com" in r.url:
            logger.info("Detected Google sign‑in page for %s (likely auth required) → fallback to Selenium", url)
            return None  # treat as failure so caller will try Selenium
        if not html_text:
            logger.warning("Empty body for %s", url)
            return None

        soup = BeautifulSoup(html_text, "html.parser")
        structured = extract_structured_text(soup.body, url)
        cleaned = re.sub(r'\n\s*\n\s*\n+', '\n\n', structured).strip()
        return cleaned if cleaned else None
    except requests.exceptions.RequestException as ex:
        logger.error("requests error for %s: %s", url, ex)
        return None

# === 再帰的な構造化テキスト抽出ヘルパー関数 ===
# (変更なし)
def extract_structured_text(element, base_url):
    text = ''
    if element is None: return ''
    if isinstance(element, NavigableString):
        parent_tag = getattr(element, 'parent', None); parent_name = getattr(parent_tag, 'name', None)
        if parent_name in ['script', 'style', 'noscript']: return ''
        stripped_string = element.string.strip(); return stripped_string if stripped_string else ''
    elif isinstance(element, Tag):
        tag_name = element.name.lower()
        if tag_name in ['script', 'style', 'header', 'footer', 'nav', 'aside', 'form', 'button', 'iframe', 'noscript', 'svg', 'meta', 'link', 'head']: return ''
        prefix, suffix = '\n', '\n'; content = ''
        if tag_name in ['a', 'span', 'strong', 'b', 'em', 'i', 'img', 'code', 'td', 'th']: prefix, suffix = '', ''
        if tag_name == 'li': prefix, suffix = "\n- ", "\n"
        if tag_name == 'br': return "\n"
        # --- タグ種類に応じた処理 ---
        if tag_name.startswith('h') and tag_name[1:].isdigit(): level = int(tag_name[1:]); content = '#' * level + ' ' + element.get_text(strip=True)
        elif tag_name == 'a':
            href = element.get('href'); anchor_text = "".join(extract_structured_text(child, base_url).strip() for child in element.children)
            if not anchor_text: anchor_text = "link"
            if href and not href.strip().lower().startswith('javascript:'): full_url = urljoin(base_url, href.strip()); content = f"[{anchor_text}]({full_url})"
            else: content = anchor_text
        elif tag_name == 'img':
            alt = element.get('alt', '').strip(); src = element.get('src', ''); full_src = urljoin(base_url, src.strip()) if src else ''
            alt_text = f": {alt}" if alt else ""
            if full_src and full_src.lower().startswith('http'): content = f"[画像{alt_text}]({full_src})"
            elif alt: content = f"[画像{alt_text}]"
            else: content = "[画像]"
            prefix, suffix = ' ', ' '
        elif tag_name in ['strong', 'b']: content = f"**{''.join(extract_structured_text(child, base_url).strip() for child in element.children)}**"
        elif tag_name in ['em', 'i']: content = f"*{''.join(extract_structured_text(child, base_url).strip() for child in element.children)}*"
        elif tag_name == 'table':
            table_text = "\n---\n"; rows = element.find_all('tr')
            for row in rows: cells = [extract_structured_text(cell, base_url).strip() for cell in row.find_all(['th', 'td'])]; table_text += " | ".join(cells) + "\n"
            table_text += "---\n"; content = table_text; return content
        elif tag_name in ['td', 'th', 'tr']: pass
        elif tag_name == 'pre': content = element.get_text(); prefix = '\n```\n'; suffix = '\n```\n'
        elif tag_name == 'code': content = f"`{element.get_text(strip=True)}`"; prefix, suffix = '', ''
        elif tag_name == 'blockquote': prefix = '\n> '; content = "".join(extract_structured_text(child, base_url) for child in element.children); suffix = '\n'
        else: content = "".join(extract_structured_text(child, base_url) for child in element.children)
        if content.strip(): text = prefix + content + suffix
        else: text = ''
    text = re.sub(r'[ \t]+', ' ', text); return text

# --- URLからのテキスト抽出 (変更あり) ---
def fetch_text_from_url(url: str, user_id: int | None = None, timeout_sec=45, wait_after_load_sec=5, scroll_attempts=3) -> str | None:
    """Seleniumを使ってURLを開き、構造化テキストを抽出する"""
    # まずは簡易 requests で取れないか試す
    simple = fetch_text_simple(url, user_id, timeout_sec)
    if simple:
        return simple
    # ↓ ここから先は従来の Selenium 流れ ...
    logger.info(f"Fetching URL with Selenium: {url}")
    options = webdriver.ChromeOptions(); # オプション設定...
    options.add_argument('--headless'); options.add_argument('--no-sandbox'); options.add_argument('--disable-dev-shm-usage'); options.add_argument('--disable-gpu'); options.add_argument('--log-level=3'); options.add_argument('--disable-blink-features=AutomationControlled'); options.add_experimental_option('excludeSwitches', ['enable-automation']); options.add_experimental_option('useAutomationExtension', False); options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36')

    logger.info("Setting up WebDriver...")
    # --- Google Sites 専用: Cookie 注入で Private ページを取得 ---
    use_google_cookie = "sites.google.com" in url and user_id is not None
    cookies_to_inject = get_google_cookies(user_id) if use_google_cookie else None

    driver = None
    try:
        # --- choose correct ChromeDriver binary automatically ---
        # Use system-installed chromedriver
        driver_path = "/usr/bin/chromedriver"
        if os.path.exists(driver_path) and os.access(driver_path, os.X_OK):
            logger.info("Using system chromedriver at %s", driver_path)
        else:
            logger.error("System chromedriver not found at %s. Please install chromium-chromedriver in the container.", driver_path)
            return None

        service = ChromeService(driver_path)
        driver = webdriver.Chrome(service=service, options=options)
        # Cookie を注入する場合は一度同ドメインに遷移してから add_cookie()
        if cookies_to_inject:
            try:
                driver.get("https://sites.google.com")  # ドメイン合わせ
                driver.delete_all_cookies()
                for ck in cookies_to_inject:
                    # Selenium add_cookie に入れるキーだけ抽出
                    driver.add_cookie({
                        "name": ck.get("name"),
                        "value": ck.get("value"),
                        "domain": ck.get("domain", ".google.com"),
                        "path": ck.get("path", "/"),
                        "secure": ck.get("secure", True),
                        "httpOnly": ck.get("httpOnly", False)
                    })
                logger.info("Injected %d Google cookies", len(cookies_to_inject))
            except Exception as cke:
                logger.warning("Cookie injection failed: %s", cke)
        driver.set_page_load_timeout(timeout_sec)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        logger.info(f"Navigating to {url}...")
        driver.get(url)
        WebDriverWait(driver, timeout_sec).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        logger.info(f"Waiting {wait_after_load_sec}s...")
        time.sleep(wait_after_load_sec)
        if scroll_attempts > 0: # スクロール処理...
             logger.info(f"Scrolling down up to {scroll_attempts} times...")
             last_height = driver.execute_script("return document.body.scrollHeight")
             for i in range(scroll_attempts):
                 driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                 time.sleep(2)
                 new_height = driver.execute_script("return document.body.scrollHeight")
                 if new_height == last_height:
                     logger.info("Scroll height did not change, breaking scroll loop.")
                     break
                 last_height = new_height
        logger.info("Getting page source...")
        html_content = driver.page_source
        if not html_content:
            logger.warning("Failed to get page source.")
            return None
        logger.info("Parsing HTML & extracting structured text...")
        soup = BeautifulSoup(html_content, 'html.parser')
        main_content_selectors = ['main', 'article', '[role="main"]', '.content', '#content', '.post-content', '#main-content', '.entry-content']
        target_element = None
        logger.info("Searching for main content area...")
        for selector in main_content_selectors:
            try:
                potential_target = soup.select_one(selector)
                if potential_target:
                    target_element = potential_target
                    logger.info(f"Found main content using selector: '{selector}'")
                    break
            except Exception as select_error:
                logger.warning(f"Error occurred while using selector '{selector}': {select_error}")
                continue
        if not target_element:
            logger.warning("Main content not found, using body.")
            target_element = soup.body
        if target_element:
            structured_text = extract_structured_text(target_element, url)
            cleaned_text = re.sub(r'\n\s*\n\s*\n+', '\n\n', structured_text).strip()
            logger.info(f"Extracted structured text length: {len(cleaned_text)} chars.")
            if len(cleaned_text) < 100:
                logger.warning("Extracted text is short.")
            return cleaned_text
        else:
            logger.error("Could not find body tag.")
            return None
    except TimeoutException:
        logger.exception("Error fetching %s", url)
        return None
    except WebDriverException as e:
        logger.exception("Error fetching %s", url)
        return None
    except Exception as e:
        logger.exception("Error fetching %s", url)
        return None
    finally:
        if driver:
            logger.info("Closing WebDriver...")
            driver.quit()

# --- テキストのチャンク分割 (変更なし) ---
def chunk_text(text: str, chunk_size_tokens=500, overlap_tokens=50) -> list[str]:
    # ... (変更なし、whileループ版) ...
    if not text: return []
    try: encoding = tiktoken.get_encoding("cl100k_base")
    except Exception as e: raise ValueError(f"Tiktoken encoding not found: {e}") from e
    tokens = encoding.encode(text); chunks, current_position, total_tokens = [], 0, len(tokens)
    while current_position < total_tokens:
        end_position = min(current_position + chunk_size_tokens, total_tokens)
        chunk_tokens = tokens[current_position:end_position]; chunk_text = encoding.decode(chunk_tokens).strip()
        if chunk_text: chunks.append(chunk_text)
        next_start = current_position + chunk_size_tokens - overlap_tokens; current_position = max(next_start, current_position + 1)
        if end_position == total_tokens: break
    return chunks

# --- ▼ PDFからのテキスト抽出 (インデント修正) ▼ ---
def extract_text_from_pdf(file_path: str) -> str | None:
    """PDFファイルからテキストを抽出する"""
    text_list: list[str] = []
    try:
        with open(file_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            num_pages = len(reader.pages)
            if num_pages == 0:
                 logger.warning(f"PDF file {file_path} has 0 pages.")
                 return ""

            # print(f"Reading PDF: {file_path} ({num_pages} pages)")
            # --- ▼ for ループ (インデントレベル 3) ▼ ---
            for i, page in enumerate(reader.pages):
                # --- ▼ ループ内部の処理 (インデントレベル 4) ▼ ---
                page_text_raw = None # 初期化

                # --- ▼ try-except ブロック (インデントレベル 4) ▼ ---
                # このブロック全体が for ループの中に入るようにインデント
                try:
                    # --- ▼ try内部 (インデントレベル 5) ▼ ---
                    page_text_raw = page.extract_text()
                    if page_text_raw and page_text_raw.strip():
                        # --- ▼ if内部 (インデントレベル 6) ▼ ---
                        text_list.append(page_text_raw.strip())
                    # --- ▲ if終了 ▲ ---
                # --- ▲ try終了 ▲ ---
                except Exception as page_error:
                    # --- ▼ except内部 (インデントレベル 5) ▼ ---
                    logger.error(f"Error extracting text from page {i+1} of {file_path}: {page_error}")
                    continue # 次のページへ
                # --- ▲ except終了 ▲ ---
            # --- ▲ for ループ終了 ▲ ---

            full_text = "\n".join(text_list)
            if not full_text and num_pages > 0:
                 logger.warning(f"No text could be extracted from any pages in {file_path}.")
            return full_text

    except FileNotFoundError:
        logger.error(f"PDF not found: {file_path}")
        return None
    except PyPDF2.errors.PdfReadError as pdf_error:
        logger.error(f"Error reading PDF {file_path}: {pdf_error}")
        return None
    except Exception as e:
        logger.exception(f"PDF error {file_path}: {e}")
        return None
# --- ▲ PDFからのテキスト抽出 (インデント修正) ▲ ---


# --- DOCXからのテキスト抽出 (変更あり) ---
def extract_text_from_docx(file_path: str) -> str | None:
    # ... (変更なし) ...
    text_list: list[str] = []
    try:
        doc = Document(file_path)
        text_list = [para.text.strip() for para in doc.paragraphs if para.text and para.text.strip()]
        full_text = "\n".join(text_list)
        if not full_text:
            logger.warning(f"No text extracted from DOCX: {file_path}")
        return full_text
    except FileNotFoundError:
        logger.error(f"DOCX not found: {file_path}")
        return None
    except Exception as e:
        logger.exception(f"Error reading DOCX {file_path}: {e}")
        return None