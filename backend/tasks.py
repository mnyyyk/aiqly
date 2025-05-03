from backend.celery_app import celery_app
from backend import models
from backend.extensions import db
import re
from slack_sdk import WebClient

def _lazy_ingest_google_sheet(file_id: str, user_id: int):
    """
    遅延 import で循環参照を回避して Google シートを再インデックスする。
    main.py から直接 import せず、関数を呼ぶ直前で解決する。
    """
    from backend.main import ingest_google_sheet  # local import to break circular dep
    return ingest_google_sheet(file_id, user_id)

@celery_app.task
def update_google_sheet_sources():
    """
    DB にある 'gsheet:<fileId>' のソースを取得し直し、
    変更があれば retriever へ再登録 → インデックス更新。
    """
    # 1. gsheet:* ソースを列挙
    sheets = db.session.scalars(
        db.select(models.Source).filter(models.Source.name.like("gsheet:%"))
    )
    for src in sheets:
        file_id = src.name.split(":", 1)[1]
        _lazy_ingest_google_sheet(file_id, src.user_id)

@celery_app.task
def handle_slack_event(event_body: dict):
    """
    Slack イベントを非同期で処理するタスク。
    Flask アプリケーションコンテキストを明示的にプッシュして
    DB・OpenAI・Slack へ安全にアクセスする。
    """
    # 遅延 importで循環参照を防止
    from backend.main import app as flask_app          # noqa: WPS433
    from backend.extensions import db                  # noqa: WPS433
    from backend.models import SlackIntegration        # noqa: WPS433
    from backend.services.chat import answer_question  # noqa: WPS433
    from slack_sdk import WebClient                    # noqa: WPS433

    with flask_app.app_context():
        event      = event_body.get("event", {})
        team_id    = event_body.get("team_id")
        channel_id = event.get("channel")
        user_text  = event.get("clean_text", "").strip()

        if not (team_id and channel_id and user_text):
            return  # 必須データ不足

        integ = db.session.scalars(
            db.select(SlackIntegration).filter_by(team_id=team_id)
        ).first()
        if not integ or not integ.bot_token:
            return  # 未設定

        # OpenAI で回答生成
        answer = answer_question(user_text, integ.user_id)

        # Slack へ返信
        client = WebClient(token=integ.bot_token)
        try:
            client.chat_postMessage(channel=channel_id, text=answer)
        except Exception as exc:
            print(f"[handle_slack_event] Failed to post: {exc}")