import asyncio
import json
import logging
import re
from pathlib import Path

from mutagen.oggopus import OggOpus

from mach_mukke.config import MAX_DURATION_SECONDS, TMP_DIR

logger = logging.getLogger("mach_mukke.downloader")

YOUTUBE_PREFIXES = (
    "https://www.youtube.com/",
    "https://youtube.com/",
    "https://youtu.be/",
    "https://m.youtube.com/",
    "http://www.youtube.com/",
    "http://youtube.com/",
    "http://youtu.be/",
)


def is_youtube_url(query: str) -> bool:
    return query.startswith(YOUTUBE_PREFIXES)


def build_yt_dlp_args(query: str, output_template: str) -> list[str]:
    is_url = is_youtube_url(query)
    args = [
        "yt-dlp",
        "-x",
        "--audio-format",
        "opus",
        "--audio-quality",
        "5",
        "--embed-metadata",
        "--write-info-json",
        "--print-json",
        "--output",
        output_template,
    ]
    if not is_url:
        args += [
            "--match-filter",
            f"duration <= {MAX_DURATION_SECONDS}",
            "--sponsorblock-remove",
            "music_offtopic,intro,outro",
        ]
    args.append(query if is_url else f"ytsearch1:{query}")
    return args


def parse_metadata(stdout: str) -> dict | None:
    for line in stdout.strip().split("\n"):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "title" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    return None


async def run_yt_dlp(query: str, output_template: str) -> tuple[dict | None, str, str]:
    args = build_yt_dlp_args(query, output_template)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stdout_str = stdout.decode()
    stderr_str = stderr.decode()
    metadata = parse_metadata(stdout_str)
    return metadata, stdout_str, stderr_str


def embed_metadata(opus_path: Path, metadata: dict) -> None:
    try:
        audio = OggOpus(str(opus_path))
        title = metadata.get("title", "")
        artist = metadata.get("artist", "")
        album = metadata.get("album", "")
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
        logger.info(f"Embedded metadata: {title} - {artist}")
    except Exception as e:
        logger.warning(f"Failed to embed metadata for {opus_path}: {e}")


async def analyze_loudness(opus_path: Path) -> tuple[dict | None, str]:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-i",
        str(opus_path),
        "-af",
        "loudnorm=print_format=json",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = (stderr + stdout).decode(errors="replace")

    json_blocks: list[str] = []
    collecting = False
    buffer: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            collecting = True
            buffer = [line]
            continue
        if collecting:
            buffer.append(line)
            if stripped.startswith("}"):
                json_blocks.append("\n".join(buffer))
                collecting = False
                buffer = []

    for block in reversed(json_blocks):
        try:
            return json.loads(block), output
        except json.JSONDecodeError:
            continue

    return None, output


def get_r128_track_gain(opus_path: Path) -> int | None:
    try:
        audio = OggOpus(str(opus_path))
        value = audio.get("R128_TRACK_GAIN")
        if not value:
            return None
        if isinstance(value, list):
            value = value[0]
        return int(str(value).strip())
    except Exception:
        return None


async def apply_r128_track_gain(
    opus_path: Path, target_lufs: float = -23.0
) -> tuple[bool, str]:
    data, _output = await analyze_loudness(opus_path)
    if not data:
        return False, "Loudness analysis failed (no JSON output)"
    if "input_i" not in data:
        return False, "Loudness analysis failed (missing input_i)"
    try:
        current_lufs = float(data["input_i"])
    except (TypeError, ValueError):
        return False, f"Invalid input_i value: {data.get('input_i')}"

    gain_db = target_lufs - current_lufs
    gain_q78 = int(round(gain_db * 256))

    try:
        audio = OggOpus(str(opus_path))
        audio["R128_TRACK_GAIN"] = str(gain_q78)
        audio.save()
        return True, f"Tagged R128_TRACK_GAIN={gain_q78} (gain {gain_db:.2f} dB)"
    except Exception as e:
        return False, f"Failed to write R128_TRACK_GAIN: {e}"


def validate_opus(opus_path: Path) -> tuple[bool, str]:
    try:
        audio = OggOpus(str(opus_path))
        duration = audio.info.length if audio.info else 0
        if duration <= 0:
            return False, "Invalid duration (0 seconds)"
        if duration > MAX_DURATION_SECONDS:
            return (
                False,
                f"Duration {duration:.0f}s exceeds max {MAX_DURATION_SECONDS}s",
            )
        return True, f"Valid Opus, duration: {duration:.0f}s"
    except Exception as e:
        return False, f"Opus validation failed: {e}"


def sanitize_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in " _-." else "_" for c in name)


def resolve_final_path(downloads_dir: Path, base_name: str) -> Path:
    final_path = downloads_dir / base_name
    counter = 1
    while final_path.exists():
        stem = base_name.rsplit(".", 1)[0]
        final_path = downloads_dir / f"{stem}_{counter}.opus"
        counter += 1
    return final_path
