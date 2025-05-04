# backend/main.py (Admin用履歴表示API追加版)

import os
from flask import (
    Flask, request, jsonify, send_from_directory, url_for,
    redirect, flash, render_template, session
)
from flask_cors import CORS
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import traceback
# --- Google OAuth 関連 ---
from flask_dance.contrib.google import make_google_blueprint, google as google_conn
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import urllib.parse
import json
import re
import time
import io
import pandas as pd  # ファイル先頭のimport部分に追加

from uuid import uuid4
from datetime import datetime, timezone

# ---- Slack integration imports ----
from slack_sdk import WebClient
import requests, hmac, hashlib, base64, time as time_mod


# --- extensions から db と login_manager をインポート ---
from backend.extensions import db, login_manager

# --- 認証関連 ---
from flask_login import UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from backend.forms import RegistrationForm, LoginForm

 # --- 文章抽出サービス (ingestion) ---
# 検索順:
#  1) backend.services.ingestion (推奨: services パッケージを backend 内に置く)
#  2) backend.ingestion          (backend 直下に ingestion.py がある場合)
#  3) ingestion                  (プロジェクト直下に ingestion.py がある場合)
ingestion_imported = False
for mod_path in [
    "backend.services.ingestion",
    "backend.ingestion",
    "ingestion",
]:
    try:
        ingestion_mod = __import__(mod_path, fromlist=["dummy"])
        fetch_text_from_url = ingestion_mod.fetch_text_from_url
        chunk_text = ingestion_mod.chunk_text
        extract_text_from_pdf = ingestion_mod.extract_text_from_pdf
        extract_text_from_docx = ingestion_mod.extract_text_from_docx
        ingestion_imported = True
        break
    except ModuleNotFoundError:
        continue

if not ingestion_imported:
    raise ModuleNotFoundError(
        "ingestion utility could not be imported. "
        "Ensure ingestion.py is located in project root, backend/, or backend/services/."
    )
from backend.services import retriever
from backend.services.chat import answer_question
from backend.tasks import handle_slack_event  # celery async processing

# --- モデル ---
from backend.models import (
    User, ChatHistory,
    DEFAULT_ICON_URL, DEFAULT_PROMPT_ROLE, DEFAULT_PROMPT_TASK,
    WatchedSheet,
    SlackIntegration
)
# ---------- Google Drive Push‑Notification (watch/unwatch) ----------
def _build_drive(creds):
    return build("drive", "v3", credentials=creds)

def start_drive_watch(file_id: str, user_id: int) -> str:
    """
    Create a push‑notification channel for given file_id.
    Returns human‑readable message.
    """
    creds = get_google_credentials()
    if creds is None:
        return "Google 未認証です"

    # 既存 watch があれば停止して削除
    prev = db.session.scalars(
        db.select(WatchedSheet).filter_by(file_id=file_id, user_id=user_id)
    ).first()
    if prev:
        stop_drive_watch(prev)

    channel_id = str(uuid4())
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": os.getenv("GDRIVE_WEBHOOK_URL", "").rstrip("/"),
        "token": str(user_id),
        "params": {"ttl": "604800"}  # 7 days
    }

    try:
        resp = _build_drive(creds).files().watch(fileId=file_id, body=body).execute()
        watch = WatchedSheet(
            user_id=user_id,
            file_id=file_id,
            channel_id=channel_id,
            resource_id=resp["resourceId"],
            expiration_ms=int(resp["expiration"])
        )
        db.session.add(watch)
        db.session.commit()
        return "監視を開始しました。"
    except Exception as e:
        traceback.print_exc()
        return f"watch 失敗: {e}"

def stop_drive_watch(watch: WatchedSheet) -> None:
    creds = get_google_credentials()
    if creds is None:
        return
    try:
        _build_drive(creds).channels().stop(body={
            "id": watch.channel_id,
            "resourceId": watch.resource_id
        }).execute()
    except Exception as e:
        print(f"stop_drive_watch error: {e}")
    finally:
        db.session.delete(watch)
        db.session.commit()
# ------------------------------------------------------------------

# ---------- Slack helpers ----------
def _get_slack_integration(user_id: int):
    return db.session.scalars(
        db.select(SlackIntegration).filter_by(user_id=user_id)
    ).first()

def _verify_slack_signature(req) -> bool:
    """
    Verify Slack request signature.

    Slack basestring = "v0:{timestamp}:{raw_body}"
    Raw body must be **bytes as sent**, so use get_data(cache=False, as_text=False).
    """
    signing_secret = os.getenv("SLACK_SIGNING_SECRET")
    if not signing_secret:
        return False

    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    sig_header = req.headers.get("X-Slack-Signature", "")
    if not timestamp or not sig_header:
        return False

    # Replay‑attack guard (5‑minute window)
    if abs(time_mod.time() - int(timestamp)) > 60 * 5:
        return False

    raw_body = req.get_data(cache=False, as_text=False)  # bytes
    basestring = b"v0:" + timestamp.encode() + b":" + raw_body
    my_sig = "v0=" + hmac.new(
        signing_secret.encode(), basestring, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(my_sig, sig_header)
# -----------------------------------

# --- Flask-Migrate ---
from flask_migrate import Migrate


load_dotenv()

# ---------- Slack redirect URI (must exactly match Slack App settings) ----------
SLACK_REDIRECT_URI = os.getenv("SLACK_REDIRECT_URI")  # e.g. https://xxxx.ngrok-free.app/slack/oauth/callback

# --- Flaskアプリケーション初期化と設定 ---
# --- Behind‑ALB HTTPS awareness ---
# Trust X‑Forwarded‑Proto / Host sent by the load balancer so that url_for(..., _external=True)
# and OAuth redirect_uri are generated with "https://app.aiqly.co".
app = Flask(__name__, static_folder='static', static_url_path='/static', template_folder='templates')
from werkzeug.middleware.proxy_fix import ProxyFix  # already imported above
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
# Force url_for to prefer HTTPS scheme globally
app.config.setdefault("PREFERRED_URL_SCHEME", "https")
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# To keep Flask session cookies bound to the same host that Slack
# sends back to, derive SERVER_NAME from SLACK_REDIRECT_URI when set.
# This prevents the "Invalid state" error caused by cookie / domain
# mismatch between 127.0.0.1 and the ngrok HTTPS domain.
# ------------------------------------------------------------------
if SLACK_REDIRECT_URI:
    _parsed = urllib.parse.urlparse(SLACK_REDIRECT_URI)
    if _parsed.scheme in ("http", "https") and _parsed.netloc:
        app.config["SERVER_NAME"] = _parsed.netloc                 # e.g. ec01-1234.ngrok-free.app
        app.config["PREFERRED_URL_SCHEME"] = _parsed.scheme        # ensures url_for(..., _external=True)
        # Use secure cookies only when scheme is https
        app.config["SESSION_COOKIE_SECURE"] = (_parsed.scheme == "https")
        # Lax is usually fine for OAuth redirects
        app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
# ------------------------------------------------------------------
# Google OAuth Blueprint
google_bp = make_google_blueprint(
    client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    scope=[
        "openid",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    ],
    redirect_url="/google_login/authorized",
    offline=True,          # ★ refresh_token を取得
    reprompt_consent=True  # ★ 既存ユーザーにも同意画面を再表示
)
app.register_blueprint(google_bp, url_prefix="/login")
CORS(app, supports_credentials=True)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'change-this-very-secret-key-in-production')
if app.config['SECRET_KEY'] == 'change-this-very-secret-key-in-production': print("WARNING: Use a strong SECRET_KEY!")
 # --- Database connection ---
db_uri = os.getenv("DATABASE_URL")
if db_uri:
    # Use the connection string supplied via environment (e.g. PostgreSQL on RDS)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
else:
    # Fallback to local sqlite for development
    db_dir = os.path.join(os.path.dirname(__file__), "instance")
    os.makedirs(db_dir, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(db_dir, "users.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)
migrate = Migrate(app, db)
login_manager.init_app(app); login_manager.login_view = 'login'; login_manager.login_message = "ログインが必要です。"; login_manager.login_message_category = "info"

@login_manager.user_loader
def load_user(user_id):
    try: return db.session.get(User, int(user_id))
    except: traceback.print_exc(); return None

# --- パスと定数 (変更なし) ---
BASE_DIR = os.path.dirname(__file__)
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
ICON_UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'icons')
ICON_FILENAME = "ai_icon.png"
ALLOWED_EXTENSIONS = {"txt", "pdf", "docx", "xls", "xlsx"}
ALLOWED_ICON_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)
if not os.path.exists(ICON_UPLOAD_FOLDER): os.makedirs(ICON_UPLOAD_FOLDER)

# --- ヘルパー関数 (変更なし) ---

# --- Google 資格情報ユーティリティ ---
def get_google_credentials():
    """flask‑dance で取得したトークンを google.oauth2.credentials.Credentials に変換"""
    if not google_conn.authorized:
        return None
    token = google_conn.token["access_token"]
    return Credentials(
        token,
        refresh_token=google_conn.token.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
        scopes=[
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "openid",
        ],
    )
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
def allowed_icon_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_ICON_EXTENSIONS

# --- フロントエンド用ルート (変更なし) ---
@app.route("/")
def serve_chatbot():
    try: return render_template("chatbot/index.html")
    except Exception as e: print(f"Error rendering chatbot: {e}"); return "Not Found", 404

@app.route("/admin")
@login_required
def serve_admin():
    try: return render_template("admin/index.html")
    except Exception as e: print(f"Error rendering admin: {e}"); return "Not Found", 404

# --- 認証ルート (変更なし) ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    # ...(変更なし)...
    if current_user.is_authenticated: return redirect(url_for('serve_admin'))
    form = RegistrationForm()
    if form.validate_on_submit():
        existing_user = db.session.scalars(db.select(User).filter_by(email=form.email.data)).first()
        if existing_user: flash('このメールアドレスは既に使用されています。', 'error'); return redirect(url_for('register'))
        try:
            user = User(email=form.email.data); user.set_password(form.password.data)
            db.session.add(user); db.session.commit()
            flash('ユーザー登録が完了しました。ログインしてください。', 'success')
            return redirect(url_for('login'))
        except Exception as e: db.session.rollback(); print(f"Error registering user: {e}"); traceback.print_exc(); flash('ユーザー登録中にエラーが発生しました。', 'error')
    return render_template('register.html', title='ユーザー登録', form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    # ...(変更なし)...
    if current_user.is_authenticated: return redirect(url_for('serve_admin'))
    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.scalars(db.select(User).filter_by(email=form.email.data)).first()
        if user is None or not user.check_password(form.password.data):
            flash('メールアドレスまたはパスワードが正しくありません。', 'error')
            return redirect(url_for('login'))
        login_user(user, remember=form.remember_me.data)
        flash(f'{user.email} さん、ようこそ！', 'success')
        next_page = request.args.get('next')
        if next_page and not next_page.startswith(('/', request.host_url)) and urllib.parse.urlparse(next_page).netloc != '': next_page = None
        return redirect(next_page or url_for('serve_admin'))
    return render_template('login.html', title='ログイン', form=form)

@app.route("/google_login/authorized")
def google_login_authorized():
    if not google_conn.authorized:
        flash("Google ログインに失敗しました。", "error")
        return redirect(url_for("login"))

    resp = google_conn.get("/oauth2/v2/userinfo")
    if not resp.ok:
        flash("Google からユーザー情報を取得できませんでした。", "error")
        return redirect(url_for("login"))

    info = resp.json()
    email = info["email"]

    # 既存ユーザー検索 or 新規作成
    user = db.session.scalars(db.select(User).filter_by(email=email)).first()
    if not user:
        user = User(email=email)
        user.password_hash = generate_password_hash(os.urandom(12).hex())  # ダミーPW
        db.session.add(user)
        db.session.commit()

    login_user(user)
    flash(f"{email} でログインしました。", "success")
    return redirect(url_for("serve_admin"))


@app.route('/logout')
@login_required
def logout():
    # ...(変更なし)...
    logout_user()
    flash('ログアウトしました。', 'success')
    return redirect(url_for('login'))


# --- APIエンドポイント ---

# ---------- Slack status endpoint ----------
@app.route("/api/slack/status")
@login_required
def slack_status():
    """
    Return whether the current user has an authorised Slack workspace.
    Response JSON: { "status": "ok", "connected": true/false }
    """
    integ = db.session.scalars(
        db.select(SlackIntegration).filter_by(user_id=current_user.id)
    ).first()
    return jsonify(status="ok", connected=bool(integ and integ.bot_token))
# ------------------------------------------------

# ---------- Slack integration routes ----------
@app.route("/api/slack/creds", methods=["POST"])
@login_required
def save_slack_creds():
    """
    Body JSON: { "client_id": "...", "client_secret": "..." }
    Saves Slack OAuth creds for current user and returns auth URL.
    """
    data = request.json or {}
    cid = data.get("client_id", "").strip()
    csec = data.get("client_secret", "").strip()
    if not cid or not csec:
        return jsonify(status="error", message="client_id and client_secret required"), 400

    integ = _get_slack_integration(current_user.id)
    if not integ:
        integ = SlackIntegration(user_id=current_user.id)
        db.session.add(integ)
    integ.client_id = cid
    integ.client_secret = csec
    integ.bot_token = None
    db.session.commit()

    # --- CSRF‑protection state (store server‑side) ---
    oauth_state = str(uuid4())
    integ.oauth_state = oauth_state
    db.session.commit()

    redirect_uri = SLACK_REDIRECT_URI or url_for("slack_oauth_callback", _external=True)

    # Build the authorise URL with urlencode (safer)
    params = {
        "client_id": cid,
        "scope": "app_mentions:read,chat:write",
        "redirect_uri": redirect_uri,
        "state": oauth_state,
    }
    auth_url = "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode(params)
    return jsonify(status="ok", auth_url=auth_url)

@app.route("/slack/oauth/callback")
def slack_oauth_callback():
    code = request.args.get("code")
    # CSRF check (server-side state validation, tolerant in dev)
    state_param = request.args.get("state", "")
    integ = None
    if state_param:
        integ = db.session.scalars(
            db.select(SlackIntegration).filter_by(oauth_state=state_param)
        ).first()

    # In development, some front‑end flows may omit state.
    # If state is missing or no match, fall back to the most‑recent integration for the current user.
    if not integ:
        print("[Slack OAuth] WARNING: state validation skipped (empty or unmatched).")
        integ = db.session.scalars(
            db.select(SlackIntegration)
              .filter_by(user_id=current_user.id)
              .order_by(SlackIntegration.updated_at.desc())
        ).first()
        if not integ:
            return "Invalid state (no integration)", 400
    if not code:
        return "Missing code", 400

    redirect_uri = SLACK_REDIRECT_URI or url_for("slack_oauth_callback", _external=True)
    resp = requests.post(
        "https://slack.com/api/oauth.v2.access",
        data={
            "client_id": integ.client_id,
            "client_secret": integ.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    ).json()
    if not resp.get("ok"):
        return f"Slack OAuth Failed: {resp}", 400

    integ.bot_token = resp["access_token"]
    integ.team_id = resp.get("team", {}).get("id")
    integ.updated_at = datetime.utcnow()
    db.session.commit()
    # Clear stored oauth_state so it cannot be reused
    integ.oauth_state = None
    db.session.commit()
    return "Slack workspace authorised! You can close this window."

# ---------- Slack Events Route ----------
@app.route("/slack/events", methods=["POST"])
def slack_events():
    body = request.get_json(silent=True) or {}

    # --- DEBUG: show incoming payload (first 300 chars) ---
    try:
        print("[DEBUG] /slack/events payload =", json.dumps(body)[:300])
    except Exception:
        print("[DEBUG] /slack/events received (payload not JSON‑serialisable)")

    # ① URL Verification
    if body.get("type") == "url_verification":
        return body.get("challenge", ""), 200, {"Content-Type": "text/plain"}

    # ② Signature check
    if not _verify_slack_signature(request):
        return "Invalid signature", 403

    # -------- extract clean_text so worker can reply ----------
    event = body.get("event", {})
    bot_user_id = body.get("authorizations", [{}])[0].get("user_id")
    clean_text = ""
    if event.get("type") == "app_mention":
        mention_re = rf"<@{bot_user_id}>"
        clean_text = re.sub(mention_re, "", event.get("text", "")).strip()
    elif (
        event.get("type") == "message"
        and event.get("channel_type") == "im"
        and not event.get("bot_id")
    ):
        clean_text = event.get("text", "").strip()

    # 保存して Celery で利用
    body.setdefault("event", {})["clean_text"] = clean_text

    # enqueue async processing – do not block / avoid duplicate replies
    try:
        handle_slack_event.delay(body)
        print("[DEBUG] handle_slack_event enqueued")  # <-- added
    except Exception as e:
        print(f"[Slack] Failed enqueue task: {e}")

    # Immediate 200 response for Slack
    return "", 200
# -----------------------------------------------

# --- Google Sheet Watch API (push notification) ---
@app.route("/api/watch_sheet", methods=["POST"])
@login_required
def watch_sheet_toggle():
    """
    JSON: { "file_id": "<Drive File ID or URL>" }
    Toggle Google Sheet watch (push notification).
    """
    data = request.json or {}
    file_id = data.get("file_id", "").strip()
    if not file_id:
        return jsonify(status="error", message="file_id required"), 400

    # URL だった場合は ID だけ取り出す
    if "://" in file_id:
        m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{10,})", file_id)
        if m:
            file_id = m.group(1)

    watch = db.session.scalars(
        db.select(WatchedSheet).filter_by(file_id=file_id, user_id=current_user.id)
    ).first()

    if watch:
        stop_drive_watch(watch)
        return jsonify(status="ok", message="監視を停止しました")
    else:
        msg = start_drive_watch(file_id, current_user.id)
        return jsonify(status="ok" if msg.startswith("監視") else "error", message=msg)

# --- Google Sheets 取り込み関数 --------------------------
def ingest_google_sheet(file_id: str, user_id: int, cell_range: str = "A1:Z1000") -> bool:
    """
    指定した Google スプレッドシート (file_id) を取得し、
    行ごとにタブ区切り文字列へ整形して retriever に登録する。
    """
    creds = get_google_credentials()
    if creds is None:
        print("Google credentials not found for ingest_google_sheet.")
        return False

    try:
        drive_svc   = build("drive",   "v3", credentials=creds)
        meta        = drive_svc.files().get(fileId=file_id, fields="mimeType,name").execute()
        mime_type   = meta.get("mimeType", "")
        sheet_name  = meta.get("name", "sheet")

        if mime_type == "application/vnd.google-apps.spreadsheet":
            # ---- Native Google Sheets → Sheets API で値取得 ----
            sheets_svc = build("sheets", "v4", credentials=creds)
            result = sheets_svc.spreadsheets().values().get(
                spreadsheetId=file_id,
                range=cell_range
            ).execute()
            rows = result.get("values", [])
        else:
            # ---- Excel など → Drive export で CSV 取得 ----
            raw  = drive_svc.files().export(fileId=file_id, mimeType="text/csv").execute()
            csv_text = raw.decode("utf-8", errors="replace")
            rows = [row.split(",") for row in csv_text.splitlines() if row.strip()]

        if not rows:
            print(f"[ingest_google_sheet] No data in sheet {file_id}")
            return False

        chunks = ["\t".join(r) for r in rows]
        retriever.add_documents(chunks, f"gsheet:{file_id}", user_id)
        print(f"[ingest_google_sheet] Added {len(chunks)} chunks from '{sheet_name}' ({file_id}) for user {user_id}")
        return True
    except Exception as e:
        print(f"[ingest_google_sheet] Error processing sheet {file_id}: {e}")
        traceback.print_exc()
        return False

# Google ドキュメント取り込み API
@app.route("/api/google/doc", methods=["POST"])
@login_required
def ingest_google_doc():
    """
    JSON: { "file_id": "<Drive File ID>" }
    Google ドキュメントをプレーンテキストで取得してナレッジ登録
    """
    data = request.json or {}
    file_id = data.get("file_id")
    if not file_id:
        return jsonify({"status": "error", "message": "file_id required"}), 400

    creds = get_google_credentials()
    if creds is None:
        return jsonify({"status": "error", "message": "Google 未認証です"}), 401

    try:
        drive = build("drive", "v3", credentials=creds)
        raw = drive.files().export(
            fileId=file_id,
            mimeType="text/plain"
        ).execute()
        text = raw.decode("utf-8")
        chunks = chunk_text(text)
        retriever.add_documents(chunks, f"gdoc:{file_id}", current_user.id)
        return jsonify({"status": "ok", "message": "Google ドキュメントを取り込みました"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"取得失敗: {e}"}), 500

# Google スプレッドシート取り込み API
@app.route("/api/google/sheet", methods=["POST"])
@login_required
def ingest_google_sheet_api():
    """
    JSON: { "file_id": "<Drive File ID>", "range": "A1:Z1000" (optional) }
    指定シートを取得し、ナレッジへ登録。
    """
    payload = request.json or {}
    file_id = payload.get("file_id")
    cell_range = payload.get("range", "A1:Z1000")

    if not file_id:
        return jsonify({"status": "error", "message": "file_id required"}), 400

    # URL が渡された場合は ID を抽出
    if "://" in file_id:
        m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{10,})", file_id)
        if m:
            file_id = m.group(1)

    success = ingest_google_sheet(file_id, current_user.id, cell_range)
    return jsonify({"status": "ok" if success else "error"})

 # ---------- New: Sheet table preview ----------
@app.route("/api/sheet_rows", methods=["POST"])
@login_required
def get_sheet_rows():
    """
    JSON: { "file_id": "<Drive File ID>", "range": "A1:Z1000" }
    Returns headers + rows for frontend table preview.
    """
    payload    = request.json or {}
    file_id    = payload.get("file_id")
    cell_range = payload.get("range", "A1:Z1000")
    if not file_id:
        return jsonify({"status": "error", "message": "file_id required"}), 400

    # If URL was passed, extract ID
    if "://" in file_id:
        m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{10,})", file_id)
        if m:
            file_id = m.group(1)

    creds = get_google_credentials()
    if creds is None:
        return jsonify({"status": "error", "message": "Google 未認証"}), 401

    try:
        drive = build("drive", "v3", credentials=creds)
        meta  = drive.files().get(fileId=file_id, fields="mimeType").execute()
        mime  = meta["mimeType"]

        rows = []
        if mime == "application/vnd.google-apps.spreadsheet":
            sheets = build("sheets", "v4", credentials=creds)
            res = sheets.spreadsheets().values().get(
                spreadsheetId=file_id, range=cell_range
            ).execute()
            rows = res.get("values", [])
        elif mime in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        ):
            from googleapiclient.http import MediaIoBaseDownload
            buf = io.BytesIO()
            dl  = MediaIoBaseDownload(buf, drive.files().get_media(fileId=file_id))
            done = False
            while not done:
                _, done = dl.next_chunk()
            buf.seek(0)
            df = pd.read_excel(buf, header=None).fillna("")
            rows = df.astype(str).values.tolist()
        else:
            return jsonify({"status": "error", "message": f"Unsupported MIME: {mime}"}), 400

        headers = rows[0] if rows else []
        body    = rows[1:] if len(rows) > 1 else []
        return jsonify({"status": "ok", "headers": headers, "rows": body})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Failed fetch rows: {e}"}), 500

@app.route("/api/ask", methods=["POST"])
@login_required
def ask():
    # ...(変更なし、DBへの履歴保存は実装済み)...
    user_id = current_user.id; user_email = current_user.email; history_to_save = []; answer = "エラーが発生しました。"
    try:
        data = request.json; question = data.get("question", "").strip(); history = data.get("history", [])
        if not data or not question: return jsonify({"error": "Question is required."}), 400
        if not isinstance(history, list): print(f"Warning: Received invalid history format for user {user_id}. Type: {type(history)}"); history = []
        print(f"--- API /api/ask --- User: {user_id}({user_email}), Question: '{question}', History length received: {len(history)}")
        history_to_save.append(ChatHistory(user_id=user_id, role="user", content=question))
        answer = answer_question(question, user_id, history)
        history_to_save.append(ChatHistory(user_id=user_id, role="assistant", content=answer))
        try:
            db.session.add_all(history_to_save); db.session.commit()
            print(f"--- API /api/ask --- Saved chat history (user & assistant) for user {user_id} to DB.")
        except Exception as db_save_error: db.session.rollback(); print(f"Error saving chat history for user {user_id} to DB: {db_save_error}"); traceback.print_exc()
        return jsonify({"answer": answer})
    except Exception as e:
        print(f"Critical Error in /api/ask for user {user_id}: {e}"); traceback.print_exc()
        try:
            error_content = f"API Error: {e}"
            if not history_to_save: history_to_save.append(ChatHistory(user_id=user_id, role="user", content=question if 'question' in locals() else "Unknown Question"))
            history_to_save.append(ChatHistory(user_id=user_id, role="assistant", content=error_content))
            db.session.add_all(history_to_save); db.session.commit(); print(f"--- API /api/ask --- Saved error occurrence for user {user_id} to DB.")
        except Exception as db_error_on_error: db.session.rollback(); print(f"Failed to save error occurrence to DB for user {user_id}: {db_error_on_error}")
        return jsonify({"error": "Internal server error processing your request."}), 500

# --- (他のAPIエンドポイント: /api/url, /api/upload などは変更なし) ---
@app.route("/api/url", methods=["POST"])
@login_required
def add_url():
    # ...(変更なし)...
    user_id = current_user.id; data = request.json; url = data.get("url", "")
    if not url: return jsonify({"status": "error", "message": "URL required"}), 400
    print(f"User {user_id} overwriting URL: {url}"); delete_success = retriever.delete_documents_by_source(url, user_id)
    if not delete_success: print(f"Warning: Failed delete for User {user_id}, URL {url}.")
    text = fetch_text_from_url(url)
    if not text or not text.strip(): return jsonify({"status": "error", "message": "Fetch failed or no text"}), 400
    chunks = chunk_text(text)
    if not chunks: return jsonify({"status": "error", "message": "No chunks generated"}), 400
    add_success = retriever.add_documents(chunks, url, user_id)
    if add_success: return jsonify({"status": "ok", "message": f"URL '{url}' (re)added."})
    else: return jsonify({"status": "error", "message": f"Failed add chunks from '{url}'."}), 500

@app.route("/api/upload", methods=["POST"])
@login_required
def upload_file():
    # ...(変更なし)...
    user_id = current_user.id; file = request.files.get('file')
    if not file: return jsonify({"status": "error", "message": "No file part"}), 400
    filename = secure_filename(file.filename)
    if filename == '': return jsonify({"status": "error", "message": "No selected file"}), 400
    if allowed_file(filename):
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{user_id}_{int(time.time())}_{filename}")
        try:
            print(f"User {user_id} overwriting File: {filename}"); delete_success = retriever.delete_documents_by_source(filename, user_id)
            if not delete_success: print(f"Warning: Failed delete for User {user_id}, file {filename}.")
            if not os.path.exists(app.config['UPLOAD_FOLDER']): os.makedirs(app.config['UPLOAD_FOLDER'])
            file.save(filepath); print(f"Temp file saved for user {user_id}: {filepath}")
            text, file_ext = None, filename.rsplit('.', 1)[1].lower()
            print(f"Extracting text from: {filename} ({file_ext})")
            if file_ext == 'pdf': text = extract_text_from_pdf(filepath)
            elif file_ext in ['xls', 'xlsx']:
                try:
                    df = pd.read_excel(filepath)
                    text = df.to_string(index=False)
                except Exception as e:
                    return jsonify({"status": "error", "message": f"Excelファイルの読み込みエラー: {e}"}), 500
            elif file_ext == 'docx': text = extract_text_from_docx(filepath)
            elif file_ext == 'txt':
                 encodings_to_try = ['utf-8', 'shift-jis', 'euc-jp']; text_read = False
                 for enc in encodings_to_try:
                     try:
                         with open(filepath, 'r', encoding=enc) as f: text = f.read(); print(f"Read {filename} with {enc}"); text_read = True; break
                     except: continue
                 if not text_read: return jsonify(status="error",message=f"Could not decode '{filename}'."),500
            if text is None and file_ext not in ['txt']: return jsonify(status="error",message=f"Failed extract text from {file_ext}."),500
            elif isinstance(text,str)and not text.strip(): return jsonify(status="error",message=f"No text in '{filename}'."),400
            chunks = chunk_text(text)
            if not chunks: return jsonify({"status": "error", "message": "No chunks generated."}), 500
            add_success = retriever.add_documents(chunks, filename, user_id)
            if add_success: return jsonify({"status": "ok", "message": f"File '{filename}' (re)added."})
            else: return jsonify({"status": "error", "message": f"Failed add chunks from '{filename}'."}), 500
        except Exception as e: print(f"Error processing file {filename} for user {user_id}: {e}"); traceback.print_exc(); return jsonify({"status": "error", "message": f"Error processing file: {e}"}), 500
        finally:
            if os.path.exists(filepath):
                try: os.remove(filepath)
                except OSError as e_rem: print(f"Error removing temp file {filepath}: {e_rem}")
    else: return jsonify({"status": "error", "message": f"File type not allowed. Allowed: {ALLOWED_EXTENSIONS}"}), 400

@app.route("/api/sources", methods=["GET"])
@login_required
def get_sources():
    # ...(変更なし)...
    user_id = current_user.id; sources = retriever.get_registered_sources(user_id); return jsonify({"status": "ok", "sources": sources})

@app.route("/api/documents/<path:source_name>", methods=["GET"])
@login_required
def get_documents(source_name):
    # ...(変更なし)...
    user_id = current_user.id; print(f"\n--- API Endpoint: /api/documents ---"); print(f"[API] Received raw path segment: '{source_name}'")
    try:
        source_name_decoded = urllib.parse.unquote(source_name); print(f"[API] Decoded source_name: '{source_name_decoded}'")
        limit = min(request.args.get('limit', default=50, type=int), 100); print(f"[API] Calling retriever.get_documents_by_source with user_id={user_id}, source='{source_name_decoded}', limit={limit}")
        documents = retriever.get_documents_by_source(source_name_decoded, user_id, limit=limit); print(f"[API] Retriever returned {len(documents)} documents.")
        return jsonify({"status": "ok", "source": source_name_decoded, "documents": documents})
    except Exception as e: print(f"[API ERROR] Error in /api/documents for user {user_id}, raw path '{source_name}': {e}"); traceback.print_exc(); return jsonify({"status": "error", "message": f"Failed to retrieve documents for source: {source_name}"}), 500

@app.route("/api/sources/<path:source_name>", methods=["DELETE"])
@login_required
def delete_source_api(source_name):
    # ...(変更なし)...
    user_id = current_user.id; source_name_decoded = urllib.parse.unquote(source_name); print(f"API delete request user {user_id}, source: {source_name_decoded}")
    success = retriever.delete_documents_by_source(source_name_decoded, user_id)
    if success: return jsonify({"status": "ok", "message": f"Source '{source_name_decoded}' deleted."})
    else: return jsonify({"status": "error", "message": f"Failed delete source '{source_name_decoded}'. Check logs."}), 500

# --- 設定系API (DB対応) (変更なし) ---
@app.route("/api/prompts", methods=["GET", "POST"])
@login_required
def handle_prompts():
    # ...(変更なし)...
    user = current_user
    if request.method == "GET":
        prompts = {"role": user.current_prompt_role, "task": user.current_prompt_task}; return jsonify({"status": "ok", "prompts": prompts})
    elif request.method == "POST":
        data = request.json
        if not data or 'role' not in data or 'task' not in data: return jsonify({"status": "error", "message": "'role' and 'task' required."}), 400
        try:
            user.prompt_role = data.get("role", "").strip(); user.prompt_task = data.get("task", "").strip(); db.session.commit(); print(f"Prompt settings updated for user {user.id}.")
            return jsonify({"status": "ok", "message": "Prompts saved."})
        except Exception as e: db.session.rollback(); print(f"Error writing prompts user {user.id}: {e}"); traceback.print_exc(); return jsonify({"status": "error", "message": "Failed save prompts."}), 500

@app.route("/api/upload_icon", methods=["POST"])
@login_required
def upload_icon():
    # ...(変更なし)...
    user = current_user; file = request.files.get('icon_file')
    if not file: return jsonify({"status": "error", "message": "No icon file part"}), 400
    if file.filename == '': return jsonify({"status": "error", "message": "No selected icon file"}), 400
    if allowed_icon_file(file.filename):
        base, ext = os.path.splitext(file.filename); ext = ext.lower() if ext.lower() in ['.png', '.jpg', '.jpeg', '.gif', '.webp'] else '.png'; filename = f"user_{user.id}_icon{ext}"
        filepath = os.path.join(ICON_UPLOAD_FOLDER, filename)
        try:
            if user.ai_icon_url and user.ai_icon_url != DEFAULT_ICON_URL:
                 old_filename = os.path.basename(urllib.parse.urlparse(user.ai_icon_url).path)
                 if old_filename and old_filename != filename:
                     old_filepath = os.path.join(ICON_UPLOAD_FOLDER, old_filename)
                     if os.path.exists(old_filepath):
                          try: os.remove(old_filepath); print(f"Removed old icon: {old_filepath}")
                          except OSError as e_rem: print(f"Error removing old icon {old_filepath}: {e_rem}")
            file.save(filepath); print(f"AI icon saved for user {user.id}: {filepath}")
            user.ai_icon_url = url_for('static', filename=f'icons/{filename}', _external=False); db.session.commit(); icon_url_for_frontend = f"{user.ai_icon_url}?t={int(time.time())}"
            return jsonify({"status": "ok", "message": "Icon uploaded and updated.", "icon_url": icon_url_for_frontend})
        except Exception as e: db.session.rollback(); print(f"Error saving icon for user {user.id}: {e}"); traceback.print_exc(); return jsonify({"status": "error", "message": "Failed save icon file."}), 500
    else: return jsonify({"status": "error", "message": f"Icon type not allowed. Allowed: {ALLOWED_ICON_EXTENSIONS}"}), 400

@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def handle_chatbot_settings():
    # ...(変更なし)...
    user = current_user
    if request.method == "GET":
        settings = { "ai_name": user.current_ai_name, "ai_icon_url": user.current_ai_icon_url, "theme_color_header": user.current_theme_color_header, "theme_color_user": user.current_theme_color_user, "initial_message": user.current_initial_message }
        if settings["ai_icon_url"]: settings["ai_icon_url"] = f"{settings['ai_icon_url']}?t={int(time.time())}"
        return jsonify({"status": "ok", "settings": settings})
    elif request.method == "POST":
        data = request.json; required_keys = ["ai_name", "theme_color_header", "theme_color_user", "initial_message"]
        if not data or not all(key in data for key in required_keys): missing = [k for k in required_keys if k not in data]; return jsonify({"status": "error", "message": f"Missing keys: {missing}"}), 400
        if not re.match(r'^#[0-9a-fA-F]{6}$', data.get("theme_color_header", "")): return jsonify({"status": "error", "message": "Invalid header color."}), 400
        if not re.match(r'^#[0-9a-fA-F]{6}$', data.get("theme_color_user", "")): return jsonify({"status": "error", "message": "Invalid user bubble color."}), 400
        if not data.get("ai_name","").strip(): return jsonify(status="error",message="AI Name empty."),400
        if not data.get("initial_message","").strip(): return jsonify(status="error",message="Initial message empty."),400
        try:
            user.ai_name = data.get("ai_name").strip(); user.theme_color_header = data.get("theme_color_header"); user.theme_color_user = data.get("theme_color_user"); user.initial_message = data.get("initial_message").strip(); db.session.commit(); print(f"Chatbot settings updated for user {user.id}.")
            return jsonify({"status": "ok", "message": "Settings saved."})
        except Exception as e: db.session.rollback(); print(f"Error writing settings user {user.id}: {e}"); traceback.print_exc(); return jsonify({"status": "error", "message": "Failed save settings."}), 500

# --- ユーザー設定用API (変更なし) ---
@app.route("/api/user/info", methods=["GET"])
@login_required
def get_user_info():
    # ...(変更なし)...
    return jsonify({"status": "ok", "email": current_user.email})

@app.route("/api/user/update_email", methods=["POST"])
@login_required
def update_user_email():
    # ...(変更なし)...
    user = current_user; data = request.json; new_email = data.get("new_email", "").strip()
    if not new_email: return jsonify({"status": "error", "message": "新しいメールアドレスが空です。"}), 400
    if not re.match(r"[^@]+@[^@]+\.[^@]+", new_email): return jsonify({"status": "error", "message": "無効なメールアドレス形式です。"}), 400
    existing_user = db.session.scalars(db.select(User).filter(User.email == new_email, User.id != user.id)).first()
    if existing_user: return jsonify({"status": "error", "message": "このメールアドレスは既に使用されています。"}), 400
    try:
        user.email = new_email; db.session.commit(); print(f"User {user.id} email updated to {new_email}.")
        return jsonify({"status": "ok", "message": "メールアドレスを更新しました。"})
    except Exception as e: db.session.rollback(); print(f"Error updating email for user {user.id}: {e}"); traceback.print_exc(); return jsonify({"status": "error", "message": "メールアドレスの更新中にエラーが発生しました。"}), 500

# (既存の /api/user/update_password エンドポイントのコードはそのまま)


# --- ▼▼▼ Admin用 履歴表示API (新規追加) ▼▼▼ ---
@app.route("/api/admin/users", methods=["GET"])
@login_required
def get_admin_users():
    """Admin画面用にユーザーリスト(IDとEmail)を取得する"""
    # ★★★ 必要であれば、ここに「管理者のみアクセス可能」という権限チェックを追加 ★★★
    # 例: if not current_user.is_admin: return jsonify({"status": "error", "message": "Unauthorized"}), 403

    try:
        # 全ユーザーのIDとEmailを取得 (パスワードハッシュなどは含めない)
        users = db.session.scalars(db.select(User).order_by(User.email)).all()
        user_list = [{"id": u.id, "email": u.email} for u in users]
        return jsonify({"status": "ok", "users": user_list})
    except Exception as e:
        print(f"Error getting user list for admin: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": "ユーザーリストの取得に失敗しました。"}), 500

@app.route("/api/admin/history/<int:user_id>", methods=["GET"])
@login_required
def get_admin_user_history(user_id):
    """指定されたユーザーIDのチャット履歴を取得する (Admin用)"""
    # ★★★ 必要であれば、ここに「管理者のみアクセス可能」という権限チェックを追加 ★★★
    # 例: if not current_user.is_admin: return jsonify({"status": "error", "message": "Unauthorized"}), 403

    target_user = db.session.get(User, user_id)
    if not target_user:
        return jsonify({"status": "error", "message": f"ユーザーID {user_id} が見つかりません。"}), 404

    try:
        # ユーザーIDでフィルタリングし、タイムスタンプの降順 (新しい順) で履歴を取得
        history_entries = db.session.scalars(
            db.select(ChatHistory)
            .where(ChatHistory.user_id == user_id)
            .order_by(ChatHistory.timestamp.desc()) # 新しい順
            # .limit(100) # 必要に応じて件数制限
        ).all()

        # フロントエンドで使いやすい形式 (辞書のリスト) に変換
        history_list = [
            {
                "id": entry.id,
                "role": entry.role,
                "content": entry.content,
                # タイムスタンプをISO 8601形式の文字列に変換 (JavaScriptで扱いやすい)
                "timestamp": entry.timestamp.isoformat() if entry.timestamp else None
            }
            for entry in history_entries
        ]

        return jsonify({
            "status": "ok",
            "user_email": target_user.email, # 対象ユーザーのEmailも返す
            "history": history_list
        })
    except Exception as e:
        print(f"Error getting chat history for user {user_id} (admin view): {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"ユーザー {user_id} のチャット履歴取得に失敗しました。"}), 500
# --- ▲▲▲ Admin用 履歴表示API (新規追加) ▲▲▲ ---

# --- ▼▼▼ Admin用 チャット履歴API (新規追加) ▼▼▼ ---
@app.route('/api/chat_history', methods=['GET'])
@login_required
def chat_history():
    histories = ChatHistory.query.order_by(ChatHistory.timestamp.asc()).all()

    grouped_histories = []
    current_session = []
    previous_time = None

    for h in histories:
        if previous_time and (h.timestamp - previous_time).total_seconds() > 600:
            if current_session:
                grouped_histories.append(current_session)
                current_session = []
        current_session.append({
            'timestamp': h.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'role': h.role,
            'content': h.content
        })
        previous_time = h.timestamp

    if current_session:
        grouped_histories.append(current_session)

    return jsonify({
        'status': 'ok',
        'sessions': grouped_histories
    })
 
# --- データベース初期化コマンド (変更なし) ---


# --- Google Drive webhook receiver ---
@app.route("/webhook/drive", methods=["POST"])
def drive_webhook():
    """
    Receives Google Drive push notifications.
    """
    resource_id = request.headers.get("X-Goog-Resource-Id")
    channel_id  = request.headers.get("X-Goog-Channel-Id")
    state       = request.headers.get("X-Goog-Resource-State")

    if state not in {"add", "update", "change"}:
        return "", 200

    watch = db.session.scalars(
        db.select(WatchedSheet).filter_by(resource_id=resource_id, channel_id=channel_id)
    ).first()
    if not watch:
        return "", 200

    # Celery task to refresh sheet
    try:
        from backend.tasks import update_google_sheet_sources
        update_google_sheet_sources.delay(watch.file_id, watch.user_id)
    except Exception as e:
        print(f"enqueue error: {e}")
        traceback.print_exc()

    return "", 200

# --- データベース初期化コマンド (変更なし) ---
@app.cli.command("init-db")
def init_db_command():
    try:
        with app.app_context():
             print("Dropping all tables..."); db.drop_all()
             print("Creating all tables..."); db.create_all()
             print("Initialized the database.")
    except Exception as e: print(f"Error initializing database: {e}"); traceback.print_exc()

# --- アプリケーション実行 (変更なし) ---
if __name__ == "__main__":
    print("Starting Flask development server...")
    app.run(debug=True, port=5001, host='0.0.0.0')