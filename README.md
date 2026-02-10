# 🔐 TeleVault

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-green.svg)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-45%20passing-brightgreen.svg)](#development)

**Turn any Telegram channel into free, unlimited cloud storage.**

Upload **any file** up to 2 GB — documents, code, archives, media, anything. Stream on demand. Browse with a sleek dark-mode dashboard. Mount as a local drive. Zero files stored locally — everything lives on Telegram.

<p align="center">
  <img src="https://img.shields.io/badge/storage-unlimited-blueviolet?style=for-the-badge" />
  <img src="https://img.shields.io/badge/cost-$0-success?style=for-the-badge" />
  <img src="https://img.shields.io/badge/max%20file-2%20GB-informational?style=for-the-badge" />
</p>

---

## Why?

Telegram gives every user **unlimited cloud storage** through channels. Files up to 2 GB (4 GB for Premium), stored forever, accessible anywhere. This project wraps that into a proper file management system:

- 🚀 **Upload** any file via CLI, Python API, or drag & drop in the web UI
- 🎬 **Stream** video with seeking — no download required
- 📁 **Browse** everything in a dashboard with search, filters, and albums
- 📄 **Store anything** — documents, spreadsheets, code, backups, ISOs, archives
- 🗂️ **Mount** as a local filesystem (FUSE) or network drive (WebDAV)
- 🔒 **Nothing stored locally** — only a tiny SQLite index + optional thumbnail cache

---

## Architecture

```
                           ┌─────────────────────┐
  Upload                   │   Telegram Cloud     │   Stream / Download
  ──────────────────────▶  │   (free, unlimited)  │  ◀──────────────────
  CLI / API / Web UI       │   up to 2 GB/file    │  Dashboard / FUSE / WebDAV
                           └──────────┬───────────┘
                                      │
                              file_id reference
                                      │
                           ┌──────────▼───────────┐
                           │   SQLite Index        │
                           │   ~1 KB per file      │
                           │   SHA-256 dedup       │
                           └──────────────────────-┘
```

**What's stored locally:** Only the SQLite index (~1 KB per file) and an optional thumbnail cache. All actual files are stored on and streamed from Telegram.

---

## Quick Start

### 1. Create a Telegram Bot & Channel

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → save the **BOT_TOKEN**
2. Create a **private channel** in Telegram
3. Add your bot as an **admin** to the channel
4. Get the channel ID (forward a channel message to [@userinfobot](https://t.me/userinfobot))

### 2. Install

```bash
# Core (CLI + upload/download)
pip install telegram-media-store

# With web dashboard
pip install telegram-media-store[server]

# Everything (dashboard + FUSE + WebDAV + large file support)
pip install telegram-media-store[all]
```

Or from source:

```bash
git clone https://github.com/Kylianmgt/televault.git
cd telegram-media-store
pip install -e ".[all]"
```

### 3. Configure

Create a `.env` file in the project root — it's loaded automatically:

```env
BOT_TOKEN=123456:ABC-DEF...
CHANNEL_ID=-1001234567890
```

That's it. No `export`, no `source` — just works.

### 4. Go

```bash
# Upload a single file (any type)
tg-media-store upload photo.jpg
tg-media-store upload report.pdf
tg-media-store upload backup.tar.gz

# Upload an entire directory (all files)
tg-media-store upload ~/Documents/project/

# Start the dashboard
tg-media-store serve
# → http://localhost:8099
```

---

## 🖥️ Web Dashboard (TeleVault)

A dark-mode dashboard with everything you need:

- **Search** across all files
- **Filter** by type — Images / Videos / Documents / Other
- **Grid & list views** — thumbnails for media, icons for documents
- **Upload** any file via drag & drop or file picker
- **Stream** video with full seeking support
- **Download** documents and archives directly
- **Lightbox** viewer with keyboard navigation (← → Esc)
- **Stats** at a glance — total files, size, albums

```bash
tg-media-store serve --host 0.0.0.0 --port 8099
```

---

## 📂 What Can You Store?

Literally anything that fits in 2 GB:

| Category | Examples |
|----------|---------|
| **Documents** | PDF, Word, Excel, PowerPoint, CSV, TXT, Markdown |
| **Code & Config** | .py, .js, .go, .rs, .json, .yaml, .toml, .sql |
| **Archives** | .zip, .tar.gz, .rar, .7z, .iso |
| **Media** | Images, videos, audio — with streaming & thumbnails |
| **Backups** | Database dumps, disk images, project archives |
| **Everything else** | Any file type — no restrictions |

---

## 🎬 Large File Streaming (Pyrogram)

The Bot API limits downloads to 20 MB. For larger files, add Pyrogram credentials:

```env
TG_API_ID=12345678
TG_API_HASH=abcdef1234567890abcdef1234567890
```

Get these from [my.telegram.org](https://my.telegram.org). With Pyrogram enabled:

- ✅ Stream files of **any size** directly from Telegram
- ✅ **Byte-range requests** for video seeking
- ✅ Upload files up to **2 GB** via MTProto

---

## 🐍 Python API

```python
from tg_media_store import TelegramMediaStore

store = TelegramMediaStore(
    bot_token="123456:ABC-DEF...",
    channel_id="-1001234567890",
)

# Upload any file with SHA-256 dedup
result = store.upload_file("report.pdf")
# → {"id": 1, "file_id": "BQA...", "message_id": 42}

# Batch upload a directory (all files, no extension filter)
stats = store.upload_directory("~/backups/")
# → {"uploaded": 15, "skipped": 3, "failed": 0}

# Organize into albums
album_id = store.get_or_create_album("Project Backups")
store.add_to_album(album_id, result["id"])

# Query
assets = store.list_assets(limit=10, album="Project Backups")

# Download on demand
path = store.fetch_asset(asset_id=1)

store.close()
```

---

## 🐳 Docker

```bash
cp .env.example .env
# Fill in BOT_TOKEN and CHANNEL_ID

docker compose up -d
# Dashboard at http://localhost:8099
```

---

## 📁 FUSE Filesystem

Mount your vault as a read-only local directory. Files are fetched on-demand from Telegram — zero local storage.

```bash
pip install telegram-media-store[fuse]
python -m tg_media_store.fuse_mount /mnt/vault
```

Features:
- LRU in-memory cache (configurable via `FUSE_CACHE_MB`, default 200 MB)
- Auto-fallback to Pyrogram for files > 20 MB
- Directory structure organized by albums/source

---

## 🌐 WebDAV Gateway

Mount your vault in Finder, Explorer, or any WebDAV client:

```bash
pip install telegram-media-store[webdav]
python -m tg_media_store.webdav --host 0.0.0.0 --port 8100
# Mount: http://localhost:8100/dav/
```

---

## ⚙️ Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Telegram Bot API token |
| `CHANNEL_ID` | ✅ | Channel/chat ID for storage |
| `TG_API_ID` | Optional | Pyrogram API ID (for files > 20 MB) |
| `TG_API_HASH` | Optional | Pyrogram API hash |
| `TG_STORE_DB` | Optional | SQLite path (default: `tg_media_store.db`) |
| `TG_STORE_USER` | Optional | Dashboard basic auth username |
| `TG_STORE_PASS` | Optional | Dashboard basic auth password |
| `TG_STORE_TOKEN` | Optional | Dashboard token auth |
| `FUSE_CACHE_MB` | Optional | FUSE cache size in MB (default: 200) |

---

## 🔐 What's Stored Where?

| Data | Location | Deletable? |
|------|----------|------------|
| **All files** | Telegram cloud | Always recoverable from channel |
| **SQLite index** | Local (`tg_media_store.db`) | Yes — re-scan channel to rebuild |
| **Thumbnails** | Local (`cache/thumbs/`) | Yes — regenerated on demand |
| **Pyrogram sessions** | `/tmp/pyro_sessions/` | Yes — auto-recreated |

**No files are ever stored permanently on disk.** Upload temp files are deleted immediately after upload. Streaming goes directly from Telegram to the client.

---

## Limits

| Limit | Details |
|-------|---------|
| **2 GB per file** | Telegram max (4 GB for Premium accounts) |
| **20 MB download via Bot API** | Use Pyrogram for larger — fully supported |
| **~20 files/min upload** | Built-in rate limiting handles this automatically |
| **No E2E encryption** | Files stored as-is on Telegram servers |

---

## Development

```bash
git clone https://github.com/Kylianmgt/televault.git
cd telegram-media-store
pip install -e ".[dev,all]"

# Run tests (no API keys needed)
pytest tests/ -v
```

---

## License

[MIT](LICENSE) — do whatever you want with it.
