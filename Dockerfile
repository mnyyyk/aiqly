# 1. 軽量な公式 Python イメージ
FROM python:3.12-slim

# 2. ログを即時フラッシュ（便利）
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# 3. OS 依存パッケージ (PostgreSQLドライバ用など)
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# 4. 作業ディレクトリ
WORKDIR /app

# 5. 依存ライブラリを先にコピー → キャッシュ効率UP
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y build-essential gcc && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# 6. アプリのソースをコピー
COPY . /app
EXPOSE 8000

# 7. gunicorn をデフォルト起動（ポート 8000）
ENV FLASK_APP=backend.main PYTHONPATH=/app
CMD ["gunicorn", "backend.main:app", "-k", "gthread", "-w", "4", "-b", "0.0.0.0:8000"]
