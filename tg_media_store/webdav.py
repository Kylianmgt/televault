"""Read-only WebDAV gateway for TeleVault.

Exposes the vault index as a virtual WebDAV filesystem so that clients like
Nextcloud, macOS Finder, or Windows Explorer can browse and stream files
directly from Telegram.

Run::

    python -m tg_media_store.webdav --host 0.0.0.0 --port 8100
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN, HTTP_NOT_FOUND
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = Path(os.environ.get("TG_STORE_DB", "tg_media_store.db"))
USER_AGENT = "tg-media-store-webdav/0.1"

# Pyrogram support (optional, for large files)
try:
    from pyrogram import Client as PyroClient
    HAS_PYROGRAM = True
except ImportError:
    PyroClient = None  # type: ignore[assignment,misc]
    HAS_PYROGRAM = False

TG_API_ID = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _tg_download_url(file_id: str) -> str:
    """Resolve a Telegram ``file_id`` to a download URL via the Bot API."""
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=30,
        headers={"User-Agent": USER_AGENT},
    )
    if r.status_code != 200:
        raise RuntimeError(f"getFile failed: {r.status_code}")
    fp = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}"


class _ReadOnlyMixin:
    def begin_write(self, *a, **k):  # type: ignore[no-untyped-def]
        raise DAVError(HTTP_FORBIDDEN)

    def create_collection(self, name):  # type: ignore[no-untyped-def]
        raise DAVError(HTTP_FORBIDDEN)

    def create_empty_resource(self, name):  # type: ignore[no-untyped-def]
        raise DAVError(HTTP_FORBIDDEN)

    def delete(self):  # type: ignore[no-untyped-def]
        raise DAVError(HTTP_FORBIDDEN)

    def support_etag(self) -> bool:
        return False


class VaultProvider(DAVProvider):
    """WsgiDAV provider that exposes ``/assets/`` and ``/albums/<name>/``."""

    def get_resource_inst(self, path: str, environ: dict):  # type: ignore[override]
        if not path.startswith("/"):
            path = "/" + path
        while "//" in path:
            path = path.replace("//", "/")

        if path in ("", "/"):
            return RootCollection("/", environ)
        if path.rstrip("/") == "/assets":
            return AssetsCollection("/assets", environ)
        if path.startswith("/assets/"):
            name = path.split("/", 2)[2]
            return AssetsCollection("/assets", environ).get_member(name)
        if path.rstrip("/") == "/albums":
            return AlbumsCollection("/albums", environ)
        if path.startswith("/albums/"):
            parts = path.split("/")
            if len(parts) >= 3 and parts[2]:
                album_dir = parts[2]
                album = AlbumsCollection("/albums", environ).get_member(album_dir)
                if len(parts) <= 3 or (len(parts) == 4 and not parts[3]):
                    return album
                if len(parts) >= 4 and parts[3]:
                    return album.get_member(parts[3])
        return None


class RootCollection(_ReadOnlyMixin, DAVCollection):  # type: ignore[misc]
    def get_member_names(self) -> list[str]:
        return ["assets", "albums"]

    def get_member(self, name: str):  # type: ignore[override]
        if name == "assets":
            return AssetsCollection(self.path + "assets", self.environ)
        if name == "albums":
            return AlbumsCollection(self.path + "albums", self.environ)
        raise DAVError(HTTP_NOT_FOUND)


class AssetsCollection(_ReadOnlyMixin, DAVCollection):  # type: ignore[misc]
    def get_member_names(self) -> list[str]:
        conn = _db()
        rows = conn.execute("SELECT id, filename FROM assets ORDER BY id DESC LIMIT 5000").fetchall()
        conn.close()
        return [f"{r['id']}_{r['filename']}" for r in rows]

    def get_member(self, name: str):  # type: ignore[override]
        try:
            asset_id = int(name.split("_", 1)[0])
        except Exception:
            raise DAVError(HTTP_NOT_FOUND)
        return AssetFile(self.path + "/" + name, self.environ, asset_id)


class AlbumsCollection(_ReadOnlyMixin, DAVCollection):  # type: ignore[misc]
    def get_member_names(self) -> list[str]:
        conn = _db()
        rows = conn.execute("SELECT id, name FROM albums ORDER BY name").fetchall()
        conn.close()
        return [f"{r['id']}_{r['name']}" for r in rows]

    def get_member(self, name: str):  # type: ignore[override]
        try:
            album_id = int(name.split("_", 1)[0])
        except Exception:
            raise DAVError(HTTP_NOT_FOUND)
        return AlbumDir(self.path + "/" + name, self.environ, album_id)


class AlbumDir(_ReadOnlyMixin, DAVCollection):  # type: ignore[misc]
    def __init__(self, path: str, environ: dict, album_id: int) -> None:
        super().__init__(path, environ)
        self.album_id = album_id

    def get_member_names(self) -> list[str]:
        conn = _db()
        rows = conn.execute(
            """SELECT a.id, a.filename FROM album_assets aa
               JOIN assets a ON a.id = aa.asset_id
               WHERE aa.album_id = ? ORDER BY a.id DESC""",
            (self.album_id,),
        ).fetchall()
        conn.close()
        return [f"{r['id']}_{r['filename']}" for r in rows]

    def get_member(self, name: str):  # type: ignore[override]
        try:
            asset_id = int(name.split("_", 1)[0])
        except Exception:
            raise DAVError(HTTP_NOT_FOUND)
        return AssetFile(self.path + "/" + name, self.environ, asset_id)


class AssetFile(_ReadOnlyMixin, DAVNonCollection):  # type: ignore[misc]
    def __init__(self, path: str, environ: dict, asset_id: int) -> None:
        super().__init__(path, environ)
        self.asset_id = asset_id

    def _row(self) -> sqlite3.Row:
        conn = _db()
        row = conn.execute("SELECT * FROM assets WHERE id=?", (self.asset_id,)).fetchone()
        conn.close()
        if not row:
            raise DAVError(HTTP_NOT_FOUND)
        return row

    def get_content_length(self) -> int:
        return int(self._row()["file_size"] or 0)

    def get_content_type(self) -> str:
        return self._row()["mime_type"] or "application/octet-stream"

    def get_etag(self) -> str:
        return self._row()["file_hash"] or str(self.asset_id)

    def get_last_modified(self):  # type: ignore[override]
        row = self._row()
        uploaded = row["uploaded_at"]
        if not uploaded:
            return None
        try:
            from datetime import datetime
            return datetime.fromisoformat(uploaded.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def get_content(self):  # type: ignore[override]
        row = self._row()
        file_size = int(row["file_size"] or 0)

        # Try Bot API first (works for files ≤ 20 MB)
        if file_size <= 20 * 1024 * 1024:
            try:
                url = _tg_download_url(row["telegram_file_id"])
                r = requests.get(url, stream=True, timeout=(10, 60), headers={"User-Agent": USER_AGENT})
                if r.status_code == 200:
                    return r.raw
            except Exception:
                pass

        # Pyrogram fallback for large files
        if HAS_PYROGRAM and TG_API_ID and TG_API_HASH and BOT_TOKEN:
            import asyncio
            import io

            channel_id = int(os.environ.get("CHANNEL_ID", "0"))
            msg_id = int(row["telegram_message_id"])
            session_dir = "/tmp/webdav_sessions"
            os.makedirs(session_dir, exist_ok=True)

            async def _download():
                client = PyroClient(
                    "webdav_dl", api_id=TG_API_ID, api_hash=TG_API_HASH,
                    bot_token=BOT_TOKEN, workdir=session_dir, no_updates=True,
                )
                async with client:
                    msg = await client.get_messages(channel_id, msg_id)
                    if msg:
                        out = f"/tmp/webdav_dl_{msg_id}"
                        await client.download_media(msg, file_name=out)
                        return out
                return None

            try:
                loop = asyncio.new_event_loop()
                try:
                    path = loop.run_until_complete(_download())
                finally:
                    loop.close()
                if path and os.path.exists(path):
                    data = open(path, "rb").read()
                    os.remove(path)
                    return io.BytesIO(data)
            except Exception:
                pass

        # Last resort: try Bot API anyway
        try:
            url = _tg_download_url(row["telegram_file_id"])
            r = requests.get(url, stream=True, timeout=(10, 60), headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                return r.raw
        except Exception:
            pass

        raise DAVError(HTTP_NOT_FOUND)


def main() -> None:
    ap = argparse.ArgumentParser(description="TeleVault WebDAV Gateway")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--user", default=os.environ.get("TG_STORE_WEBDAV_USER", "vault"))
    ap.add_argument("--password", default=os.environ.get("TG_STORE_WEBDAV_PASS", "changeme"))
    args = ap.parse_args()

    from cheroot.wsgi import Server as CherootServer
    from wsgidav.wsgidav_app import WsgiDAVApp

    config = {
        "provider_mapping": {"/dav": VaultProvider()},
        "simple_dc": {"user_mapping": {"*": {args.user: {"password": args.password}}}},
        "http_authenticator": {
            "domain_controller": "wsgidav.dc.simple_dc.SimpleDomainController",
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
        "verbose": 1,
    }
    app = WsgiDAVApp(config)
    server = CherootServer((args.host, args.port), app)
    print(f"✅ WebDAV running: http://{args.host}:{args.port}/dav/")
    server.start()


if __name__ == "__main__":
    main()
