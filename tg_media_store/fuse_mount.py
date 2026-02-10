"""FUSE filesystem that exposes Telegram vault media as local files.

Files appear as real files to any application, but content is streamed
on-demand from Telegram.  Supports both direct SQLite DB access and the
gallery HTTP API for index discovery.

Features ported from the original vault's ``vault_fuse.py``:
- LRU in-memory cache (configurable via ``FUSE_CACHE_MB``, default 200 MB)
- Disk cache for large files (> 50 MB) at ``/tmp/fuse_cache``
- Bot API for files ≤ 20 MB, pyrogram/MTProto for larger files
- Tree-based directory structure (files organized by source/album)
- Background index refresh every 5 minutes
- Proper FUSE operations: getattr, readdir, open, read, statfs

Requires: ``fusepy`` (``pip install fusepy``), FUSE kernel module.

Mount::

    tg-media-store-fuse /mountpoint

Or::

    python -m tg_media_store.fuse_mount /mountpoint
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

try:
    from fuse import FUSE, FuseOSError, Operations
except ImportError:
    raise ImportError("Install fusepy: pip install fusepy")

# Optional pyrogram
try:
    from pyrogram import Client as _PyroClient
    HAS_PYROGRAM = True
except ImportError:
    _PyroClient = None  # type: ignore[assignment,misc]
    HAS_PYROGRAM = False

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg_media_store.fuse")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
DB_PATH = os.environ.get("TG_STORE_DB", "tg_media_store.db")
MAX_CACHE_MB = int(os.environ.get("FUSE_CACHE_MB", "200"))
MAX_CACHE_BYTES = MAX_CACHE_MB * 1024 * 1024
GALLERY_URL = os.environ.get("GALLERY_URL", "")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

DISK_CACHE_DIR = Path("/tmp/fuse_cache")
DISK_CACHE_THRESHOLD = 50 * 1024 * 1024  # Files > 50 MB go to disk cache
BOT_API_LIMIT = 20 * 1024 * 1024  # Bot API max download


class FileCache:
    """Simple LRU in-memory cache for file content."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.cache: Dict[int, bytes] = {}
        self.access_order: list[int] = []
        self.total = 0
        self.lock = threading.Lock()

    def get(self, key: int) -> Optional[bytes]:
        with self.lock:
            data = self.cache.get(key)
            if data is not None:
                if key in self.access_order:
                    self.access_order.remove(key)
                self.access_order.append(key)
            return data

    def put(self, key: int, data: bytes) -> None:
        with self.lock:
            if key in self.cache:
                return
            while self.total + len(data) > self.max_bytes and self.access_order:
                oldest = self.access_order.pop(0)
                if oldest in self.cache:
                    self.total -= len(self.cache[oldest])
                    del self.cache[oldest]
            self.cache[key] = data
            self.access_order.append(key)
            self.total += len(data)


class VaultFS(Operations):  # type: ignore[misc]
    """Read-only FUSE filesystem backed by Telegram media.

    Index source: SQLite DB (direct) or gallery HTTP API (``GALLERY_URL``).
    File download: Bot API (≤ 20 MB) or pyrogram MTProto (> 20 MB).
    """

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.tree: Dict[str, Any] = {}
        self.file_cache = FileCache(MAX_CACHE_BYTES)
        self.index_lock = threading.Lock()
        self._refresh_index()
        t = threading.Thread(target=self._refresh_loop, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _refresh_index(self) -> None:
        """Fetch index from gallery API or SQLite DB and build tree."""
        items: list[dict] = []

        # Try gallery API first (if configured)
        if GALLERY_URL:
            items = self._fetch_from_api()

        # Fall back to SQLite
        if not items:
            items = self._fetch_from_db()

        if not items:
            log.warning("No items found in index")
            return

        tree: Dict[str, Any] = {"/": {"type": "dir", "children": set()}}

        for item in items:
            msg_id = item.get("msg_id")
            if not msg_id:
                continue

            # Determine directory (source/album-based tree)
            source = (item.get("album") or "").replace("/", "_").strip()
            source = self._safe_name(source) if source else "unsorted"

            # Build filename
            title = item.get("title") or item.get("filename") or f"media_{msg_id}"
            mime = item.get("mime") or item.get("mime_type") or "application/octet-stream"
            media_type = item.get("type", "photo")

            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "image/webp": ".webp", "video/mp4": ".mp4", "video/webm": ".webm",
                "video/quicktime": ".mov", "application/pdf": ".pdf",
            }
            ext = ext_map.get(mime, ".bin")
            if media_type == "animation":
                ext = ".mp4"

            filename = self._safe_name(f"{msg_id}_{title[:80]}") + ext
            dir_path = f"/{source}"
            file_path = f"/{source}/{filename}"

            if dir_path not in tree:
                tree[dir_path] = {"type": "dir", "children": set()}
                tree["/"]["children"].add(source)

            tree[dir_path]["children"].add(filename)
            tree[file_path] = {
                "type": "file",
                "item": item,
                "size": item.get("size") or item.get("file_size") or 1024,
                "msg_id": msg_id,
                "file_id": item.get("file_id") or item.get("telegram_file_id") or "",
            }

        with self.index_lock:
            self.items = items
            self.tree = tree

        dir_count = sum(1 for v in tree.values() if v["type"] == "dir")
        log.info("Index refreshed: %d items, %d dirs", len(items), dir_count)

    def _fetch_from_api(self) -> list[dict]:
        """Fetch media index from gallery HTTP API."""
        try:
            r = requests.get(f"{GALLERY_URL}/api/media", timeout=30)
            data = r.json()
            items = data.get("items", data) if isinstance(data, dict) else data
            return items if isinstance(items, list) else []
        except Exception as e:
            log.error("Failed to fetch from API: %s", e)
            return []

    def _fetch_from_db(self) -> list[dict]:
        """Fetch media index from SQLite database."""
        import sqlite3
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, filename, file_size, mime_type, telegram_file_id, telegram_message_id FROM assets"
            ).fetchall()
            conn.close()
            return [
                {
                    "msg_id": r["telegram_message_id"],
                    "file_id": r["telegram_file_id"],
                    "filename": r["filename"],
                    "title": r["filename"],
                    "size": r["file_size"],
                    "mime": r["mime_type"],
                    "type": "video" if (r["mime_type"] or "").startswith("video/") else "photo",
                }
                for r in rows
            ]
        except Exception as e:
            log.error("Failed to read DB: %s", e)
            return []

    def _refresh_loop(self) -> None:
        while True:
            time.sleep(300)
            self._refresh_index()

    @staticmethod
    def _safe_name(name: str) -> str:
        for c in '<>:"/\\|?*\0':
            name = name.replace(c, "_")
        return name.strip().strip(".")[:200] or "unnamed"

    def _get_node(self, path: str) -> Optional[Dict[str, Any]]:
        with self.index_lock:
            return self.tree.get(path)

    # ------------------------------------------------------------------
    # Download helpers
    # ------------------------------------------------------------------

    def _download_bot_api(self, file_id: str) -> Optional[bytes]:
        """Download via Bot API (≤ 20 MB)."""
        if not BOT_TOKEN or not file_id:
            return None
        try:
            r = requests.post(f"{TG_API}/getFile", json={"file_id": file_id}, timeout=30)
            data = r.json()
            if not data.get("ok"):
                return None
            file_path = data["result"]["file_path"]
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            dl = requests.get(url, timeout=120)
            return dl.content if dl.status_code == 200 else None
        except Exception as e:
            log.error("Bot API download failed: %s", e)
            return None

    def _download_pyrogram(self, msg_id: int) -> Optional[bytes]:
        """Download via pyrogram MTProto (any size)."""
        if not HAS_PYROGRAM:
            return None

        api_id = int(os.environ.get("TG_API_ID", "0"))
        api_hash = os.environ.get("TG_API_HASH", "")
        if not api_id or not api_hash:
            return None

        channel_id = int(CHANNEL_ID) if CHANNEL_ID else 0
        if not channel_id:
            return None

        import asyncio

        cache_dir = "/tmp/fuse_dl"
        session_dir = "/tmp/fuse_sessions"
        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(session_dir, exist_ok=True)
        out_path = f"{cache_dir}/{msg_id}"

        log.info("Pyrogram download: msg_id=%d", msg_id)

        async def dl():
            client = _PyroClient(
                "fuse_bot", api_id=api_id, api_hash=api_hash,
                bot_token=BOT_TOKEN, workdir=session_dir, no_updates=True,
            )
            async with client:
                msg = await client.get_messages(channel_id, msg_id)
                if msg:
                    await client.download_media(msg, file_name=out_path)

        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(dl())
            finally:
                loop.close()

            if os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    data = f.read()
                os.remove(out_path)
                log.info("Pyrogram download complete: %d bytes", len(data))
                return data
        except Exception as e:
            log.error("Pyrogram download failed: %s", e, exc_info=True)

        return None

    def _get_disk_cache_path(self, msg_id: int) -> Path:
        DISK_CACHE_DIR.mkdir(exist_ok=True)
        return DISK_CACHE_DIR / str(msg_id)

    def _ensure_downloaded(self, msg_id: int, file_id: str, file_size: int) -> Tuple[Optional[str], Any]:
        """Ensure file is available.  Returns ("mem", bytes) or ("disk", Path) or (None, None)."""
        # Check disk cache first
        disk_path = self._get_disk_cache_path(msg_id)
        if disk_path.exists() and disk_path.stat().st_size > 0:
            return "disk", disk_path

        # Check memory cache
        data = self.file_cache.get(msg_id)
        if data is not None:
            return "mem", data

        # Download: Bot API for small, pyrogram for large
        downloaded: Optional[bytes] = None
        if (file_size or 0) <= BOT_API_LIMIT and file_id:
            downloaded = self._download_bot_api(file_id)

        if downloaded is None:
            downloaded = self._download_pyrogram(msg_id)

        if downloaded is None:
            log.error("Failed to download msg_id=%d", msg_id)
            return None, None

        # Store: small in memory, large on disk
        if len(downloaded) <= DISK_CACHE_THRESHOLD:
            self.file_cache.put(msg_id, downloaded)
            return "mem", downloaded
        else:
            disk_path.write_bytes(downloaded)
            log.info("Cached to disk: %s (%d bytes)", disk_path, len(downloaded))
            return "disk", disk_path

    # ------------------------------------------------------------------
    # FUSE operations
    # ------------------------------------------------------------------

    def getattr(self, path: str, fh: Any = None) -> Dict[str, Any]:
        node = self._get_node(path)
        if node is None:
            raise FuseOSError(errno.ENOENT)
        now = time.time()
        if node["type"] == "dir":
            return {
                "st_mode": stat.S_IFDIR | 0o555, "st_nlink": 2,
                "st_uid": os.getuid(), "st_gid": os.getgid(),
                "st_atime": now, "st_mtime": now, "st_ctime": now,
            }
        return {
            "st_mode": stat.S_IFREG | 0o444, "st_nlink": 1,
            "st_size": node["size"],
            "st_uid": os.getuid(), "st_gid": os.getgid(),
            "st_atime": now, "st_mtime": now, "st_ctime": now,
        }

    def readdir(self, path: str, fh: Any) -> list[str]:
        node = self._get_node(path)
        if node is None or node["type"] != "dir":
            raise FuseOSError(errno.ENOENT)
        return [".", ".."] + list(node.get("children", []))

    def open(self, path: str, flags: int) -> int:
        if self._get_node(path) is None:
            raise FuseOSError(errno.ENOENT)
        return 0

    def read(self, path: str, size: int, offset: int, fh: Any) -> bytes:
        node = self._get_node(path)
        if node is None or node["type"] != "file":
            raise FuseOSError(errno.ENOENT)

        msg_id = node["msg_id"]
        file_id = node.get("file_id", "")
        file_size = node.get("size", 0) or 0

        kind, result = self._ensure_downloaded(msg_id, file_id, file_size)

        if result is None:
            raise FuseOSError(errno.EIO)

        if kind == "mem":
            return result[offset:offset + size]
        else:
            try:
                with open(result, "rb") as f:
                    f.seek(offset)
                    return f.read(size)
            except Exception as e:
                log.error("Disk cache read failed: %s", e)
                raise FuseOSError(errno.EIO)

    def statfs(self, path: str) -> Dict[str, int]:
        return {
            "f_bsize": 4096, "f_blocks": 1024 * 1024,
            "f_bfree": 512 * 1024, "f_bavail": 512 * 1024,
            "f_files": len(self.items), "f_ffree": 0, "f_namemax": 255,
        }


def main() -> None:
    import argparse
    import subprocess

    ap = argparse.ArgumentParser(description="Mount TeleVault as FUSE filesystem")
    ap.add_argument("mountpoint", help="Where to mount")
    ap.add_argument("--foreground", "-f", action="store_true", default=True)
    args = ap.parse_args()

    try:
        subprocess.run(["fusermount", "-uz", args.mountpoint], capture_output=True, timeout=5)
    except Exception:
        pass

    os.makedirs(args.mountpoint, exist_ok=True)

    # If using gallery API, wait for it
    if GALLERY_URL:
        log.info("Waiting for gallery API at %s …", GALLERY_URL)
        for _ in range(30):
            try:
                r = requests.get(f"{GALLERY_URL}/api/media", timeout=5)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2)

    log.info("Mounting at %s (cache: %d MB in-memory LRU)", args.mountpoint, MAX_CACHE_MB)

    try:
        FUSE(VaultFS(), args.mountpoint, foreground=args.foreground,
             nothreads=False, allow_other=True, ro=True)
    except Exception as e:
        log.error("FUSE crashed: %s", e)
        try:
            subprocess.run(["fusermount", "-uz", args.mountpoint], capture_output=True, timeout=5)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
