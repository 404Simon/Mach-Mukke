import asyncio
import hashlib
import hmac
import logging
import secrets
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pylast
from fastapi import Cookie, Depends, FastAPI, HTTPException, Header, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from mach_mukke.config import (
    API_KEY,
    BIRTHDAY_AGE,
    BIRTHDAY_NAME,
    COOKIE_SECRET,
    DOWNLOADS_DIR,
    LASTFM_API_KEY,
    LASTFM_API_SECRET,
    TMP_DIR,
    WISHES_DIR,
)
from mach_mukke.downloader import (
    embed_metadata,
    resolve_final_path,
    run_yt_dlp,
    sanitize_filename,
    validate_opus,
)
from mach_mukke import downloader
from mach_mukke.sse import create_subscriber, notify as notify_sse, sse_generator

logger = logging.getLogger("mach_mukke.server")

download_queue: asyncio.Queue = asyncio.Queue()
download_tasks: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    WISHES_DIR.mkdir(exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    asyncio.create_task(process_downloads())
    yield


app = FastAPI(title="Mach Mukke Server", lifespan=lifespan)


def verify_api_key(x_api_key: str | None = Header(default=None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def sign_cookie(name: str, age: str) -> str:
    payload = f"{name}:{age}"
    sig = hmac.new(COOKIE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_cookie(cookie: str | None) -> bool:
    if not cookie:
        return False
    parts = cookie.rsplit(":", 1)
    if len(parts) != 2:
        return False
    payload, sig = parts
    expected = hmac.new(
        COOKIE_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def normalize_track_key(artist: str, title: str) -> str:
    return f"{normalize_artist(artist)} - {normalize_title(title)}"


def normalize_artist(artist: str) -> str:
    return artist.strip().lower()


def normalize_title(title: str) -> str:
    base = title.strip().lower()
    base = base.replace("&", "and")
    base = base.replace("/", " ")
    base = base.replace("_", " ")
    base = re.sub(r"\s+", " ", base)
    base = re.sub(r"\s*\([^)]*\)", "", base)
    base = re.sub(r"\s*\[[^]]*\]", "", base)
    base = re.sub(r"\s*\{[^}]*\}", "", base)
    base = re.sub(
        r"\b(live|remaster(ed)?|remix|version|edit|mix|acoustic|mono|stereo|radio|demo|deluxe|explicit|clean)\b",
        "",
        base,
    )
    base = re.sub(r"\s+", " ", base)
    return base.strip()


class LoginRequest(BaseModel):
    name: str
    age: str


def require_auth(mukke_auth: str | None = Cookie(default=None)):
    if not verify_cookie(mukke_auth):
        raise HTTPException(status_code=401, detail="Nicht angemeldet")


def require_auth_or_api_key(
    mukke_auth: str | None = Cookie(default=None),
    x_api_key: str | None = Header(default=None),
):
    if x_api_key == API_KEY:
        return
    if not verify_cookie(mukke_auth):
        raise HTTPException(status_code=401, detail="Nicht angemeldet")


async def enqueue_download(query: str) -> str:
    wish_id = secrets.token_hex(8)
    download_tasks[wish_id] = {
        "query": query,
        "status": "queued",
        "filename": None,
        "error": None,
    }
    await download_queue.put(wish_id)
    return wish_id


class WishRequest(BaseModel):
    query: str


class WishStatus(BaseModel):
    id: str
    query: str
    status: str
    filename: str | None = None
    error: str | None = None


class Track(BaseModel):
    artist: str
    title: str


class SimilarRequest(BaseModel):
    tracks: list[Track]


class SimilarResponse(BaseModel):
    queued: int
    skipped: int
    tracks: list[Track]


def clean_track_for_lookup(artist: str, title: str) -> Track:
    cleaned_artist = artist.strip()
    cleaned_title = title.strip()
    if cleaned_artist:
        prefix = f"{cleaned_artist} - "
        if cleaned_title.lower().startswith(prefix.lower()):
            cleaned_title = cleaned_title[len(prefix) :].strip()
    cleaned_title = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", cleaned_title)
    cleaned_title = re.sub(r"\s+", " ", cleaned_title).strip()
    return Track(artist=cleaned_artist, title=cleaned_title)


def fetch_similar_tracks_sync(artist: str, title: str, limit: int = 3) -> list[Track]:
    network = pylast.LastFMNetwork(
        api_key=LASTFM_API_KEY, api_secret=LASTFM_API_SECRET
    )
    cleaned = clean_track_for_lookup(artist, title)
    track = network.get_track(cleaned.artist, cleaned.title)
    results: list[Track] = []
    try:
        similar_items = track.get_similar(limit=limit, autocorrect=True)
    except TypeError:
        similar_items = track.get_similar(limit=limit)
    for item in similar_items:
        similar = item.item
        results.append(Track(artist=similar.artist.name, title=similar.title))
    return results


@app.get("/")
async def index():
    return HTMLResponse(
        content=(Path(__file__).parent / "static" / "index.html").read_text()
    )


@app.get("/api/auth")
async def check_auth(_=Depends(require_auth)):
    return {"ok": True}


@app.post("/api/login")
async def login(body: LoginRequest, response: Response):
    if body.name.strip().lower() != BIRTHDAY_NAME.strip().lower():
        raise HTTPException(status_code=401, detail="Falsche Antwort!")
    if body.age.strip() != BIRTHDAY_AGE.strip():
        raise HTTPException(status_code=401, detail="Falsche Antwort!")

    cookie_value = sign_cookie(body.name, body.age)
    response.set_cookie(
        key="mukke_auth",
        value=cookie_value,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 60 * 60,  # 30 days
    )
    return {"ok": True}


@app.post("/api/wish")
async def submit_wish(wish: WishRequest, _=Depends(require_auth_or_api_key)):
    wish_id = await enqueue_download(wish.query)
    return {"id": wish_id, "status": "queued"}


@app.get("/api/wish/{wish_id}")
async def get_wish_status(wish_id: str):
    if wish_id not in download_tasks:
        raise HTTPException(status_code=404, detail="Wish not found")
    task = download_tasks[wish_id]
    return WishStatus(id=wish_id, **task)


@app.get("/api/wishes")
async def list_wishes():
    return [WishStatus(id=wish_id, **task) for wish_id, task in download_tasks.items()]


@app.post("/api/similar", response_model=SimilarResponse)
async def find_similar_tracks(body: SimilarRequest, _=Depends(verify_api_key)):
    if not LASTFM_API_KEY or not LASTFM_API_SECRET:
        raise HTTPException(
            status_code=500, detail="Last.fm API key/secret not configured on server"
        )

    source_tracks = [
        clean_track_for_lookup(t.artist, t.title)
        for t in body.tracks
        if t.artist.strip() and t.title.strip()
    ]
    if not source_tracks:
        raise HTTPException(status_code=400, detail="No valid tracks provided")

    results = await asyncio.gather(
        *[
            asyncio.to_thread(
                fetch_similar_tracks_sync, track.artist, track.title, 3
            )
            for track in source_tracks
        ],
        return_exceptions=True,
    )

    similar: list[Track] = []
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Similar track lookup failed: %s", result)
            continue
        similar.extend(result)

    source_keys = {normalize_track_key(t.artist, t.title) for t in source_tracks}
    seen: set[str] = set()
    unique: list[Track] = []
    for track in similar:
        key = normalize_track_key(track.artist, track.title)
        if key in seen or key in source_keys:
            continue
        seen.add(key)
        unique.append(track)

    existing_queries: set[str] = set()
    for task in download_tasks.values():
        query = task["query"]
        if " - " in query:
            artist, title = query.split(" - ", 1)
            existing_queries.add(normalize_track_key(artist, title))
    queued = 0
    skipped = 0
    for track in unique:
        query = f"{track.artist} - {track.title}"
        if normalize_track_key(track.artist, track.title) in existing_queries:
            skipped += 1
            continue
        await enqueue_download(query)
        queued += 1

    return SimilarResponse(queued=queued, skipped=skipped, tracks=unique)


@app.get("/api/downloads")
async def list_downloads(_=Depends(verify_api_key)):
    if not DOWNLOADS_DIR.exists():
        return []
    return [
        {"filename": f.name, "path": str(f)}
        for f in sorted(
            DOWNLOADS_DIR.glob("*.opus"), key=lambda f: f.stat().st_mtime, reverse=True
        )
    ]


@app.get("/api/downloads/{filename}")
async def get_download(filename: str, _=Depends(verify_api_key)):
    file_path = DOWNLOADS_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))


@app.get("/api/sse")
async def sse_endpoint(_=Depends(verify_api_key)):
    queue = create_subscriber()
    return StreamingResponse(
        sse_generator(queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def process_wish(wish_id: str):
    task = download_tasks[wish_id]
    task["status"] = "downloading"
    TMP_DIR.mkdir(exist_ok=True)

    output_template = str(TMP_DIR / f"temp_{wish_id}.%(ext)s")

    logger.info(f"Downloading: {task['query']}")
    metadata, stdout, stderr = await run_yt_dlp(task["query"], output_template)

    if metadata and metadata.get("duration", 0) > downloader.MAX_DURATION_SECONDS:
        task["status"] = "failed"
        task["error"] = (
            f"Video too long ({metadata['duration']:.0f}s > {downloader.MAX_DURATION_SECONDS}s)"
        )
        logger.warning(task["error"])
        return

    opus_files = list(TMP_DIR.glob(f"temp_{wish_id}.opus"))
    if not opus_files:
        task["status"] = "failed"
        task["error"] = stderr.strip() or "No file downloaded"
        logger.error(f"Download failed: {task['error']}")
        return

    opus_path = opus_files[0]

    valid, msg = validate_opus(opus_path)
    if not valid:
        task["status"] = "failed"
        task["error"] = msg
        logger.error(f"Validation failed for {opus_path}: {msg}")
        opus_path.unlink(missing_ok=True)
        return

    logger.info(f"Downloaded: {msg}")

    if metadata:
        embed_metadata(opus_path, metadata)

    base_name = (
        f"{sanitize_filename(metadata.get('title', 'unknown'))}.opus"
        if metadata
        else opus_path.name
    )
    final_path = resolve_final_path(DOWNLOADS_DIR, base_name)

    opus_path.rename(final_path)

    task["status"] = "done"
    task["filename"] = final_path.name
    notify_sse({"event": "download_complete", "filename": final_path.name})
    logger.info(f"Saved as: {final_path.name}")


async def cleanup_temp_files(wish_id: str):
    for f in TMP_DIR.glob(f"temp_{wish_id}.*"):
        f.unlink(missing_ok=True)


async def process_downloads():
    while True:
        wish_id = await download_queue.get()
        try:
            await process_wish(wish_id)
        except Exception as e:
            download_tasks[wish_id]["status"] = "failed"
            download_tasks[wish_id]["error"] = str(e)
            logger.exception(f"Unexpected error processing wish {wish_id}")
        finally:
            await cleanup_temp_files(wish_id)
            download_queue.task_done()


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    print(f"API Key: {API_KEY}")
    print("Set MACH_MUKKE_API_KEY env var for the player client")
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
