import asyncio
import json
import os
import re
import shlex
from datetime import datetime
from pathlib import Path

import httpx
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog

SERVER_URL = os.environ.get("MACH_MUKKE_SERVER_URL", "http://localhost:8000")
API_KEY = os.environ.get("MACH_MUKKE_API_KEY", "")
MUSIC_DIR = Path.home() / "Music" / "mach_mukke"
MPD_MUSIC_DIR = Path.home() / "Music"


def load_known_files() -> set[str]:
    if not MUSIC_DIR.exists():
        return set()
    return {f.name for f in MUSIC_DIR.iterdir() if f.is_file()}


def parse_rmpc_queue(output: str) -> list[dict[str, str]]:
    tracks: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*\d+\.\s*", "", line)
        line = re.sub(r"\s*[•·]\s*\d{1,2}:\d{2}\s*$", "", line)
        line = re.sub(r"\s*\(\d{1,2}:\d{2}\)\s*$", "", line)
        line = re.sub(r"\s*\[\d{1,2}:\d{2}\]\s*$", "", line)
        parts = re.split(r"\s[-–—]\s", line, maxsplit=1)
        if len(parts) != 2:
            continue
        artist, title = (p.strip() for p in parts)
        if not artist or not title:
            continue
        tracks.append({"artist": artist, "title": title})
    return tracks


class PlayerClientApp(App):
    TITLE = "Mach Mukke!"
    CSS = """
    Screen {
        layout: vertical;
        background: #101416;
    }

    #log {
        height: 1fr;
        border: round #4da3ff;
        padding: 1 2;
        background: #0c0f12;
    }

    #input {
        height: 3;
        border: round #f4b860;
        padding: 0 1;
        background: #12171a;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+l", "clear_log", "Clear Log", priority=True),
        Binding("f1", "help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.client: httpx.AsyncClient | None = None
        self.known_files: set[str] = load_known_files()
        self.tasks: list[asyncio.Task] = []
        self.wishing_enabled: bool | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Vertical(RichLog(id="log", wrap=True))
        yield Input(placeholder="Type a command (F1 for help)", id="input")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one(Input).focus()
        if not API_KEY:
            self.log_line("MACH_MUKKE_API_KEY is required.", "red")
            return
        self.client = httpx.AsyncClient(base_url=SERVER_URL, timeout=60.0)
        self.log_line(f"Known files: {len(self.known_files)} in {MUSIC_DIR}")
        self.log_line(f"Connecting to {SERVER_URL}")
        await self.fetch_wishing_state()
        self.action_help()
        self.tasks = [asyncio.create_task(self.sse_listener())]

    async def on_unmount(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.client:
            await self.client.aclose()

    def log_line(self, message: str, style: str | None = None) -> None:
        log = self.query_one(RichLog)
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{timestamp}] "
        if style:
            log.write(Text(prefix + message, style=style))
        else:
            log.write(prefix + message)

    def action_clear_log(self) -> None:
        self.query_one(RichLog).clear()

    def action_help(self) -> None:
        self.log_line(
            "Commands: wish <query> | similar | tag <tag> [limit] | wishing | togglewishing | reconnect | help | quit",
            "cyan",
        )
        self.log_line(
            "similar -> fetch similar tracks for the current rmpc queue", "cyan"
        )
        self.log_line("wish <query> -> submit a download wish", "cyan")
        self.log_line("tag <tag> [limit] -> queue top tracks for a Last.fm tag", "cyan")
        self.log_line("wishing -> show current wishing state", "cyan")
        self.log_line("togglewishing -> toggle wishing on the server", "cyan")
        self.log_line("reconnect -> restart server connection tasks", "cyan")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        await self.handle_command(text)

    async def handle_command(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as e:
            self.log_line(f"Command parse error: {e}", "red")
            return
        if not parts:
            return
        cmd = parts[0].lower()
        if cmd in {"quit", "exit"}:
            self.exit()
        elif cmd in {"help", "?"}:
            self.action_help()
        elif cmd in {"wish"}:
            if len(parts) < 2:
                self.log_line("Usage: wish <query>", "yellow")
            else:
                await self.submit_wish(" ".join(parts[1:]))
        elif cmd in {"similar", "sim"}:
            await self.request_similar_tracks()
        elif cmd in {"tag", "search"}:
            if len(parts) < 2:
                self.log_line("Usage: tag <tag> [limit]", "yellow")
            else:
                limit = 10
                tag_parts = parts[1:]
                if len(tag_parts) >= 2 and tag_parts[-1].isdigit():
                    limit = int(tag_parts[-1])
                    tag_parts = tag_parts[:-1]
                tag = " ".join(tag_parts).strip()
                if not tag:
                    self.log_line("Usage: tag <tag> [limit]", "yellow")
                else:
                    await self.request_tag_top_tracks(tag, limit)
        elif cmd in {"wishing", "wishstate", "ws"}:
            await self.fetch_wishing_state()
        elif cmd in {"togglewishing", "togglewish", "tw"}:
            await self.toggle_wishing()
        elif cmd in {"reconnect", "reconn"}:
            await self.reconnect()
        else:
            self.log_line(f"Unknown command: {cmd}", "yellow")

    async def submit_wish(self, query: str) -> None:
        if not self.client:
            return
        self.log_line(f"Submitting wish: {query}")
        try:
            resp = await self.client.post(
                "/api/wish",
                headers={"X-API-Key": API_KEY},
                json={"query": query},
            )
            resp.raise_for_status()
            data = resp.json()
            wish_id = data.get("id", "unknown")
            self.log_line(f"Wish queued: {wish_id}", "green")
        except httpx.HTTPStatusError as e:
            detail = e.response.text if e.response else str(e)
            self.log_line(f"Wish failed: {detail}", "red")
        except Exception as e:
            self.log_line(f"Wish error: {e}", "red")

    def log_wishing_state(self) -> None:
        if self.wishing_enabled is None:
            self.log_line("Wishing: unknown", "yellow")
            return
        state = "enabled" if self.wishing_enabled else "disabled"
        style = "green" if self.wishing_enabled else "yellow"
        self.log_line(f"Wishing: {state}", style)

    async def fetch_wishing_state(self) -> None:
        if not self.client:
            return
        try:
            resp = await self.client.get("/api/wishing")
            resp.raise_for_status()
            payload = resp.json()
            self.wishing_enabled = bool(payload.get("enabled", False))
            self.log_wishing_state()
        except httpx.HTTPStatusError as e:
            detail = e.response.text if e.response else str(e)
            self.log_line(f"Failed to read wishing state: {detail}", "red")
        except Exception as e:
            self.log_line(f"Wishing state error: {e}", "red")

    async def toggle_wishing(self) -> None:
        if not self.client:
            return
        try:
            resp = await self.client.post(
                "/api/wishing/toggle", headers={"X-API-Key": API_KEY}
            )
            resp.raise_for_status()
            payload = resp.json()
            self.wishing_enabled = bool(payload.get("enabled", False))
            self.log_wishing_state()
        except httpx.HTTPStatusError as e:
            detail = e.response.text if e.response else str(e)
            self.log_line(f"Failed to toggle wishing: {detail}", "red")
        except Exception as e:
            self.log_line(f"Toggle wishing error: {e}", "red")

    async def reconnect(self) -> None:
        for task in self.tasks:
            task.cancel()
        self.tasks = []
        if self.client:
            await self.client.aclose()
            self.client = None
        self.log_line("Reconnecting...", "yellow")
        try:
            self.client = httpx.AsyncClient(base_url=SERVER_URL, timeout=60.0)
            await self.fetch_wishing_state()
            self.tasks = [asyncio.create_task(self.sse_listener())]
            self.log_line("Reconnect tasks started.", "green")
        except Exception as e:
            self.log_line(f"Reconnect failed: {e}", "red")

    async def download_file(self, filename: str) -> Path:
        assert self.client is not None
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        dest = MUSIC_DIR / filename

        async with self.client.stream(
            "GET", f"/api/downloads/{filename}", headers={"X-API-Key": API_KEY}
        ) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)

        return dest

    async def add_to_rmpc(self, filepath: Path) -> None:
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
            self.log_line(
                f"Failed to add {relative_path} to rmpc: {stderr.decode().strip()}",
                "red",
            )
        else:
            self.log_line(f"Added to rmpc queue: {relative_path}", "green")

    async def sse_listener(self) -> None:
        assert self.client is not None
        delay = 1
        while True:
            try:
                async with self.client.stream(
                    "GET",
                    "/api/sse",
                    headers={"X-API-Key": API_KEY},
                    timeout=None,
                ) as resp:
                    resp.raise_for_status()
                    if delay > 1:
                        self.log_line("SSE reconnected successfully", "green")
                    delay = 1
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            if data.get("event") == "download_complete":
                                filename = data.get("filename")
                                if filename and filename not in self.known_files:
                                    self.log_line(f"Downloading (SSE): {filename}")
                                    try:
                                        filepath = await self.download_file(filename)
                                        self.known_files.add(filename)
                                        await self.add_to_rmpc(filepath)
                                    except Exception as e:
                                        self.log_line(
                                            f"Failed to download {filename}: {e}",
                                            "red",
                                        )
            except asyncio.CancelledError:
                raise
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                self.log_line(
                    f"SSE connection lost ({e}), retrying in {delay}s...", "yellow"
                )
            except Exception as e:
                self.log_line(f"SSE error ({e}), retrying in {delay}s...", "yellow")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def read_rmpc_queue(self) -> list[dict[str, str]]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "rmpc",
                "queue",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                self.log_line(f"rmpc queue failed: {stderr.decode().strip()}", "red")
                return []
            output = stdout.decode()
            payload = None
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                start = output.find("[")
                end = output.rfind("]")
                if start != -1 and end != -1 and end > start:
                    try:
                        payload = json.loads(output[start : end + 1])
                    except json.JSONDecodeError:
                        payload = None

            if isinstance(payload, list):
                tracks: list[dict[str, str]] = []
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    meta = item.get("metadata") or {}
                    artist = (meta.get("artist") or "").strip()
                    title = (meta.get("title") or "").strip()
                    if not title:
                        title = (item.get("name") or "").strip()
                    if not artist:
                        artist = (item.get("artist") or "").strip()

                    if not artist and " - " in title:
                        guessed_artist, guessed_title = title.split(" - ", 1)
                        artist = guessed_artist.strip()
                        title = guessed_title.strip()

                    if (
                        artist
                        and title
                        and title.lower().startswith(artist.lower() + " - ")
                    ):
                        title = title[len(artist) + 3 :].strip()
                    if artist and title:
                        tracks.append({"artist": artist, "title": title})
                if tracks:
                    return tracks

            return parse_rmpc_queue(output)
        except FileNotFoundError:
            self.log_line("rmpc not found in PATH", "red")
            return []

    async def request_similar_tracks(self) -> None:
        if not self.client:
            return
        tracks = await self.read_rmpc_queue()
        if not tracks:
            self.log_line("No tracks found in rmpc queue.", "yellow")
            return
        self.log_line(f"Requesting similar tracks for {len(tracks)} queue entries...")
        try:
            resp = await self.client.post(
                "/api/similar",
                headers={"X-API-Key": API_KEY},
                json={"tracks": tracks},
            )
            resp.raise_for_status()
            payload = resp.json()
            queued = payload.get("queued", 0)
            skipped = payload.get("skipped", 0)
            returned = payload.get("tracks", [])
            self.log_line(
                f"Similar tracks queued: {queued} (skipped: {skipped}, found: {len(returned)})",
                "green",
            )
            if not returned:
                self.log_line(
                    "No similar tracks found for the current queue.", "yellow"
                )
            else:
                preview = ", ".join(
                    f"{t.get('artist')} - {t.get('title')}" for t in returned[:5]
                )
                self.log_line(f"Similar preview: {preview}", "cyan")
        except httpx.HTTPStatusError as e:
            detail = e.response.text if e.response else str(e)
            self.log_line(f"Similar request failed: {detail}", "red")
        except Exception as e:
            self.log_line(f"Similar request error: {e}", "red")

    async def request_tag_top_tracks(self, tag: str, limit: int = 10) -> None:
        if not self.client:
            return
        safe_limit = max(1, min(limit, 50))
        self.log_line(f"Queueing top tracks for tag: {tag} (limit {safe_limit})")
        try:
            resp = await self.client.post(
                "/api/tag/queue",
                headers={"X-API-Key": API_KEY},
                params={"tag": tag, "limit": safe_limit},
            )
            resp.raise_for_status()
            payload = resp.json()
            queued = payload.get("queued", 0)
            skipped = payload.get("skipped", 0)
            returned = payload.get("tracks", [])
            if not returned:
                self.log_line(f"No tracks found for tag: {tag}", "yellow")
                return
            self.log_line(
                f"Tag tracks queued: {queued} (skipped: {skipped}, found: {len(returned)})",
                "green",
            )
        except httpx.HTTPStatusError as e:
            detail = e.response.text if e.response else str(e)
            self.log_line(f"Tag lookup failed: {detail}", "red")
        except Exception as e:
            self.log_line(f"Tag lookup error: {e}", "red")


def main() -> None:
    PlayerClientApp().run()


if __name__ == "__main__":
    main()
