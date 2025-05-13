# 1. 軽量な公式 Python イメージ
FROM python:3.12-slim

# アーキテクチャ自動判定用
ARG ARCH=$(dpkg --print-architecture)

# 2. ログを即時フラッシュ（便利）
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# 3. OS 依存パッケージ (PostgreSQLドライバ用など)
#    + ChromeDriver と Google Chrome に必要なライブラリを追加
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
      gcc \
      # ChromeDriver と Google Chrome に必要なライブラリ
      wget \
      unzip \
      jq \
      gnupg \
      libglib2.0-0 \
      libnss3 \
      libnspr4 \
      libdbus-1-3 \
      libatk1.0-0 \
      libatk-bridge2.0-0 \
      libcups2 \
      libdrm2 \
      libgtk-3-0 \
      libxss1 \
      libasound2 \
      lsb-release \
      xdg-utils && \
    rm -rf /var/lib/apt/lists/*

# 4. Google Chrome のインストール (アーキテクチャ自動判定)
RUN CHROME_DEB="google-chrome-stable_current_${ARCH}.deb" && \
    wget -q "https://dl.google.com/linux/direct/${CHROME_DEB}" -P /tmp && \
    apt-get update && apt-get install -y --no-install-recommends /tmp/${CHROME_DEB} && \
    rm /tmp/${CHROME_DEB} && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 5. ChromeDriver のインストール (アーキテクチャ自動判定)
RUN LATEST_STABLE_CFT_JSON=$(wget -qO- https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json) && \
    if [ "$ARCH" = "amd64" ]; then PLATFORM="linux64"; else PLATFORM="linux-arm64"; fi && \
    CHROMEDRIVER_URL=$(echo "$LATEST_STABLE_CFT_JSON" | jq -r ".channels.Stable.downloads.chromedriver[] | select(.platform==\"$PLATFORM\") | .url") && \
    wget -q -O /tmp/chromedriver.zip "$CHROMEDRIVER_URL" && \
    unzip -q /tmp/chromedriver.zip -d /tmp && \
    mv /tmp/*/chromedriver /usr/local/bin/chromedriver && \
    chmod +x /usr/local/bin/chromedriver && \
    rm /tmp/chromedriver.zip

# 6. 作業ディレクトリ
WORKDIR /app

# 7. 依存ライブラリを先にコピー → キャッシュ効率UP
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    # ビルド後に不要なパッケージを削除 (build-essential, gcc はPythonライブラリのビルドに必要だったため、ここでの削除は適切)
    apt-get purge -y --auto-remove build-essential gcc && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 8. アプリのソースをコピー
COPY . /app

# 9. ポート指定
EXPOSE 8000

# 10. gunicorn をデフォルト起動（ポート 8000）
ENV FLASK_APP=backend.main PYTHONPATH=/app
CMD ["gunicorn", "backend.main:app", "-k", "gthread", "-w", "4", "-b", "0.0.0.0:8000"]