"""
Shared data models for the CCTV MCP server.

Every backend (Frigate, Hikvision, Milestone, ...) converts its own native
responses into these types. The MCP tool layer only ever sees these, which is
what makes backends swappable.

Rule that matters most here: every datetime in this module is timezone-aware
and stored in UTC. A camera's local timezone is used only for parsing user
input and formatting output, never for storage or comparison.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


def _require_utc(value: datetime) -> datetime:
    """Reject naive datetimes, normalise everything else to UTC."""
    if value.tzinfo is None:
        raise ValueError(
            "naive datetime rejected - attach a timezone before constructing "
            "the model (use Camera.local_tz or datetime.timezone.utc)"
        )
    return value.astimezone(timezone.utc)


class Capability(str, Enum):
    """What a given camera can actually do through its backend.

    Not every backend supports every operation. Frigate has object-labelled
    event search; a bare Hikvision NVR usually only has motion events. Tools
    check capabilities and degrade gracefully instead of throwing 500s.
    """

    LIVE_SNAPSHOT = "live_snapshot"
    HISTORICAL_SNAPSHOT = "historical_snapshot"
    CLIP_EXPORT = "clip_export"
    EVENT_SEARCH = "event_search"
    PLAYBACK_URL = "playback_url"


class Camera(BaseModel):
    """One camera, as the model should understand it."""

    id: str = Field(description="Backend-native identifier, e.g. 'front_gate' or '4'")
    name: str = Field(description="Human label, e.g. 'Front Gate'")
    aliases: list[str] = Field(
        default_factory=list,
        description="Other names people use for this camera: 'main entrance', "
        "'gate cam'. Used to resolve natural language to an id.",
    )
    location: str | None = Field(
        default=None, description="Site or building, e.g. 'Andheri Warehouse'"
    )
    timezone: str = Field(
        description="IANA timezone the camera physically sits in, e.g. 'Asia/Kolkata'"
    )
    retention_days: int = Field(
        description="How far back footage is kept. Used to reject impossible requests."
    )
    capabilities: list[Capability] = Field(default_factory=list)
    is_online: bool = True

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @property
    def local_tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def matches(self, query: str) -> bool:
        """Loose match against id, name, or any alias. Case/space insensitive."""
        needle = query.strip().lower()
        candidates = [self.id, self.name, *self.aliases]
        return any(needle == c.strip().lower() for c in candidates)


class Coverage(BaseModel):
    """The window of footage a camera actually holds right now.

    Queried before any search so tools can say "I only have footage back to
    14 Aug" instead of silently returning nothing.
    """

    camera_id: str
    earliest: datetime
    latest: datetime

    _norm = field_validator("earliest", "latest")(_require_utc)

    def contains(self, moment: datetime) -> bool:
        return self.earliest <= moment.astimezone(timezone.utc) <= self.latest


class Frame(BaseModel):
    """A single still image pulled from live or recorded video.

    image_bytes is raw encoded image data. The MCP layer base64-encodes it
    into an image content block; backends never deal with base64.
    """

    camera_id: str
    captured_at: datetime
    image_bytes: bytes
    media_type: str = "image/jpeg"
    width: int | None = None
    height: int | None = None
    is_live: bool = False

    _norm = field_validator("captured_at")(_require_utc)


class RecordingSegment(BaseModel):
    """A continuous stretch of recorded footage.

    Gaps between segments are real - cameras drop offline, disks fill,
    motion-only recording leaves holes. Tools surface gaps rather than
    pretending the timeline is continuous.
    """

    camera_id: str
    start: datetime
    end: datetime

    _norm = field_validator("start", "end")(_require_utc)

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


class ClipExport(BaseModel):
    """Result of cutting a video file to disk."""

    camera_id: str
    start: datetime
    end: datetime
    file_path: str
    size_bytes: int
    duration_seconds: float

    _norm = field_validator("start", "end")(_require_utc)


class Event(BaseModel):
    """Something the NVR/VMS flagged: motion, a person, a vehicle.

    'label' is deliberately a free string, not an enum - Frigate emits COCO
    labels, Hikvision emits its own vocabulary, and forcing them into a shared
    enum loses information. Normalise at the presentation layer if needed.
    """

    id: str
    camera_id: str
    label: str
    start: datetime
    end: datetime | None = None
    score: float | None = Field(default=None, description="Detector confidence, 0-1")
    thumbnail_url: str | None = None

    _norm = field_validator("start")(_require_utc)
