FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# pyrofork enables the MTProto path. Without it the Bot API caps transfers at
# 50 MB, so anything larger cannot be stored — which for a media vault is most
# videos. Only useful when TG_API_ID/TG_API_HASH are set.
#
# tgcrypto is deliberately NOT installed: it is a C extension and this image has
# no compiler, so adding it fails the build with "gcc: No such file or
# directory". Pyrofork falls back to pure-Python AES, which is slower but
# correct; adding a toolchain just for faster crypto is not worth the image size.
RUN pip install --no-cache-dir -r requirements.txt pyrofork

COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 8099

ENV BOT_TOKEN="" \
    CHANNEL_ID="" \
    TG_STORE_DB="/app/data/tg_media_store.db"

CMD ["tg-media-store", "serve", "--host", "0.0.0.0", "--port", "8099"]
