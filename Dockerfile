# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

ARG ARCH
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# ステップ3 修正案 (sources.list に contrib non-free を追加する例)
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    set -eux; \
    # sources.list を変更して contrib と non-free を追加 (Debian Bookwormの場合)
    # 注意: non-free を追加することはライセンスポリシーに影響する可能性があります。
    #       必要なパッケージが本当に non-free にしかないか確認してください。
    #       多くの場合、main と contrib で十分です。
    sed -i 's/main/main contrib non-free non-free-firmware/g' /etc/apt/sources.list.d/debian.sources || \
    sed -i 's/bookworm main/bookworm main contrib non-free non-free-firmware/g' /etc/apt/sources.list || \
    echo "Failed to modify sources.list, proceeding with default." ; \
    cat /etc/apt/sources.list.d/debian.sources || cat /etc/apt/sources.list || echo "No sources.list found to display"; \
    \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
      gcc \
      wget \
      unzip \
      jq \
      gnupg \
      ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# 4. Google Chrome / Chromium ＋ ChromeDriver インストール
#
#   * amd64  : Google Chrome Stable ＋ 同バージョン ChromeDriver (CfT)
#   * arm64  : Debian 公式 chromium ＋ chromium-driver (または CfT の arm64 ChromeDriver)
#
RUN set -e; \
    ARCH="$(dpkg --print-architecture)"; \
    echo "Detected architecture: ${ARCH}"; \
    # 必要なライブラリをChrome/Chromiumインストール前に再度updateしてインストール試行
    # これにより、Chrome/Chromiumの依存関係を解決しやすくなる
    apt-get update && \
    apt-get install -y --no-install-recommends \
        libnss3 \
        libglib2.0-0 \
        # 他にエラーが出るようなら、ここに追加していく
        lsb-release \
        xdg-utils \
    || echo "Some optional libraries might not be available, continuing chrome/chromium installation." ; \
    # || true; # エラーが出ても続行させる場合（非推奨だが、slimイメージで特定ライブラリがない場合の一時しのぎ）

    if [ "${ARCH}" = "amd64" ]; then \
        CHROME_PLATFORM="linux64"; \
        CHROME_DEB="google-chrome-stable_current_amd64.deb"; \
        echo "Installing Google Chrome and ChromeDriver for amd64..."; \
        wget -q "https://dl.google.com/linux/direct/${CHROME_DEB}" -P /tmp; \
        # apt-get update; # この直前でupdate済み
        # dpkgでインストールし、依存関係の問題があれば -f installで修正
        dpkg -i /tmp/${CHROME_DEB} || apt-get -f install -y --no-install-recommends; \
        rm /tmp/${CHROME_DEB}; \
        \
        LATEST_JSON=$(wget -qO- https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json); \
        CHROMEDRIVER_URL=$(echo "${LATEST_JSON}" | jq -r ".channels.Stable.downloads.chromedriver[] | select(.platform==\"${CHROME_PLATFORM}\") | .url"); \
        if [ -z "${CHROMEDRIVER_URL}" ]; then echo "Failed to obtain ChromeDriver URL for amd64"; exit 1; fi; \
        wget -q -O /tmp/chromedriver.zip "${CHROMEDRIVER_URL}"; \
        unzip -q /tmp/chromedriver.zip -d /tmp; \
        # CfTのzipは chromedriver-linux64/chromedriver のように展開されることが多い
        if [ -f "/tmp/chromedriver-${CHROME_PLATFORM}/chromedriver" ]; then \
            mv "/tmp/chromedriver-${CHROME_PLATFORM}/chromedriver" /usr/local/bin/chromedriver; \
            rm -rf "/tmp/chromedriver-${CHROME_PLATFORM}"; \
        else \
            echo "ChromeDriver binary not found in /tmp/chromedriver-${CHROME_PLATFORM}/ after unzipping (amd64)."; ls -lR /tmp; exit 1; \
        fi; \
        chmod +x /usr/local/bin/chromedriver; \
        rm -rf /tmp/chromedriver.zip; \
    elif [ "${ARCH}" = "arm64" ]; then \
        echo "Installing Chromium and chromium-driver for arm64..."; \
        # apt-get update; # この直前でupdate済み
        apt-get install -y --no-install-recommends chromium chromium-driver; \
        # chromium-driver パッケージが /usr/bin/chromedriver を提供することが多い
        if [ -f /usr/bin/chromedriver ]; then \
            ln -sf /usr/bin/chromedriver /usr/local/bin/chromedriver; \
            echo "Linked /usr/bin/chromedriver to /usr/local/bin/chromedriver for arm64"; \
        elif [ -f /usr/lib/chromium-driver/chromedriver ]; then \
            ln -sf /usr/lib/chromium-driver/chromedriver /usr/local/bin/chromedriver; \
            echo "Linked /usr/lib/chromium-driver/chromedriver to /usr/local/bin/chromedriver for arm64"; \
        else \
            echo "Chromium WebDriver not found in expected locations for arm64. Trying CfT for arm64..."; \
            # CfT の arm64 ChromeDriver を試す (chromium-driver が見つからない場合のフォールバック)
            CHROME_PLATFORM_ARM="linux-arm64"; \
            LATEST_JSON_ARM=$(wget -qO- https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json); \
            CHROMEDRIVER_URL_ARM=$(echo "${LATEST_JSON_ARM}" | jq -r ".channels.Stable.downloads.chromedriver[] | select(.platform==\"${CHROME_PLATFORM_ARM}\") | .url"); \
            if [ -z "${CHROMEDRIVER_URL_ARM}" ]; then echo "Failed to obtain ChromeDriver URL for arm64 from CfT"; exit 1; fi; \
            wget -q -O /tmp/chromedriver_arm.zip "${CHROMEDRIVER_URL_ARM}"; \
            unzip -q /tmp/chromedriver_arm.zip -d /tmp; \
            if [ -f "/tmp/chromedriver-${CHROME_PLATFORM_ARM}/chromedriver" ]; then \
                mv "/tmp/chromedriver-${CHROME_PLATFORM_ARM}/chromedriver" /usr/local/bin/chromedriver; \
                rm -rf "/tmp/chromedriver-${CHROME_PLATFORM_ARM}"; \
            else \
                echo "ChromeDriver binary not found in /tmp/chromedriver-${CHROME_PLATFORM_ARM}/ after unzipping (arm64 CfT)."; ls -lR /tmp; exit 1; \
            fi; \
            chmod +x /usr/local/bin/chromedriver; \
            rm -rf /tmp/chromedriver_arm.zip; \
        fi; \
        # google-chrome という名前でchromiumを呼び出せるようにシンボリックリンク (オプション)
        if [ -f /usr/bin/chromium ]; then \
             ln -sf /usr/bin/chromium /usr/local/bin/google-chrome; \
        fi; \
    else \
        echo "Unsupported architecture: ${ARCH}"; exit 1; \
    fi; \
    # --- バージョン確認 ---
    if [ -x /usr/local/bin/google-chrome ]; then google-chrome --version; elif [ -x /usr/bin/chromium ]; then chromium --version; else echo "Chrome/Chromium not found for version check."; fi; \
    if [ -x /usr/local/bin/chromedriver ]; then chromedriver --version; else echo "ChromeDriver not found for version check."; fi; \
    # キャッシュクリア
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# 6. 作業ディレクトリ
WORKDIR /app

# 7. 依存ライブラリを先にコピー → キャッシュ効率UP
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    # ビルド後に不要なパッケージを削除
    apt-get purge -y --auto-remove build-essential gcc && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 8. アプリのソースをコピー
COPY . /app

# 9. ポート指定
EXPOSE 8000

# 10. gunicorn をデフォルト起動（ポート 8000）
ENV FLASK_APP=backend.main PYTHONPATH=/app
CMD ["gunicorn", "backend.main:app", "-k", "gthread", "-w", "4", "-b", "0.0.0.0:8000"]