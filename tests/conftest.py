"""Shared fixtures for telegram-media-store tests."""

import tempfile
from pathlib import Path

import pytest

from tg_media_store.client import TelegramMediaStore


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def store(tmp_path: Path) -> TelegramMediaStore:
    """Create a TelegramMediaStore with a temporary database."""
    db = tmp_path / "test.db"
    s = TelegramMediaStore(
        bot_token="123456:ABC-DEF",
        channel_id="-1001234567890",
        db_path=db,
        cache_dir=tmp_path / "cache",
    )
    yield s
    s.close()


@pytest.fixture
def sample_image(tmp_path: Path) -> Path:
    """Create a small valid JPEG file."""
    # Minimal JPEG (1x1 red pixel)
    import struct
    p = tmp_path / "test.jpg"
    # Use PIL if available, otherwise write minimal bytes
    try:
        from PIL import Image
        img = Image.new("RGB", (10, 10), color="red")
        img.save(p, format="JPEG")
    except ImportError:
        # Minimal valid JPEG
        p.write_bytes(
            b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
            b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t'
            b'\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a'
            b'\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342'
            b'\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00'
            b'\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b'
            b'\xff\xda\x00\x08\x01\x01\x00\x00?\x00T\xdb\x9e\xa7\x13\xff\xd9'
        )
    return p


@pytest.fixture
def sample_files(tmp_path: Path) -> Path:
    """Create a directory with several test files."""
    d = tmp_path / "media"
    d.mkdir()
    try:
        from PIL import Image
        for i, color in enumerate(["red", "green", "blue"]):
            img = Image.new("RGB", (10, 10), color=color)
            img.save(d / f"img_{i}.jpg", format="JPEG")
    except ImportError:
        for i in range(3):
            (d / f"img_{i}.jpg").write_bytes(b'\xff\xd8\xff\xe0' + bytes([i]) * 100 + b'\xff\xd9')
    return d
