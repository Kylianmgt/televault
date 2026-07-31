"""Core upload/download/index logic for TeleVault — Telegram File Vault.

This module provides the ``TelegramMediaStore`` class — the main programmatic
interface for uploading **any file** to a Telegram channel, querying the local
SQLite index, fetching files back, and performing housekeeping (dedup, cleanup,
stats).  All file types are supported: documents, archives, code, media, etc.

Pyrogram/MTProto support is **optional** — install ``pyrofork`` and set
``api_id``/``api_hash`` to enable large-file uploads (>50 MB) and downloads
(>20 MB) that exceed the Bot API limits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

logger = logging.getLogger(__name__)

# Optional pyrogram import
try:
    from pyrogram import Client as PyroClient
    HAS_PYROGRAM = True
except ImportError:
    PyroClient = None  # type: ignore[assignment,misc]
    HAS_PYROGRAM = False

# ---------------------------------------------------------------------------
# Defaults & constants
# ---------------------------------------------------------------------------

UPLOAD_DELAY: float = 3.5  # seconds between uploads (safe Telegram rate)
MAX_FILE_SIZE: int = 2 * 1024 * 1024 * 1024  # 2 GB (regular accounts)
BOT_API_DOWNLOAD_LIMIT: int = 20 * 1024 * 1024  # 20 MB
LARGE_FILE_THRESHOLD: int = 50 * 1024 * 1024  # 50 MB — prefer pyrogram above this

MEDIA_EXTENSIONS: set[str] = {
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg", ".ico",
    # Video
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv", ".wmv",
    # Audio
    ".mp3", ".ogg", ".flac", ".wav", ".aac", ".m4a",
    # Documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".csv", ".rtf", ".odt", ".ods", ".odp",
    # Data & config
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    # Code
    ".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp", ".h",
    ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".sql", ".r",
    # Markup
    ".md", ".rst", ".tex", ".log",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z", ".iso",
    ".tgz", ".tar.gz",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def file_sha256(filepath: Union[str, Path]) -> str:
    """Return the hex SHA-256 digest of *filepath*."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_file_id(result: Dict[str, Any]) -> str:
    """Pull the file_id out of a sendX response, whatever kind Telegram chose.

    The response key does not have to match the method used: an MP4 sent via
    ``sendDocument`` comes back under ``video``, and an image sent as a document
    may come back under ``document``. Inferring the key from the request is
    therefore wrong and silently discards successful uploads, so probe the
    response instead.
    """
    for key in ("document", "video", "animation", "audio", "voice", "video_note"):
        file_id = (result.get(key) or {}).get("file_id")
        if file_id:
            return file_id
    # Photos come back as a size ladder; the last entry is the largest.
    photos = result.get("photo") or []
    if photos:
        return photos[-1].get("file_id", "")
    return ""


def _extract_thumb_file_id(result: Dict[str, Any]) -> str:
    """Telegram's own generated thumbnail for a media message, if any.

    Worth storing: it is a few KB and downloadable through the Bot API whatever
    the original weighs, so serving it avoids pulling a whole video back just to
    make a preview — which the Bot API cannot even do above 20 MB.
    """
    for key in ("video", "document", "animation", "audio"):
        media = result.get(key) or {}
        thumb = media.get("thumbnail") or media.get("thumb") or {}
        file_id = thumb.get("file_id")
        if file_id:
            return file_id
    return ""


def make_video_poster(filepath: Union[str, Path], dest: Union[str, Path]) -> bool:
    """Extract a small JPEG poster from a video with ffmpeg.

    Telegram only keeps a preview for a *document* if the uploader supplies one,
    and archival uploads are documents so the bytes survive intact. Sending this
    alongside means the stored message carries a preview, which is what makes a
    thumbnail cheap to serve later instead of requiring the whole file back.

    Returns True if a poster was written.
    """
    try:
        subprocess.run(
            [
                "ffmpeg", "-loglevel", "error", "-y",
                "-i", str(filepath), "-frames:v", "1",
                # Telegram wants the preview under 200 KB and 320 px on a side.
                "-vf", "thumbnail,scale=320:-2",
                "-f", "image2", str(dest),
            ],
            check=True, timeout=120,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    dest = Path(dest)
    return dest.exists() and 0 < dest.stat().st_size


# A large upload is slow but not unbounded; past this it is wedged, and saying
# so beats occupying a worker forever.
MTPROTO_UPLOAD_TIMEOUT = float(os.environ.get("TG_STORE_MTPROTO_TIMEOUT", "1800"))


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class TelegramMediaStore:
    """High-level client for storing and retrieving media via Telegram.

    Parameters
    ----------
    bot_token : str
        Telegram Bot API token.
    channel_id : str | int
        Telegram channel / chat ID where files are stored.
    db_path : str | Path
        Path to the SQLite index database.  Created automatically.
    cache_dir : str | Path | None
        Directory used for downloaded / cached files.
    upload_delay : float
        Seconds to sleep between consecutive uploads (rate-limit protection).
    api_id : int | None
        Telegram API ID for pyrogram/MTProto (optional, enables large files).
    api_hash : str | None
        Telegram API hash for pyrogram/MTProto (optional, enables large files).
    """

    def __init__(
        self,
        bot_token: str,
        channel_id: Union[str, int],
        db_path: Union[str, Path] = "tg_media_store.db",
        cache_dir: Union[str, Path, None] = None,
        upload_delay: float = UPLOAD_DELAY,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
    ) -> None:
        self.bot_token = bot_token
        self.channel_id = str(channel_id)
        self.db_path = Path(db_path)
        self.cache_dir = Path(cache_dir) if cache_dir else self.db_path.parent / "cache"
        self.upload_delay = upload_delay
        self._base_url = f"https://api.telegram.org/bot{self.bot_token}"

        # Pyrogram (optional)
        self.api_id = api_id or int(os.environ.get("TG_API_ID", "0")) or None
        self.api_hash = api_hash or os.environ.get("TG_API_HASH", "") or None
        self._pyro_client: Any = None

        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    @property
    def has_pyrogram(self) -> bool:
        """Return True if pyrogram is available and configured."""
        return HAS_PYROGRAM and bool(self.api_id) and bool(self.api_hash)

    def _get_pyro_client(self) -> Any:
        """Kept for callers that drive pyrogram themselves.

        The upload path no longer uses this: it runs pyrogram in a subprocess,
        because driving it from a worker thread hangs indefinitely.
        """
        if not self.has_pyrogram:
            return None
        if self._pyro_client is None:
            self._pyro_client = PyroClient(
                "tg_media_store",
                api_id=self.api_id,
                api_hash=self.api_hash,
                bot_token=self.bot_token,
                no_updates=True,
                in_memory=True,
            )
        return self._pyro_client

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash TEXT UNIQUE,
                original_path TEXT,
                filename TEXT,
                file_size INTEGER,
                mime_type TEXT,
                telegram_file_id TEXT,
                telegram_message_id INTEGER,
                channel_id TEXT,
                uploaded_at TEXT,
                metadata TEXT,
                telegram_thumb_file_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS albums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                description TEXT,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS album_assets (
                album_id INTEGER,
                asset_id INTEGER,
                FOREIGN KEY (album_id) REFERENCES albums(id),
                FOREIGN KEY (asset_id) REFERENCES assets(id),
                UNIQUE(album_id, asset_id)
            )
        """)
        # Existing vaults predate this column; adding it in place keeps their
        # index (and therefore their dedup history) intact.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
        if "telegram_thumb_file_id" not in cols:
            conn.execute("ALTER TABLE assets ADD COLUMN telegram_thumb_file_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_hash ON assets(file_hash)")
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            # WAL + a real busy timeout so parallel uploads queue behind each
            # other instead of failing with "database is locked". The default
            # 5 s timeout and rollback journal do not survive concurrent writers.
            self._conn = sqlite3.connect(str(self.db_path), timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        return self._conn

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_file(
        self,
        filepath: Union[str, Path],
        metadata: Optional[Dict[str, Any]] = None,
        caption: str = "",
        as_document: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Upload a single file to the Telegram channel.

        Returns a dict with ``id``, ``file_id``, and ``message_id`` on success,
        ``None`` on failure, or a dict with only ``id`` if the file was already
        uploaded (dedup hit).

        Files larger than 50 MB are automatically routed through pyrogram
        (MTProto) if configured.

        Set ``as_document=True`` to store the file byte-for-byte. Telegram
        re-encodes anything sent as a photo — a PNG comes back as JPEG, and
        images wider than ~1280 px are downscaled — so archival callers that
        need the original should pass this. The trade-off is that Telegram
        clients show the file as an attachment rather than an inline preview.
        """
        filepath = Path(filepath)
        if not filepath.exists():
            return None

        fsize = filepath.stat().st_size
        if fsize > MAX_FILE_SIZE or fsize == 0:
            return None

        fhash = file_sha256(filepath)
        conn = self._get_conn()

        # Dedup check
        existing = conn.execute(
            "SELECT id, telegram_file_id FROM assets WHERE file_hash = ?", (fhash,)
        ).fetchone()
        if existing:
            return dict(existing)

        # Route large files through pyrogram
        if fsize > LARGE_FILE_THRESHOLD and self.has_pyrogram:
            return self.upload_large_file(
                filepath, metadata=metadata, caption=caption, _hash=fhash,
                as_document=as_document,
            )

        mime, _ = mimetypes.guess_type(str(filepath))
        if not mime:
            mime = "application/octet-stream"

        # as_document forces the lossless path: Telegram leaves documents alone
        # but re-encodes photos and may transcode videos.
        is_video = mime.startswith("video/") and not as_document
        is_image = mime.startswith("image/") and not as_document

        poster_dir = None
        poster_path = None
        poster_handle = None
        if mime.startswith("video/") and (as_document or not is_video):
            poster_dir = tempfile.mkdtemp()
            candidate = Path(poster_dir) / "poster.jpg"
            poster_path = candidate if make_video_poster(filepath, candidate) else None

        try:
            with open(filepath, "rb") as f:
                if is_video:
                    endpoint = f"{self._base_url}/sendVideo"
                    files = {"video": (filepath.name, f, mime)}
                    data = {"chat_id": self.channel_id, "caption": caption[:1024], "supports_streaming": "true"}
                elif is_image and mime != "image/gif":
                    endpoint = f"{self._base_url}/sendPhoto"
                    files = {"photo": (filepath.name, f, mime)}
                    data = {"chat_id": self.channel_id, "caption": caption[:1024]}
                elif mime == "image/gif" and not as_document:
                    endpoint = f"{self._base_url}/sendAnimation"
                    files = {"animation": (filepath.name, f, mime)}
                    data = {"chat_id": self.channel_id, "caption": caption[:1024]}
                else:
                    endpoint = f"{self._base_url}/sendDocument"
                    files = {"document": (filepath.name, f, mime)}
                    data = {"chat_id": self.channel_id, "caption": caption[:1024]}
                    # Telegram keeps no preview for a document unless one is
                    # attached. Without it every archival video is previewless.
                    if mime.startswith("video/") and poster_path is not None:
                        try:
                            poster_handle = open(poster_path, "rb")
                            files["thumbnail"] = ("poster.jpg", poster_handle, "image/jpeg")
                        except OSError:
                            # A preview is a nice-to-have; never lose the upload
                            # itself because the poster could not be read.
                            logger.warning("poster unreadable for %s", filepath.name)

                r = requests.post(endpoint, files=files, data=data, timeout=300)

            # Handle rate-limit
            if r.status_code == 429:
                retry_after = r.json().get("parameters", {}).get("retry_after", 30)
                time.sleep(retry_after)
                return self.upload_file(filepath, metadata, caption, as_document)

            if r.status_code == 400 and is_image and "PHOTO_INVALID_DIMENSIONS" in (r.text or ""):
                # Retry as document
                with open(filepath, "rb") as f2:
                    r = requests.post(
                        f"{self._base_url}/sendDocument",
                        files={"document": (filepath.name, f2, mime)},
                        data={"chat_id": self.channel_id, "caption": caption[:1024]},
                        timeout=300,
                    )

            if r.status_code != 200:
                return None

            result = r.json()["result"]
            message_id = result["message_id"]
            file_id = _extract_file_id(result)
            thumb_file_id = _extract_thumb_file_id(result)

            if not file_id:
                return None

            meta_json = json.dumps(metadata) if metadata else None
            conn.execute(
                """INSERT INTO assets
                   (file_hash, original_path, filename, file_size, mime_type,
                    telegram_file_id, telegram_message_id, channel_id, uploaded_at,
                    metadata, telegram_thumb_file_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fhash, str(filepath), filepath.name, fsize, mime,
                    file_id, message_id, self.channel_id,
                    datetime.now().isoformat(), meta_json, thumb_file_id,
                ),
            )
            conn.commit()

            return {
                "id": conn.execute("SELECT last_insert_rowid()").fetchone()[0],
                "file_id": file_id,
                "message_id": message_id,
            }

        except requests.exceptions.Timeout:
            logger.warning("Bot API upload timed out: %s", filepath.name)
            return None
        except Exception:
            # Callers only see None, so without this the cause is unrecoverable.
            logger.exception("Bot API upload failed: %s", filepath.name)
            return None
        finally:
            if poster_handle is not None:
                try:
                    poster_handle.close()
                except Exception:
                    pass
            if poster_dir:
                shutil.rmtree(poster_dir, ignore_errors=True)

    def upload_large_file(
        self,
        filepath: Union[str, Path],
        metadata: Optional[Dict[str, Any]] = None,
        caption: str = "",
        _hash: Optional[str] = None,
        as_document: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Upload a file using pyrogram/MTProto (no size limit).

        This is called automatically by :meth:`upload_file` for files > 50 MB
        when pyrogram is configured.  Can also be called directly.

        Requires ``api_id`` and ``api_hash`` to be set (constructor or env vars).
        """
        if not self.has_pyrogram:
            raise RuntimeError("Pyrogram not available — install pyrofork and set TG_API_ID/TG_API_HASH")

        import asyncio

        filepath = Path(filepath)
        if not filepath.exists():
            return None

        fsize = filepath.stat().st_size
        if fsize == 0 or fsize > MAX_FILE_SIZE:
            return None

        fhash = _hash or file_sha256(filepath)
        conn = self._get_conn()

        # Dedup
        existing = conn.execute(
            "SELECT id, telegram_file_id FROM assets WHERE file_hash = ?", (fhash,)
        ).fetchone()
        if existing:
            return dict(existing)

        mime, _ = mimetypes.guess_type(str(filepath))
        if not mime:
            mime = "application/octet-stream"
        # Documents are stored byte-for-byte; send_video may transcode.
        is_video = mime.startswith("video/") and not as_document

        poster_dir = None
        poster_path = None
        if mime.startswith("video/"):
            poster_dir = tempfile.mkdtemp()
            candidate = Path(poster_dir) / "poster.jpg"
            poster_path = candidate if make_video_poster(filepath, candidate) else None

        # Driven in a subprocess on its own main thread. The same call issued
        # from a worker thread of the server never completes — no error, no
        # progress — while a dedicated process finishes in seconds. See
        # tg_media_store/_mtproto_upload.py.
        job = {
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "bot_token": self.bot_token,
            "channel_id": self.channel_id,
            "path": str(filepath),
            "caption": caption,
            "file_name": filepath.name,
            "as_video": bool(is_video),
            "thumb": str(poster_path) if poster_path else None,
        }

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "tg_media_store._mtproto_upload", json.dumps(job)],
                capture_output=True, text=True, timeout=MTPROTO_UPLOAD_TIMEOUT,
            )
            payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
            if not payload.get("ok"):
                logger.error(
                    "MTProto upload failed: %s — %s", filepath.name, payload.get("error")
                )
                return None
            message_id = payload["message_id"]
            file_id = payload.get("file_id") or ""
            thumb_file_id = payload.get("thumb_file_id") or ""
            if not file_id:
                logger.error("MTProto upload returned no file_id: %s", filepath.name)
                return None

            meta_json = json.dumps(metadata) if metadata else None
            conn.execute(
                """INSERT INTO assets
                   (file_hash, original_path, filename, file_size, mime_type,
                    telegram_file_id, telegram_message_id, channel_id, uploaded_at,
                    metadata, telegram_thumb_file_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fhash, str(filepath), filepath.name, fsize, mime,
                    file_id, message_id, self.channel_id,
                    datetime.now().isoformat(), meta_json, thumb_file_id,
                ),
            )
            conn.commit()

            return {
                "id": conn.execute("SELECT last_insert_rowid()").fetchone()[0],
                "file_id": file_id,
                "message_id": message_id,
            }
        except subprocess.TimeoutExpired:
            logger.error(
                "MTProto upload timed out after %ss: %s",
                MTPROTO_UPLOAD_TIMEOUT, filepath.name,
            )
            return None
        except Exception:
            logger.exception("MTProto upload failed: %s", filepath.name)
            return None
        finally:
            if poster_dir:
                shutil.rmtree(poster_dir, ignore_errors=True)

    def upload_directory(
        self,
        dir_path: Union[str, Path],
        extensions: Optional[set[str]] = None,
    ) -> Dict[str, int]:
        """Upload all matching files in *dir_path*.

        When *extensions* is ``None`` (the default), **every** file in the
        directory is uploaded regardless of its extension.  Pass a set of
        extensions (e.g. ``{".jpg", ".png"}``) to restrict.

        Returns a dict ``{"uploaded": N, "skipped": M, "failed": F}``.
        """
        dir_path = Path(dir_path)
        if extensions is not None:
            files = sorted(
                f for f in dir_path.iterdir()
                if f.is_file() and f.suffix.lower() in extensions
            )
        else:
            files = sorted(f for f in dir_path.iterdir() if f.is_file())
        uploaded = skipped = failed = 0
        for fp in files:
            result = self.upload_file(fp)
            if result and "file_id" in result:
                uploaded += 1
                time.sleep(self.upload_delay)
            elif result:
                skipped += 1
            else:
                failed += 1
        return {"uploaded": uploaded, "skipped": skipped, "failed": failed}

    # ------------------------------------------------------------------
    # Fetch / download
    # ------------------------------------------------------------------

    def fetch_asset(self, asset_id: int) -> Optional[Path]:
        """Download an asset from Telegram by its local DB id.

        Returns the path to the downloaded file, or ``None`` on failure.

        For files > 20 MB, automatically uses pyrogram if available.
        """
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            return None

        file_size = row["file_size"] or 0

        # Use pyrogram for large files
        if file_size > BOT_API_DOWNLOAD_LIMIT and self.has_pyrogram:
            return self.fetch_asset_large(asset_id)

        file_id = row["telegram_file_id"]
        r = requests.get(
            f"{self._base_url}/getFile",
            params={"file_id": file_id},
            timeout=30,
        )
        if r.status_code != 200:
            # Fallback to pyrogram if Bot API fails (file too large)
            if self.has_pyrogram:
                return self.fetch_asset_large(asset_id)
            return None

        file_path = r.json()["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        output_file = self.cache_dir / row["filename"]

        dl = requests.get(download_url, stream=True, timeout=120)
        with open(output_file, "wb") as f:
            for chunk in dl.iter_content(8192):
                f.write(chunk)
        return output_file

    def fetch_asset_large(self, asset_id: int) -> Optional[Path]:
        """Download an asset using pyrogram/MTProto (no size limit).

        Requires ``api_id`` and ``api_hash`` to be set.
        """
        if not self.has_pyrogram:
            raise RuntimeError("Pyrogram not available — install pyrofork and set TG_API_ID/TG_API_HASH")

        import asyncio

        conn = self._get_conn()
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            return None

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        output_file = self.cache_dir / row["filename"]
        msg_id = row["telegram_message_id"]
        channel_id = int(row["channel_id"])

        client = self._get_pyro_client()

        async def _download() -> Optional[str]:
            async with client:
                msg = await client.get_messages(channel_id, msg_id)
                if not msg:
                    return None
                path = await client.download_media(msg, file_name=str(output_file))
                return path

        try:
            loop = asyncio.new_event_loop()
            try:
                result_path = loop.run_until_complete(_download())
            finally:
                loop.close()
                self._pyro_client = None

            if result_path and Path(result_path).exists():
                return Path(result_path)
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Albums
    # ------------------------------------------------------------------

    def get_or_create_album(self, name: str, description: str = "") -> int:
        """Return the album id for *name*, creating it if necessary."""
        conn = self._get_conn()
        row = conn.execute("SELECT id FROM albums WHERE name = ?", (name,)).fetchone()
        if row:
            return row[0]
        conn.execute(
            "INSERT INTO albums (name, description, created_at) VALUES (?, ?, ?)",
            (name, description, datetime.now().isoformat()),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def add_to_album(self, album_id: int, asset_id: int) -> None:
        """Link *asset_id* to *album_id*."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO album_assets (album_id, asset_id) VALUES (?, ?)",
            (album_id, asset_id),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Query / stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Return a dict of vault statistics."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        total_size = conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM assets").fetchone()[0]
        albums = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "total_assets": total,
            "total_size_bytes": total_size,
            "albums": albums,
            "db_size_bytes": db_size,
        }

    def list_assets(
        self, limit: int = 100, offset: int = 0, album: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return a list of asset dicts from the index."""
        conn = self._get_conn()
        if album:
            rows = conn.execute(
                """SELECT a.* FROM assets a
                   JOIN album_assets aa ON aa.asset_id = a.id
                   JOIN albums al ON al.id = aa.album_id
                   WHERE al.name = ?
                   ORDER BY a.id DESC LIMIT ? OFFSET ?""",
                (album, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM assets ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_asset(self, asset_id: int) -> Optional[Dict[str, Any]]:
        """Return a single asset dict or ``None``."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_local(self) -> Dict[str, int]:
        """Remove local copies of files that are safely vaulted.

        Returns ``{"removed": N, "freed_bytes": M}``.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT original_path FROM assets WHERE telegram_file_id IS NOT NULL"
        ).fetchall()
        removed = freed = 0
        for row in rows:
            p = Path(row["original_path"])
            if p.exists():
                freed += p.stat().st_size
                p.unlink()
                removed += 1
        return {"removed": removed, "freed_bytes": freed}
