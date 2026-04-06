import asyncio
import json
import logging
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC

logger = logging.getLogger("mach_mukke.server")

WISHES_DIR = Path("wishes")
DOWNLOADS_DIR = Path("downloads")
TMP_DIR = Path("downloads_tmp")
API_KEY = os.environ.get("MACH_MUKKE_API_KEY", secrets.token_hex(32))
MAX_DURATION_SECONDS = 10 * 60

app = FastAPI(title="Mach Mukke Server")

download_queue: asyncio.Queue = asyncio.Queue()
download_tasks: dict[str, dict] = {}


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
            DOWNLOADS_DIR.glob("*.mp3"), key=lambda f: f.stat().st_mtime, reverse=True
        )
    ]


@app.get("/api/downloads/{filename}")
async def get_download(filename: str, _=Depends(verify_api_key)):
    file_path = DOWNLOADS_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))


async def run_yt_dlp(query: str, output_template: str) -> tuple[dict | None, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--embed-metadata",
        "--match-filter",
        f"duration <= {MAX_DURATION_SECONDS}",
        "--sponsorblock-remove",
        "music_offtopic,intro,outro",
        "--write-info-json",
        "--print-json",
        "--output",
        output_template,
        f"ytsearch1:{query}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stdout_str = stdout.decode()
    stderr_str = stderr.decode()

    json_output = None
    for line in stdout_str.strip().split("\n"):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "title" in parsed:
                json_output = parsed
                break
        except json.JSONDecodeError:
            continue

    return json_output, stdout_str, stderr_str


def embed_metadata(mp3_path: Path, metadata: dict) -> None:
    try:
        audio = MP3(mp3_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()

        title = metadata.get("title", "")
        artist = metadata.get("artist", "")
        album = metadata.get("album", "")

        if title:
            audio.tags[TIT2(encoding=3, text=[title])] = TIT2(encoding=3, text=[title])
        if artist:
            audio.tags[TPE1(encoding=3, text=[artist])] = TPE1(
                encoding=3, text=[artist]
            )
        if album:
            audio.tags[TALB(encoding=3, text=[album])] = TALB(encoding=3, text=[album])

        thumbnail = metadata.get("thumbnail")
        if thumbnail:
            thumb_path = Path(str(mp3_path) + ".thumb.jpg")
            if thumb_path.exists():
                audio.tags[
                    APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=thumb_path.read_bytes(),
                    )
                ] = APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=thumb_path.read_bytes(),
                )
                thumb_path.unlink()

        audio.save()
        logger.info(f"Embedded metadata: {title} - {artist}")
    except Exception as e:
        logger.warning(f"Failed to embed metadata for {mp3_path}: {e}")


def validate_mp3(mp3_path: Path) -> tuple[bool, str]:
    try:
        audio = MP3(mp3_path)
        duration = audio.info.length if audio.info else 0
        if duration <= 0:
            return False, "Invalid duration (0 seconds)"
        if duration > MAX_DURATION_SECONDS:
            return (
                False,
                f"Duration {duration:.0f}s exceeds max {MAX_DURATION_SECONDS}s",
            )
        return True, f"Valid MP3, duration: {duration:.0f}s"
    except Exception as e:
        return False, f"MP3 validation failed: {e}"


async def process_downloads():
    while True:
        wish_id = await download_queue.get()
        temp_file = None
        try:
            task = download_tasks[wish_id]
            task["status"] = "downloading"
            TMP_DIR.mkdir(exist_ok=True)

            output_template = str(TMP_DIR / f"temp_{wish_id}.%(ext)s")

            logger.info(f"Downloading: {task['query']}")
            metadata, stdout, stderr = await run_yt_dlp(task["query"], output_template)

            if metadata and metadata.get("duration", 0) > MAX_DURATION_SECONDS:
                task["status"] = "failed"
                task["error"] = (
                    f"Video too long ({metadata['duration']:.0f}s > {MAX_DURATION_SECONDS}s)"
                )
                logger.warning(task["error"])
                continue

            mp3_files = list(TMP_DIR.glob(f"temp_{wish_id}.mp3"))
            if not mp3_files:
                task["status"] = "failed"
                task["error"] = stderr.strip() or "No file downloaded"
                logger.error(f"Download failed: {task['error']}")
                continue

            mp3_path = mp3_files[0]

            valid, msg = validate_mp3(mp3_path)
            if not valid:
                task["status"] = "failed"
                task["error"] = msg
                logger.error(f"Validation failed for {mp3_path}: {msg}")
                mp3_path.unlink(missing_ok=True)
                continue

            logger.info(f"Downloaded: {msg}")

            if metadata:
                embed_metadata(mp3_path, metadata)

            final_name = (
                f"{metadata.get('title', 'unknown')}.mp3" if metadata else mp3_path.name
            )
            final_name = "".join(
                c if c.isalnum() or c in " _-." else "_" for c in final_name
            )
            final_path = DOWNLOADS_DIR / final_name

            counter = 1
            while final_path.exists():
                final_path = (
                    DOWNLOADS_DIR / f"{final_name.rsplit('.', 1)[0]}_{counter}.mp3"
                )
                counter += 1

            mp3_path.rename(final_path)

            task["status"] = "done"
            task["filename"] = final_path.name
            logger.info(f"Saved as: {final_path.name}")

        except Exception as e:
            download_tasks[wish_id]["status"] = "failed"
            download_tasks[wish_id]["error"] = str(e)
            logger.exception(f"Unexpected error processing wish {wish_id}")
        finally:
            if temp_file and temp_file.exists():
                temp_file.unlink(missing_ok=True)
            for f in TMP_DIR.glob(f"temp_{wish_id}.*"):
                f.unlink(missing_ok=True)
            download_queue.task_done()


@app.on_event("startup")
async def startup():
    WISHES_DIR.mkdir(exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    asyncio.create_task(process_downloads())


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    print(f"API Key: {API_KEY}")
    print("Set MACH_MUKKE_API_KEY env var for the player client")
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
