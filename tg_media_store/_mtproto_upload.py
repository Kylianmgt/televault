"""Standalone MTProto uploader, run as a subprocess.

Pyrogram is driven from this module's own main thread rather than from a worker
thread of the server process. That is not a stylistic choice: an upload driven
from a worker thread never completes — no exception, no progress, it simply
hangs until the caller times out — while the identical call in a process of its
own finishes in seconds. Isolating it here also means a wedged transfer can be
killed without touching the server.

Reads a JSON job on argv[1], prints a JSON result on stdout.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any


def _thumb_of(media: Any) -> str:
    thumbs = getattr(media, "thumbs", None) or []
    return thumbs[-1].file_id if thumbs else ""


async def _run(job: dict) -> dict:
    from pyrogram import Client

    client = Client(
        "tg_media_store_upload",
        api_id=int(job["api_id"]),
        api_hash=job["api_hash"],
        bot_token=job["bot_token"],
        no_updates=True,
        in_memory=True,
    )

    async with client:
        chat = int(job["channel_id"])
        path = job["path"]
        caption = (job.get("caption") or "")[:1024]
        name = job.get("file_name") or None
        thumb = job.get("thumb") or None

        if job.get("as_video"):
            msg = await client.send_video(
                chat, path, caption=caption, file_name=name,
                supports_streaming=True,
                **({"thumb": thumb} if thumb else {}),
            )
            media = msg.video
        else:
            msg = await client.send_document(
                chat, path, caption=caption, file_name=name,
                **({"thumb": thumb} if thumb else {}),
            )
            media = msg.document

        return {
            "ok": True,
            "message_id": msg.id,
            "file_id": getattr(media, "file_id", "") if media else "",
            "thumb_file_id": _thumb_of(media) if media else "",
        }


def main() -> int:
    try:
        job = json.loads(sys.argv[1])
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"bad job: {exc}"}))
        return 2

    try:
        print(json.dumps(asyncio.run(_run(job))))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
