"""Bounded local video preparation; ffmpeg never opens a network URL."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import httpx

UPLOAD_BYTES = 50 * 1024 * 1024
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
    async def collect():
        output = bytearray()
        while block := await process.stdout.read(64 * 1024):
            output.extend(block)
            if len(output) > 32 * 1024 * 1024:
                raise VideoPreparationError("Video metadata exceeds its processing limit.")
        await process.wait()
        return bytes(output)

    try:
        stdout = await asyncio.wait_for(collect(), timeout)
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


def plan_cuts(keyframes: list[tuple[float, int]], size: int, budget: int) -> list[float]:
    """Fill each part at the furthest safe keyframe to minimize part count.

    Packet offsets account for VBR/interleaved audio; equal times do not.
    Actual muxed sizes are checked separately.
    """
    cuts = []
    current = 0
    start = 0  # the first part also needs the original container's header room
    while size - start > budget:
        end = current
        while end + 1 < len(keyframes) and keyframes[end + 1][1] - start <= budget:
            end += 1
        if end == current:
            raise VideoPreparationError("This video's keyframes prevent safe size-bounded splitting.")
        cuts.append(keyframes[end][0] - keyframes[0][0])
        current = end
        start = keyframes[current][1]
        if len(cuts) >= MAX_SEGMENTS:
            raise VideoPreparationError("Video splitting would exceed the 64-segment limit.")
    return cuts


async def keyframe_offsets(source: Path) -> list[tuple[float, int]]:
    raw = await run_media_tool(
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mov",
        "-select_streams", "v:0", "-show_packets", "-show_entries", "packet=pts_time,pos,flags",
        "-of", "compact=p=0:nk=0", str(source),
    )
    result = []
    try:
        for line in raw.decode().splitlines():
            values = dict(part.split("=", 1) for part in line.split("|") if "=" in part)
            if "K" not in values.get("flags", ""):
                continue
            seconds, position = float(values["pts_time"]), int(values["pos"])
            if not math.isfinite(seconds) or position < 0:
                raise ValueError
            if result and (seconds <= result[-1][0] or position <= result[-1][1]):
                raise ValueError
            result.append((seconds, position))
    except (UnicodeError, ValueError, KeyError):
        raise VideoPreparationError("The video has no usable keyframe boundaries.") from None
    if not result:
        raise VideoPreparationError("The video has no usable keyframe boundaries.")
    return result


async def split_video(source: Path, directory: Path, max_bytes: int = UPLOAD_BYTES) -> list[Path]:
    """Leave uploadable sources intact; fill the fewest size-safe video parts."""
    size = source.stat().st_size
    if size <= max_bytes:
        return [source]
    seconds = await duration(source)
    keyframes = await keyframe_offsets(source)
    reserve = min(64 * 1024, max(1, max_bytes // 100))
    budget = max_bytes - reserve
    directory.mkdir()
    for _ in range(8):
        cuts = plan_cuts(keyframes, size, budget)
        if not cuts:
            raise VideoPreparationError("The video has no usable split boundaries.")
        for path in directory.glob("part-*.mp4"):
            path.unlink()
        try:
            await run_media_tool(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "file,pipe", "-f", "mov", "-enable_drefs", "0", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-f", "segment", "-segment_times",
                ",".join(f"{cut:.6f}" for cut in cuts),
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
        overshoot = max(sizes) - max_bytes
        if overshoot < 0:
            break
        budget -= overshoot + reserve
        if budget <= 0:
            break
    raise VideoPreparationError("This video's keyframes prevent safe size-bounded splitting.")
