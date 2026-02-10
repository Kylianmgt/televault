"""Tests for tg_media_store.client with mocked Telegram API."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tg_media_store.client import TelegramMediaStore, file_sha256


class TestFileHash:
    def test_consistent(self, sample_image: Path) -> None:
        h1 = file_sha256(sample_image)
        h2 = file_sha256(sample_image)
        assert h1 == h2
        assert len(h1) == 64

    def test_different_files(self, sample_files: Path) -> None:
        files = sorted(sample_files.glob("*.jpg"))
        hashes = [file_sha256(f) for f in files]
        assert len(set(hashes)) == len(hashes), "Different files should have different hashes"


class TestDatabaseInit:
    def test_creates_tables(self, store: TelegramMediaStore) -> None:
        conn = store._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in tables}
        assert "assets" in names
        assert "albums" in names
        assert "album_assets" in names

    def test_stats_empty(self, store: TelegramMediaStore) -> None:
        s = store.stats()
        assert s["total_assets"] == 0
        assert s["total_size_bytes"] == 0
        assert s["albums"] == 0


class TestUploadDedup:
    @patch("tg_media_store.client.requests.post")
    def test_upload_returns_result(self, mock_post: MagicMock, store: TelegramMediaStore, sample_image: Path) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "ok": True,
                "result": {
                    "message_id": 42,
                    "photo": [
                        {"file_id": "small_id", "width": 90, "height": 90},
                        {"file_id": "big_id", "width": 800, "height": 800},
                    ],
                },
            },
        )
        result = store.upload_file(sample_image)
        assert result is not None
        assert result["file_id"] == "big_id"
        assert result["message_id"] == 42

    @patch("tg_media_store.client.requests.post")
    def test_dedup_skips_second_upload(self, mock_post: MagicMock, store: TelegramMediaStore, sample_image: Path) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "ok": True,
                "result": {
                    "message_id": 42,
                    "photo": [{"file_id": "big_id", "width": 800, "height": 800}],
                },
            },
        )
        r1 = store.upload_file(sample_image)
        r2 = store.upload_file(sample_image)
        assert r1 is not None
        assert r2 is not None
        # Second call should NOT have called the API again
        assert mock_post.call_count == 1
        assert r2["id"] == r1["id"]

    def test_upload_nonexistent(self, store: TelegramMediaStore) -> None:
        result = store.upload_file("/nonexistent/file.jpg")
        assert result is None

    def test_upload_empty_file(self, store: TelegramMediaStore, tmp_path: Path) -> None:
        empty = tmp_path / "empty.jpg"
        empty.write_bytes(b"")
        result = store.upload_file(empty)
        assert result is None


class TestUploadDirectory:
    @patch("tg_media_store.client.requests.post")
    def test_upload_directory(self, mock_post: MagicMock, store: TelegramMediaStore, sample_files: Path) -> None:
        call_count = [0]

        def side_effect(*a, **kw):
            call_count[0] += 1
            return MagicMock(
                status_code=200,
                json=lambda: {
                    "ok": True,
                    "result": {
                        "message_id": call_count[0],
                        "photo": [{"file_id": f"fid_{call_count[0]}", "width": 10, "height": 10}],
                    },
                },
            )

        mock_post.side_effect = side_effect
        store.upload_delay = 0  # speed up test

        result = store.upload_directory(sample_files)
        assert result["uploaded"] == 3
        assert result["skipped"] == 0

    @patch("tg_media_store.client.requests.post")
    def test_upload_non_media_files(self, mock_post: MagicMock, store: TelegramMediaStore, tmp_path: Path) -> None:
        """upload_directory uploads any file type by default (no extension filter)."""
        d = tmp_path / "mixed"
        d.mkdir()
        (d / "notes.txt").write_text("hello world")
        (d / "report.pdf").write_bytes(b"%PDF-1.4 fake content here")
        (d / "data.csv").write_text("a,b,c\n1,2,3")

        call_count = [0]
        def side_effect(*a, **kw):
            call_count[0] += 1
            return MagicMock(
                status_code=200,
                json=lambda: {
                    "ok": True,
                    "result": {
                        "message_id": call_count[0],
                        "document": {"file_id": f"doc_{call_count[0]}"},
                    },
                },
            )

        mock_post.side_effect = side_effect
        store.upload_delay = 0

        result = store.upload_directory(d)
        assert result["uploaded"] == 3
        assert result["failed"] == 0


class TestAlbums:
    def test_create_and_get_album(self, store: TelegramMediaStore) -> None:
        aid = store.get_or_create_album("Test Album", "A test")
        aid2 = store.get_or_create_album("Test Album")
        assert aid == aid2
        assert store.stats()["albums"] == 1

    @patch("tg_media_store.client.requests.post")
    def test_add_to_album(self, mock_post: MagicMock, store: TelegramMediaStore, sample_image: Path) -> None:
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
        album_id = store.get_or_create_album("My Album")
        store.add_to_album(album_id, result["id"])

        items = store.list_assets(album="My Album")
        assert len(items) == 1


class TestListAndGet:
    @patch("tg_media_store.client.requests.post")
    def test_list_and_get(self, mock_post: MagicMock, store: TelegramMediaStore, sample_image: Path) -> None:
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
        r = store.upload_file(sample_image)
        assets = store.list_assets()
        assert len(assets) == 1
        assert assets[0]["filename"] == sample_image.name

        asset = store.get_asset(r["id"])
        assert asset is not None
        assert asset["file_hash"] is not None
