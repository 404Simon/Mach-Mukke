import asyncio
import json
import os
from pathlib import Path

import httpx

SERVER_URL = os.environ.get("MACH_MUKKE_SERVER_URL", "http://localhost:8000")
API_KEY = os.environ.get("MACH_MUKKE_API_KEY", "")
MUSIC_DIR = Path.home() / "Music" / "mach_mukke"
MPD_MUSIC_DIR = Path.home() / "Music"
POLL_INTERVAL = 30


def load_known_files() -> set[str]:
    if not MUSIC_DIR.exists():
        return set()
    return {f.name for f in MUSIC_DIR.iterdir() if f.is_file()}


async def poll_new_downloads(
    client: httpx.AsyncClient, known_files: set[str]
) -> list[str]:
    resp = await client.get("/api/downloads", headers={"X-API-Key": API_KEY})
    resp.raise_for_status()
    downloads = resp.json()

    new_files = []
    for d in downloads:
        filename = d["filename"]
        if filename not in known_files:
            new_files.append(filename)

    return new_files


async def download_file(client: httpx.AsyncClient, filename: str) -> Path:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    dest = MUSIC_DIR / filename

    async with client.stream(
        "GET", f"/api/downloads/{filename}", headers={"X-API-Key": API_KEY}
    ) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)

    return dest


async def add_to_rmpc(filepath: Path) -> None:
    relative_path = filepath.relative_to(MPD_MUSIC_DIR)

    update_proc = await asyncio.create_subprocess_exec(
        "rmpc",
        "update",
        str(relative_path.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await update_proc.communicate()

    proc = await asyncio.create_subprocess_exec(
        "rmpc",
        "add",
        str(relative_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        print(f"Failed to add {relative_path} to rmpc: {stderr.decode()}")
    else:
        print(f"Added to rmpc queue: {relative_path}")


async def sse_listener(client: httpx.AsyncClient, known_files: set[str]):
    while True:
        try:
            async with client.stream(
                "GET",
                "/api/sse",
                headers={"X-API-Key": API_KEY},
                timeout=None,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        if data.get("event") == "download_complete":
                            filename = data.get("filename")
                            if filename and filename not in known_files:
                                print(f"Downloading (SSE): {filename}")
                                try:
                                    filepath = await download_file(client, filename)
                                    known_files.add(filename)
                                    await add_to_rmpc(filepath)
                                except Exception as e:
                                    print(f"Failed to download {filename}: {e}")
        except (httpx.RequestError, asyncio.CancelledError):
            print("SSE connection lost, will retry...")
            await asyncio.sleep(5)


async def poll_fallback(client: httpx.AsyncClient, known_files: set[str]):
    while True:
        try:
            new_files = await poll_new_downloads(client, known_files)
            for filename in new_files:
                print(f"Downloading (poll): {filename}")
                try:
                    filepath = await download_file(client, filename)
                    known_files.add(filename)
                    await add_to_rmpc(filepath)
                except Exception as e:
                    print(f"Failed to download {filename}: {e}")
        except httpx.RequestError as e:
            print(f"Connection error: {e}")
        except Exception as e:
            print(f"Error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


async def main_loop():
    if not API_KEY:
        print("Error: MACH_MUKKE_API_KEY environment variable is required")
        return

    headers = {"X-API-Key": API_KEY}
    known_files: set[str] = load_known_files()
    print(f"Already known {len(known_files)} files from {MUSIC_DIR}")

    async with httpx.AsyncClient(base_url=SERVER_URL, timeout=60.0) as client:
        print(f"Connecting to {SERVER_URL} for new music...")
        print(f"Music directory: {MUSIC_DIR}")

        sse_task = asyncio.create_task(sse_listener(client, known_files))
        poll_task = asyncio.create_task(poll_fallback(client, known_files))

        try:
            await asyncio.gather(sse_task, poll_task)
        except asyncio.CancelledError:
            sse_task.cancel()
            poll_task.cancel()


def main():
    asyncio.run(main_loop())


if __name__ == "__main__":
    main()
