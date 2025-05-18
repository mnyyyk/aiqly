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
logger.setLevel(logging.DEBUG)


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
    # --- Ensure cookies (incl. SameSite=None and third‑party) are accepted ---
    options.add_argument("--disable-features=SameSiteByDefaultCookies,CookiesWithoutSameSiteMustBeSecure,BlockThirdPartyCookies")
    options.add_argument("--disable-features=ImprovedCookieControls,ChromeCookieCrumbs")
    options.add_experimental_option(
        "prefs",
        {
            "profile.default_content_settings.cookies": 1,
            "profile.block_third_party_cookies": False,
            "profile.cookie_controls_mode": 0,  # Chrome 115+
        },
    )

    logger.info("Setting up WebDriver...")
    # --- Google Sites 専用: Cookie 注入で Private ページを取得 ---
    use_google_cookie = "sites.google.com" in url and user_id is not None
    cookies_to_inject = get_google_cookies(user_id) if use_google_cookie else None
    logger.debug("cookies_to_inject (raw) = %s",
                 json.dumps(cookies_to_inject, indent=2, ensure_ascii=False) if cookies_to_inject else "None")

    driver = None
    try:
        driver_path = "/usr/local/bin/chromedriver"  # Dockerfileで配置したパス
        if not os.path.exists(driver_path):
            logger.error(f"ChromeDriver not found at specified path: {driver_path}")
            return None
        if not os.access(driver_path, os.X_OK):
            logger.error(f"ChromeDriver at {driver_path} is not executable.")
            return None
        logger.info(f"Using ChromeDriver from: {driver_path}")

        service = ChromeService(driver_path)
        driver = webdriver.Chrome(service=service, options=options)
        # --- Cookie injection: group by domain and inject per domain ---
        try:
            if cookies_to_inject:
                # --- Build domain→cookies map ---
                domain_map: dict[str, list[dict]] = {}
                for ck in cookies_to_inject:
                    dom_raw = ck.get("domain") or "sites.google.com"
                    # keep both dotted & non‑dotted variants
                    dom = dom_raw.lstrip(".")
                    domain_map.setdefault(dom, []).append(ck)

                # --- Fallback: ensure accounts.google.com cookies exist ---
                if "accounts.google.com" not in domain_map:
                    base_cookies = domain_map.get("google.com", []) + domain_map.get(".google.com", [])
                    if base_cookies:
                        domain_map["accounts.google.com"] = [
                            {**bc, "domain": "accounts.google.com"} for bc in base_cookies
                        ]
                        logger.debug(
                            "Synthesised %d cookies for accounts.google.com from google.com",
                            len(base_cookies)
                        )
                # guarantee G_AUTHUSER_H=0
                has_authuser = any(
                    c.get("name") == "G_AUTHUSER_H" for c in domain_map.get("accounts.google.com", [])
                )
                if not has_authuser:
                    domain_map.setdefault("accounts.google.com", []).append({
                        "name": "G_AUTHUSER_H",
                        "value": "0",
                        "domain": "accounts.google.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                    })
                    logger.debug("Added synthetic G_AUTHUSER_H=0 cookie for accounts.google.com")

                # --- Propagate base google.com cookies to other Google sub‑domains ---
                # Private Google Sites often require the same SID/HSID/SSIDs that live on
                # *.google.com to also be available on sites.google.com.  Here we copy any
                # cookie that currently lives on google.com and is *not already present*
                # for the destination domain.
                base_google_cookies = domain_map.get("google.com", [])
                if base_google_cookies:
                    for dest in ("sites.google.com",):
                        dest_list = domain_map.setdefault(dest, [])
                        for ck in base_google_cookies:
                            if not any(existing.get("name") == ck.get("name") for existing in dest_list):
                                dest_list.append({**ck, "domain": dest})

                # --- Guarantee G_AUTHUSER_H=0 for all key Google domains -------------
                for dom in ("google.com", "sites.google.com"):
                    if not any(c.get("name") == "G_AUTHUSER_H" for c in domain_map.setdefault(dom, [])):
                        domain_map[dom].append({
                            "name": "G_AUTHUSER_H",
                            "value": "0",
                            "domain": dom,
                            "path": "/",
                            "secure": True,
                            "httpOnly": False,
                        })

                logger.debug("Cookie domain_map = %s",
                             json.dumps({k: len(v) for k, v in domain_map.items()}, indent=2))
                injected_count = 0
                # Clear once at session start; we don’t want to wipe cookies added for earlier domains later
                driver.delete_all_cookies()
                for dom, ck_list in domain_map.items():
                    dummy_url = f"https://{dom}/robots.txt"
                    driver.get(dummy_url)           # ensure domain match
                    for ck in ck_list:
                        add_ck = {
                            "name": ck.get("name"),
                            "value": ck.get("value"),
                            "domain": dom,
                            "path": ck.get("path", "/"),
                            "secure": bool(ck.get("secure", True)),
                            "httpOnly": bool(ck.get("httpOnly", False)),
                            "sameSite": ck.get("sameSite", "None"),  # Force SameSite=None
                        }
                        if isinstance(ck.get("expiry"), int):
                            add_ck["expiry"] = ck["expiry"]
                        try:
                            driver.add_cookie(add_ck)
                            logger.debug("add_cookie OK -> %s (domain %s)", add_ck["name"], dom)
                            injected_count += 1
                        except Exception as add_err:
                            logger.warning("add_cookie failed (%s): %s", ck.get('name'), add_err, exc_info=True)
                logger.info("Injected %d cookies across %d domains", injected_count, len(domain_map))
                # --- Sanity‑check: visit accounts.google.com once for Google Sites ---
                if "sites.google.com" in url:
                    acct_url = (
                        "https://accounts.google.com/ServiceLogin"
                        "?continue=https://sites.google.com"
                    )
                    try:
                        logger.info("Pre‑visiting %s to validate cookies…", acct_url)
                        driver.get(acct_url)
                        WebDriverWait(driver, 10).until(
                            lambda d: d.execute_script("return document.readyState") == "complete"
                        )
                    except Exception as pre_chk_err:
                        logger.debug("accounts.google.com pre‑visit failed (ignored): %s", pre_chk_err)
        except Exception as cke:
            logger.warning("Cookie injection failed: %s", cke)
        driver.set_page_load_timeout(timeout_sec)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        logger.info(f"Navigating to {url}...")
        driver.get(url)
        logger.info("After navigation current_url=%s", driver.current_url)
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
            logger.info("Extracted structured text length: %d chars.", len(cleaned_text))

            # ------------------------------------------------------------------
            # Fallback: if .content 直下のテキストがほとんど無い場合は <body> で再抽出
            # ------------------------------------------------------------------
            if len(cleaned_text.strip()) < 50:
                logger.warning(
                    "Extracted text is short (%d chars) – falling back to <body>.",
                    len(cleaned_text),
                )
                cleaned_text = re.sub(
                    r'\n\s*\n\s*\n+',
                    '\n\n',
                    extract_structured_text(soup.body, url)
                ).strip()
                logger.info("Fallback <body> text length: %d chars.", len(cleaned_text))
                if len(cleaned_text.strip()) < 50:
                    logger.warning(
                        "Fallback <body> text is also short – giving up."
                    )
                    return None

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