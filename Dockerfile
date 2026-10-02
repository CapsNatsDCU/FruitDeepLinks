# Dockerfile for FruitDeepLinks
# Multi-source sports event aggregator (Apple TV + partners)
# Uses Debian Chromium for cross-platform compatibility (arm64 + amd64)

FROM python:3.11-slim-bookworm

ARG FDL_BUILD_REVISION=unknown
ENV FDL_BUILD_REVISION=${FDL_BUILD_REVISION}
LABEL org.opencontainers.image.revision=${FDL_BUILD_REVISION}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# --- System deps ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    sqlite3 \
    ca-certificates \
    ffmpeg \
    fonts-liberation \
    fonts-dejavu \
    fonts-noto-color-emoji \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- Install Chromium + ChromeDriver for Selenium (native arm64 + amd64 support) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- App code ---
COPY bin ./bin
COPY templates ./templates
COPY VERSION .
# COPY static ./static

# Ensure runtime dirs exist
RUN mkdir -p /app/data /app/out /app/logs

# Start the web server. APScheduler, configured from the dashboard, owns the
# daily refresh schedule inside this process.
RUN printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -e' \
  '' \
  '# Ensure runtime directories exist' \
  'mkdir -p /app/data /app/out /app/logs' \
  '# Start FruitDeepLinks web server' \
  'cd /app' \
  'exec python3 -u /app/bin/fruitdeeplinks_v2.py' \
  > /app/start.sh \
  && chmod +x /app/start.sh

# Health check - verify web server is responding
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:6655/health || exit 1

# Persistent volumes
VOLUME ["/app/data", "/app/out", "/app/logs"]

EXPOSE 6655
CMD ["/app/start.sh"]
