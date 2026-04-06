import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from mach_mukke.config import API_KEY, DOWNLOADS_DIR, TMP_DIR, WISHES_DIR
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


class WishRequest(BaseModel):
    query: str


class WishStatus(BaseModel):
    id: str
    query: str
    status: str
    filename: str | None = None


@app.get("/")
async def index():
    return HTMLResponse(
        content=(Path(__file__).parent / "static" / "index.html").read_text()
    )


@app.post("/api/wish")
async def submit_wish(wish: WishRequest):
    wish_id = secrets.token_hex(8)
    download_tasks[wish_id] = {
        "query": wish.query,
        "status": "queued",
        "filename": None,
    }
    await download_queue.put(wish_id)
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
