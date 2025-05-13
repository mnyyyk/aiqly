# syntax=docker/dockerfile:1.4
# 1. 軽量な公式 Python イメージ
FROM python:3.12-slim

# アーキテクチャ自動判定用
ARG ARCH=$(dpkg --print-architecture)

# 2. ログを即時フラッシュ（便利）
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# 3. OS 依存パッケージ (PostgreSQLドライバ用など) と
#    Chrome / ChromeDriver 導入に最低限必要なランタイム
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        gcc \
        wget \
        unzip \
        jq \
        curl \
        gnupg \
        ca-certificates \
        libasound2 \
        libnss3 \
        libxss1 \
        libatk-bridge2.0-0 \
        libgbm1 \
        lsb-release \
        xdg-utils; \
    rm -rf /var/lib/apt/lists/*

# 4. Google Chrome / Chromium ＋ ChromeDriver インストール
#
#   * amd64  : Google Chrome Stable ＋ 同バージョン ChromeDriver
#   * arm64  : Debian 公式 chromium ＋ chromium-driver
#
RUN set -e; \
    ARCH="$(dpkg --print-architecture)"; \
    echo "Detected architecture: ${ARCH}"; \
    if [ "${ARCH}" = "amd64" ]; then \
        CHROME_PLATFORM="linux64"; \
        CHROME_DEB="google-chrome-stable_current_amd64.deb"; \
        wget -q "https://dl.google.com/linux/direct/${CHROME_DEB}" -P /tmp; \
        apt-get update; \
        apt-get install -y --no-install-recommends /tmp/${CHROME_DEB} || apt-get -f install -y --no-install-recommends; \
        rm /tmp/${CHROME_DEB}; \
        LATEST_JSON=$(wget -qO- https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json); \
        CHROMEDRIVER_URL=$(echo "${LATEST_JSON}" | jq -r ".channels.Stable.downloads.chromedriver[] | select(.platform==\"${CHROME_PLATFORM}\") | .url"); \
        if [ -z "${CHROMEDRIVER_URL}" ]; then echo "Failed to obtain ChromeDriver URL"; exit 1; fi; \
        wget -q -O /tmp/chromedriver.zip "${CHROMEDRIVER_URL}"; \
        unzip -q /tmp/chromedriver.zip -d /tmp; \
        find /tmp -type f -name chromedriver -exec mv {} /usr/local/bin/chromedriver \;; \
        chmod +x /usr/local/bin/chromedriver; \
        rm -rf /tmp/chromedriver.zip /tmp/chromedriver*; \
    elif [ "${ARCH}" = "arm64" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends chromium chromium-driver; \
        ln -s /usr/bin/chromedriver /usr/local/bin/chromedriver; \
        ln -s /usr/bin/chromium /usr/local/bin/google-chrome || true; \
    else \
        echo "Unsupported architecture: ${ARCH}"; exit 1; \
    fi; \
    # --- バージョン確認 ---
    google-chrome --version || chromium --version; \
    chromedriver --version; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/google-chrome
ENV CHROMEDRIVER_BIN=/usr/local/bin/chromedriver


# 6. 作業ディレクトリ
WORKDIR /app

# 7. 依存ライブラリを先にコピー → キャッシュ効率UP
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --upgrade pip && \
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
# syntax=docker/dockerfile:1.4
# -----------------------------------------------------------------------------
# Multi-arch Python + Chrome/Chromium + ChromeDriver Dockerfile
#   - Supports amd64 (Google Chrome Stable + matching ChromeDriver)
#   - Supports arm64 (Debian chromium + chromium-driver, fallback to CfT)
#   - Installs system and Python dependencies, cleans up after build
#   - For use with gunicorn and Flask app
# -----------------------------------------------------------------------------

FROM python:3.12-slim

# -- Architecture detection early for later use
ARG ARCH
ENV ARCH=${ARCH}

# -- Immediate output for logs, no pip cache
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# -----------------------------------------------------------------------------
# 1. Install system dependencies
# -----------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        gcc \
        wget \
        unzip \
        jq \
        curl \
        gnupg \
        ca-certificates \
        libasound2 \
        libnss3 \
        libxss1 \
        libatk-bridge2.0-0 \
        libgbm1 \
        lsb-release \
        xdg-utils; \
    rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# 2. Chrome/Chromium + ChromeDriver install (arch-aware)
# -----------------------------------------------------------------------------
RUN set -eux; \
    ARCH_DETECTED="${ARCH:-$(dpkg --print-architecture)}"; \
    echo "Detected architecture: ${ARCH_DETECTED}"; \
    if [ "${ARCH_DETECTED}" = "amd64" ]; then \
        # -- Google Chrome Stable & matching ChromeDriver --
        CHROME_PLATFORM="linux64"; \
        CHROME_DEB="google-chrome-stable_current_amd64.deb"; \
        wget -q "https://dl.google.com/linux/direct/${CHROME_DEB}" -P /tmp; \
        apt-get update; \
        apt-get install -y --no-install-recommends /tmp/${CHROME_DEB} || apt-get -f install -y --no-install-recommends; \
        rm /tmp/${CHROME_DEB}; \
        # ChromeDriver from Chrome for Testing (CfT)
        LATEST_JSON=$(wget -qO- https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json); \
        CHROMEDRIVER_URL=$(echo "${LATEST_JSON}" | jq -r ".channels.Stable.downloads.chromedriver[] | select(.platform==\"${CHROME_PLATFORM}\") | .url"); \
        if [ -z "${CHROMEDRIVER_URL}" ]; then echo "Failed to obtain ChromeDriver URL"; exit 1; fi; \
        wget -q -O /tmp/chromedriver.zip "${CHROMEDRIVER_URL}"; \
        unzip -q /tmp/chromedriver.zip -d /tmp; \
        find /tmp -type f -name chromedriver -exec mv {} /usr/local/bin/chromedriver \;; \
        chmod +x /usr/local/bin/chromedriver; \
        rm -rf /tmp/chromedriver.zip /tmp/chromedriver*; \
        # Symlink for compatibility
        ln -sf /usr/bin/google-chrome /usr/local/bin/google-chrome || true; \
    elif [ "${ARCH_DETECTED}" = "arm64" ]; then \
        # -- Try Debian chromium & chromium-driver first --
        apt-get update; \
        if apt-get install -y --no-install-recommends chromium chromium-driver; then \
            ln -sf /usr/bin/chromedriver /usr/local/bin/chromedriver; \
            ln -sf /usr/bin/chromium /usr/local/bin/google-chrome || true; \
        else \
            echo "Debian chromium install failed, falling back to Chrome for Testing..."; \
            # CfT for arm64
            CHROME_PLATFORM="linux-arm64"; \
            LATEST_JSON=$(wget -qO- https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json); \
            CHROME_URL=$(echo "${LATEST_JSON}" | jq -r ".channels.Stable.downloads.chrome[] | select(.platform==\"${CHROME_PLATFORM}\") | .url"); \
            CHROMEDRIVER_URL=$(echo "${LATEST_JSON}" | jq -r ".channels.Stable.downloads.chromedriver[] | select(.platform==\"${CHROME_PLATFORM}\") | .url"); \
            if [ -z "${CHROME_URL}" ] || [ -z "${CHROMEDRIVER_URL}" ]; then echo "Failed to obtain Chrome/ChromeDriver URLs for arm64"; exit 1; fi; \
            wget -q -O /tmp/chrome-arm64.zip "${CHROME_URL}"; \
            wget -q -O /tmp/chromedriver-arm64.zip "${CHROMEDRIVER_URL}"; \
            unzip -q /tmp/chrome-arm64.zip -d /tmp/cft-chrome; \
            unzip -q /tmp/chromedriver-arm64.zip -d /tmp/cft-chromedriver; \
            mv /tmp/cft-chrome/*/chrome /usr/local/bin/google-chrome; \
            chmod +x /usr/local/bin/google-chrome; \
            mv /tmp/cft-chromedriver/*/chromedriver /usr/local/bin/chromedriver; \
            chmod +x /usr/local/bin/chromedriver; \
            rm -rf /tmp/chrome-arm64.zip /tmp/chromedriver-arm64.zip /tmp/cft-chrome /tmp/cft-chromedriver; \
        fi; \
    else \
        echo "Unsupported architecture: ${ARCH_DETECTED}"; exit 1; \
    fi; \
    # -- Version checks --
    (google-chrome --version || chromium --version || /usr/local/bin/google-chrome --version || true); \
    chromedriver --version; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    apt-get autoremove -y

# Standard environment variables for Chrome/Chromedriver
ENV CHROME_BIN=/usr/local/bin/google-chrome
ENV CHROMEDRIVER_BIN=/usr/local/bin/chromedriver

# -----------------------------------------------------------------------------
# 3. Python dependencies (pip install, cache-efficient)
# -----------------------------------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    # Remove build deps after pip install
    apt-get purge -y --auto-remove build-essential gcc && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# 4. Copy app source & expose port
# -----------------------------------------------------------------------------
COPY . /app
EXPOSE 8000

# -----------------------------------------------------------------------------
# 5. Launch via gunicorn (default)
# -----------------------------------------------------------------------------
ENV FLASK_APP=backend.main PYTHONPATH=/app
CMD ["gunicorn", "backend.main:app", "-k", "gthread", "-w", "4", "-b", "0.0.0.0:8000"]