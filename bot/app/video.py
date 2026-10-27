"""Bounded local video preparation; ffmpeg never opens a network URL."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import httpx

SEGMENT_BYTES = 20 * 1024 * 1024
SOURCE_BYTES = 500 * 1024 * 1024
MAX_SEGMENTS = 64


class VideoPreparationError(RuntimeError):
    """Fixed, safe errors only: no cookies, signed URLs or subprocess stderr."""


async def run_media_tool(*args: str, timeout: float = 120) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise VideoPreparationError("Video processing tools are unavailable.") from None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        # Cancellation must not leave ffmpeg writing into a removed temp dir.
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise VideoPreparationError("The video could not be processed safely.")
    return stdout


async def duration(path: Path) -> float:
    try:
        value = json.loads(await run_media_tool(
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mov",
            "-show_entries", "format=duration", "-of", "json", str(path),
        ))
        seconds = float(value["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        raise VideoPreparationError("The video has no usable duration.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise VideoPreparationError("The video has no usable duration.")
    return seconds


async def download_video(client: httpx.AsyncClient, url: str, path: Path, limit: int) -> int:
    """Stream to a private temporary file; never buffer a whole source in RAM."""
    size = 0
    try:
        async with asyncio.timeout(300):
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and int(declared) > limit:
                    raise VideoPreparationError("Video sources exceed the 500 MiB preparation limit.")
                with path.open("xb") as output:
                    async for block in response.aiter_bytes(64 * 1024):
                        size += len(block)
                        if size > limit:
                            raise VideoPreparationError("Video sources exceed the 500 MiB preparation limit.")
                        output.write(block)
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise VideoPreparationError("The video source could not be downloaded.") from None
    if not size:
        raise VideoPreparationError("The video source was empty.")
    return size


async def split_video(source: Path, directory: Path, max_bytes: int = SEGMENT_BYTES) -> list[Path]:
    """Copy complete keyframe-aligned segments, then verify actual byte sizes.

    VBR/keyframe placement makes a time estimate insufficient. Retry with a
    shorter interval when necessary, and fail before any post if a bound cannot
    be met. No arbitrary non-keyframe cuts or silent truncation.
    """
    size = source.stat().st_size
    if size <= max_bytes:
        return [source]
    seconds = await duration(source)
    interval = seconds * max_bytes / size * 0.8
    directory.mkdir()
    for _ in range(8):
        for path in directory.glob("part-*.mp4"):
            path.unlink()
        try:
            await run_media_tool(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "file,pipe", "-f", "mov", "-enable_drefs", "0", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-f", "segment", "-segment_times",
                ",".join(str(max(0.1, interval) * i) for i in range(1, MAX_SEGMENTS)
                         if max(0.1, interval) * i < seconds),
                "-reset_timestamps", "1", "-segment_format", "mp4",
                "-segment_format_options", "movflags=+faststart",
                str(directory / "part-%03d.mp4"),
            )
        except TimeoutError:
            raise VideoPreparationError("Video splitting exceeded its processing time limit.") from None
        parts = sorted(directory.glob("part-*.mp4"))
        if not parts or len(parts) > MAX_SEGMENTS:
            raise VideoPreparationError("Video splitting would exceed the 64-segment limit.")
        sizes = [part.stat().st_size for part in parts]
        if min(sizes) > 0 and max(sizes) <= max_bytes:
            # ffmpeg closes every file before returning; every segment must
            # also be independently parseable, with near-zero timestamps.
            total = sum([await duration(part) for part in parts])
            if abs(total - seconds) > max(2.0, len(parts) * 0.25):
                raise VideoPreparationError("Video segment durations did not preserve the full source.")
            return parts
        if interval <= 0.1:
            break
        interval = max(0.1, interval * min(0.5, max_bytes / max(sizes) * 0.8))
    raise VideoPreparationError("This video's keyframes prevent safe size-bounded splitting.")
