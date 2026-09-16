"""
The contract every camera backend implements.

MCP tools call these methods and never know whether Frigate, a Hikvision NVR,
or Milestone is underneath. Adding a new system means writing one subclass -
no tool signatures change.

All methods are async because every real backend is an HTTP client. Even
though ffmpeg work is subprocess-based, keeping one calling convention avoids
sync/async mixing later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from app.models import (
    Camera,
    Capability,
    ClipExport,
    Coverage,
    Event,
    Frame,
    RecordingSegment,
)


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------
# Tools catch these and turn them into clear messages. The distinction matters:
# "that camera doesn't exist" and "that camera exists but footage that old is
# gone" need different responses, and an empty list communicates neither.


class CameraBackendError(Exception):
    """Base for everything this layer raises."""


class BackendConnectionError(CameraBackendError):
    """Could not reach the NVR/VMS at all - network, DNS, service down."""


class BackendAuthError(CameraBackendError):
    """Reached it, but credentials were rejected."""


class CameraNotFoundError(CameraBackendError):
    """No camera matched the given id, name, or alias."""

    def __init__(self, query: str, available: list[str] | None = None):
        self.query = query
        self.available = available or []
        hint = f" Available: {', '.join(self.available)}" if self.available else ""
        super().__init__(f"No camera matches {query!r}.{hint}")


class AmbiguousCameraError(CameraBackendError):
    """More than one camera matched - ask the user which one."""

    def __init__(self, query: str, matches: list[str]):
        self.query = query
        self.matches = matches
        super().__init__(
            f"{query!r} matches multiple cameras: {', '.join(matches)}. "
            "Ask the user to pick one."
        )


class FootageUnavailableError(CameraBackendError):
    """Camera is real, the time is real, but there is no footage there.

    Carries the actual coverage window so the tool can tell the user what
    IS available instead of just failing.
    """

    def __init__(self, camera_id: str, requested: datetime, coverage: Coverage | None = None):
        self.camera_id = camera_id
        self.requested = requested
        self.coverage = coverage
        msg = f"No footage for camera {camera_id!r} at {requested.isoformat()}."
        if coverage:
            msg += (
                f" Available range: {coverage.earliest.isoformat()} to "
                f"{coverage.latest.isoformat()}."
            )
        super().__init__(msg)


class CapabilityNotSupportedError(CameraBackendError):
    """This backend or camera cannot do what was asked."""

    def __init__(self, camera_id: str, capability: Capability):
        self.camera_id = camera_id
        self.capability = capability
        super().__init__(
            f"Camera {camera_id!r} does not support {capability.value}."
        )


class ExportTooLargeError(CameraBackendError):
    """Requested clip exceeds the configured duration or size ceiling."""


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


class CameraBackend(ABC):
    """One implementation per NVR/VMS product.

    Subclasses implement the abstract methods. The concrete helpers below
    (resolve_camera, require_capability, check_coverage) are shared logic
    every backend gets for free - do not reimplement them.
    """

    #: Short identifier for logs and error messages, e.g. "frigate"
    name: str = "unnamed"

    # -- discovery ---------------------------------------------------------

    @abstractmethod
    async def list_cameras(self) -> list[Camera]:
        """Every camera this backend can see, with capabilities populated."""

    @abstractmethod
    async def get_coverage(self, camera_id: str) -> Coverage:
        """The oldest and newest footage currently held for this camera."""

    # -- stills ------------------------------------------------------------

    @abstractmethod
    async def get_frame(self, camera_id: str, at: datetime | None = None) -> Frame:
        """One still image.

        at=None means live (current view). A timestamp means pull from
        recordings. Backends should snap to the nearest available frame
        rather than failing on an exact-match miss, and report the actual
        time in Frame.captured_at.
        """

    @abstractmethod
    async def get_frames(
        self,
        camera_id: str,
        start: datetime,
        end: datetime,
        max_frames: int = 6,
    ) -> list[Frame]:
        """Evenly spaced stills across a window.

        Capped low on purpose - each image costs roughly 1-1.5k tokens in
        the model's context. This is sampling, not watching.
        """

    # -- recordings --------------------------------------------------------

    @abstractmethod
    async def find_recordings(
        self, camera_id: str, start: datetime, end: datetime
    ) -> list[RecordingSegment]:
        """Continuous segments overlapping the window, oldest first.

        Gaps between returned segments are genuine gaps in the footage.
        """

    @abstractmethod
    async def export_clip(
        self,
        camera_id: str,
        start: datetime,
        end: datetime,
        dest_dir: str,
    ) -> ClipExport:
        """Cut a video file to disk and return its path.

        Implementations must enforce max_clip_seconds - raise
        ExportTooLargeError rather than filling the disk.
        """

    # -- events ------------------------------------------------------------

    @abstractmethod
    async def search_events(
        self,
        camera_id: str | None,
        start: datetime,
        end: datetime,
        labels: list[str] | None = None,
        limit: int = 50,
    ) -> list[Event]:
        """Detections in the window. camera_id=None searches all cameras."""

    # -- links -------------------------------------------------------------

    @abstractmethod
    def get_playback_url(self, camera_id: str, at: datetime) -> str:
        """Deep link into the vendor's own web UI at this camera and moment.

        Synchronous - it is string construction, no I/O.
        """

    # -- lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        """Release HTTP clients or sessions. Override if needed."""
        return None

    # -- shared helpers (do not override) ----------------------------------

    async def resolve_camera(self, query: str) -> Camera:
        """Turn 'front gate' into a Camera object.

        Exact id/name/alias match first. Falls back to substring matching so
        'gate' finds 'Front Gate'. Raises rather than guessing when the
        substring pass hits more than one camera.
        """
        cameras = await self.list_cameras()
        if not cameras:
            raise CameraNotFoundError(query)

        for cam in cameras:
            if cam.matches(query):
                return cam

        needle = query.strip().lower()
        partial = [
            cam
            for cam in cameras
            if needle in cam.name.lower()
            or needle in cam.id.lower()
            or any(needle in a.lower() for a in cam.aliases)
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise AmbiguousCameraError(query, [c.name for c in partial])

        raise CameraNotFoundError(query, available=[c.name for c in cameras])

    async def require_capability(self, camera_id: str, capability: Capability) -> Camera:
        """Fetch the camera and assert it can do the thing."""
        cam = await self.resolve_camera(camera_id)
        if not cam.supports(capability):
            raise CapabilityNotSupportedError(cam.id, capability)
        return cam

    async def check_coverage(self, camera_id: str, moment: datetime) -> Coverage:
        """Fail early and informatively if the moment is outside retention."""
        coverage = await self.get_coverage(camera_id)
        if not coverage.contains(moment):
            raise FootageUnavailableError(camera_id, moment, coverage)
        return coverage
