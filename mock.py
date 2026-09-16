"""
A fake camera system for developing against with no hardware.

Same role seed_data.py played in customer-support-mcp: lets you build and test
the entire MCP tool surface before touching a real NVR.

Two things make this more than a dummy:

1. Generated frames have the camera name and timestamp BURNED INTO the image.
   When you test in Claude Desktop, Claude reads that text back to you - so you
   can verify end-to-end that "yesterday at 3pm IST" actually fetched 15:00 IST
   and not 15:00 UTC. That is the bug this whole project is most likely to have.

2. Events and recording gaps are seeded deterministically from camera_id + date,
   so the same query returns the same results every run. Reproducible tests.
"""

from __future__ import annotations

import hashlib
import io
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.backends.base import (
    CameraBackend,
    ExportTooLargeError,
    FootageUnavailableError,
)
from app.models import (
    Camera,
    Capability,
    ClipExport,
    Coverage,
    Event,
    Frame,
    RecordingSegment,
)

_ALL = [
    Capability.LIVE_SNAPSHOT,
    Capability.HISTORICAL_SNAPSHOT,
    Capability.CLIP_EXPORT,
    Capability.EVENT_SEARCH,
    Capability.PLAYBACK_URL,
]

# One camera deliberately lacks EVENT_SEARCH and one is offline, so you exercise
# the degradation paths instead of only ever hitting the happy case.
_CAMERAS = [
    Camera(
        id="front_gate",
        name="Front Gate",
        aliases=["main entrance", "gate cam", "entry"],
        location="Andheri Warehouse",
        timezone="Asia/Kolkata",
        retention_days=30,
        capabilities=_ALL,
    ),
    Camera(
        id="reception",
        name="Reception",
        aliases=["lobby", "front desk"],
        location="Andheri Warehouse",
        timezone="Asia/Kolkata",
        retention_days=30,
        capabilities=_ALL,
    ),
    Camera(
        id="loading_bay",
        name="Loading Bay",
        aliases=["dock", "loading dock"],
        location="Andheri Warehouse",
        timezone="Asia/Kolkata",
        retention_days=14,  # shorter retention - tests the coverage check
        capabilities=_ALL,
    ),
    Camera(
        id="stockroom",
        name="Stockroom",
        aliases=["store room", "inventory"],
        location="Andheri Warehouse",
        timezone="Asia/Kolkata",
        retention_days=30,
        capabilities=[c for c in _ALL if c != Capability.EVENT_SEARCH],
    ),
    Camera(
        id="parking",
        name="Parking",
        aliases=["car park", "basement"],
        location="Andheri Warehouse",
        timezone="Asia/Kolkata",
        retention_days=30,
        capabilities=_ALL,
        is_online=False,  # offline - tests status handling
    ),
]

_LABELS = ["person", "car", "motorcycle", "truck", "bicycle"]


def _seed_for(camera_id: str, day: str) -> random.Random:
    """Stable RNG so the same camera+day always yields the same events."""
    digest = hashlib.sha256(f"{camera_id}:{day}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


class MockBackend(CameraBackend):
    """Synthetic camera system. No network, no hardware."""

    name = "mock"

    def __init__(self, max_clip_seconds: int = 900):
        self.max_clip_seconds = max_clip_seconds
        self._cameras = {c.id: c for c in _CAMERAS}

    # -- discovery ---------------------------------------------------------

    async def list_cameras(self) -> list[Camera]:
        return list(self._cameras.values())

    async def get_coverage(self, camera_id: str) -> Coverage:
        cam = await self.resolve_camera(camera_id)
        now = datetime.now(timezone.utc)
        return Coverage(
            camera_id=cam.id,
            earliest=now - timedelta(days=cam.retention_days),
            latest=now,
        )

    # -- stills ------------------------------------------------------------

    async def get_frame(self, camera_id: str, at: datetime | None = None) -> Frame:
        cam = await self.resolve_camera(camera_id)
        is_live = at is None
        moment = datetime.now(timezone.utc) if is_live else at.astimezone(timezone.utc)

        if not is_live:
            await self.check_coverage(cam.id, moment)
            # Real NVRs snap to the nearest keyframe rather than an exact match.
            # Rounding down to the second here mimics that, and proves the tool
            # layer reports actual time rather than requested time.
            moment = moment.replace(microsecond=0)

        png = self._render(cam, moment, is_live)
        return Frame(
            camera_id=cam.id,
            captured_at=moment,
            image_bytes=png,
            media_type="image/png",
            width=640,
            height=360,
            is_live=is_live,
        )

    async def get_frames(
        self, camera_id: str, start: datetime, end: datetime, max_frames: int = 6
    ) -> list[Frame]:
        cam = await self.resolve_camera(camera_id)
        if end <= start:
            raise ValueError("end must be after start")

        max_frames = max(1, min(max_frames, 10))
        await self.check_coverage(cam.id, start)
        await self.check_coverage(cam.id, end)

        span = (end - start).total_seconds()
        step = span / max_frames
        return [
            await self.get_frame(cam.id, start + timedelta(seconds=step * i))
            for i in range(max_frames)
        ]

    # -- recordings --------------------------------------------------------

    async def find_recordings(
        self, camera_id: str, start: datetime, end: datetime
    ) -> list[RecordingSegment]:
        """Returns segments with a deliberate gap injected each day.

        Every camera 'drops offline' for 12 minutes at a seeded hour, so your
        tools get exercised against real-world discontinuity instead of
        assuming an unbroken timeline.
        """
        cam = await self.resolve_camera(camera_id)
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)

        gap_duration = timedelta(minutes=12)
        span = (end - start).total_seconds()

        # Window too short to hold a gap - return one continuous segment.
        if span <= gap_duration.total_seconds() * 2:
            return [RecordingSegment(camera_id=cam.id, start=start, end=end)]

        # Offset from the window start rather than replacing wall-clock fields:
        # start is UTC, so replace(hour=...) would place the gap outside the
        # window whenever the caller's local day does not align with the UTC day.
        rng = _seed_for(cam.id, start.strftime("%Y-%m-%d"))
        room = span - gap_duration.total_seconds()
        gap_start = start + timedelta(seconds=rng.uniform(room * 0.1, room * 0.9))
        gap_end = gap_start + gap_duration

        segments = []
        if gap_start > start:
            segments.append(
                RecordingSegment(camera_id=cam.id, start=start, end=gap_start)
            )
        if gap_end < end:
            segments.append(RecordingSegment(camera_id=cam.id, start=gap_end, end=end))
        return segments

    async def export_clip(
        self, camera_id: str, start: datetime, end: datetime, dest_dir: str
    ) -> ClipExport:
        """Writes a real, playable MP4 built from generated frames."""
        cam = await self.resolve_camera(camera_id)
        duration = (end - start).total_seconds()
        if duration <= 0:
            raise ValueError("end must be after start")
        if duration > self.max_clip_seconds:
            raise ExportTooLargeError(
                f"Requested {duration:.0f}s exceeds the "
                f"{self.max_clip_seconds}s ceiling for exports."
            )
        await self.check_coverage(cam.id, start)

        out_dir = Path(dest_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = start.astimezone(cam.local_tz).strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"{cam.id}_{stamp}.mp4"

        self._write_mp4(cam, start, duration, path)
        return ClipExport(
            camera_id=cam.id,
            start=start,
            end=end,
            file_path=str(path.resolve()),
            size_bytes=path.stat().st_size,
            duration_seconds=duration,
        )

    # -- events ------------------------------------------------------------

    async def search_events(
        self,
        camera_id: str | None,
        start: datetime,
        end: datetime,
        labels: list[str] | None = None,
        limit: int = 50,
    ) -> list[Event]:
        if camera_id is None:
            targets = [c for c in self._cameras.values() if c.supports(Capability.EVENT_SEARCH)]
        else:
            targets = [await self.require_capability(camera_id, Capability.EVENT_SEARCH)]

        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        wanted = {lbl.lower() for lbl in labels} if labels else None

        events: list[Event] = []
        for cam in targets:
            rng = _seed_for(cam.id, start.strftime("%Y-%m-%d"))
            span = (end - start).total_seconds()
            for i in range(rng.randrange(2, 9)):
                label = rng.choice(_LABELS)
                if wanted and label not in wanted:
                    continue
                offset = rng.random() * span
                begins = start + timedelta(seconds=offset)
                events.append(
                    Event(
                        id=f"{cam.id}-{begins:%Y%m%d%H%M%S}-{i}",
                        camera_id=cam.id,
                        label=label,
                        start=begins,
                        end=begins + timedelta(seconds=rng.randrange(4, 45)),
                        score=round(rng.uniform(0.62, 0.98), 2),
                    )
                )

        events.sort(key=lambda e: e.start)
        return events[:limit]

    # -- links -------------------------------------------------------------

    def get_playback_url(self, camera_id: str, at: datetime) -> str:
        epoch = int(at.astimezone(timezone.utc).timestamp())
        return f"http://mock-nvr.local:5000/playback?camera={camera_id}&t={epoch}"

    # -- internals ---------------------------------------------------------

    def _render(self, cam: Camera, moment: datetime, is_live: bool) -> bytes:
        """Draw a frame with identifying text burned in.

        The local-time line is the important one - it is what lets Claude
        confirm the timezone round-trip when you test in Desktop.
        """
        local = moment.astimezone(cam.local_tz)
        rng = random.Random(int(moment.timestamp()) // 2)

        img = Image.new("RGB", (640, 360), (26, 30, 38))
        draw = ImageDraw.Draw(img)

        # crude scene so successive frames are visibly different
        draw.rectangle([0, 250, 640, 360], fill=(46, 52, 64))
        for _ in range(rng.randrange(0, 4)):
            x = rng.randrange(40, 580)
            draw.rectangle([x, 200, x + 34, 280], fill=(140, 150, 165))
            draw.ellipse([x + 8, 182, x + 26, 202], fill=(200, 190, 175))

        try:
            big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 26)
            small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 17)
        except OSError:
            big = small = ImageFont.load_default()

        draw.text((16, 14), cam.name.upper(), font=big, fill=(240, 240, 240))
        draw.text((16, 48), f"LOCAL {local:%Y-%m-%d %H:%M:%S} {cam.timezone}",
                  font=small, fill=(120, 220, 140))
        draw.text((16, 70), f"UTC   {moment:%Y-%m-%d %H:%M:%S}",
                  font=small, fill=(150, 180, 220))
        draw.text((16, 92), "LIVE" if is_live else "RECORDED",
                  font=small, fill=(230, 120, 120) if is_live else (180, 180, 180))
        draw.text((16, 330), "SYNTHETIC - MockBackend", font=small, fill=(110, 110, 120))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _write_mp4(self, cam: Camera, start: datetime, duration: float, path: Path) -> None:
        """Encode generated frames into a playable MP4 via ffmpeg.

        Falls back to a single-frame PNG alongside if ffmpeg is unavailable,
        so development is never blocked on the binary being installed.
        """
        import shutil
        import subprocess
        import tempfile

        frame_count = max(2, min(int(duration / 5), 60))
        step = duration / frame_count

        if shutil.which("ffmpeg") is None:
            path.with_suffix(".png").write_bytes(self._render(cam, start, False))
            path.write_bytes(b"")  # placeholder so file_path exists
            return

        with tempfile.TemporaryDirectory() as tmp:
            for i in range(frame_count):
                moment = start + timedelta(seconds=step * i)
                (Path(tmp) / f"f{i:05d}.png").write_bytes(self._render(cam, moment, False))
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-framerate", "2",
                 "-i", str(Path(tmp) / "f%05d.png"),
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                check=True,
            )
