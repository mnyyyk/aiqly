import os
from celery import Celery
from datetime import timedelta

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "chachat",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["backend.tasks"]   # tasks.py を読み込む
)

# Flask の設定を Celery に流用したい場合は
celery_app.conf.update(
    timezone="Asia/Tokyo",
    enable_utc=False,
)

# 5 分に 1 回の Beat スケジュール
celery_app.conf.beat_schedule = {
    "update-google-sheets-every-5min": {
        "task": "backend.tasks.update_google_sheet_sources",
        "schedule": timedelta(minutes=5),
    }
}