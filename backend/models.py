# backend/models.py (ChatHistoryモデル追加版)

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from backend.extensions import db  # extensions.py から db をインポート
from sqlalchemy import Text, ForeignKey, DateTime, BigInteger # ForeignKey, DateTime をインポート
from sqlalchemy.sql import func # func をインポート (タイムスタンプ用)
from sqlalchemy.orm import relationship # relationship をインポート

# --- デフォルト設定値 (変更なし) ---
DEFAULT_ICON_URL = "/static/icons/default_icon.png"
DEFAULT_AI_NAME = "AIアシスタント 彩"
DEFAULT_HEADER_COLOR = "#C8A2C8"
DEFAULT_USER_COLOR = "#B76E79"
DEFAULT_INITIAL_MESSAGE = "こんにちは！何か質問はありますか？(Shift+Enterで改行)"
DEFAULT_PROMPT_ROLE="あなたは与えられた内部文書（コンテキスト）に基づいて、ユーザーの質問に誠実に答えるAIアシスタントです。"
DEFAULT_PROMPT_TASK="回答はコンテキストの内容のみを根拠とし、それ以外の知識や憶測で答えないでください。\nコンテキストに回答に該当する情報が全くない場合は、「コンテキスト内に該当する情報が見つかりませんでした。」とだけ答えてください。\nコンテキストの内容を要約するのではなく、質問に対する直接的な答えを具体的に記述してください。"
# --- デフォルト設定値ここまで ---


class User(UserMixin, db.Model):
    """ユーザー情報を格納するモデル (テーブル)"""
    __tablename__ = 'users' # テーブル名を明示的に指定 (推奨)
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, unique=True, nullable=False)
    password_hash = db.Column(db.String(256))

    # --- 設定用カラム (変更なし) ---
    ai_name = db.Column(db.String(80), nullable=True, default=DEFAULT_AI_NAME)
    ai_icon_url = db.Column(db.String(255), nullable=True, default=None)
    theme_color_header = db.Column(db.String(7), nullable=True, default=DEFAULT_HEADER_COLOR)
    theme_color_user = db.Column(db.String(7), nullable=True, default=DEFAULT_USER_COLOR)
    initial_message = db.Column(db.Text, nullable=True, default=DEFAULT_INITIAL_MESSAGE)
    prompt_role = db.Column(db.Text, nullable=True, default=DEFAULT_PROMPT_ROLE)
    prompt_task = db.Column(db.Text, nullable=True, default=DEFAULT_PROMPT_TASK)

    # --- Slack OAuth クレデンシャル (管理画面で入力) ---
    slack_client_id     = db.Column(db.String(128), nullable=True)
    slack_client_secret = db.Column(db.String(128), nullable=True)

    # --- ▼▼▼ ChatHistory とのリレーションシップを追加 ▼▼▼ ---
    # User が削除されたら、関連する ChatHistory も削除されるように cascade を設定
    chat_histories = relationship("ChatHistory", back_populates="user", cascade="all, delete-orphan")
    # --- ▲▲▲ ChatHistory とのリレーションシップを追加 ▲▲▲ ---


    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    # --- 設定値取得用のプロパティ (変更なし) ---
    @property
    def current_ai_name(self): return self.ai_name or DEFAULT_AI_NAME
    @property
    def current_ai_icon_url(self): return self.ai_icon_url or DEFAULT_ICON_URL
    @property
    def current_theme_color_header(self): return self.theme_color_header or DEFAULT_HEADER_COLOR
    @property
    def current_theme_color_user(self): return self.theme_color_user or DEFAULT_USER_COLOR
    @property
    def current_initial_message(self): return self.initial_message or DEFAULT_INITIAL_MESSAGE
    @property
    def current_prompt_role(self): return self.prompt_role or DEFAULT_PROMPT_ROLE
    @property
    def current_prompt_task(self): return self.prompt_task or DEFAULT_PROMPT_TASK

    def __repr__(self):
        return f'<User {self.email}>'


# --- ▼▼▼ ChatHistory モデルを新規追加 ▼▼▼ ---
class ChatHistory(db.Model):
    """チャット履歴を格納するモデル (テーブル)"""
    __tablename__ = 'chat_history' # テーブル名を定義
    id = db.Column(db.Integer, primary_key=True)
    # 外部キー制約: usersテーブルのidカラムを参照
    user_id = db.Column(db.Integer, ForeignKey('users.id'), nullable=False, index=True)
    role = db.Column(db.String(10), nullable=False)  # 'user' または 'assistant'
    content = db.Column(db.Text, nullable=False)
    # タイムスタンプ: レコード作成時に自動的に現在日時を記録
    timestamp = db.Column(DateTime(timezone=True), server_default=func.now())

    # --- User とのリレーションシップを追加 ---
    user = relationship("User", back_populates="chat_histories")

    # --- (オプション) APIトークン使用量を保存する場合 ---
    # prompt_tokens = db.Column(db.Integer, nullable=True)
    # completion_tokens = db.Column(db.Integer, nullable=True)
    # total_tokens = db.Column(db.Integer, nullable=True)

    def __repr__(self):
        return f'<ChatHistory {self.id} user={self.user_id} role={self.role}>'
# --- ▲▲▲ ChatHistory モデルを新規追加 ▲▲▲ ---

# --- ▼▼▼ WatchedSheet モデルを追加 (Drive Push 通知管理) ▼▼▼ ---
class WatchedSheet(db.Model):
    """Drive Push Notification 用にウォッチ中のシート情報を保持"""
    __tablename__ = "watched_sheets"
    id            = db.Column(db.Integer, primary_key=True)
    file_id       = db.Column(db.String(128), unique=True, nullable=False)   # Drive File ID
    channel_id    = db.Column(db.String(128), nullable=False)                # Google が発行
    resource_id   = db.Column(db.String(128), nullable=False)                # Google が発行
    expiration_ms = db.Column(db.BigInteger, nullable=False)                 # 失効エポック ms

    user_id       = db.Column(db.Integer, ForeignKey("users.id"), nullable=False, index=True)
    user          = relationship("User", backref="watched_sheets")

    def __repr__(self):
        return f"<WatchedSheet file={self.file_id} expires={self.expiration_ms}>"
# --- ▲▲▲ WatchedSheet モデルを追加 ▲▲▲ ---

# --- ▼▼▼ SlackWorkspace モデルを追加 (Slack 連携用) ▼▼▼ ---
class SlackWorkspace(db.Model):
    """Slack ワークスペースと Bot トークンを保持"""
    __tablename__ = "slack_workspaces"

    id           = db.Column(db.Integer, primary_key=True)
    team_id      = db.Column(db.String(32), unique=True, nullable=False)   # T01234567
    team_name    = db.Column(db.String(128), nullable=True)
    bot_token    = db.Column(db.String(256), nullable=False)               # xoxb-...
    installed_at = db.Column(DateTime(timezone=True), server_default=func.now())

    installed_by = db.Column(db.Integer, ForeignKey('users.id'), nullable=False, index=True)
    installer    = relationship("User", backref="slack_workspaces")

    def __repr__(self):
        return f"<SlackWorkspace {self.team_name} ({self.team_id})>"
# --- ▲▲▲ SlackWorkspace モデルを追加 ▲▲▲ ---

# --- ▼▼▼ Source モデル (ドキュメントや URL ソース管理) ▼▼▼ ---
class Source(db.Model):
    """
    ナレッジの元となるファイル / URL / Google シート等を保持する汎用テーブル。
    name には 'gsheet:<fileId>' や 'https://example.com/foo.pdf' などを保存する。
    """
    __tablename__ = "sources"

    id        = db.Column(db.Integer, primary_key=True)
    name      = db.Column(db.String(255), unique=True, nullable=False, index=True)
    created_at = db.Column(DateTime(timezone=True), server_default=func.now())

    # 紐付くユーザー
    user_id   = db.Column(db.Integer, ForeignKey("users.id"), nullable=False, index=True)
    user      = relationship("User", backref="sources")

    def __repr__(self):
        return f"<Source {self.name} user={self.user_id}>"
# --- ▲▲▲ Source モデル追加 ▲▲▲ ---

# --- ▼▼▼ SlackIntegration モデル (ユーザー別 OAuth クレデンシャル) ▼▼▼ ---
class SlackIntegration(db.Model):
    """
    管理画面で入力した Client ID / Secret と、
    OAuth フローで取得する Bot Token・Workspace 情報を保持
    """
    __tablename__ = "slack_integrations"

    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, ForeignKey("users.id"), nullable=False, index=True)
    client_id     = db.Column(db.String(128), nullable=False)
    client_secret = db.Column(db.String(256), nullable=False)
    # 一時的な CSRF 検証用ステート値
    oauth_state   = db.Column(db.String(64), nullable=True)

    bot_token     = db.Column(db.String(256), nullable=True)   # xoxb-...
    team_id       = db.Column(db.String(32), nullable=True)    # T01234567
    updated_at    = db.Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", backref="slack_integration")   # one‑to‑one 想定

    def __repr__(self):
        return f"<SlackIntegration user={self.user_id} team={self.team_id}>"
# --- ▲▲▲ SlackIntegration モデル追加 ▲▲▲ ---