"""Tests for tg_media_store.server FastAPI endpoints."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    """Create a test database with sample data."""
    db = tmp_path / "test_server.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE assets (
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
            metadata TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            description TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE album_assets (
            album_id INTEGER,
            asset_id INTEGER,
            UNIQUE(album_id, asset_id)
        )
    """)
    # Insert sample assets
    conn.execute(
        "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        ("abc123", "photo1.jpg", 102400, "image/jpeg", "fid_1", 101, "-100123", "2025-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        ("def456", "video1.mp4", 5242880, "video/mp4", "fid_2", 102, "-100123", "2025-01-02T00:00:00"),
    )
    conn.execute(
        "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        ("ghi789", "anim.gif", 51200, "image/gif", "fid_3", 103, "-100123", "2025-01-03T00:00:00"),
    )
    # Insert album
    conn.execute("INSERT INTO albums (name, description, created_at) VALUES (?,?,?)", ("Vacation", "Trip photos", "2025-01-01"))
    conn.execute("INSERT INTO album_assets (album_id, asset_id) VALUES (1, 1)")
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def client(test_db: Path) -> TestClient:
    """Create a TestClient with the test database."""
    import tg_media_store.server as srv
    # Patch module-level DB_PATH
    original_db = srv.DB_PATH
    srv.DB_PATH = test_db
    # Allow access without auth
    original_pass = srv.VIEWER_PASS
    srv.VIEWER_PASS = "changeme"
    original_token = srv.VIEWER_TOKEN
    srv.VIEWER_TOKEN = ""

    tc = TestClient(srv.app)
    yield tc

    srv.DB_PATH = original_db
    srv.VIEWER_PASS = original_pass
    srv.VIEWER_TOKEN = original_token


class TestIndex:
    def test_get_index_returns_html(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "TeleVault" in r.text

    def test_get_index_contains_dashboard_elements(self, client: TestClient) -> None:
        r = client.get("/")
        assert "stats-bar" in r.text
        assert "upload" in r.text.lower()


class TestApiStats:
    def test_stats(self, client: TestClient) -> None:
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert data["total_size"] == 102400 + 5242880 + 51200


class TestApiMedia:
    def test_list_all(self, client: TestClient) -> None:
        r = client.get("/api/media")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_search(self, client: TestClient) -> None:
        r = client.get("/api/media?q=photo")
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "photo1.jpg"

    def test_type_filter(self, client: TestClient) -> None:
        r = client.get("/api/media?type=video")
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["type"] == "video"

    def test_album_filter(self, client: TestClient) -> None:
        r = client.get("/api/media?album=Vacation")
        data = r.json()
        assert data["total"] == 1

    def test_pagination(self, client: TestClient) -> None:
        r = client.get("/api/media?limit=1&offset=0")
        data = r.json()
        assert len(data["items"]) == 1
        assert data["total"] == 3


class TestApiAlbums:
    def test_albums(self, client: TestClient) -> None:
        r = client.get("/api/albums")
        assert r.status_code == 200
        data = r.json()
        assert len(data["albums"]) == 1
        assert data["albums"][0]["album"] == "Vacation"
        assert data["albums"][0]["count"] == 1


class TestAuth:
    def test_auth_required_when_configured(self, test_db: Path) -> None:
        import tg_media_store.server as srv
        original_db = srv.DB_PATH
        original_pass = srv.VIEWER_PASS
        original_token = srv.VIEWER_TOKEN
        srv.DB_PATH = test_db
        srv.VIEWER_PASS = "secret123"
        srv.VIEWER_TOKEN = ""

        tc = TestClient(srv.app)
        r = tc.get("/api/stats")
        assert r.status_code == 401

        # With correct credentials
        r = tc.get("/api/stats", auth=("viewer", "secret123"))
        assert r.status_code == 200

        srv.DB_PATH = original_db
        srv.VIEWER_PASS = original_pass
        srv.VIEWER_TOKEN = original_token
