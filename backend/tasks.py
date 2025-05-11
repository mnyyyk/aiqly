from backend.celery_app import celery_app
import logging
logger = logging.getLogger(__name__)
from backend import models
from backend.extensions import db
import re
from slack_sdk import WebClient

@celery_app.task
def add(x, y):
    result = x + y
    # logger と print の両方でログを出す
    logger.info(f"======= TASK ADD EXECUTED: {x} + {y} = {result} =======")
    print(f"======= PRINT TASK ADD EXECUTED: {x} + {y} = {result} =======")
    return result

def _lazy_ingest_google_sheet(file_id: str, user_id: int):
    """
    遅延 import で循環参照を回避して Google シートを再インデックスする。
    main.py から直接 import せず、関数を呼ぶ直前で解決する。
    """
    from backend.main import ingest_google_sheet  # local import to break circular dep
    return ingest_google_sheet(file_id, user_id)

@celery_app.task
def update_google_sheet_sources(file_id=None, user_id=None):
    logger.info(f"======= UPDATE_GOOGLE_SHEET_SOURCES RECEIVED - file_id: {file_id}, user_id: {user_id} =======")
    print(f"======= PRINT UPDATE_GOOGLE_SHEET_SOURCES RECEIVED - file_id: {file_id}, user_id: {user_id} =======")
    sheets = db.session.scalars(
        db.select(models.Source).filter(models.Source.name.like("gsheet:%"))
    )
    for src in sheets:
        file_id = src.name.split(":", 1)[1]
        _lazy_ingest_google_sheet(file_id, src.user_id)

@celery_app.task
def handle_slack_event(body):
    logger.info(f"======= HANDLE_SLACK_EVENT RECEIVED (first 100): {str(body)[:100]} =======")
    print(f"======= PRINT HANDLE_SLACK_EVENT RECEIVED (first 100): {str(body)[:100]} =======")
    try:
        from backend.main import app as flask_app          # noqa: WPS433
        from backend.extensions import db                  # noqa: WPS433
        from backend.models import SlackIntegration        # noqa: WPS433
        from backend.services.chat import answer_question  # noqa: WPS433
        from slack_sdk import WebClient                    # noqa: WPS433

        with flask_app.app_context():
            event      = body.get("event", {})
            team_id    = body.get("team_id")
            channel_id = event.get("channel")
            user_text  = event.get("clean_text", "").strip()

            if not (team_id and channel_id and user_text):
                return

            log_prefix = "======= HANDLE_SLACK_EVENT ======="
            integ = db.session.scalars(
                db.select(SlackIntegration).filter_by(team_id=team_id)
            ).first()
            if not integ or not integ.bot_token:
                logger.error(f"{log_prefix} SlackIntegration not found or no bot token for team_id: {team_id}")
                return

            # ★★★ このログで実際に使われるトークンを確認 ★★★
            logger.info(f"{log_prefix} USING BOT TOKEN (tasks.py): First 5: {integ.bot_token[:5]}, Last 5: {integ.bot_token[-5:]}, Length: {len(integ.bot_token)}")
            print(f"{log_prefix} PRINT USING BOT TOKEN (tasks.py): First 5: {integ.bot_token[:5]}, Last 5: {integ.bot_token[-5:]}, Length: {len(integ.bot_token)}")

            answer = answer_question(user_text, integ.user_id, [])
            client = WebClient(token=integ.bot_token)
            client.chat_postMessage(channel=channel_id, text=answer)

        logger.info("======= Slack event processed successfully. =======")
        print("======= PRINT Slack event processed successfully. =======")
    except Exception as e:
        logger.error(f"======= ERROR processing slack event: {e} =======", exc_info=True)
        print(f"======= PRINT ERROR processing slack event: {e} =======")
        raise