"""
CCTV MCP server - the tool layer.

Everything the model can do lives here. This layer:

  1. Parses timestamps against the camera's local timezone (timeutil)
  2. Calls the backend through the CameraBackend contract (backends/)
  3. Converts backend exceptions into messages the model can act on
  4. Encodes frames into MCP image content blocks
  5. Writes an audit line for every footage access

Nothing here knows whether Frigate, Hikvision or MockBackend is underneath.

Run:
    uv run python -m app.server                  # stdio, for Claude Desktop
    $env:MCP_TRANSPORT="http"; uv run python -m app.server   # http, for Inspector
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import Image, MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backends.base import CameraBackend, CameraBackendError
from app.backends.mock import MockBackend
from app.models import Capability
from app.timeutil import (
    TimeParseError,
    clamp_window,
    describe_duration,
    fmt,
    fmt_short,
    parse_moment,
    parse_window,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("cctv-mcp")

EXPORT_DIR = os.getenv("CCTV_EXPORT_DIR", "exports")
MAX_CLIP_SECONDS = int(os.getenv("CCTV_MAX_CLIP_SECONDS", "900"))
AUDIT_LOG = os.getenv("CCTV_AUDIT_LOG", "audit.jsonl")
ACTOR = os.getenv("MCP_ACTOR", "unknown")

# Frames are the expensive thing in this server - roughly 1-1.5k tokens each.
MAX_FRAMES = 8
MAX_TIMELAPSE_SECONDS = 6 * 3600


def build_backend() -> CameraBackend:
    """Choose the backend from config. One line per new NVR/VMS supported."""
    kind = os.getenv("CCTV_BACKEND", "mock").strip().lower()
    if kind == "mock":
        return MockBackend(max_clip_seconds=MAX_CLIP_SECONDS)
    # if kind == "frigate":
    #     return FrigateBackend(base_url=os.environ["FRIGATE_BASE_URL"], ...)
    raise ValueError(
        f"Unknown CCTV_BACKEND={kind!r}. Supported: mock. "
        "Set it in .env."
    )


backend = build_backend()
mcp = MCPServer("cctv")


# --------------------------------------------------------------------------
# Cross-cutting concerns
# --------------------------------------------------------------------------


def audit(action: str, **fields) -> None:
    """Append one JSON line per footage access.

    Surveillance access logging is usually a compliance requirement, not a
    nice-to-have. Keep this even when it feels like overhead - 'who looked at
    the reception camera last Tuesday' is a question that does get asked.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": ACTOR,
        "backend": backend.name,
        "action": action,
        **fields,
    }
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        log.warning("audit write failed", exc_info=True)


def handled(fn):
    """Turn exceptions into text the model can read and recover from.

    A raised exception reaches the model as an opaque error. A returned string
    reaches it as information - 'footage only goes back to 2 Sep' lets Claude
    tell the user something useful instead of just failing.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (CameraBackendError, TimeParseError, ValueError) as exc:
            log.info("%s: %s", type(exc).__name__, exc)
            return f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # pragma: no cover
            log.exception("unhandled error in %s", fn.__name__)
            return f"Unexpected error in {fn.__name__}: {exc}"

    return wrapper


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@mcp.tool()
@handled
async def list_cameras() -> str:
    """List every camera, with its aliases, retention and CURRENT local time.

    Call this first in any footage conversation. The current local time is
    what lets you resolve relative phrases like "yesterday at 3pm" or "an
    hour ago" into the absolute timestamps the other tools require.
    """
    cameras = await backend.list_cameras()
    if not cameras:
        return "No cameras configured on this backend."

    lines = []
    for cam in cameras:
        now_local = datetime.now(cam.local_tz)
        status = "online" if cam.is_online else "OFFLINE"
        caps = ", ".join(c.value for c in cam.capabilities) or "none"
        alias = f" (also: {', '.join(cam.aliases)})" if cam.aliases else ""
        lines.append(
            f"- {cam.name} [id={cam.id}]{alias}\n"
            f"    status: {status} | location: {cam.location or 'n/a'}\n"
            f"    timezone: {cam.timezone} | local time now: {now_local:%Y-%m-%d %H:%M:%S}\n"
            f"    retention: {cam.retention_days} days | supports: {caps}"
        )
    return f"{len(cameras)} camera(s):\n\n" + "\n".join(lines)


@mcp.tool()
@handled
async def get_camera_status(camera: str) -> str:
    """Check whether a camera is online and exactly what footage it holds.

    Use before searching a period that might be outside retention, so you can
    tell the user what IS available rather than returning nothing.

    Args:
        camera: Camera id, name or alias, e.g. 'front_gate' or 'loading dock'.
    """
    cam = await backend.resolve_camera(camera)
    coverage = await backend.get_coverage(cam.id)
    span = describe_duration((coverage.latest - coverage.earliest).total_seconds())
    return (
        f"{cam.name} [id={cam.id}]\n"
        f"  status: {'online' if cam.is_online else 'OFFLINE'}\n"
        f"  footage held: {fmt(coverage.earliest, cam.local_tz)}\n"
        f"           to: {fmt(coverage.latest, cam.local_tz)}\n"
        f"  that is roughly {span} of recording\n"
        f"  capabilities: {', '.join(c.value for c in cam.capabilities)}"
    )


# --------------------------------------------------------------------------
# Viewing
# --------------------------------------------------------------------------


@mcp.tool()
@handled
async def view_snapshot(camera: str, at: str = "now") -> list:
    """Fetch ONE still image so you can see and describe what was happening.

    This is the tool to use when the user asks a question about the CONTENT
    of footage - who was there, what was happening, what a vehicle looked
    like. The image comes back to you directly.

    Args:
        camera: Camera id, name or alias.
        at: ISO-8601 timestamp, or 'now' for live. A timestamp with no
            timezone offset is read as the camera's local time.
    """
    cam = await backend.resolve_camera(camera)
    moment = parse_moment(at, cam.local_tz)
    frame = await backend.get_frame(cam.id, None if at.strip().lower() == "now" else moment)

    audit("view_snapshot", camera=cam.id, at=frame.captured_at.isoformat())
    header = (
        f"{cam.name} - {'LIVE' if frame.is_live else 'RECORDED'}\n"
        f"Captured: {fmt(frame.captured_at, cam.local_tz)}"
    )
    if not frame.is_live and abs((frame.captured_at - moment).total_seconds()) > 1:
        header += f"\n(nearest available frame to the {fmt(moment, cam.local_tz)} requested)"
    return [header, Image(data=frame.image_bytes, format=frame.media_type.split("/")[-1])]


@mcp.tool()
@handled
async def view_timelapse(
    camera: str, start: str, end: str, max_frames: int = 6
) -> list:
    """Fetch several stills spread evenly across a time window.

    Use when the user asks what happened OVER a period rather than at one
    moment. Each frame is labelled with its local time so you can describe
    changes in sequence.

    This samples, it does not watch - a 4-second event between two frames
    will be missed. If the user needs certainty, export a clip instead.

    Args:
        camera: Camera id, name or alias.
        start: ISO-8601 start of the window.
        end: ISO-8601 end of the window.
        max_frames: How many stills, 1-8. Each one costs context, so keep
            it low unless the user needs fine detail.
    """
    cam = await backend.resolve_camera(camera)
    begins, finishes = parse_window(start, end, cam.local_tz)
    begins, finishes, trimmed = clamp_window(begins, finishes, MAX_TIMELAPSE_SECONDS)
    count = max(1, min(max_frames, MAX_FRAMES))

    frames = await backend.get_frames(cam.id, begins, finishes, max_frames=count)
    audit(
        "view_timelapse",
        camera=cam.id,
        start=begins.isoformat(),
        end=finishes.isoformat(),
        frames=len(frames),
    )

    out: list = [
        f"{cam.name} - {len(frames)} frames from {fmt(begins, cam.local_tz)} "
        f"to {fmt(finishes, cam.local_tz)}"
        + (
            f"\nNOTE: window was trimmed to the first "
            f"{describe_duration(MAX_TIMELAPSE_SECONDS)}; ask for a later "
            f"window to see the rest."
            if trimmed
            else ""
        )
    ]
    for frame in frames:
        out.append(f"--- {fmt_short(frame.captured_at, cam.local_tz)} local ---")
        out.append(Image(data=frame.image_bytes, format=frame.media_type.split("/")[-1]))
    return out


# --------------------------------------------------------------------------
# Searching
# --------------------------------------------------------------------------


@mcp.tool()
@handled
async def search_events(
    camera: str = "",
    start: str = "",
    end: str = "",
    labels: str = "",
    limit: int = 50,
) -> str:
    """Find detections (person, car, motion) in a time window.

    Far cheaper than pulling frames - use this FIRST to narrow down when
    something happened, then view_snapshot at the interesting timestamps.

    Args:
        camera: Camera id, name or alias. Leave empty to search all cameras.
        start: ISO-8601 start of the window.
        end: ISO-8601 end of the window.
        labels: Comma-separated filter, e.g. 'person,car'. Empty means all.
        limit: Maximum results.
    """
    if not start or not end:
        return "start and end are required. Call list_cameras first for the current local time."

    if camera.strip():
        cam = await backend.require_capability(camera, Capability.EVENT_SEARCH)
        tz, cam_id, scope = cam.local_tz, cam.id, cam.name
    else:
        cameras = await backend.list_cameras()
        if not cameras:
            return "No cameras configured."
        tz, cam_id, scope = cameras[0].local_tz, None, "all cameras"

    begins, finishes = parse_window(start, end, tz)
    wanted = [l.strip() for l in labels.split(",") if l.strip()] or None

    events = await backend.search_events(cam_id, begins, finishes, wanted, limit)
    audit("search_events", camera=cam_id or "*", start=begins.isoformat(), end=finishes.isoformat())

    window = f"{fmt(begins, tz)} to {fmt(finishes, tz)}"
    if not events:
        filt = f" matching {', '.join(wanted)}" if wanted else ""
        return (
            f"No detections{filt} on {scope} between {window}.\n"
            f"The camera was recording - there was simply nothing detected. "
            f"Use check_recording_gaps to confirm footage exists for this period."
        )

    lines = [f"{len(events)} detection(s) on {scope}, {window}:\n"]
    for ev in events:
        dur = describe_duration((ev.end - ev.start).total_seconds()) if ev.end else "?"
        score = f" {ev.score:.0%}" if ev.score is not None else ""
        lines.append(
            f"  {fmt_short(ev.start, tz)}  {ev.label:<12} {dur:>7}{score}  "
            f"[{ev.camera_id}]"
        )
    lines.append("\nUse view_snapshot at any of these times to see what happened.")
    return "\n".join(lines)


@mcp.tool()
@handled
async def check_recording_gaps(camera: str, start: str, end: str) -> str:
    """Check whether footage is actually continuous across a window.

    Cameras drop offline, disks fill, and motion-only recording leaves holes.
    Call this before telling a user that nothing happened - the difference
    between 'nothing was detected' and 'the camera was not recording' matters.

    Args:
        camera: Camera id, name or alias.
        start: ISO-8601 start of the window.
        end: ISO-8601 end of the window.
    """
    cam = await backend.resolve_camera(camera)
    begins, finishes = parse_window(start, end, cam.local_tz)
    segments = await backend.find_recordings(cam.id, begins, finishes)

    if not segments:
        return f"No footage at all for {cam.name} between {fmt(begins, cam.local_tz)} and {fmt(finishes, cam.local_tz)}."

    recorded = sum(s.duration_seconds for s in segments)
    requested = (finishes - begins).total_seconds()
    lines = [
        f"{cam.name}: {len(segments)} segment(s), "
        f"{describe_duration(recorded)} recorded of {describe_duration(requested)} requested "
        f"({recorded / requested:.0%} coverage)\n"
    ]
    for seg in segments:
        lines.append(
            f"  {fmt_short(seg.start, cam.local_tz)} - {fmt_short(seg.end, cam.local_tz)}"
            f"  ({describe_duration(seg.duration_seconds)})"
        )
    for prev, nxt in zip(segments, segments[1:]):
        gap = (nxt.start - prev.end).total_seconds()
        lines.append(
            f"  GAP: no footage {fmt_short(prev.end, cam.local_tz)} - "
            f"{fmt_short(nxt.start, cam.local_tz)} ({describe_duration(gap)})"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


@mcp.tool()
@handled
async def export_clip(camera: str, start: str, end: str) -> str:
    """Save a real video file the user can open, keep, or send on.

    Use for evidence, incident reports, or anything leaving the building.
    You cannot see the contents of the exported file - if the user wants to
    know what is IN the footage, use view_snapshot or view_timelapse instead.

    Args:
        camera: Camera id, name or alias.
        start: ISO-8601 start of the clip.
        end: ISO-8601 end of the clip.
    """
    cam = await backend.resolve_camera(camera)
    begins, finishes = parse_window(start, end, cam.local_tz)

    clip = await backend.export_clip(cam.id, begins, finishes, EXPORT_DIR)
    audit("export_clip", camera=cam.id, start=begins.isoformat(), end=finishes.isoformat(), path=clip.file_path)

    return (
        f"Exported {describe_duration(clip.duration_seconds)} from {cam.name}.\n"
        f"  period: {fmt(clip.start, cam.local_tz)} to {fmt(clip.end, cam.local_tz)}\n"
        f"  file:   {clip.file_path}\n"
        f"  size:   {clip.size_bytes / 1_048_576:.1f} MB"
    )


@mcp.tool()
@handled
async def get_playback_link(camera: str, at: str) -> str:
    """Build a link that opens the NVR's own player at this camera and moment.

    The cheapest option and often the most useful - the user scrubs the real
    timeline themselves. Offer this alongside snapshots.

    Args:
        camera: Camera id, name or alias.
        at: ISO-8601 timestamp.
    """
    cam = await backend.resolve_camera(camera)
    moment = parse_moment(at, cam.local_tz)
    url = backend.get_playback_url(cam.id, moment)
    audit("get_playback_link", camera=cam.id, at=moment.isoformat())
    return f"{cam.name} at {fmt(moment, cam.local_tz)}:\n{url}"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    log.info("starting cctv mcp | backend=%s transport=%s actor=%s", backend.name, transport, ACTOR)
    if transport == "http":
        mcp.run(transport="streamable-http", port=int(os.getenv("MCP_PORT", "3002")))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
