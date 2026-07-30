FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# pyrofork enables the MTProto path, which is the only route for files above the
# Bot API's 50 MB limit — for a media vault, most videos.
#
# tgcrypto is not optional in practice. It is a C extension, and without it
# pyrofork falls back to pure-Python AES: a 60 MB upload then takes well over
# ten minutes and times out the caller, which is indistinguishable from being
# broken. The toolchain is installed only to build the wheel and purged in the
# same layer, so the runtime image keeps the compiled .so and not the compiler.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && pip install --no-cache-dir -r requirements.txt pyrofork tgcrypto \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 8099

ENV BOT_TOKEN="" \
    CHANNEL_ID="" \
    TG_STORE_DB="/app/data/tg_media_store.db"

CMD ["tg-media-store", "serve", "--host", "0.0.0.0", "--port", "8099"]
