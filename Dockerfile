FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg нужен для mp3-постпроцессинга, сжатия и конвертации обложек;
# ca-certificates — для HTTPS-запросов yt-dlp/gallery-dl.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data /tmp/media_saver_bot

# Все зависимости уже установлены на этапе сборки: при старте не нужен доступ к сети.
CMD ["python", "bot.py"]
