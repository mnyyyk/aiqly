# backend/tasks.py
# ============================================================================
# Celery task definitions for Aiqly backend.
# Updated 2025‑06‑15 — Slack メンション応答の安定化 & 詳細ロギング追加
# ============================================================================

from __future__ import annotations

import logging
import re
import time
import traceback
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from backend.celery_app import celery_app
from backend.extensions import db
from backend import models

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# simple demo task
# ---------------------------------------------------------------------------
@celery_app.task
def add(x: int | float, y: int | float) -> int | float:
    """Test task."""
    result = x + y
    logger.info("ADD %s + %s = %s", x, y, result)
    return result


# ---------------------------------------------------------------------------
# Google Sheet re‑ingest (unchanged)
# ---------------------------------------------------------------------------
def _lazy_ingest_google_sheet(file_id: str, user_id: int) -> None:
    """Avoid circular import when calling ingest_google_sheet."""
    from backend.main import ingest_google_sheet  # local import
    ingest_google_sheet(file_id, user_id)


@celery_app.task
def update_google_sheet_sources(file_id: str | None = None, user_id: int | None = None) -> None:
    """Re‑ingest all gsheet:* sources for all users (maintenance)."""
    logger.info("UPDATE_GOOGLE_SHEET_SOURCES – file_id=%s user_id=%s", file_id, user_id)
    sheets = db.session.scalars(
        db.select(models.Source).filter(models.Source.name.like("gsheet:%"))
    )
    for src in sheets:
        fid = src.name.split(":", 1)[1]
        _lazy_ingest_google_sheet(fid, src.user_id)


# ---------------------------------------------------------------------------
# Slack event handler
# ---------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    acks_late=True,              # ★ 失敗時にタスクをキューへ戻す
    max_retries=2,
    soft_time_limit=150,         # ★ ハング検出
    time_limit=180,
)
def handle_slack_event(self, body: dict[str, Any]) -> None:
    """
    Process an `app_mention` or DM event from Slack.

    1. Validate payload & extract user text
    2. Call LLM (backend.services.chat.answer_question)
    3. Post reply back to Slack
    """
    t0 = time.time()
    prefix = "[HANDLE_SLACK_EVENT]"
    logger.info("%s received %s", prefix, str(body)[:120])

    try:
        # --- lazy imports to avoid circular deps --------------------------------
        from backend.main import app as flask_app        # noqa: WPS433
        from backend.models import SlackIntegration      # noqa: WPS433
        from backend.services.chat import answer_question  # noqa: WPS433

        event = body.get("event", {})
        event_type = event.get("type")
        logger.debug("%s event_type=%s", prefix, event_type)
        team_id = body.get("team_id")
        channel_id = event.get("channel")
        # Slack sends <@U123> mention, remove it:
        raw_text = (event.get("clean_text") or event.get("text", "")).strip()
        # Slack のメンション形式 <@UXXXXXX|username> にも対応して削除
        user_text = re.sub(r"<@[^>]+>", "", raw_text).strip()

        if not (team_id and channel_id and user_text):
            logger.warning("%s Missing required fields – skip. team=%s, channel=%s, text_length=%d",
                           prefix, team_id, channel_id, len(user_text))
            return

        with flask_app.app_context():
            integ: SlackIntegration | None = db.session.scalars(
                db.select(SlackIntegration).filter_by(team_id=team_id)
            ).first()

            if not integ or not integ.bot_token:
                logger.error("%s SlackIntegration not found or no bot token for team=%s", prefix, team_id)
                return

            logger.debug("%s Using bot token **%s…%s** (len=%d)",
                         prefix, integ.bot_token[:5], integ.bot_token[-5:], len(integ.bot_token))

            # --- LLM call -------------------------------------------------------
            try:
                answer = answer_question(user_text, integ.user_id, [])
                logger.info("%s answer_question OK (%.2fs)", prefix, time.time() - t0)
            except Exception as llm_err:                                      # pylint: disable=broad-except
                logger.error("%s answer_question failed: %s\n%s",
                             prefix, llm_err, traceback.format_exc())
                answer = "申し訳ありません。現在応答できませんでした。"

            # --- Slack post -----------------------------------------------------
            client = WebClient(token=integ.bot_token)
            thread_ts = event.get("thread_ts") or event.get("ts")

            try:
                resp = client.chat_postMessage(channel=channel_id, text=answer, thread_ts=thread_ts)
                logger.info("%s Slack reply sent (ts=%s)", prefix, resp.data.get("ts"))
            except SlackApiError as api_err:
                logger.error("%s Slack API error: %s – %s",
                             prefix, api_err.response.status_code, api_err.response.get("error"))
                raise self.retry(exc=api_err, countdown=30)
            except Exception as post_err:                                      # pylint: disable=broad-except
                logger.error("%s Unexpected error posting message: %s\n%s",
                             prefix, post_err, traceback.format_exc())
                raise self.retry(exc=post_err, countdown=30)

    except SoftTimeLimitExceeded:
        logger.error("%s Soft time‑limit exceeded – task aborted.", prefix)
        raise
    except Exception as exc:                                                   # pylint: disable=broad-except
        logger.error("%s Unhandled exception: %s\n%s", prefix, exc, traceback.format_exc())
        raise
    finally:
        logger.info("%s done (%.2fs)", prefix, time.time() - t0)