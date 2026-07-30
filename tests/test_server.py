"""Tests for tg_media_store.server FastAPI endpoints."""

import pathlib
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

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
            metadata TEXT,
            telegram_thumb_file_id TEXT
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
    # Thumbnails cache to a real directory that outlives the test run, so a
    # previously generated file would short-circuit the endpoint under test.
    original_thumbs = srv.THUMBS_DIR
    srv.THUMBS_DIR = test_db.parent / "thumbs"
    srv.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    # Allow access without auth
    original_pass = srv.VIEWER_PASS
    srv.VIEWER_PASS = "changeme"
    original_token = srv.VIEWER_TOKEN
    srv.VIEWER_TOKEN = ""

    tc = TestClient(srv.app)
    yield tc

    srv.DB_PATH = original_db
    srv.THUMBS_DIR = original_thumbs
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
            # The recorded filename must be the intended one, with no temp-file
            # prefix leaking into it.
            assert pathlib.Path(path).name == "video.mp4"
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig_token, orig_channel


class TestHealthz:
    """The liveness probe must work without credentials — an orchestrator has
    none, and a probe that 401s marks a healthy container as failed."""

    def test_healthz_open_when_auth_is_configured(self, test_db: Path) -> None:
        import tg_media_store.server as srv
        original_db, original_pass, original_token = srv.DB_PATH, srv.VIEWER_PASS, srv.VIEWER_TOKEN
        srv.DB_PATH = test_db
        srv.VIEWER_PASS = "secret123"
        srv.VIEWER_TOKEN = ""
        try:
            tc = TestClient(srv.app)
            # A credentialled endpoint rejects us…
            assert tc.get("/api/stats").status_code == 401
            # …but the probe still answers.
            r = tc.get("/healthz")
            assert r.status_code == 200
            assert r.json() == {"ok": True}
        finally:
            srv.DB_PATH, srv.VIEWER_PASS, srv.VIEWER_TOKEN = original_db, original_pass, original_token

    def test_healthz_reports_503_when_index_unreadable(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        with patch.object(srv, "_db", side_effect=sqlite3.OperationalError("locked")):
            r = client.get("/healthz")
        assert r.status_code == 503


class TestThumbnails:
    """A thumbnail endpoint must return an image or 404 — never the original
    file. Returning a multi-MB MP4 made every video render as a broken <img>."""

    def _asset(self, test_db: Path, msg_id: int, mime: str, name: str) -> None:
        conn = sqlite3.connect(str(test_db))
        conn.execute(
            "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"h{msg_id}", name, 4_000_000, mime, f"fid_{msg_id}", msg_id, "-100123", "2026-07-30T00:00:00"),
        )
        conn.commit()
        conn.close()

    def test_video_without_ffmpeg_success_returns_404_not_the_video(
        self, client: TestClient, test_db: Path
    ) -> None:
        import tg_media_store.server as srv
        self._asset(test_db, 4001, "video/mp4", "clip.mp4")
        original = srv.BOT_TOKEN
        srv.BOT_TOKEN = "tok"
        try:
            with patch.object(srv, "requests") as rq:
                rq.get.return_value = MagicMock(
                    status_code=200,
                    json=lambda: {"result": {"file_path": "videos/clip.mp4"}},
                    content=b"not-a-real-mp4",
                )
                # ffmpeg cannot make a frame from this, so no thumbnail exists
                with patch.object(srv.subprocess, "run", side_effect=OSError("no ffmpeg")):
                    r = client.get("/thumb/4001")
            assert r.status_code == 404
            assert "video" not in r.headers.get("content-type", "")
        finally:
            srv.BOT_TOKEN = original

    def test_video_thumbnail_served_as_jpeg_when_ffmpeg_succeeds(
        self, client: TestClient, test_db: Path
    ) -> None:
        import tg_media_store.server as srv
        self._asset(test_db, 4002, "video/mp4", "clip2.mp4")
        original = srv.BOT_TOKEN
        srv.BOT_TOKEN = "tok"

        def fake_run(cmd, **kwargs):
            # ffmpeg writes its output to the last argument
            Path(cmd[-1]).write_bytes(b"\xff\xd8\xff-jpeg-bytes")
            return MagicMock(returncode=0)

        try:
            with patch.object(srv, "requests") as rq:
                rq.get.return_value = MagicMock(
                    status_code=200,
                    json=lambda: {"result": {"file_path": "videos/clip2.mp4"}},
                    content=b"fake-mp4",
                )
                with patch.object(srv.subprocess, "run", side_effect=fake_run):
                    r = client.get("/thumb/4002")
            assert r.status_code == 200
            assert r.headers["content-type"] == "image/jpeg"
            assert r.content.startswith(b"\xff\xd8\xff")
        finally:
            srv.BOT_TOKEN = original

    def test_unknown_type_returns_404_rather_than_raw_bytes(
        self, client: TestClient, test_db: Path
    ) -> None:
        import tg_media_store.server as srv
        self._asset(test_db, 4003, "application/zip", "archive.zip")
        original = srv.BOT_TOKEN
        srv.BOT_TOKEN = "tok"
        try:
            with patch.object(srv, "requests") as rq:
                rq.get.return_value = MagicMock(
                    status_code=200,
                    json=lambda: {"result": {"file_path": "docs/archive.zip"}},
                    content=b"PK\x03\x04payload",
                )
                r = client.get("/thumb/4003")
            assert r.status_code == 404
            assert b"payload" not in r.content
        finally:
            srv.BOT_TOKEN = original


class TestSchemaMigration:
    """Existing vaults predate telegram_thumb_file_id. Adding it in place keeps
    their dedup history — recreating the table would lose every stored hash."""

    def test_missing_column_is_added_in_place(self, tmp_path: Path) -> None:
        import tg_media_store.server as srv
        legacy = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.execute("""
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash TEXT UNIQUE, original_path TEXT, filename TEXT,
                file_size INTEGER, mime_type TEXT, telegram_file_id TEXT,
                telegram_message_id INTEGER, channel_id TEXT, uploaded_at TEXT,
                metadata TEXT
            )
        """)
        conn.execute(
            "INSERT INTO assets (file_hash, filename, telegram_message_id) VALUES (?,?,?)",
            ("keepme", "old.jpg", 1),
        )
        conn.commit()
        conn.close()

        original = srv.DB_PATH
        srv.DB_PATH = legacy
        try:
            srv._init_db()
            conn = sqlite3.connect(str(legacy))
            cols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
            rows = conn.execute("SELECT file_hash FROM assets").fetchall()
            conn.close()
            assert "telegram_thumb_file_id" in cols
            # the pre-existing row survived
            assert rows == [("keepme",)]
        finally:
            srv.DB_PATH = original


class TestParallelIngest:
    """Items are fetched concurrently; ordering and accounting must survive it."""

    def test_results_keep_request_order(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        orig = (srv.BOT_TOKEN, srv.CHANNEL_ID)
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"
        urls = [f"https://e.test/{i}.jpg" for i in range(6)]
        try:
            with patch.object(srv, "_ingest_one", side_effect=lambda item, f: {"url": item["url"], "ok": True, "deduped": False, "asset_id": 1, "msg_id": 2}):
                r = client.post("/api/ingest-url", json=[{"url": u} for u in urls])
            body = r.json()
            assert [x["url"] for x in body["results"]] == urls
            assert body["added"] == 6
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig

    def test_runs_items_concurrently(self, client: TestClient) -> None:
        """Sequential execution of 6 slow items would take ~6x one item."""
        import time
        import tg_media_store.server as srv
        orig = (srv.BOT_TOKEN, srv.CHANNEL_ID)
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"

        def slow(item, factory):
            time.sleep(0.3)
            return {"url": item["url"], "ok": True, "deduped": False, "asset_id": 1, "msg_id": 2}

        try:
            with patch.object(srv, "_ingest_one", side_effect=slow):
                started = time.monotonic()
                r = client.post("/api/ingest-url", json=[{"url": f"u{i}"} for i in range(6)])
                elapsed = time.monotonic() - started
            assert r.json()["added"] == 6
            # 6 x 0.3s sequential = 1.8s; with 3 workers it should be well under
            assert elapsed < 1.2, f"took {elapsed:.2f}s — looks sequential"
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig

    def test_one_failing_item_does_not_sink_the_batch(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        orig = (srv.BOT_TOKEN, srv.CHANNEL_ID)
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"

        def mixed(item, factory):
            if item["url"].endswith("bad"):
                return {"url": item["url"], "ok": False, "error": "boom"}
            return {"url": item["url"], "ok": True, "deduped": False, "asset_id": 1, "msg_id": 2}

        try:
            with patch.object(srv, "_ingest_one", side_effect=mixed):
                r = client.post("/api/ingest-url", json=[{"url": "ok1"}, {"url": "bad"}, {"url": "ok2"}])
            body = r.json()
            assert body["added"] == 2 and body["failed"] == 1
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig


class TestTelegramNativeThumbnail:
    """Prefer Telegram's own preview over pulling the original back."""

    def test_uses_stored_thumb_file_id_without_downloading_original(
        self, client: TestClient, test_db: Path
    ) -> None:
        import tg_media_store.server as srv
        conn = sqlite3.connect(str(test_db))
        conn.execute(
            "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at, telegram_thumb_file_id) VALUES (?,?,?,?,?,?,?,?,?)",
            ("bigvid", "big.mp4", 300_000_000, "video/mp4", "orig_fid", 7001, "-100123",
             "2026-07-30T00:00:00", "thumb_fid"),
        )
        conn.commit()
        conn.close()

        original = srv.BOT_TOKEN
        srv.BOT_TOKEN = "tok"
        requested = []

        def fake_get(url, **kwargs):
            requested.append((url, kwargs.get("params")))
            if "getFile" in url:
                return MagicMock(status_code=200, json=lambda: {"result": {"file_path": "thumbs/t.jpg"}})
            return MagicMock(status_code=200, content=b"\xff\xd8\xff-tiny-jpeg")

        try:
            with patch.object(srv.requests, "get", side_effect=fake_get):
                r = client.get("/thumb/7001")
            assert r.status_code == 200
            assert r.headers["content-type"] == "image/jpeg"
            # It asked Telegram for the *thumbnail* id, never the original
            asked = [p for _, p in requested if p]
            assert {"file_id": "thumb_fid"} in asked
            assert {"file_id": "orig_fid"} not in asked
        finally:
            srv.BOT_TOKEN = original

    def test_falls_back_to_original_when_no_thumb_id(
        self, client: TestClient, test_db: Path
    ) -> None:
        import tg_media_store.server as srv
        conn = sqlite3.connect(str(test_db))
        conn.execute(
            "INSERT INTO assets (file_hash, filename, file_size, mime_type, telegram_file_id, telegram_message_id, channel_id, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
            ("nothumb", "img.jpg", 2048, "image/jpeg", "orig2", 7002, "-100123", "2026-07-30T00:00:00"),
        )
        conn.commit()
        conn.close()

        original = srv.BOT_TOKEN
        srv.BOT_TOKEN = "tok"
        asked = []

        def fake_get(url, **kwargs):
            if kwargs.get("params"):
                asked.append(kwargs["params"])
            if "getFile" in url:
                return MagicMock(status_code=200, json=lambda: {"result": {"file_path": "photos/i.jpg"}})
            return MagicMock(status_code=200, content=b"rawbytes")

        try:
            with patch.object(srv.requests, "get", side_effect=fake_get):
                client.get("/thumb/7002")
            assert {"file_id": "orig2"} in asked
        finally:
            srv.BOT_TOKEN = original


class TestDiskGuards:
    """Scratch space is the only place this service grows. An ingest that runs
    the volume dry takes the container with it, so the limits are load-bearing."""

    def test_sweep_removes_stale_dirs_but_keeps_fresh_ones(self, tmp_path: Path) -> None:
        import os as _os
        import time as _time
        import tg_media_store.server as srv
        original = srv.INGEST_TMP_ROOT
        srv.INGEST_TMP_ROOT = tmp_path / "ingest"
        srv.INGEST_TMP_ROOT.mkdir()
        try:
            stale = srv.INGEST_TMP_ROOT / "stale"
            stale.mkdir()
            (stale / "big.bin").write_bytes(b"x" * 1024)
            _os.utime(stale, (_time.time() - 7200, _time.time() - 7200))

            fresh = srv.INGEST_TMP_ROOT / "fresh"
            fresh.mkdir()
            (fresh / "wip.bin").write_bytes(b"y" * 1024)

            removed = srv.sweep_ingest_temp(max_age_seconds=1800)
            assert removed == 1
            assert not stale.exists(), "stale scratch dir survived"
            assert fresh.exists(), "swept away an in-flight download"
        finally:
            srv.INGEST_TMP_ROOT = original

    def test_sweep_never_touches_the_system_temp_root(self, tmp_path: Path) -> None:
        import tg_media_store.server as srv
        # The sweep must be scoped to our own directory, never /tmp at large.
        assert "televault" in str(srv.INGEST_TMP_ROOT).lower()

    def test_refuses_to_download_when_disk_is_low(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        orig = (srv.BOT_TOKEN, srv.CHANNEL_ID)
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"
        try:
            with patch.object(srv, "_free_bytes", return_value=1024):
                r = client.post("/api/ingest-url", json={"url": "https://e.test/a.mp4"})
            body = r.json()
            assert body["failed"] == 1
            assert "free" in body["results"][0]["error"]
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig

    def test_download_over_the_cap_is_aborted(self, client: TestClient) -> None:
        import tg_media_store.server as srv
        orig = (srv.BOT_TOKEN, srv.CHANNEL_ID, srv.MAX_DOWNLOAD_BYTES)
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"
        srv.MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024

        class Endless:
            headers: dict = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): return None
            def iter_content(self, chunk_size=0):
                # No Content-Length; the cap must hold on the stream itself.
                for _ in range(10):
                    yield b"z" * (1024 * 1024)

        try:
            with patch("tg_media_store.client.TelegramMediaStore"):
                with patch.object(srv.requests, "get", return_value=Endless()):
                    r = client.post("/api/ingest-url", json={"url": "https://e.test/huge.mp4"})
            body = r.json()
            assert body["failed"] == 1
            assert "download limit" in body["results"][0]["error"]
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID, srv.MAX_DOWNLOAD_BYTES = orig

    def test_thumbnail_cache_is_trimmed_oldest_first(self, tmp_path: Path) -> None:
        import os as _os
        import time as _time
        import tg_media_store.server as srv
        orig = (srv.THUMBS_DIR, srv.THUMBS_MAX_BYTES)
        srv.THUMBS_DIR = tmp_path / "thumbs"
        srv.THUMBS_DIR.mkdir()
        srv.THUMBS_MAX_BYTES = 3000
        try:
            for i, age in enumerate([5000, 4000, 100]):
                f = srv.THUMBS_DIR / f"t{i}.jpg"
                f.write_bytes(b"x" * 2000)
                _os.utime(f, (_time.time() - age, _time.time() - age))

            removed = srv.trim_thumbnail_cache()
            assert removed >= 1
            # newest survives
            assert (srv.THUMBS_DIR / "t2.jpg").exists()
            # oldest evicted first
            assert not (srv.THUMBS_DIR / "t0.jpg").exists()
        finally:
            srv.THUMBS_DIR, srv.THUMBS_MAX_BYTES = orig

    def test_caller_max_bytes_overrides_the_server_default(self, client: TestClient) -> None:
        """A caller's cap must be enforced here: its own pre-check relies on
        Content-Length, which a server may not send."""
        import tg_media_store.server as srv
        orig = (srv.BOT_TOKEN, srv.CHANNEL_ID)
        srv.BOT_TOKEN, srv.CHANNEL_ID = "tok", "-100123"

        class NoLength:
            headers: dict = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): return None
            def iter_content(self, chunk_size=0):
                for _ in range(8):
                    yield b"q" * (1024 * 1024)

        try:
            with patch("tg_media_store.client.TelegramMediaStore"):
                with patch.object(srv.requests, "get", return_value=NoLength()):
                    r = client.post("/api/ingest-url", json={
                        "url": "https://e.test/nolength.mp4",
                        "max_bytes": 2 * 1024 * 1024,
                    })
            body = r.json()
            assert body["failed"] == 1
            assert "2 MB download limit" in body["results"][0]["error"]
        finally:
            srv.BOT_TOKEN, srv.CHANNEL_ID = orig

    def test_caller_cannot_raise_the_limit_above_the_server_ceiling(self) -> None:
        import tg_media_store.server as srv
        # min() of the two, so a caller can tighten but never loosen.
        assert min(srv.MAX_DOWNLOAD_BYTES, 999 * 1024**3) == srv.MAX_DOWNLOAD_BYTES
