"""Tests for pyrogram/MTProto integration (mocked)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from tg_media_store.client import TelegramMediaStore, HAS_PYROGRAM


class TestPyrogramConfig:
    """Test pyrogram configuration detection."""

    def test_has_pyrogram_false_without_credentials(self, store: TelegramMediaStore) -> None:
        # Default store has no api_id/api_hash
        assert store.api_id is None or store.api_hash is None or not HAS_PYROGRAM

    def test_has_pyrogram_with_credentials(self, tmp_path: Path) -> None:
        s = TelegramMediaStore(
            bot_token="123:ABC",
            channel_id="-100123",
            db_path=tmp_path / "test.db",
            api_id=12345,
            api_hash="abc123",
        )
        if HAS_PYROGRAM:
            assert s.has_pyrogram is True
        else:
            assert s.has_pyrogram is False
        s.close()

    def test_env_var_fallback(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"TG_API_ID": "99999", "TG_API_HASH": "envhash"}):
            s = TelegramMediaStore(
                bot_token="123:ABC",
                channel_id="-100123",
                db_path=tmp_path / "test.db",
            )
            assert s.api_id == 99999
            assert s.api_hash == "envhash"
            s.close()


class TestUploadLargeFile:
    """Test upload_large_file raises when pyrogram not available."""

    def test_raises_without_pyrogram(self, store: TelegramMediaStore, sample_image: Path) -> None:
        # Force no pyrogram
        store.api_id = None
        store.api_hash = None
        with pytest.raises(RuntimeError, match="Pyrogram not available"):
            store.upload_large_file(sample_image)


class TestFetchAssetLarge:
    """Test fetch_asset_large raises when pyrogram not available."""

    def test_raises_without_pyrogram(self, store: TelegramMediaStore) -> None:
        store.api_id = None
        store.api_hash = None
        with pytest.raises(RuntimeError, match="Pyrogram not available"):
            store.fetch_asset_large(1)


class TestAutoRouting:
    """Test that large files are auto-routed to pyrogram methods."""

    @patch("tg_media_store.client.requests.post")
    def test_small_file_uses_bot_api(self, mock_post: MagicMock, store: TelegramMediaStore, sample_image: Path) -> None:
        """Files under threshold should use Bot API."""
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "photo": [{"file_id": "fid", "width": 10, "height": 10}],
                },
            },
        )
        result = store.upload_file(sample_image)
        assert result is not None
        mock_post.assert_called_once()  # Bot API was used

    @patch("tg_media_store.client.requests.get")
    def test_fetch_small_uses_bot_api(self, mock_get: MagicMock, store: TelegramMediaStore) -> None:
        """fetch_asset for small files uses Bot API."""
        # Insert a small asset
        conn = store._get_conn()
        conn.execute(
            """INSERT INTO assets (file_hash, filename, file_size, mime_type,
               telegram_file_id, telegram_message_id, channel_id, uploaded_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("h1", "small.jpg", 1000, "image/jpeg", "fid_small", 1, "-100", "2025-01-01"),
        )
        conn.commit()

        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: {"result": {"file_path": "photos/small.jpg"}}),
            MagicMock(status_code=200, iter_content=lambda chunk_size: [b"data"]),
        ]

        result = store.fetch_asset(1)
        assert result is not None


class TestServerIngest:
    """Test the /api/ingest endpoint."""

    def test_ingest_items(self) -> None:
        from fastapi.testclient import TestClient
        import tg_media_store.server as srv

        original_pass = srv.VIEWER_PASS
        srv.VIEWER_PASS = "changeme"

        # Clear index
        srv.MEDIA_INDEX.clear()

        tc = TestClient(srv.app)
        r = tc.post("/api/ingest", json=[
            {"msg_id": 1, "file_id": "f1", "title": "test1"},
            {"msg_id": 2, "file_id": "f2", "title": "test2"},
        ])
        assert r.status_code == 200
        data = r.json()
        assert data["added"] == 2
        assert data["total"] == 2

        # Duplicate should not be added
        r = tc.post("/api/ingest", json={"msg_id": 1, "file_id": "f1"})
        assert r.json()["added"] == 0

        # Clean up
        srv.MEDIA_INDEX.clear()
        srv.VIEWER_PASS = original_pass


class TestServerStream:
    """Test the /stream endpoint with range requests."""

    def test_stream_small_file(self) -> None:
        """Stream endpoint returns file content for small files."""
        import sqlite3
        import tempfile
        from fastapi.testclient import TestClient
        import tg_media_store.server as srv

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            conn = sqlite3.connect(str(db))
            conn.execute("""CREATE TABLE assets (
                id INTEGER PRIMARY KEY, file_hash TEXT, original_path TEXT,
                filename TEXT, file_size INTEGER, mime_type TEXT,
                telegram_file_id TEXT, telegram_message_id INTEGER,
                channel_id TEXT, uploaded_at TEXT, metadata TEXT)""")
            conn.execute("""CREATE TABLE albums (id INTEGER PRIMARY KEY, name TEXT, description TEXT, created_at TEXT)""")
            conn.execute("""CREATE TABLE album_assets (album_id INTEGER, asset_id INTEGER)""")
            conn.execute(
                "INSERT INTO assets VALUES (1,'h','','test.jpg',100,'image/jpeg','fid',42,'-100','2025-01-01',NULL)"
            )
            conn.commit()
            conn.close()

            original_db = srv.DB_PATH
            original_pass = srv.VIEWER_PASS
            original_token = srv.BOT_TOKEN
            srv.DB_PATH = db
            srv.VIEWER_PASS = "changeme"
            srv.BOT_TOKEN = "fake:token"

            tc = TestClient(srv.app)

            with patch("tg_media_store.server.requests.get") as mock_get:
                mock_get.side_effect = [
                    MagicMock(status_code=200, json=lambda: {"result": {"file_path": "photos/test.jpg"}}),
                    MagicMock(status_code=200, iter_content=lambda chunk_size: [b"image_data"], headers={}),
                ]
                r = tc.get("/stream/42")
                assert r.status_code == 200

            srv.DB_PATH = original_db
            srv.VIEWER_PASS = original_pass
            srv.BOT_TOKEN = original_token
