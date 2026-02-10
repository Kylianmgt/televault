FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 8099

ENV BOT_TOKEN="" \
    CHANNEL_ID="" \
    TG_STORE_DB="/app/data/tg_media_store.db"

CMD ["tg-media-store", "serve", "--host", "0.0.0.0", "--port", "8099"]
