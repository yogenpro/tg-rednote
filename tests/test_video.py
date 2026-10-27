"""Video continuations: offline media fixtures and fake Telegram delivery."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
from app import media, video
from app.media import MediaSender, delivery_groups
from app.telegram import TelegramError
from app.xhs import MediaItem


class Telegram:
    def __init__(self, fail_part=None):
        self.calls = []
        self.fail_part = fail_part

    async def call(self, method, payload, files=None, **kwargs):
        self.calls.append((method, dict(payload), dict(files or {})))
        index = len(self.calls)
        if index == self.fail_part:
            raise TelegramError(method, 0, "WriteTimeout")
        return {"message_id": index, "video": {"file_id": f"segment-{index}"}}


def mock_preparation(monkeypatch, tmp_path):
    directories = []
    original = media.tempfile.TemporaryDirectory

    def temporary(**kwargs):
        result = original(dir=tmp_path, **kwargs)
        directories.append(Path(result.name))
        return result

    async def split(source, directory, max_bytes):
        assert source.read_bytes() == b"v" * 2048
        directory.mkdir()
        paths = [directory / "part-000.mp4", directory / "part-001.mp4"]
        for index, path in enumerate(paths):
            path.write_bytes(bytes([index]) * 600)
        return paths

    monkeypatch.setattr(media.tempfile, "TemporaryDirectory", temporary)
    monkeypatch.setattr(media, "split_video", split)
    return directories


def prepared_sender(telegram):
    sender = MediaSender(telegram, mode="upload", max_bytes=1024)
    sender._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"v" * 2048, request=request)
    ))
    return sender


@pytest.mark.asyncio
async def test_segments_are_individual_numbered_posts_with_reply_chain_and_distinct_cache(monkeypatch, tmp_path):
    directories = mock_preparation(monkeypatch, tmp_path)
    telegram = Telegram()
    sender = prepared_sender(telegram)
    item = MediaItem("video", "https://cdn/source", size=2048)
    try:
        report = await sender.send(1, [item], "description", reply_to=90,
                                   followup_captions=["overflow"])
        assert [method for method, _, _ in telegram.calls] == ["sendVideo", "sendVideo"]
        assert [payload["caption"] for _, payload, _ in telegram.calls] == [
            "[1/2] description", "[2/2] overflow",
        ]
        assert [payload["reply_to_message_id"] for _, payload, _ in telegram.calls] == [90, 1]
        assert all(len(files["file0"][1]) <= 1024 for _, _, files in telegram.calls)
        assert report.parts == [(1, "[1/2] description"), (2, "[2/2] overflow")]
        assert report.sent == 2 and report.uploaded and report.unused_captions == []
        assert sender._file_ids.get(item.url) is None  # never cache part 1 as the whole video
        assert all(not directory.exists() for directory in directories)
        again = await sender.send(1, [item], "again")
        assert again.sent == 2 and not again.uploaded
        assert all(payload["video"].startswith("segment-") for _, payload, _ in telegram.calls[2:])
    finally:
        await sender.aclose()


@pytest.mark.asyncio
async def test_segment_files_are_removed_when_a_followup_upload_fails(monkeypatch, tmp_path):
    directories = mock_preparation(monkeypatch, tmp_path)
    sender = prepared_sender(Telegram(fail_part=2))
    try:
        with pytest.raises(TelegramError):
            await sender.send(1, [MediaItem("video", "https://cdn/source", size=2048)], "caption")
        assert all(not directory.exists() for directory in directories)
        assert not sender._video_slot.locked()
    finally:
        await sender.aclose()


@pytest.mark.asyncio
async def test_cancelled_preparation_releases_temp_files_and_slot(monkeypatch, tmp_path):
    directories = mock_preparation(monkeypatch, tmp_path)
    started = asyncio.Event()

    async def download(client, url, path, limit):
        path.write_bytes(b"partial")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(media, "download_video", download)
    sender = prepared_sender(Telegram())
    task = asyncio.create_task(sender.send(1, [MediaItem("video", "https://cdn/source")], "caption"))
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(not directory.exists() for directory in directories)
        assert not sender._video_slot.locked()
    finally:
        await sender.aclose()


def test_delivery_groups_keep_photo_albums_and_segment_order():
    photos = [MediaItem("photo", str(i)) for i in range(12)]
    segments = [MediaItem("video", str(i), continuation=True) for i in range(3)]
    groups = delivery_groups(photos + segments + photos[:2])
    assert list(map(len, groups)) == [10, 2, 1, 1, 1, 2]
    assert [item for group in groups for item in group] == photos + segments + photos[:2]


@pytest.mark.asyncio
async def test_known_small_videos_and_explicit_url_mode_keep_url_delivery():
    for mode, size in [("auto", 100), ("url", video.SOURCE_BYTES * 2)]:
        telegram = Telegram()
        sender = MediaSender(telegram, mode=mode)
        try:
            await sender.send(1, [MediaItem("video", "https://cdn/small", size=size)], "caption")
            assert telegram.calls[0][1]["video"] == "https://cdn/small"
        finally:
            await sender.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False])
async def test_source_download_is_bounded_with_or_without_content_length(tmp_path, declared):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 6
            yield b"x" * 6

    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, request=r, stream=Stream(),
                                headers={"content-length": "12"} if declared else {})
    ))
    try:
        with pytest.raises(video.VideoPreparationError, match="500 MiB"):
            await video.download_video(client, "https://cdn/video?private-signed-url", tmp_path / "v.mp4", 8)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "0", "-2", None])
async def test_invalid_duration_fails_before_sending(monkeypatch, tmp_path, raw):
    async def tool(*args, **kwargs):
        return json.dumps({"format": {"duration": raw}}).encode()
    monkeypatch.setattr(video, "run_media_tool", tool)
    with pytest.raises(video.VideoPreparationError, match="duration"):
        await video.duration(tmp_path / "source.mp4")


@pytest.mark.asyncio
async def test_split_rechecks_actual_bytes_and_retries_shorter_interval(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 100)
    intervals = []

    async def tool(*args, **kwargs):
        if args[0] == "ffprobe":
            return b'{"format":{"duration":"12"}}' if args[-1] == str(source) else b'{"format":{"duration":"6"}}'
        intervals.append(args[args.index("-segment_times") + 1])
        directory = Path(args[-1]).parent
        for n in range(2):
            (directory / f"part-{n:03d}.mp4").write_bytes(b"x" * (60 if len(intervals) == 1 else 40))
        return b""

    monkeypatch.setattr(video, "run_media_tool", tool)
    result = await video.split_video(source, tmp_path / "parts", 50)
    assert len(intervals) == 2
    assert float(intervals[1].split(',')[0]) < float(intervals[0].split(',')[0])
    assert len(result) == 2 and all(p.stat().st_size <= 50 for p in result)


@pytest.fixture
def sample_video(tmp_path):
    assert shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required for media regression tests"
    path = tmp_path / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "12",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", "10",
        "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(path),
    ], check=True, stdin=subprocess.DEVNULL, timeout=30)
    return path


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_real_segments_are_decodable_under_limit_and_preserve_all_frames(sample_video, tmp_path):
    target = 48 * 1024
    assert sample_video.stat().st_size > target
    parts = await video.split_video(sample_video, tmp_path / "parts", target)
    assert len(parts) > 1 and all(p.stat().st_size <= target for p in parts)
    counts = []
    for path in [sample_video, *parts]:
        data = json.loads(await video.run_media_tool(
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames", "-of", "json", str(path),
        ))
        counts.append(int(data["streams"][0]["nb_read_frames"]))
        await video.run_media_tool("ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-")
    assert counts[0] == sum(counts[1:])


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_small_real_video_is_not_split(sample_video, tmp_path):
    assert await video.split_video(sample_video, tmp_path / "parts", sample_video.stat().st_size) == [sample_video]


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_invalid_real_media_fails_safely(tmp_path):
    source = tmp_path / "not-video.mp4"
    source.write_bytes(b"not an mp4" * 100)
    with pytest.raises(video.VideoPreparationError):
        await video.split_video(source, tmp_path / "parts", 100)


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_real_preparation_flows_into_separate_telegram_continuations(sample_video, monkeypatch):
    target = 48 * 1024
    monkeypatch.setattr(media, "SEGMENT_BYTES", target)
    telegram = Telegram()
    sender = MediaSender(telegram, mode="auto")
    await sender._client.aclose()
    sender._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, request=r, content=sample_video.read_bytes())
    ))
    try:
        item = MediaItem("video", "https://cdn/video", size=sample_video.stat().st_size)
        async with sender.prepare([item]) as items:
            paths = [item.local_path for item in items]
            assert len(items) > 1
            report = await sender.send(1, items, "caption", followup_captions=["overflow"])
            assert report.sent == len(items)
            assert len(report.parts) == len(items)
            assert all(method == "sendVideo" for method, _, _ in telegram.calls)
            assert all(len(files["file0"][1]) <= target for _, _, files in telegram.calls)
            assert "overflow" in telegram.calls[1][1]["caption"]
        assert all(not path.exists() for path in paths)
    finally:
        await sender.aclose()
