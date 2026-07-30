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


class TestApiMediaMetadata:
    """The metadata column is exposed and queryable — external ingesters rely
    on it to group and re-find their own assets."""

    def test_metadata_returned_parsed(self, client: TestClient, test_db: Path) -> None:
        conn = sqlite3.connect(str(test_db))
        conn.execute(
            "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            ("meta1", "tagged.jpg", 2048, "image/jpeg", "fid_9", 900, "-100123",
             "2025-02-01T00:00:00", '{"source": "importer", "ref": "abc"}'),
        )
        conn.commit()
        conn.close()

        r = client.get("/api/media?q=tagged")
        item = r.json()["items"][0]
        assert item["metadata"] == {"source": "importer", "ref": "abc"}

    def test_metadata_absent_is_empty_dict(self, client: TestClient) -> None:
        r = client.get("/api/media?q=photo1")
        assert r.json()["items"][0]["metadata"] == {}

    def test_corrupt_metadata_does_not_break_listing(
        self, client: TestClient, test_db: Path
    ) -> None:
        conn = sqlite3.connect(str(test_db))
        conn.execute(
            "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            ("bad1", "broken.jpg", 1024, "image/jpeg", "fid_10", 910, "-100123",
             "2025-02-02T00:00:00", "not-json{"),
        )
        conn.commit()
        conn.close()

        r = client.get("/api/media?q=broken")
        assert r.status_code == 200
        assert r.json()["items"][0]["metadata"] == {}

    def test_tag_filter_selects_only_matching_assets(
        self, client: TestClient, test_db: Path
    ) -> None:
        conn = sqlite3.connect(str(test_db))
        conn.execute(
            "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            ("tag1", "a.jpg", 1024, "image/jpeg", "fid_11", 920, "-100123",
             "2025-02-03T00:00:00", '{"collection": "alpha"}'),
        )
        conn.execute(
            "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            ("tag2", "b.jpg", 1024, "image/jpeg", "fid_12", 921, "-100123",
             "2025-02-04T00:00:00", '{"collection": "beta"}'),
        )
        conn.commit()
        conn.close()

        r = client.get('/api/media?tag="collection": "alpha"')
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "a.jpg"

    def test_q_matches_metadata_as_well_as_filename(
        self, client: TestClient, test_db: Path
    ) -> None:
        conn = sqlite3.connect(str(test_db))
        conn.execute(
            "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            ("qmeta", "opaque-name.jpg", 1024, "image/jpeg", "fid_13", 930, "-100123",
             "2025-02-05T00:00:00", '{"description": "sunset over water"}'),
        )
        conn.commit()
        conn.close()

        r = client.get("/api/media?q=sunset")
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "opaque-name.jpg"


class TestApiIngestUrl:
    """URL ingest downloads remote files and routes them through the normal
    upload path, so dedup and MIME handling are inherited."""

    def test_requires_bot_token(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        original = srv.BOT_TOKEN
        srv.BOT_TOKEN = ""
        try:
            r = client.post("/api/ingest-url", json={"url": "https://e.test/a.jpg"})
            assert r.status_code == 503
        finally:
            srv.BOT_TOKEN = original

    def test_missing_url_is_reported_not_raised(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        orig_token, orig_channel = srv.BOT_TOKEN, srv.CHANNEL_ID
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"
        try:
            with patch("tg_media_store.client.TelegramMediaStore") as store_cls:
                r = client.post("/api/ingest-url", json=[{"filename": "x.jpg"}])
            assert r.status_code == 200
            body = r.json()
            assert body["failed"] == 1
            assert body["results"][0]["error"] == "missing url"
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig_token, orig_channel

    def test_download_failure_is_isolated_per_item(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        orig_token, orig_channel = srv.BOT_TOKEN, srv.CHANNEL_ID
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"
        try:
            with patch("tg_media_store.client.TelegramMediaStore") as store_cls:
                store = store_cls.return_value
                store.upload_file.return_value = {"id": 5, "message_id": 55, "file_id": "f"}
                with patch("tg_media_store.server.requests.get", side_effect=OSError("boom")):
                    r = client.post("/api/ingest-url", json=[
                        {"url": "https://e.test/a.jpg"},
                        {"url": "https://e.test/b.jpg"},
                    ])
            body = r.json()
            assert body["failed"] == 2
            assert body["added"] == 0
            assert all(item["ok"] is False for item in body["results"])
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig_token, orig_channel

    def test_dedup_hit_counts_as_skipped(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        orig_token, orig_channel = srv.BOT_TOKEN, srv.CHANNEL_ID
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): return None
            def iter_content(self, chunk_size=0): yield b"data"

        try:
            with patch("tg_media_store.client.TelegramMediaStore") as store_cls:
                store = store_cls.return_value
                # upload_file returns only {"id": …} on a dedup hit
                store.upload_file.return_value = {"id": 7}
                with patch("tg_media_store.server.requests.get", return_value=FakeResponse()):
                    r = client.post("/api/ingest-url", json={"url": "https://e.test/a.jpg"})
            body = r.json()
            assert body["added"] == 0
            assert body["skipped"] == 1
            assert body["results"][0]["deduped"] is True
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig_token, orig_channel

    def test_successful_ingest_passes_metadata_and_album(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        orig_token, orig_channel = srv.BOT_TOKEN, srv.CHANNEL_ID
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): return None
            def iter_content(self, chunk_size=0): yield b"payload"

        try:
            with patch("tg_media_store.client.TelegramMediaStore") as store_cls:
                store = store_cls.return_value
                store.upload_file.return_value = {"id": 11, "message_id": 99, "file_id": "f"}
                store.get_or_create_album.return_value = 3
                with patch("tg_media_store.server.requests.get", return_value=FakeResponse()):
                    r = client.post("/api/ingest-url", json={
                        "url": "https://e.test/clip.mp4",
                        "metadata": {"source": "importer", "ref": "xyz"},
                        "album": "imported",
                    })

            body = r.json()
            assert body["added"] == 1
            assert body["results"][0]["msg_id"] == 99

            # metadata reached the upload path verbatim
            _, kwargs = store.upload_file.call_args
            assert kwargs["metadata"] == {"source": "importer", "ref": "xyz"}
            # album bookkeeping ran against the returned asset id
            store.get_or_create_album.assert_called_once_with("imported")
            store.add_to_album.assert_called_once_with(3, 11)
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig_token, orig_channel

    def test_filename_suffix_preserved_for_mime_routing(self, client: TestClient) -> None:
        """The upload path infers the Telegram send method from the extension,
        so a query-string URL must still yield a suffixed temp file."""
        import tg_media_store.server as srv
        orig_token, orig_channel = srv.BOT_TOKEN, srv.CHANNEL_ID
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): return None
            def iter_content(self, chunk_size=0): yield b"payload"

        try:
            with patch("tg_media_store.client.TelegramMediaStore") as store_cls:
                store = store_cls.return_value
                store.upload_file.return_value = {"id": 1, "message_id": 2, "file_id": "f"}
                with patch("tg_media_store.server.requests.get", return_value=FakeResponse()):
                    client.post("/api/ingest-url", json={
                        "url": "https://e.test/video.mp4?tag=1&name=x",
                    })
            path = store.upload_file.call_args[0][0]
            assert path.endswith("video.mp4")
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig_token, orig_channel
