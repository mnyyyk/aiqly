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
    broker_connection_retry_on_startup=True, 
)

celery_app.conf.task_default_queue = "default"

# 5 分に 1 回の Beat スケジュール
celery_app.conf.beat_schedule = {
    "update-google-sheets-every-5min": {
        "task": "backend.tasks.update_google_sheet_sources",
        "schedule": timedelta(minutes=5),
    }
}

print("="*80)
print("CELERY APP INITIALIZED. REGISTERED TASKS:")
if celery_app.tasks:  # tasks属性が存在するか確認
    for task_name in celery_app.tasks.keys():
        print(f"  - Registered Task: {task_name}")
else:
    print("  - No tasks seem to be registered with this Celery app instance.")
print(f"  - Celery App Name: {celery_app.main}")
print(f"  - Included Modules: {celery_app.conf.include}")
print(f"  - Broker URL: {celery_app.conf.broker_url}")
print("="*80)

# === Celery worker signals hook ===
from celery.signals import worker_ready, worker_shutdown
import logging  # loggingをインポート

logger = logging.getLogger(__name__)  # ロガーを取得

@worker_ready.connect
def worker_ready_handler(sender, **kwargs):
    msg = f"====== WORKER READY SIGNAL: Worker {getattr(sender, 'hostname', 'N/A')} is ready. ======"  # getattrで安全にアクセス
    print(msg)
    logger.info(msg)
    queues_info = getattr(sender.app.amqp, 'queues', {})  # 安全にアクセス
    print(f"====== Worker {getattr(sender, 'hostname', 'N/A')} - Queues: {queues_info} ======")
    logger.info(f"====== Worker {getattr(sender, 'hostname', 'N/A')} - Queues: {queues_info} ======")
    # senderオブジェクトの内容を確認するために、他の属性も試してみる (デバッグ用)
    # print(f"====== Worker Sender Object Type: {type(sender)} ======")
    # print(f"====== Worker Sender Object Dir: {dir(sender)} ======")

@worker_shutdown.connect
def worker_shutdown_handler(sender, **kwargs):
    msg = f"====== WORKER SHUTDOWN SIGNAL: Worker {sender.hostname} is shutting down. ======"
    print(msg)
    logger.info(msg)

print("="*80)
print("CELERY APP SCRIPT END. Worker signals connected.")
print("="*80)