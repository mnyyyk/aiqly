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
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    set -eux; \
    echo "Modifying sources.list files if they exist..."; \
    # /etc/apt/sources.list.d/debian.sources が存在すれば編集
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        echo "Modifying /etc/apt/sources.list.d/debian.sources"; \
        sed -i 's/^\(deb.* main\)$/\1 contrib non-free non-free-firmware/g; s/^\(deb-src.* main\)$/\1 contrib non-free non-free-firmware/g' /etc/apt/sources.list.d/debian.sources; \
        cat /etc/apt/sources.list.d/debian.sources; \
    else \
        echo "/etc/apt/sources.list.d/debian.sources not found."; \
    fi; \
    # /etc/apt/sources.list が存在すれば編集 (フォールバック)
    if [ -f /etc/apt/sources.list ]; then \
        echo "Modifying /etc/apt/sources.list"; \
        sed -i 's/^\(deb.* main\)$/\1 contrib non-free non-free-firmware/g; s/^\(deb-src.* main\)$/\1 contrib non-free non-free-firmware/g' /etc/apt/sources.list; \
        cat /etc/apt/sources.list; \
    else \
        echo "/etc/apt/sources.list not found."; \
    fi; \
    \
    apt-get update; \
    echo "Installing core build packages..."; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
      gcc; \
    echo "Installing utility packages..."; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      wget \
      unzip \
      jq \
      gnupg \
      ca-certificates; \
    echo "Core packages installed."; \
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