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
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

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

# --- URLからのテキスト抽出 (変更なし) ---
def fetch_text_from_url(url: str, timeout_sec=45, wait_after_load_sec=5, scroll_attempts=3) -> str | None:
    """Seleniumを使ってURLを開き、構造化テキストを抽出する"""
    print(f"Fetching URL with Selenium: {url}")
    options = webdriver.ChromeOptions(); # オプション設定...
    options.add_argument('--headless'); options.add_argument('--no-sandbox'); options.add_argument('--disable-dev-shm-usage'); options.add_argument('--disable-gpu'); options.add_argument('--log-level=3'); options.add_argument('--disable-blink-features=AutomationControlled'); options.add_experimental_option('excludeSwitches', ['enable-automation']); options.add_experimental_option('useAutomationExtension', False); options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36')
    driver = None
    try:
        print("Setting up WebDriver..."); service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options); driver.set_page_load_timeout(timeout_sec)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        print(f"Navigating to {url}..."); driver.get(url)
        WebDriverWait(driver, timeout_sec).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        print(f"Waiting {wait_after_load_sec}s..."); time.sleep(wait_after_load_sec)
        if scroll_attempts > 0: # スクロール処理...
             print(f"Scrolling down up to {scroll_attempts} times...")
             last_height = driver.execute_script("return document.body.scrollHeight")
             for i in range(scroll_attempts):
                 driver.execute_script("window.scrollTo(0, document.body.scrollHeight);"); time.sleep(2)
                 new_height = driver.execute_script("return document.body.scrollHeight")
                 if new_height == last_height: print("Scroll height did not change, breaking scroll loop."); break
                 last_height = new_height
        print("Getting page source..."); html_content = driver.page_source
        if not html_content: print("Warning: Failed to get page source."); return None
        print("Parsing HTML & extracting structured text..."); soup = BeautifulSoup(html_content, 'html.parser')
        main_content_selectors = ['main', 'article', '[role="main"]', '.content', '#content', '.post-content', '#main-content', '.entry-content']; target_element = None
        print("Searching for main content area...")
        for selector in main_content_selectors:
            try:
                potential_target = soup.select_one(selector)
                if potential_target:
                    target_element = potential_target
                    print(f"Found main content using selector: '{selector}'")
                    break
            except Exception as select_error: print(f"Warning: Error occurred while using selector '{selector}': {select_error}"); continue
        if not target_element: print("Warning: Main content not found, using body."); target_element = soup.body
        if target_element:
            structured_text = extract_structured_text(target_element, url)
            cleaned_text = re.sub(r'\n\s*\n\s*\n+', '\n\n', structured_text).strip()
            print(f"Extracted structured text length: {len(cleaned_text)} chars.")
            if len(cleaned_text) < 100: print("Warning: Extracted text is short.")
            return cleaned_text
        else: print("Error: Could not find body tag."); return None
    except TimeoutException: print(f"Error: Page load/wait timeout for {url}."); return None
    except WebDriverException as e: print(f"Error: WebDriverException for {url}: {e}"); traceback.print_exc(); return None
    except Exception as e: print(f"Error fetching {url}: {e}"); traceback.print_exc(); return None
    finally:
        if driver: print("Closing WebDriver..."); driver.quit()

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
                 print(f"Warning: PDF file {file_path} has 0 pages.")
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
                    print(f"Error extracting text from page {i+1} of {file_path}: {page_error}")
                    continue # 次のページへ
                # --- ▲ except終了 ▲ ---
            # --- ▲ for ループ終了 ▲ ---

            full_text = "\n".join(text_list)
            if not full_text and num_pages > 0:
                 print(f"Warning: No text could be extracted from any pages in {file_path}.")
            return full_text

    except FileNotFoundError: print(f"Error: PDF not found: {file_path}"); return None
    except PyPDF2.errors.PdfReadError as pdf_error: print(f"Error reading PDF {file_path}: {pdf_error}"); return None
    except Exception as e: print(f"PDF error {file_path}: {e}"); traceback.print_exc(); return None
# --- ▲ PDFからのテキスト抽出 (インデント修正) ▲ ---


# --- DOCXからのテキスト抽出 (変更なし) ---
def extract_text_from_docx(file_path: str) -> str | None:
    # ... (変更なし) ...
    text_list: list[str] = []
    try:
        doc = Document(file_path)
        text_list = [para.text.strip() for para in doc.paragraphs if para.text and para.text.strip()]
        full_text = "\n".join(text_list)
        if not full_text: print(f"Warning: No text extracted from DOCX: {file_path}")
        return full_text
    except FileNotFoundError: print(f"Error: DOCX not found: {file_path}"); return None
    except Exception as e: print(f"Error reading DOCX {file_path}: {e}"); traceback.print_exc(); return None