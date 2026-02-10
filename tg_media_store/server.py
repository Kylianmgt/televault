"""FastAPI server for TeleVault — Telegram-backed cloud storage.

Provides a web UI to browse, search, upload, and stream **any file** stored
in Telegram.  Thumbnails are generated on-the-fly for images; other file types
display intuitive type icons.

Supports pyrogram/MTProto streaming for files > 20 MB when ``TG_API_ID`` and
``TG_API_HASH`` environment variables are set.

Run standalone::

    tg-media-store serve --host 0.0.0.0 --port 8099

Or import the ``app`` object for custom ASGI deployments.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import tempfile
import threading
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

# Auto-load .env file if present (no dependency required)
def _load_dotenv() -> None:
    """Load .env file from CWD or package root into os.environ."""
    for candidate in [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]:
        if candidate.is_file():
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    # Strip optional 'export ' prefix
                    if line.startswith("export "):
                        line = line[7:]
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = value
            break

_load_dotenv()

import requests
from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

try:
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Optional pyrogram
try:
    from pyrogram import Client as PyroClient
    HAS_PYROGRAM = True
except ImportError:
    PyroClient = None  # type: ignore[assignment,misc]
    HAS_PYROGRAM = False

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
DB_PATH = Path(os.environ.get("TG_STORE_DB", "tg_media_store.db"))

TG_API_ID = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")

VIEWER_USER = os.environ.get("TG_STORE_USER", "viewer")
VIEWER_PASS = os.environ.get("TG_STORE_PASS", "changeme")
VIEWER_TOKEN = os.environ.get("TG_STORE_TOKEN", "")

THUMBS_DIR = Path(os.environ.get("TG_STORE_THUMBS", "cache/thumbs"))
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# ---------------------------------------------------------------------------
# Pyrogram client (module-level, lazy init)
# ---------------------------------------------------------------------------
_pyro_client: Any = None
_pyro_ready = False
_pyro_lock = threading.Lock()


def _get_pyro_client() -> Any:
    """Return a started pyrogram client, or None."""
    global _pyro_client
    if not HAS_PYROGRAM or not TG_API_ID or not TG_API_HASH or not BOT_TOKEN:
        return None
    with _pyro_lock:
        if _pyro_client is None:
            session_dir = "/tmp/pyro_sessions"
            os.makedirs(session_dir, exist_ok=True)
            _pyro_client = PyroClient(
                "tg_store_server",
                api_id=TG_API_ID,
                api_hash=TG_API_HASH,
                bot_token=BOT_TOKEN,
                workdir=session_dir,
                no_updates=True,
            )
        return _pyro_client


# ---------------------------------------------------------------------------
# In-memory index (optional, alongside SQLite)
# ---------------------------------------------------------------------------
MEDIA_INDEX: list[dict] = []
INDEX_LOCK = threading.Lock()
INDEX_PATH: Optional[Path] = None


def _load_json_index() -> None:
    """Load in-memory index from JSON file if it exists next to the DB."""
    global INDEX_PATH
    INDEX_PATH = DB_PATH.parent / "index.json"
    if INDEX_PATH.exists():
        try:
            data = json.loads(INDEX_PATH.read_text())
            if isinstance(data, list):
                with INDEX_LOCK:
                    MEDIA_INDEX.clear()
                    MEDIA_INDEX.extend(data)
        except Exception:
            pass


def _save_json_index() -> None:
    """Persist in-memory index to JSON file."""
    if INDEX_PATH:
        try:
            with INDEX_LOCK:
                INDEX_PATH.write_text(json.dumps(MEDIA_INDEX, ensure_ascii=False))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="TeleVault — Telegram Cloud Storage")
security = HTTPBasic(auto_error=False)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _require_auth(
    token: Optional[str] = Query(default=None),
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> bool:
    if VIEWER_TOKEN and token and secrets.compare_digest(token, VIEWER_TOKEN):
        return True
    if credentials is not None:
        if secrets.compare_digest(credentials.username, VIEWER_USER) and secrets.compare_digest(credentials.password, VIEWER_PASS):
            return True
    # If no auth configured (defaults), allow access
    if VIEWER_PASS == "changeme" and not VIEWER_TOKEN:
        return True
    raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


def _init_db() -> None:
    """Create tables if they don't exist yet."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS assets (
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
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            description TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS album_assets (
            album_id INTEGER,
            asset_id INTEGER,
            FOREIGN KEY (album_id) REFERENCES albums(id),
            FOREIGN KEY (asset_id) REFERENCES assets(id),
            UNIQUE(album_id, asset_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_hash ON assets(file_hash)")
    conn.commit()
    conn.close()


# Auto-init DB on import
_init_db()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _tg_base() -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}"


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    global _pyro_ready
    _load_json_index()

    # Start pyrogram if configured
    pyro = _get_pyro_client()
    if pyro:
        try:
            await pyro.start()
            # Register channel peer
            channel_id = int(CHANNEL_ID)
            msg = await pyro.send_message(channel_id, "init")
            await pyro.delete_messages(channel_id, msg.id)
            _pyro_ready = True
        except Exception:
            pass


@app.on_event("shutdown")
async def shutdown() -> None:
    global _pyro_ready
    if _pyro_client and _pyro_ready:
        try:
            await _pyro_client.stop()
            _pyro_ready = False
        except Exception:
            pass


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(_auth: bool = Depends(_require_auth)) -> str:
    """Serve the single-page gallery UI."""
    return _GALLERY_HTML


@app.get("/api/media")
def api_media(
    q: str = "",
    type: str = "",
    album: str = "",
    limit: int = 100,
    offset: int = 0,
    _auth: bool = Depends(_require_auth),
):
    conn = _db()
    where: list[str] = []
    params: list = []

    if q:
        where.append("filename LIKE ?")
        params.append(f"%{q}%")
    if type:
        where.append("mime_type LIKE ?")
        params.append(f"{type}/%")
    if album:
        where.append("a.id IN (SELECT asset_id FROM album_assets aa JOIN albums al ON al.id = aa.album_id WHERE al.name = ?)")
        params.append(album)

    w = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT a.* FROM assets a {w} ORDER BY a.id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()

    total = conn.execute(f"SELECT COUNT(*) FROM assets a {w}", params).fetchone()[0]

    items = []
    for r in rows:
        mime = r["mime_type"] or ""
        fname = r["filename"] or ""
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

        if mime.startswith("video/"):
            media_type = "video"
        elif mime == "image/gif":
            media_type = "animation"
        elif mime.startswith("image/"):
            media_type = "photo"
        elif mime.startswith("audio/"):
            media_type = "audio"
        elif mime == "application/pdf" or ext in ("doc", "docx", "xls", "xlsx",
                "ppt", "pptx", "odt", "ods", "odp", "rtf", "txt", "csv", "md"):
            media_type = "document"
        elif ext in ("zip", "tar", "gz", "bz2", "xz", "rar", "7z", "iso", "tgz"):
            media_type = "archive"
        elif ext in ("py", "js", "ts", "html", "css", "java", "c", "cpp", "go",
                "rs", "rb", "php", "sh", "sql", "json", "xml", "yaml", "yml",
                "toml", "ini", "cfg"):
            media_type = "code"
        else:
            media_type = "other"

        items.append({
            "msg_id": r["telegram_message_id"],
            "file_id": r["telegram_file_id"],
            "title": fname,
            "mime": mime,
            "type": media_type,
            "ext": ext,
            "size": r["file_size"],
            "uploaded_at": r["uploaded_at"] or "",
            "caption": "",
        })

    conn.close()
    return {"items": items, "total": total, "scanning": False}


@app.get("/api/albums")
def api_albums(_auth: bool = Depends(_require_auth)):
    conn = _db()
    rows = conn.execute(
        """SELECT al.name as album, COUNT(*) as cnt
           FROM albums al JOIN album_assets aa ON al.id = aa.album_id
           GROUP BY al.name ORDER BY al.name"""
    ).fetchall()
    conn.close()
    return {"albums": [{"album": r["album"], "count": r["cnt"]} for r in rows]}


@app.get("/api/stats")
def api_stats(_auth: bool = Depends(_require_auth)):
    conn = _db()
    total = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    total_size = conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM assets").fetchone()[0]
    conn.close()
    return {"total": total, "total_size": total_size}


@app.get("/thumb/{msg_id}")
def thumb(msg_id: int, _auth: bool = Depends(_require_auth)):
    """Return a thumbnail for the given message_id."""
    conn = _db()
    row = conn.execute(
        "SELECT telegram_file_id, mime_type, file_size FROM assets WHERE telegram_message_id = ?",
        (msg_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)

    thumb_key = base64.urlsafe_b64encode(str(msg_id).encode()).decode().rstrip("=")
    thumb_path = THUMBS_DIR / f"{thumb_key}.jpg"

    if thumb_path.exists() and thumb_path.stat().st_size > 0:
        return Response(thumb_path.read_bytes(), media_type="image/jpeg")

    # Download original and create thumbnail
    if not BOT_TOKEN:
        raise HTTPException(503, detail="BOT_TOKEN not configured")

    file_id = row["telegram_file_id"]
    try:
        r = requests.get(f"{_tg_base()}/getFile", params={"file_id": file_id}, timeout=30)
        if r.status_code != 200:
            raise HTTPException(502)
        file_path = r.json()["result"]["file_path"]
        dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        content = requests.get(dl_url, timeout=120).content
    except Exception:
        raise HTTPException(502)

    if HAS_PIL and (row["mime_type"] or "").startswith("image/"):
        try:
            im = Image.open(BytesIO(content)).convert("RGB")
            im.thumbnail((420, 420))
            im.save(thumb_path, format="JPEG", quality=85)
            return Response(thumb_path.read_bytes(), media_type="image/jpeg")
        except Exception:
            pass

    # Fallback: return raw content (or placeholder)
    return Response(content, media_type=row["mime_type"] or "application/octet-stream")


@app.get("/stream/{msg_id}")
async def stream(msg_id: int, request: Request, _auth: bool = Depends(_require_auth)):
    """Stream the full file from Telegram.

    Files ≤ 20 MB use the Bot API ``getFile``.
    Files > 20 MB use pyrogram MTProto streaming (with byte-range support for
    video seeking).
    """
    conn = _db()
    row = conn.execute(
        "SELECT telegram_file_id, mime_type, file_size FROM assets WHERE telegram_message_id = ?",
        (msg_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)

    if not BOT_TOKEN:
        raise HTTPException(503, detail="BOT_TOKEN not configured")

    file_id = row["telegram_file_id"]
    file_size = row["file_size"] or 0
    mime = row["mime_type"] or "application/octet-stream"

    # ── Small files: Bot API ──
    if file_size <= 20 * 1024 * 1024:
        try:
            r = requests.get(f"{_tg_base()}/getFile", params={"file_id": file_id}, timeout=30)
            if r.status_code == 200:
                file_path = r.json()["result"]["file_path"]
                dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

                # Forward range request to Telegram CDN
                headers: dict[str, str] = {}
                range_header = request.headers.get("range")
                if range_header:
                    headers["Range"] = range_header

                upstream = requests.get(dl_url, stream=True, headers=headers, timeout=(10, 120))

                def gen():
                    for chunk in upstream.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            yield chunk

                resp_headers: dict[str, str] = {}
                for h in ("Content-Range", "Accept-Ranges", "Content-Length"):
                    if h in upstream.headers:
                        resp_headers[h] = upstream.headers[h]

                return StreamingResponse(
                    gen(),
                    status_code=upstream.status_code,
                    media_type=mime,
                    headers=resp_headers,
                )
        except Exception:
            pass  # Fall through to pyrogram

    # ── Large files: pyrogram MTProto streaming ──
    if not _pyro_client or not _pyro_ready:
        # Last resort: try Bot API anyway (will fail for >20 MB)
        try:
            r = requests.get(f"{_tg_base()}/getFile", params={"file_id": file_id}, timeout=30)
            if r.status_code == 200:
                file_path = r.json()["result"]["file_path"]
                dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                upstream = requests.get(dl_url, stream=True, timeout=(10, 120))
                def gen():
                    for chunk in upstream.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            yield chunk
                return StreamingResponse(gen(), media_type=mime)
        except Exception:
            pass
        raise HTTPException(503, detail="Pyrogram not available for large file streaming")

    channel_id = int(CHANNEL_ID)

    # Parse range header
    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1 if file_size else 0
    partial = False

    if range_header and file_size:
        partial = True
        range_spec = range_header.replace("bytes=", "").strip()
        parts = range_spec.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        end = min(end, file_size - 1)

    length = end - start + 1

    async def gen():
        msg = await _pyro_client.get_messages(channel_id, msg_id)
        if not msg:
            return

        remaining = length
        chunk_offset_skip = start % (1024 * 1024)
        first_chunk = True

        async for chunk in _pyro_client.stream_media(msg, offset=start // (1024 * 1024), limit=0):
            if not chunk:
                break
            data = chunk
            if first_chunk and chunk_offset_skip:
                data = data[chunk_offset_skip:]
                first_chunk = False
            else:
                first_chunk = False
            if len(data) > remaining:
                data = data[:remaining]
            yield data
            remaining -= len(data)
            if remaining <= 0:
                break

    resp_headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }

    if partial:
        resp_headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        return StreamingResponse(gen(), status_code=206, media_type=mime, headers=resp_headers)
    else:
        return StreamingResponse(gen(), status_code=200, media_type=mime, headers=resp_headers)


# ---------------------------------------------------------------------------
# Ingest / Upload API
# ---------------------------------------------------------------------------

@app.post("/api/ingest")
async def api_ingest(request: Request, _auth: bool = Depends(_require_auth)):
    """Receive new items from external sync scripts.

    Accepts a single item dict or a list of item dicts.  Items are added to the
    in-memory index and persisted to ``index.json``.  Each item should have at
    least ``msg_id`` and ``file_id``.
    """
    body = await request.json()
    items = body if isinstance(body, list) else [body]

    with INDEX_LOCK:
        known = {it["msg_id"] for it in MEDIA_INDEX}
        added = 0
        for item in items:
            if item.get("msg_id") not in known:
                MEDIA_INDEX.append(item)
                known.add(item["msg_id"])
                added += 1

    if added:
        _save_json_index()

    return {"added": added, "total": len(MEDIA_INDEX)}


@app.post("/api/upload")
async def api_upload(file: UploadFile, _auth: bool = Depends(_require_auth)):
    """Upload a file via the web UI."""
    if not BOT_TOKEN:
        raise HTTPException(503, detail="BOT_TOKEN not set — check your .env file")
    if not CHANNEL_ID:
        raise HTTPException(503, detail="CHANNEL_ID not set — check your .env file")

    from .client import TelegramMediaStore

    with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{file.filename or 'upload'}") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        store = TelegramMediaStore(
            bot_token=BOT_TOKEN,
            channel_id=CHANNEL_ID,
            db_path=str(DB_PATH),
            upload_delay=0,
            api_id=TG_API_ID if TG_API_ID else None,
            api_hash=TG_API_HASH if TG_API_HASH else None,
        )
        result = store.upload_file(tmp_path)
        store.close()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not result:
        raise HTTPException(500, detail="Upload failed")
    return {"ok": True, "result": result}


# ---------------------------------------------------------------------------
# Embedded gallery HTML (single-page app)
# ---------------------------------------------------------------------------

_GALLERY_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TeleVault — Telegram Cloud Storage</title>
<style>
:root{--bg:#0a0a0f;--surface:#12121a;--surface2:#1a1a26;--surface3:#222234;--border:#2a2a3e;--text:#e8e8f4;--text2:#7878a0;--accent:#7c6cf0;--accent2:#a5a0ff;--accent-glow:rgba(124,108,240,.15);--green:#34d399;--red:#f87171;--radius:14px;--radius-sm:10px;--transition:.2s cubic-bezier(.4,0,.2,1)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
a{color:var(--accent2);text-decoration:none}

/* Stats bar */
.stats-bar{display:flex;gap:16px;padding:20px 28px;border-bottom:1px solid var(--border);background:var(--surface)}
.stat-card{flex:1;padding:16px 20px;border-radius:var(--radius-sm);background:var(--surface2);border:1px solid var(--border);transition:border-color var(--transition)}
.stat-card:hover{border-color:var(--accent)}
.stat-label{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text2);margin-bottom:4px}
.stat-value{font-size:24px;font-weight:700;color:var(--text)}

/* Topbar */
.topbar{position:sticky;top:0;z-index:20;border-bottom:1px solid var(--border);padding:14px 28px;display:flex;align-items:center;gap:14px;backdrop-filter:blur(24px);background:rgba(10,10,15,.88)}
.topbar h1{font-size:20px;font-weight:800;white-space:nowrap;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.search-input{flex:1;max-width:440px;padding:10px 16px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:14px;outline:none;transition:border-color var(--transition)}
.search-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
.search-input::placeholder{color:var(--text2)}

/* Filter buttons */
.filters{display:flex;gap:8px;align-items:center}
.filter-btn{padding:7px 16px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:13px;cursor:pointer;transition:all var(--transition);font-weight:500}
.filter-btn:hover{border-color:var(--accent);color:var(--text)}
.filter-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}

/* Album dropdown */
.album-select{padding:7px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:13px;outline:none;cursor:pointer}
.album-select:focus{border-color:var(--accent)}

/* Upload button */
.upload-btn{padding:8px 18px;border-radius:var(--radius-sm);border:none;background:linear-gradient(135deg,var(--accent),#9b8afb);color:#fff;font-size:13px;font-weight:600;cursor:pointer;transition:opacity var(--transition);white-space:nowrap}
.upload-btn:hover{opacity:.85}
.upload-btn:disabled{opacity:.5;cursor:not-allowed}

/* Grid */
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));padding:24px 28px}
.card{position:relative;border-radius:var(--radius);overflow:hidden;cursor:pointer;background:var(--surface2);aspect-ratio:4/3;transition:transform var(--transition),box-shadow var(--transition);border:1px solid transparent}
.card:hover{transform:translateY(-4px);box-shadow:0 16px 48px rgba(0,0,0,.5);border-color:var(--border)}
.card img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .3s}
.card:hover img{transform:scale(1.03)}
.card-overlay{position:absolute;inset:0;background:linear-gradient(transparent 50%,rgba(0,0,0,.8));opacity:0;transition:opacity var(--transition);display:flex;flex-direction:column;justify-content:flex-end;padding:16px}
.card:hover .card-overlay{opacity:1}
.card-title{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#fff}
.card-meta{font-size:11px;color:rgba(255,255,255,.6);margin-top:2px}
.card-badge{position:absolute;top:10px;right:10px;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.badge-video{background:rgba(248,113,113,.9);color:#fff}
.badge-gif{background:rgba(251,191,36,.9);color:#000}

/* Viewer */
.viewer{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.96);display:none;align-items:center;justify-content:center;backdrop-filter:blur(8px)}
.viewer.active{display:flex}
.viewer img,.viewer video{max-width:92vw;max-height:88vh;border-radius:var(--radius);box-shadow:0 20px 60px rgba(0,0,0,.6)}
.close-btn{position:fixed;top:20px;right:20px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.1);color:#fff;width:44px;height:44px;border-radius:50%;cursor:pointer;font-size:18px;z-index:102;transition:background var(--transition)}
.close-btn:hover{background:rgba(255,255,255,.15)}
.nav-btn{position:fixed;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);color:#fff;width:48px;height:48px;border-radius:50%;cursor:pointer;font-size:20px;z-index:102;transition:background var(--transition)}
.nav-btn:hover{background:rgba(255,255,255,.12)}
.nav-prev{left:20px}
.nav-next{right:20px}

/* Empty state */
.empty{text-align:center;padding:100px 20px;color:var(--text2)}
.empty h2{font-size:20px;margin-bottom:8px}
.empty p{font-size:14px}

/* File icon cards */
.card-icon{display:flex;flex-direction:column;align-items:center;justify-content:center;width:100%;height:100%;background:var(--surface3)}
.card-icon .icon{font-size:56px;margin-bottom:8px}
.card-icon .fname{font-size:12px;color:var(--text2);max-width:90%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:center}
.ext-badge{position:absolute;top:10px;left:10px;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;background:rgba(124,108,240,.85);color:#fff}

/* List / table view */
.list-view{display:none;padding:8px 28px}
.list-view table{width:100%;border-collapse:collapse}
.list-view th{text-align:left;padding:10px 14px;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text2);border-bottom:1px solid var(--border)}
.list-view td{padding:10px 14px;border-bottom:1px solid var(--border);font-size:13px;color:var(--text)}
.list-view tr:hover{background:var(--surface2)}
.list-view .icon-cell{font-size:20px;width:40px}
.list-view .fname-cell{cursor:pointer;color:var(--accent2)}
.list-view .fname-cell:hover{text-decoration:underline}

/* View toggle */
.view-toggle{display:flex;gap:4px;align-items:center}
.view-btn{padding:6px 10px;border-radius:var(--radius-sm);border:1px solid var(--border);background:transparent;color:var(--text2);font-size:15px;cursor:pointer;transition:all var(--transition)}
.view-btn:hover{border-color:var(--accent);color:var(--text)}
.view-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}

/* Upload overlay */
.upload-overlay{display:none;position:fixed;inset:0;z-index:50;background:rgba(0,0,0,.7);backdrop-filter:blur(6px);align-items:center;justify-content:center}
.upload-overlay.active{display:flex}
.upload-box{background:var(--surface);border:2px dashed var(--border);border-radius:var(--radius);padding:48px;text-align:center;max-width:400px;width:90%}
.upload-box.dragging{border-color:var(--accent);background:var(--accent-glow)}
.upload-progress{margin-top:16px;height:4px;border-radius:2px;background:var(--surface3);overflow:hidden}
.upload-progress-bar{height:100%;background:var(--accent);width:0%;transition:width .3s}

/* Responsive */
@media(max-width:768px){
  .stats-bar{flex-wrap:wrap;gap:10px;padding:14px 16px}
  .stat-card{min-width:calc(50% - 5px)}
  .topbar{flex-wrap:wrap;padding:12px 16px;gap:10px}
  .grid{grid-template-columns:repeat(auto-fill,minmax(160px,1fr));padding:16px;gap:10px}
  .list-view{padding:8px 16px}
  .filters{flex-wrap:wrap}
}
</style>
</head>
<body>

<!-- Stats Bar -->
<div class="stats-bar" id="stats-bar">
  <div class="stat-card"><div class="stat-label">Total Files</div><div class="stat-value" id="st-total">—</div></div>
  <div class="stat-card"><div class="stat-label">Total Size</div><div class="stat-value" id="st-size">—</div></div>
  <div class="stat-card"><div class="stat-label">Albums</div><div class="stat-value" id="st-albums">—</div></div>
</div>

<!-- Topbar -->
<div class="topbar">
  <h1>🔐 TeleVault</h1>
  <input class="search-input" id="q" placeholder="Search files…" autocomplete="off">
  <div class="filters">
    <button class="filter-btn active" data-type="">All</button>
    <button class="filter-btn" data-type="image">Images</button>
    <button class="filter-btn" data-type="video">Videos</button>
    <button class="filter-btn" data-type="document">Documents</button>
    <button class="filter-btn" data-type="other">Other</button>
  </div>
  <select class="album-select" id="album-filter"><option value="">All Albums</option></select>
  <div class="view-toggle">
    <button class="view-btn active" id="grid-view-btn" title="Grid view">▦</button>
    <button class="view-btn" id="list-view-btn" title="List view">☰</button>
  </div>
  <button class="upload-btn" id="upload-btn">⬆ Upload</button>
</div>

<!-- Grid -->
<div class="grid" id="grid"></div>
<!-- List view -->
<div class="list-view" id="list-view">
  <table><thead><tr><th></th><th>Name</th><th>Type</th><th>Size</th><th>Uploaded</th></tr></thead><tbody id="list-body"></tbody></table>
</div>
<div class="empty" id="empty" style="display:none"><h2>No files found</h2><p>Upload some files to get started.</p></div>

<!-- Viewer -->
<div class="viewer" id="viewer">
  <button class="close-btn" onclick="closeV()">✕</button>
  <button class="nav-btn nav-prev" onclick="navV(-1)">‹</button>
  <button class="nav-btn nav-next" onclick="navV(1)">›</button>
  <div id="stage"></div>
</div>

<!-- Upload overlay -->
<div class="upload-overlay" id="upload-overlay">
  <div class="upload-box" id="upload-box">
    <div style="font-size:48px;margin-bottom:16px">📁</div>
    <div style="font-size:16px;font-weight:600;margin-bottom:8px">Upload File</div>
    <div style="color:var(--text2);font-size:13px;margin-bottom:20px">Click to browse or drag & drop</div>
    <input type="file" id="file-input" style="display:none">
    <button class="upload-btn" onclick="document.getElementById('file-input').click()">Choose File</button>
    <div class="upload-progress" id="upload-progress" style="display:none"><div class="upload-progress-bar" id="upload-bar"></div></div>
    <div id="upload-status" style="margin-top:12px;font-size:13px;color:var(--text2)"></div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const $$=s=>document.querySelectorAll(s);
let items=[], idx=-1, currentType='', currentAlbum='', viewMode='grid';

const FILE_ICONS={photo:'🖼️',video:'🎬',animation:'🎬',audio:'🎵',document:'📄',archive:'📦',code:'💻',other:'📎'};
function fileIcon(type){return FILE_ICONS[type]||'📎'}
function isMedia(type){return type==='photo'||type==='video'||type==='animation'}

function fmtSize(b){if(!b)return '0 B';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++}return b.toFixed(i?1:0)+' '+u[i]}

// Load stats
async function loadStats(){
  try{
    const[sr,ar]=await Promise.all([fetch('/api/stats'),fetch('/api/albums')]);
    const s=await sr.json(), a=await ar.json();
    $('#st-total').textContent=s.total.toLocaleString();
    $('#st-size').textContent=fmtSize(s.total_size);
    $('#st-albums').textContent=(a.albums||[]).length;
    const sel=$('#album-filter');
    sel.innerHTML='<option value="">All Albums</option>';
    (a.albums||[]).forEach(al=>{const o=document.createElement('option');o.value=al.album;o.textContent=`${al.album} (${al.count})`;sel.appendChild(o)});
  }catch(e){console.error(e)}
}

// Load files
async function load(){
  const q=$('#q').value;
  const params=new URLSearchParams({q,limit:'500'});
  // 'document' and 'other' filter client-side; image/video use server type param
  if(currentType==='image'||currentType==='video')params.set('type',currentType);
  if(currentAlbum)params.set('album',currentAlbum);
  try{
    const r=await fetch('/api/media?'+params);
    const d=await r.json();
    let all=d.items||[];
    if(currentType==='document')all=all.filter(i=>i.type==='document'||i.type==='archive');
    else if(currentType==='other')all=all.filter(i=>!isMedia(i.type)&&i.type!=='document'&&i.type!=='archive');
    items=all;
    render();
  }catch(e){console.error(e)}
}

function render(){
  const g=$('#grid');const lb=$('#list-body');const lv=$('#list-view');
  g.innerHTML='';lb.innerHTML='';
  if(!items.length){$('#empty').style.display='block';g.style.display='none';lv.style.display='none';return}
  $('#empty').style.display='none';

  if(viewMode==='grid'){
    g.style.display='grid';lv.style.display='none';
    items.forEach((it,i)=>{
      const c=document.createElement('div');c.className='card';
      const ext=it.ext?`<span class="ext-badge">.${esc(it.ext)}</span>`:'';
      let badge='';
      if(it.type==='video')badge='<span class="card-badge badge-video">Video</span>';
      else if(it.type==='animation')badge='<span class="card-badge badge-gif">GIF</span>';
      if(isMedia(it.type)){
        c.innerHTML=`<img loading="lazy" src="/thumb/${it.msg_id}">${ext}${badge}<div class="card-overlay"><div class="card-title">${esc(it.title)}</div><div class="card-meta">${fmtSize(it.size)}</div></div>`;
      }else{
        c.innerHTML=`<div class="card-icon"><div class="icon">${fileIcon(it.type)}</div><div class="fname">${esc(it.title)}</div></div>${ext}<div class="card-overlay"><div class="card-title">${esc(it.title)}</div><div class="card-meta">${fmtSize(it.size)}</div></div>`;
      }
      c.onclick=()=>openV(i);
      g.appendChild(c);
    });
  }else{
    g.style.display='none';lv.style.display='block';
    items.forEach((it,i)=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`<td class="icon-cell">${fileIcon(it.type)}</td><td class="fname-cell">${esc(it.title)}</td><td>${it.ext?'.'+esc(it.ext):it.type}</td><td>${fmtSize(it.size)}</td><td>${it.uploaded_at?it.uploaded_at.slice(0,10):''}</td>`;
      tr.querySelector('.fname-cell').onclick=()=>openV(i);
      lb.appendChild(tr);
    });
  }
}

function openV(i){idx=i;showItem();$('#viewer').classList.add('active');document.body.style.overflow='hidden'}
function closeV(){$('#viewer').classList.remove('active');document.body.style.overflow='';$('#stage').innerHTML='';idx=-1}
function navV(d){if(idx<0)return;idx=(idx+d+items.length)%items.length;showItem()}
function showItem(){
  const it=items[idx],s=$('#stage');s.innerHTML='';
  if(it.type==='video'||it.type==='animation'){const v=document.createElement('video');v.controls=true;v.autoplay=true;v.src=`/stream/${it.msg_id}`;s.appendChild(v)}
  else if(it.type==='photo'){const img=document.createElement('img');img.src=`/stream/${it.msg_id}`;s.appendChild(img)}
  else{
    // Non-media: show icon + download link
    const d=document.createElement('div');
    d.style.cssText='text-align:center;color:#fff;';
    d.innerHTML=`<div style="font-size:96px;margin-bottom:20px">${fileIcon(it.type)}</div><div style="font-size:18px;margin-bottom:12px">${esc(it.title)}</div><div style="color:var(--text2);margin-bottom:20px">${fmtSize(it.size)}</div><a href="/stream/${it.msg_id}" download="${esc(it.title)}" style="padding:10px 24px;border-radius:10px;background:var(--accent);color:#fff;font-weight:600;text-decoration:none">⬇ Download</a>`;
    s.appendChild(d);
  }
}
function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML}

// Filter buttons
$$('.filter-btn').forEach(b=>b.addEventListener('click',()=>{
  $$('.filter-btn').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  currentType=b.dataset.type;
  load();
}));

$('#album-filter').addEventListener('change',e=>{currentAlbum=e.target.value;load()});
$('#q').addEventListener('input',()=>{clearTimeout(window._t);window._t=setTimeout(load,300)});

// Viewer keyboard nav
$('#viewer').addEventListener('click',e=>{if(e.target===$('#viewer'))closeV()});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape')closeV();
  if(idx>=0&&e.key==='ArrowLeft')navV(-1);
  if(idx>=0&&e.key==='ArrowRight')navV(1);
});

// Upload
$('#upload-btn').addEventListener('click',()=>$('#upload-overlay').classList.add('active'));
$('#upload-overlay').addEventListener('click',e=>{if(e.target===$('#upload-overlay'))$('#upload-overlay').classList.remove('active')});
const ub=$('#upload-box');
ub.addEventListener('dragover',e=>{e.preventDefault();ub.classList.add('dragging')});
ub.addEventListener('dragleave',()=>ub.classList.remove('dragging'));
ub.addEventListener('drop',e=>{e.preventDefault();ub.classList.remove('dragging');if(e.dataTransfer.files.length)doUpload(e.dataTransfer.files[0])});
$('#file-input').addEventListener('change',e=>{if(e.target.files.length)doUpload(e.target.files[0])});

async function doUpload(file){
  const bar=$('#upload-bar'),prog=$('#upload-progress'),status=$('#upload-status');
  prog.style.display='block';bar.style.width='30%';status.textContent='Uploading '+file.name+'…';
  const fd=new FormData();fd.append('file',file);
  try{
    bar.style.width='60%';
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    bar.style.width='100%';
    if(r.ok){status.textContent='✅ Uploaded!';status.style.color='var(--green)';loadStats();load()}
    else{const d=await r.json();status.textContent='❌ '+(d.detail||'Failed');status.style.color='var(--red)'}
  }catch(e){status.textContent='❌ Network error';status.style.color='var(--red)'}
  setTimeout(()=>{prog.style.display='none';bar.style.width='0%';status.style.color='var(--text2)'},3000);
}

// View toggle
$('#grid-view-btn').addEventListener('click',()=>{viewMode='grid';$('#grid-view-btn').classList.add('active');$('#list-view-btn').classList.remove('active');render()});
$('#list-view-btn').addEventListener('click',()=>{viewMode='list';$('#list-view-btn').classList.add('active');$('#grid-view-btn').classList.remove('active');render()});

// Init
loadStats();load();
</script>
</body>
</html>"""
