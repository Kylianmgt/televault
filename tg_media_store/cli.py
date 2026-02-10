"""CLI entry point for TeleVault.

Usage::

    tg-media-store upload <file_or_dir>
    tg-media-store fetch <asset_id>
    tg-media-store stats
    tg-media-store cleanup [--yes]
    tg-media-store serve [--host 0.0.0.0] [--port 8099]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    """Load .env file from CWD into os.environ (only sets unset keys)."""
    env_file = Path.cwd() / ".env"
    if not env_file.is_file():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

from .client import TelegramMediaStore


def _get_store(args: argparse.Namespace) -> TelegramMediaStore:
    bot_token = os.environ.get("BOT_TOKEN", "")
    channel_id = os.environ.get("CHANNEL_ID", "")
    if not bot_token or not channel_id:
        print("❌ Set BOT_TOKEN and CHANNEL_ID environment variables.")
        sys.exit(1)
    db_path = getattr(args, "db", None) or os.environ.get("TG_STORE_DB", "tg_media_store.db")
    return TelegramMediaStore(bot_token=bot_token, channel_id=channel_id, db_path=db_path)


def cmd_upload(args: argparse.Namespace) -> None:
    store = _get_store(args)
    target = Path(args.target)
    if target.is_dir():
        result = store.upload_directory(target)
        print(f"✅ Upload complete: {result['uploaded']} new, {result['skipped']} duplicates, {result['failed']} failed")
    elif target.is_file():
        result = store.upload_file(target)
        if result and "file_id" in result:
            print(f"✅ Uploaded: {target.name} (ID: {result['id']})")
        elif result:
            print(f"⏭️ Already uploaded (ID: {result['id']})")
        else:
            print(f"❌ Upload failed: {target.name}")
    else:
        print(f"❌ Not found: {target}")
    store.close()


def cmd_fetch(args: argparse.Namespace) -> None:
    store = _get_store(args)
    path = store.fetch_asset(int(args.asset_id))
    if path:
        print(f"✅ Downloaded: {path}")
    else:
        print(f"❌ Fetch failed for asset {args.asset_id}")
    store.close()


def cmd_stats(args: argparse.Namespace) -> None:
    store = _get_store(args)
    s = store.stats()
    print(f"\n📊 TeleVault Statistics")
    print(f"{'=' * 40}")
    print(f"  Total assets:  {s['total_assets']}")
    print(f"  Total size:    {s['total_size_bytes'] / 1e6:.1f} MB")
    print(f"  Albums:        {s['albums']}")
    print(f"  DB size:       {s['db_size_bytes'] / 1024:.1f} KB")
    store.close()


def cmd_cleanup(args: argparse.Namespace) -> None:
    if not args.yes:
        print("⚠️  This will DELETE local copies of vaulted files!")
        print("   Add --yes to confirm.")
        return
    store = _get_store(args)
    result = store.cleanup_local()
    print(f"🧹 Cleaned up {result['removed']} files, freed {result['freed_bytes'] / 1e6:.1f} MB")
    store.close()


def cmd_serve(args: argparse.Namespace) -> None:
    # Lazy import so uvicorn/fastapi aren't required for CLI-only usage
    os.environ.setdefault("TG_STORE_DB", getattr(args, "db", None) or os.environ.get("TG_STORE_DB", "tg_media_store.db"))
    import uvicorn
    from .server import app  # noqa: F811

    uvicorn.run(app, host=args.host, port=args.port)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tg-media-store",
        description="Use Telegram channels as free, unlimited media storage.",
    )
    parser.add_argument("--db", default=None, help="Path to SQLite database (default: $TG_STORE_DB or tg_media_store.db)")
    sub = parser.add_subparsers(dest="command")

    p_upload = sub.add_parser("upload", help="Upload a file or directory")
    p_upload.add_argument("target", help="File or directory to upload")

    p_fetch = sub.add_parser("fetch", help="Download an asset by ID")
    p_fetch.add_argument("asset_id", help="Asset ID from the database")

    sub.add_parser("stats", help="Show vault statistics")

    p_clean = sub.add_parser("cleanup", help="Remove local copies of vaulted files")
    p_clean.add_argument("--yes", action="store_true", help="Confirm deletion")

    p_serve = sub.add_parser("serve", help="Start the gallery web server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8099)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "upload": cmd_upload,
        "fetch": cmd_fetch,
        "stats": cmd_stats,
        "cleanup": cmd_cleanup,
        "serve": cmd_serve,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
