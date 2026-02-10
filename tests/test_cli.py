"""Tests for tg_media_store.cli argument parsing and command dispatch."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tg_media_store import cli


class TestArgParsing:
    def test_no_command_exits(self) -> None:
        with patch("sys.argv", ["tg-media-store"]):
            with pytest.raises(SystemExit):
                cli.main()

    def test_upload_parses(self) -> None:
        parser = self._make_parser()
        args = parser.parse_args(["upload", "/tmp/test.jpg"])
        assert args.command == "upload"
        assert args.target == "/tmp/test.jpg"

    def test_fetch_parses(self) -> None:
        parser = self._make_parser()
        args = parser.parse_args(["fetch", "42"])
        assert args.command == "fetch"
        assert args.asset_id == "42"

    def test_stats_parses(self) -> None:
        parser = self._make_parser()
        args = parser.parse_args(["stats"])
        assert args.command == "stats"

    def test_serve_defaults(self) -> None:
        parser = self._make_parser()
        args = parser.parse_args(["serve"])
        assert args.command == "serve"
        assert args.host == "0.0.0.0"
        assert args.port == 8099

    def test_serve_custom(self) -> None:
        parser = self._make_parser()
        args = parser.parse_args(["serve", "--host", "127.0.0.1", "--port", "9000"])
        assert args.host == "127.0.0.1"
        assert args.port == 9000

    def test_cleanup_yes(self) -> None:
        parser = self._make_parser()
        args = parser.parse_args(["cleanup", "--yes"])
        assert args.command == "cleanup"
        assert args.yes is True

    def _make_parser(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--db", default=None)
        sub = parser.add_subparsers(dest="command")
        p_upload = sub.add_parser("upload")
        p_upload.add_argument("target")
        p_fetch = sub.add_parser("fetch")
        p_fetch.add_argument("asset_id")
        sub.add_parser("stats")
        p_clean = sub.add_parser("cleanup")
        p_clean.add_argument("--yes", action="store_true")
        p_serve = sub.add_parser("serve")
        p_serve.add_argument("--host", default="0.0.0.0")
        p_serve.add_argument("--port", type=int, default=8099)
        return parser


class TestCmdUpload:
    @patch.dict("os.environ", {"BOT_TOKEN": "fake:token", "CHANNEL_ID": "-100123"})
    @patch("tg_media_store.cli.TelegramMediaStore")
    def test_upload_file(self, MockStore: MagicMock, tmp_path: Path, capsys) -> None:
        f = tmp_path / "pic.jpg"
        f.write_bytes(b"\xff\xd8test")

        mock_store = MockStore.return_value
        mock_store.upload_file.return_value = {"id": 1, "file_id": "fid_1", "message_id": 10}

        args = MagicMock(target=str(f), db=None)
        cli.cmd_upload(args)

        mock_store.upload_file.assert_called_once()
        out = capsys.readouterr().out
        assert "Uploaded" in out

    @patch.dict("os.environ", {"BOT_TOKEN": "fake:token", "CHANNEL_ID": "-100123"})
    @patch("tg_media_store.cli.TelegramMediaStore")
    def test_upload_dir(self, MockStore: MagicMock, tmp_path: Path, capsys) -> None:
        d = tmp_path / "media"
        d.mkdir()

        mock_store = MockStore.return_value
        mock_store.upload_directory.return_value = {"uploaded": 5, "skipped": 1, "failed": 0}

        args = MagicMock(target=str(d), db=None)
        cli.cmd_upload(args)

        mock_store.upload_directory.assert_called_once()
        out = capsys.readouterr().out
        assert "5 new" in out


class TestCmdStats:
    @patch.dict("os.environ", {"BOT_TOKEN": "fake:token", "CHANNEL_ID": "-100123"})
    @patch("tg_media_store.cli.TelegramMediaStore")
    def test_stats_output(self, MockStore: MagicMock, capsys) -> None:
        mock_store = MockStore.return_value
        mock_store.stats.return_value = {
            "total_assets": 42,
            "total_size_bytes": 1_000_000,
            "albums": 3,
            "db_size_bytes": 8192,
        }

        args = MagicMock(db=None)
        cli.cmd_stats(args)

        out = capsys.readouterr().out
        assert "42" in out
        assert "Albums" in out


class TestCmdFetch:
    @patch.dict("os.environ", {"BOT_TOKEN": "fake:token", "CHANNEL_ID": "-100123"})
    @patch("tg_media_store.cli.TelegramMediaStore")
    def test_fetch_success(self, MockStore: MagicMock, tmp_path: Path, capsys) -> None:
        mock_store = MockStore.return_value
        mock_store.fetch_asset.return_value = tmp_path / "downloaded.jpg"

        args = MagicMock(asset_id="1", db=None)
        cli.cmd_fetch(args)

        out = capsys.readouterr().out
        assert "Downloaded" in out

    @patch.dict("os.environ", {"BOT_TOKEN": "fake:token", "CHANNEL_ID": "-100123"})
    @patch("tg_media_store.cli.TelegramMediaStore")
    def test_fetch_failure(self, MockStore: MagicMock, capsys) -> None:
        mock_store = MockStore.return_value
        mock_store.fetch_asset.return_value = None

        args = MagicMock(asset_id="999", db=None)
        cli.cmd_fetch(args)

        out = capsys.readouterr().out
        assert "failed" in out.lower()
