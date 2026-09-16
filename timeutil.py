"""
Turning what a user says about time into an unambiguous instant.

This module exists because the question "what does 3pm mean?" has three
different answers depending on who is asking - the user (IST), the NVR (often
UTC), and Python (naive, which silently means UTC in comparisons).

The rule: a timestamp with no offset is interpreted in the CAMERA's local
timezone, because that is what the person standing in front of the camera
means. Everything is returned as UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class TimeParseError(ValueError):
    """Raised when a timestamp string cannot be understood.

    The message is written for the model to read and retry, so it states the
    expected format rather than just reporting failure.
    """


_EXPECTED = (
    "Expected ISO-8601, e.g. '2026-09-14T15:00:00' (interpreted in the "
    "camera's local timezone) or '2026-09-14T15:00:00+05:30' (explicit "
    "offset). The word 'now' is also accepted."
)


def parse_moment(value: str, tz: ZoneInfo) -> datetime:
    """Parse a user/model supplied timestamp into an aware UTC datetime.

    Accepts:
      'now'                        -> current instant
      '2026-09-14T15:00:00'        -> 15:00 in the camera's timezone
      '2026-09-14T15:00:00+05:30'  -> explicit offset, honoured as given
      '2026-09-14T09:30:00Z'       -> UTC
      '2026-09-14 15:00'           -> space separator and partial time are fine
    """
    if not isinstance(value, str) or not value.strip():
        raise TimeParseError(f"Empty timestamp. {_EXPECTED}")

    raw = value.strip()
    if raw.lower() == "now":
        return datetime.now(timezone.utc)

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeParseError(f"Could not parse {value!r}. {_EXPECTED}") from exc

    # No offset supplied - the user meant local time at the camera.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)

    return parsed.astimezone(timezone.utc)


def parse_window(start: str, end: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Parse a start/end pair, rejecting inverted or empty ranges."""
    begins = parse_moment(start, tz)
    finishes = parse_moment(end, tz)
    if finishes <= begins:
        raise TimeParseError(
            f"End ({finishes.isoformat()}) must be after start "
            f"({begins.isoformat()}). Check whether the two values were swapped."
        )
    return begins, finishes


def to_local(moment: datetime, tz: ZoneInfo) -> datetime:
    return moment.astimezone(tz)


def fmt(moment: datetime, tz: ZoneInfo) -> str:
    """Human-readable local time, always tagged with the zone.

    Every timestamp shown to the user goes through this. Tagging the zone is
    not decoration - an untagged time is how a timezone bug stays invisible.
    """
    local = moment.astimezone(tz)
    return f"{local:%d %b %Y, %I:%M:%S %p} {local:%Z}"


def fmt_short(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return f"{local:%H:%M:%S}"


def describe_duration(seconds: float) -> str:
    """'2h 14m', '45s' - for clip lengths and footage gaps."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def clamp_window(
    start: datetime, end: datetime, max_seconds: int
) -> tuple[datetime, datetime, bool]:
    """Trim a window to a ceiling, reporting whether it was trimmed.

    Used so a request for a whole day of frames returns the first hour with a
    clear note, rather than either failing or producing an enormous response.
    """
    if (end - start).total_seconds() <= max_seconds:
        return start, end, False
    return start, start + timedelta(seconds=max_seconds), True
