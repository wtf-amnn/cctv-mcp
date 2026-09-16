# cctv-mcp

An MCP server that lets Claude query CCTV footage by camera, date and time.

Ask *"was anyone at the loading bay around 9pm yesterday?"* and Claude resolves
which camera you mean, works out the absolute timestamp in that camera's local
timezone, searches for detections, and shows you frames it can actually describe.

> **Status: in development.** Only the `mock` backend exists. Every tool works
> end to end against synthetic cameras, but nothing has been tested against real
> hardware yet. The project is not complete until a real NVR backend is wired in
> and the acceptance scenarios in [TESTING.md](TESTING.md) pass against it.

---

## Read this first: Claude cannot watch video

This is the single most important thing to understand about the project, and the
constraint that shaped every design decision in it.

MCP tool results carry text and images. Not video streams. So *"show me camera 3
at 2pm"* can never mean playing footage in the chat. The server answers in three
other ways instead:

| Mode | Claude sees it? | Good for |
|---|---|---|
| **Stills** — frames extracted at a timestamp | **Yes** | Questions about what *happened*. Claude describes, counts, compares. |
| **Clip export** — a real video file on disk | No | Evidence, incident reports, anything leaving the building. |
| **Playback link** — deep link into the NVR's own player | No | The default. User scrubs the real timeline themselves. |

A well-formed answer usually combines all three: a few frames so Claude can tell
you what it sees, a link so you can look yourself, and an offer to export a clip
if it turns out to matter.

---

## Quick start

```powershell
git clone <repo> cctv-mcp
cd cctv-mcp

uv venv
uv pip install mcp pydantic pillow python-dotenv httpx tzdata

Copy-Item .env.example .env    # defaults work as-is for the mock backend
uv run python -m app.server    # should log: starting cctv mcp | backend=mock
```

Then add it to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cctv": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\cctv-mcp", "run", "python", "-m", "app.server"],
      "env": {
        "CCTV_BACKEND": "mock",
        "MCP_ACTOR": "your-name",
        "CCTV_EXPORT_DIR": "C:\\path\\to\\cctv-mcp\\exports"
      }
    }
  }
}
```

Restart Claude Desktop and try:

> *"What cameras do I have, and show me the front gate yesterday at 3pm."*

Mock frames have their timestamp burned into the image, so Claude reads back
`LOCAL 2026-09-15 15:00:00 Asia/Kolkata`. That is your end-to-end proof that the
timezone round-trip is correct — see [DESIGN.md](DESIGN.md) for why that matters
more than it sounds.

---

## Architecture

```
MCP tools  (app/server.py)          stable interface, knows no vendor
    |
CameraBackend  (app/backends/base.py)   abstract contract, 8 methods
    |
MockBackend | FrigateBackend | HikvisionBackend | ...
```

Tools call the contract; the contract has one implementation per NVR or VMS
product. Adding support for a new system means writing one subclass. No tool
signature changes, no docstrings rewritten, no retesting of the tool layer.

```
cctv-mcp/
├── app/
│   ├── models.py            Pydantic types shared by every layer
│   ├── timeutil.py          timestamp parsing and formatting
│   ├── server.py            MCP tools, error translation, audit log
│   └── backends/
│       ├── base.py          CameraBackend ABC + exception hierarchy
│       └── mock.py          synthetic cameras for development
├── exports/                 clip output
├── audit.jsonl              one line per footage access
├── .env                     credentials, gitignored
├── README.md
├── DESIGN.md                why it is built this way
└── TESTING.md               acceptance scenarios
```

---

## Tools

Eight tools. They differ enormously in cost, and the docstrings deliberately
push Claude toward the cheap ones first.

| Tool | Cost | Purpose |
|---|---|---|
| `list_cameras` | free | Cameras, aliases, retention, **current local time** |
| `get_camera_status` | free | Online state and exact footage held |
| `check_recording_gaps` | free | Whether footage is continuous across a window |
| `get_playback_link` | free | Deep link into the NVR's own player |
| `search_events` | cheap | Detections (person, car, motion) in a window |
| `export_clip` | moderate | Write a real video file to disk |
| `view_snapshot` | ~1.5k tokens | One frame Claude can see and describe |
| `view_timelapse` | ~1.5k × N | Several frames across a window |

**The intended flow is search, then look.** `search_events` narrows down *when*
something happened for almost nothing; `view_snapshot` then spends context on
*what* it was. Pulling frames blindly across a window burns context and misses
things between samples.

`list_cameras` returns each camera's current local time. That is what lets Claude
resolve *"yesterday at 3pm"* into an absolute timestamp — the tools themselves
only accept ISO-8601, never natural language.

### Time arguments

Every timestamp argument accepts:

| Input | Meaning |
|---|---|
| `2026-09-14T15:00:00` | 15:00 **in the camera's local timezone** |
| `2026-09-14T15:00:00+05:30` | explicit offset, honoured as given |
| `2026-09-14T09:30:00Z` | UTC |
| `now` | current instant (`view_snapshot` only) |

A timestamp with no offset means camera-local, because that is what someone
standing in front of the camera means when they say "3pm".

---

## Configuration

All via `.env` or the `env` block in `claude_desktop_config.json`.

| Variable | Default | Purpose |
|---|---|---|
| `CCTV_BACKEND` | `mock` | Which backend to load |
| `CCTV_EXPORT_DIR` | `exports` | Where clips are written |
| `CCTV_MAX_CLIP_SECONDS` | `900` | Ceiling on export length |
| `CCTV_AUDIT_LOG` | `audit.jsonl` | Access log path |
| `MCP_ACTOR` | `unknown` | Identity recorded in the audit log |
| `MCP_TRANSPORT` | `stdio` | `stdio` for Desktop, `http` for Inspector |
| `MCP_PORT` | `3002` | Port when transport is `http` |
| `FRIGATE_BASE_URL` | — | Reserved, not yet used |
| `NVR_HOST` / `NVR_USER` / `NVR_PASSWORD` | — | Reserved, not yet used |

Hard limits are compiled into `server.py`, not configurable: 8 frames per call,
6-hour timelapse window, both to bound context cost.

---

## Adding a backend

1. Subclass `CameraBackend` in `app/backends/yourvendor.py`
2. Implement the eight abstract methods. Python refuses to instantiate the class
   until you have, and names the ones you missed.
3. Set `name`, and `playback_urls_resolve = False` if the NVR's web UI is not
   reachable from where users sit.
4. Add one line to `build_backend()` in `server.py`.
5. Run the scenarios in [TESTING.md](TESTING.md) against it. Any behaviour that
   differs from the mock is a bug in your adapter, not in the tools.

Notes on likely targets:

- **Frigate** — cleanest path. REST API, recordings by epoch range, snapshots at
  arbitrary timestamps, object-labelled events out of the box.
- **Hikvision / Dahua / CP Plus** — what is actually installed on most sites in
  India. ISAPI is XML and semi-documented; expect to watch the web UI's network
  requests to work out some endpoints.
- **ONVIF Profile G** — vendor-neutral in theory. Implementations are
  inconsistent and RTSP replay with `Range: clock=` headers is painful. Only
  worth it for genuinely mixed hardware.

Frame extraction from recorded segments is ffmpeg in every case:

```bash
ffmpeg -ss <offset> -i segment.mp4 -frames:v 1 -q:v 2 frame.jpg
```

`-ss` before `-i` for fast seeking.

---

## Security

- **Run on the LAN.** Cameras should never be exposed to the internet. Claude
  Desktop launches this server locally over stdio, so nothing needs to be.
- **Use a view-only NVR account.** Never admin credentials.
- **Credentials live in `.env`**, which is gitignored. `.env.example` is not.
- **Every footage access is logged** to `audit.jsonl` with the `MCP_ACTOR`
  identity. For surveillance this is usually a compliance requirement rather
  than a nicety — *who viewed reception last Tuesday* is a question that gets
  asked, and India's DPDP Act creates obligations around footage handling.
- **The server is read-only.** No PTZ, no deletion, no config changes. Keep it
  that way until there is a concrete reason not to.

---

## Gotchas

**`tzdata` is required on Windows.** Windows has no IANA timezone database, so
`zoneinfo` finds nothing and every timezone name looks invalid:

```
ValidationError: unknown IANA timezone: 'Asia/Kolkata'
```

Fix: `uv pip install tzdata`. Pure-Python, no code changes.

**Claude Desktop caches the server process.** Editing a file changes nothing
until you fully restart Desktop.

**Errors come back as tool results, not crashes.** The `@handled` decorator
converts exceptions into readable text so Claude can recover. This also means
genuine bugs look like tool responses. Full tracebacks go to stderr:

```powershell
Get-Content "$env:APPDATA\Claude\logs\mcp-server-cctv.log" -Tail 40
```

**MCP resources are user-attached only** — the model cannot fetch them
autonomously. Anything Claude needs on its own must be a tool. That is why this
server exposes eight tools and zero resources.

**Claude Desktop cannot reach a localhost HTTP MCP server.** Custom connectors
connect from Anthropic's infrastructure. Use stdio for Desktop;
`MCP_TRANSPORT=http` exists for the Inspector, which needs CORS with
`expose_headers=["Mcp-Session-Id"]`.

---

## Known gaps

Things found but not yet fixed:

- **Offline cameras still return frames.** `MockBackend.get_frame` ignores
  `is_online`, so asking for the Parking camera silently produces a synthetic
  image. A real backend cannot do this, which makes it a mock-fidelity problem
  and a reminder that tools should check status before assuming a live frame.
- **No pytest suite yet.** The scenarios in TESTING.md are run by hand.
- **`search_events` label filtering is post-hoc** in the mock, so filtering can
  return zero results where an unfiltered search returned several. Harmless, but
  not how a real detector behaves.
- **No rate limiting.** A loop of `view_timelapse` calls could hammer an NVR.

---

## Roadmap

- [x] Data models and backend contract
- [x] Mock backend with deterministic fixtures
- [x] Eight MCP tools, error translation, audit log
- [x] Manual acceptance pass against the mock
- [ ] pytest conformance suite (runs against *any* backend)
- [ ] First real backend
- [ ] Re-run acceptance scenarios against real hardware
- [ ] Package as a plugin with an incident-review skill
