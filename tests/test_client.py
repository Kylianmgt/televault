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


class TestLosslessUpload:
    """as_document stores bytes verbatim — Telegram re-encodes photos, so an
    archival importer must be able to bypass that."""

    @patch("tg_media_store.client.requests.post")
    def test_as_document_sends_to_sendDocument(
        self, mock_post: MagicMock, store: TelegramMediaStore, sample_image: Path
    ) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 7, "document": {"file_id": "doc_id"}}},
        )
        result = store.upload_file(sample_image, as_document=True)

        assert result is not None
        # The file_id must be read from the document key, not photo
        assert result["file_id"] == "doc_id"
        endpoint = mock_post.call_args[0][0]
        assert endpoint.endswith("/sendDocument")

    @patch("tg_media_store.client.requests.post")
    def test_default_still_sends_photo_inline(
        self, mock_post: MagicMock, store: TelegramMediaStore, sample_image: Path
    ) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 8, "photo": [{"file_id": "photo_id", "width": 800, "height": 800}]}},
        )
        result = store.upload_file(sample_image)

        assert result["file_id"] == "photo_id"
        assert mock_post.call_args[0][0].endswith("/sendPhoto")

    @patch("tg_media_store.client.requests.post")
    def test_dimension_fallback_reads_document_file_id(
        self, mock_post: MagicMock, store: TelegramMediaStore, sample_image: Path
    ) -> None:
        """A photo Telegram rejects for its dimensions is retried as a document;
        the successful upload must not be dropped by reading the wrong key."""
        first = MagicMock(status_code=400, text="Bad Request: PHOTO_INVALID_DIMENSIONS")
        second = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 9, "document": {"file_id": "fallback_doc"}}},
        )
        mock_post.side_effect = [first, second]

        result = store.upload_file(sample_image)

        assert result is not None
        assert result["file_id"] == "fallback_doc"
        assert result["message_id"] == 9


class TestFileIdExtraction:
    """Telegram's response key need not match the method used: an MP4 sent via
    sendDocument comes back under `video`. Inferring the key from the request
    silently discarded successful uploads."""

    def test_mp4_sent_as_document_returns_video_key(self) -> None:
        from tg_media_store.client import _extract_file_id
        assert _extract_file_id({"video": {"file_id": "vid"}}) == "vid"

    def test_document_key(self) -> None:
        from tg_media_store.client import _extract_file_id
        assert _extract_file_id({"document": {"file_id": "doc"}}) == "doc"

    def test_animation_key(self) -> None:
        from tg_media_store.client import _extract_file_id
        assert _extract_file_id({"animation": {"file_id": "anim"}}) == "anim"

    def test_photo_ladder_takes_largest(self) -> None:
        from tg_media_store.client import _extract_file_id
        result = {"photo": [{"file_id": "small"}, {"file_id": "large"}]}
        assert _extract_file_id(result) == "large"

    def test_unknown_shape_returns_empty(self) -> None:
        from tg_media_store.client import _extract_file_id
        assert _extract_file_id({"message_id": 1}) == ""

    def test_null_valued_key_does_not_crash(self) -> None:
        from tg_media_store.client import _extract_file_id
        assert _extract_file_id({"document": None, "video": {"file_id": "v"}}) == "v"

    @patch("tg_media_store.client.requests.post")
    def test_video_as_document_upload_succeeds_end_to_end(
        self, mock_post: MagicMock, store: TelegramMediaStore, tmp_path: Path
    ) -> None:
        """Regression: this returned None before, losing a stored file."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00\x01\x02fake-mp4-payload")
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 55, "video": {"file_id": "vid_id"}}},
        )
        result = store.upload_file(clip, as_document=True)

        assert result is not None
        assert result["file_id"] == "vid_id"
        assert result["message_id"] == 55


class TestVideoPoster:
    """Documents carry no Telegram preview unless one is attached, and archival
    uploads are documents — so videos must ship a poster with them."""

    @patch("tg_media_store.client.requests.post")
    def test_video_document_upload_attaches_a_thumbnail(
        self, mock_post: MagicMock, store: TelegramMediaStore, tmp_path: Path
    ) -> None:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00fake-mp4")
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 3,
                                                 "document": {"file_id": "d",
                                                              "thumbnail": {"file_id": "t"}}}},
        )
        def fake_poster(src, dest):
            Path(dest).write_bytes(b"\xff\xd8\xff-poster")
            return True

        with patch("tg_media_store.client.make_video_poster", side_effect=fake_poster):
            store.upload_file(clip, as_document=True)

        files = mock_post.call_args.kwargs["files"]
        assert "thumbnail" in files, "no poster sent — Telegram will store no preview"

    @patch("tg_media_store.client.requests.post")
    def test_stores_the_returned_thumb_file_id(
        self, mock_post: MagicMock, store: TelegramMediaStore, tmp_path: Path
    ) -> None:
        clip = tmp_path / "clip2.mp4"
        clip.write_bytes(b"\x00fake-mp4-2")
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 4,
                                                 "document": {"file_id": "d2",
                                                              "thumbnail": {"file_id": "thumb42"}}}},
        )
        def fake_poster(src, dest):
            Path(dest).write_bytes(b"\xff\xd8\xff-poster")
            return True

        with patch("tg_media_store.client.make_video_poster", side_effect=fake_poster):
            result = store.upload_file(clip, as_document=True)

        conn = store._get_conn()
        row = conn.execute(
            "SELECT telegram_thumb_file_id FROM assets WHERE id = ?", (result["id"],)
        ).fetchone()
        assert row[0] == "thumb42"

    @patch("tg_media_store.client.requests.post")
    def test_non_video_document_sends_no_poster(
        self, mock_post: MagicMock, store: TelegramMediaStore, tmp_path: Path
    ) -> None:
        doc = tmp_path / "notes.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 5, "document": {"file_id": "d3"}}},
        )
        store.upload_file(doc, as_document=True)
        assert "thumbnail" not in mock_post.call_args.kwargs["files"]

    @patch("tg_media_store.client.requests.post")
    def test_unreadable_poster_does_not_lose_the_upload(
        self, mock_post: MagicMock, store: TelegramMediaStore, tmp_path: Path
    ) -> None:
        """A preview is cosmetic — the file must still be stored without it."""
        clip = tmp_path / "clip3.mp4"
        clip.write_bytes(b"\x00fake-mp4-3")
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 6, "document": {"file_id": "d4"}}},
        )
        # claims success but writes nothing
        with patch("tg_media_store.client.make_video_poster", return_value=True):
            result = store.upload_file(clip, as_document=True)

        assert result is not None
        assert result["file_id"] == "d4"
