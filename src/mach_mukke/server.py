import asyncio
import hashlib
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from mach_mukke import downloader, similarity
from mach_mukke.config import (
    API_KEY,
    BIRTHDAY_AGE,
    BIRTHDAY_NAME,
    COOKIE_SECRET,
    DOWNLOADS_DIR,
    LASTFM_API_KEY,
    LASTFM_API_SECRET,
    TMP_DIR,
    WISHING_ENABLED_DEFAULT,
)
from mach_mukke.downloader import (
    apply_r128_track_gain,
    embed_metadata,
    get_r128_track_gain,
    resolve_final_path,
    run_yt_dlp,
    sanitize_filename,
    validate_opus,
)
from mach_mukke.sse import create_subscriber
from mach_mukke.sse import notify as notify_sse
from mach_mukke.sse import sse_generator

logger = logging.getLogger("mach_mukke.server")

download_queue: asyncio.Queue = asyncio.Queue()
download_tasks: dict[str, dict] = {}
wishing_enabled = WISHING_ENABLED_DEFAULT


@asynccontextmanager
async def lifespan(app: FastAPI):
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    asyncio.create_task(process_downloads())
    yield


app = FastAPI(title="Mach Mukke Server", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)


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


class LoginRequest(BaseModel):
    name: str
    age: str


def require_auth(mukke_auth: str | None = Cookie(default=None)):
    if not verify_cookie(mukke_auth):
        raise HTTPException(status_code=401, detail="Nicht angemeldet")


def require_auth_or_api_key(
    mukke_auth: str | None = Cookie(default=None),
    x_api_key: str | None = Header(default=None),
) -> bool:
    if x_api_key == API_KEY:
        return True
    if not verify_cookie(mukke_auth):
        raise HTTPException(status_code=401, detail="Nicht angemeldet")
    return False


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


class WishingState(BaseModel):
    enabled: bool


class Track(BaseModel):
    artist: str
    title: str


class SimilarRequest(BaseModel):
    tracks: list[Track]
    limit: int = 3


class SimilarResponse(BaseModel):
    queued: int
    skipped: int
    tracks: list[Track]


class TagQueueResponse(BaseModel):
    queued: int
    skipped: int
    tracks: list[Track]


@app.get("/")
async def index():
    if not wishing_enabled:
        return FileResponse(str(Path(__file__).parent / "static" / "disabled.html"))
    return HTMLResponse(
        content=(Path(__file__).parent / "static" / "index.html").read_text()
    )


@app.get("/api/wishing", response_model=WishingState)
async def get_wishing_state():
    return WishingState(enabled=wishing_enabled)


@app.post("/api/wishing/toggle", response_model=WishingState)
async def toggle_wishing(_=Depends(verify_api_key)):
    global wishing_enabled
    wishing_enabled = not wishing_enabled
    logger.info("Wishing toggled. Enabled=%s", wishing_enabled)
    return WishingState(enabled=wishing_enabled)


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
async def submit_wish(
    wish: WishRequest, is_api_key_client: bool = Depends(require_auth_or_api_key)
):
    if not wishing_enabled and not is_api_key_client:
        raise HTTPException(status_code=403, detail="Wishing is currently disabled")
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

    source_tracks: list[Track] = []
    for incoming in body.tracks:
        if not incoming.artist.strip() or not incoming.title.strip():
            continue
        artist, title = similarity.clean_track_for_lookup(
            incoming.artist, incoming.title
        )
        if artist and title:
            source_tracks.append(Track(artist=artist, title=title))

    if not source_tracks:
        raise HTTPException(status_code=400, detail="No valid tracks provided")

    safe_limit = max(1, min(body.limit, 50))

    results = await asyncio.gather(
        *[
            asyncio.to_thread(
                similarity.fetch_similar_tracks_sync,
                track.artist,
                track.title,
                LASTFM_API_KEY,
                LASTFM_API_SECRET,
                safe_limit,
            )
            for track in source_tracks
        ],
        return_exceptions=True,
    )

    similar: list[Track] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Similar track lookup failed: %s", result)
            continue
        for artist, title in result:
            similar.append(Track(artist=artist, title=title))

    source_keys = {
        similarity.normalize_track_key(t.artist, t.title) for t in source_tracks
    }
    seen: set[str] = set()
    unique: list[Track] = []
    for track in similar:
        key = similarity.normalize_track_key(track.artist, track.title)
        if key in seen or key in source_keys:
            continue
        seen.add(key)
        unique.append(track)

    existing_queries: set[str] = set()
    for task in download_tasks.values():
        query = task["query"]
        if " - " in query:
            artist, title = query.split(" - ", 1)
            existing_queries.add(similarity.normalize_track_key(artist, title))
    queued = 0
    skipped = 0
    for track in unique:
        query = f"{track.artist} - {track.title}"
        if (
            similarity.normalize_track_key(track.artist, track.title)
            in existing_queries
        ):
            skipped += 1
            continue
        await enqueue_download(query)
        queued += 1

    return SimilarResponse(queued=queued, skipped=skipped, tracks=unique)


@app.post("/api/tag/queue", response_model=TagQueueResponse)
async def queue_tag_top_tracks(tag: str, limit: int = 15, _=Depends(verify_api_key)):
    if not LASTFM_API_KEY or not LASTFM_API_SECRET:
        raise HTTPException(
            status_code=500, detail="Last.fm API key/secret not configured on server"
        )
    safe_limit = max(1, min(limit, 50))
    try:
        result_tracks = await asyncio.to_thread(
            similarity.fetch_tag_top_tracks_sync,
            tag,
            LASTFM_API_KEY,
            LASTFM_API_SECRET,
            safe_limit,
        )
    except Exception as e:
        logger.warning("Tag lookup failed: %s", e)
        raise HTTPException(status_code=502, detail="Tag lookup failed") from e

    if not result_tracks:
        return TagQueueResponse(queued=0, skipped=0, tracks=[])

    tracks = [Track(artist=artist, title=title) for artist, title in result_tracks]

    existing_queries: set[str] = set()
    for task in download_tasks.values():
        query = task["query"]
        if " - " in query:
            artist, title = query.split(" - ", 1)
            existing_queries.add(similarity.normalize_track_key(artist, title))

    queued = 0
    skipped = 0
    unique: list[Track] = []
    seen: set[str] = set()
    for track in tracks:
        key = similarity.normalize_track_key(track.artist, track.title)
        if key in seen:
            continue
        seen.add(key)
        unique.append(track)

    for track in unique:
        if (
            similarity.normalize_track_key(track.artist, track.title)
            in existing_queries
        ):
            skipped += 1
            continue
        await enqueue_download(f"{track.artist} - {track.title}")
        queued += 1

    return TagQueueResponse(queued=queued, skipped=skipped, tracks=unique)


@app.get("/api/downloads")
async def list_downloads(_=Depends(verify_api_key)):
    if not DOWNLOADS_DIR.exists():
        return []
    return [
        {"filename": f.name}
        for f in sorted(
            DOWNLOADS_DIR.glob("*.opus"), key=lambda f: f.stat().st_mtime, reverse=True
        )
        if get_r128_track_gain(f) is not None
    ]


@app.get("/api/downloads/{filename}")
async def get_download(filename: str, _=Depends(verify_api_key)):
    file_path = DOWNLOADS_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if get_r128_track_gain(file_path) is None:
        raise HTTPException(status_code=409, detail="File not tagged yet")
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

    tagged, tag_msg = await apply_r128_track_gain(opus_path)
    if not tagged:
        task["status"] = "failed"
        task["error"] = tag_msg
        logger.error(f"Tagging failed for {opus_path}: {tag_msg}")
        opus_path.unlink(missing_ok=True)
        return

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
    logging.basicConfig(level=logging.INFO)
    print(f"API Key: {API_KEY}")
    print("Set MACH_MUKKE_API_KEY env var for the player client")
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
