import asyncio
import os
from pathlib import Path

import httpx

SERVER_URL = os.environ.get("MACH_MUKKE_SERVER_URL", "http://localhost:8000")
API_KEY = os.environ.get("MACH_MUKKE_API_KEY", "")
MUSIC_DIR = Path.home() / "Music" / "mach_mukke"
MPD_MUSIC_DIR = Path.home() / "Music"
POLL_INTERVAL = 5


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


async def main_loop():
    if not API_KEY:
        print("Error: MACH_MUKKE_API_KEY environment variable is required")
        return

    headers = {"X-API-Key": API_KEY}
    known_files: set[str] = set()

    async with httpx.AsyncClient(base_url=SERVER_URL, timeout=60.0) as client:
        print(f"Polling {SERVER_URL} for new music...")
        print(f"Music directory: {MUSIC_DIR}")

        while True:
            try:
                new_files = await poll_new_downloads(client, known_files)

                for filename in new_files:
                    print(f"Downloading: {filename}")
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


def main():
    asyncio.run(main_loop())


if __name__ == "__main__":
    main()
