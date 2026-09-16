# Design notes

Why this project is built the way it is. README covers *what* and *how*; this
covers *why*, so the reasoning survives longer than the memory of making it.

> Written while only the mock backend exists. Anything marked **untested against
> real hardware** may need revisiting once a real NVR is wired in.

---

## The constraint everything follows from

Claude cannot watch video. MCP tool results carry text and images; there is no
video content type and no streaming.

This is not a limitation to work around — it decides the shape of the whole
server. A design that assumes "show me the camera" means playing footage
produces a tool surface that cannot work. So the three output modes (stills,
clip files, playback links) are not features layered on top; they are the only
three things the server can ever do, and every tool is one of them.

The practical consequence is that **tools must be honest about which mode they
are**. `export_clip` says in its own docstring that Claude cannot see the
exported file. Without that, Claude would export a clip and then confidently
describe contents it has no access to.

---

## Time is the hard problem

Not the video. Not the vendor APIs. Time.

Three parties disagree about what "3pm" means: the user (IST), the NVR (often
UTC internally), and Python (naive datetimes, which silently behave as UTC in
comparisons). Nothing errors when they disagree. The footage just comes back
from the wrong moment, and the natural conclusion is that the camera clock
drifted.

### Everything is UTC, enforced at construction

`models._require_utc` rejects naive datetimes outright rather than assuming a
timezone:

```python
if value.tzinfo is None:
    raise ValueError("naive datetime rejected - attach a timezone before ...")
```

Rejecting is deliberately harsher than defaulting. A default would be correct
most of the time and silently wrong the rest, which is the worst possible
failure profile for this class of bug.

### Camera-local is the parsing default

A timestamp with no offset is read in the **camera's** timezone, not the
server's:

```python
if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=tz)   # tz comes from the Camera
```

Someone standing in front of the loading bay saying "3pm" means 3pm where that
camera is. This matters for multi-site deployments where cameras sit in
different zones from the server.

### Tools reject natural language

`parse_moment("yesterday 3pm")` raises. The tools accept only ISO-8601.

This looks unhelpful until you see what makes it work: `list_cameras` returns
each camera's **current local time**. Claude reads that, does the date
arithmetic itself, and passes an absolute timestamp.

The alternative — a natural-language date parser — is a genuine project once you
account for "last Tuesday evening", "the night before the break-in", and
relative phrases across DST boundaries. LLMs already do this well. Better to
give the model the anchor it needs than to reimplement it badly.

### Every displayed timestamp is tagged with its zone

`timeutil.fmt` always appends `%Z`. An untagged time is exactly how a timezone
bug stays invisible for weeks.

### The mock burns timestamps into the frames

`MockBackend._render` draws both the local and UTC time onto every generated
image. This is observability, not decoration. When Claude reads back
`LOCAL 2026-09-15 15:00:00 Asia/Kolkata`, that is end-to-end proof that the
whole round trip — model → ISO string → parse → backend → frame → back — landed
on the right instant.

**This caught a real bug during development.** `find_recordings` placed its
injected footage gap using `start.replace(hour=...)` on a UTC datetime. For any
window starting at IST midnight (18:30 UTC the previous day) the gap landed
outside the window and the function quietly returned one unbroken segment. The
fix offsets from the window start instead. Nothing errored; the output was just
wrong in a plausible-looking way.

---

## Why an abstract backend

There are three realistic families of camera system — Frigate, vendor NVRs
(Hikvision/Dahua/CP Plus), and full VMS platforms (Milestone, Genetec, Network
Optix) — and they agree on almost nothing. Different auth, different timestamp
formats, different event vocabularies, different retention semantics.

The tool layer must not care. Docstrings are prompt engineering; rewriting them
per vendor would mean re-testing model behaviour for every integration.

### Enforcement is at instantiation, not call time

`@abstractmethod` means Python refuses to construct a subclass with methods
missing:

```
TypeError: Can't instantiate abstract class HikvisionBackend with abstract
methods export_clip, find_recordings, get_coverage, ...
```

A half-finished backend fails at startup with a list of what is missing, rather
than six months later when someone first calls `export_clip`.

### Shared logic lives in the base class

`resolve_camera`, `require_capability` and `check_coverage` are concrete. Every
backend inherits them. `resolve_camera` calls the abstract `list_cameras` and
layers alias matching on top — the template method pattern.

This is the difference between natural-language camera resolution working
identically everywhere, and each adapter author reinventing it slightly
differently.

### Normalisation targets, not wrappers

`models.py` types are what every backend converts *into*. Frigate returns epoch
floats, Hikvision returns `2026-09-14T15:00:00+05:30` inside XML, Milestone
returns its own envelope. All three become the same `Camera` and `Frame`.

Two deliberate choices inside that:

- **`Frame.image_bytes` is raw bytes, not base64.** Backends produce images; the
  MCP layer handles transport encoding. Base64 inside a backend would mean
  encoding for a protocol the backend should not know exists.
- **`Event.label` is `str`, not an enum.** Frigate emits COCO labels, Hikvision
  emits its own vocabulary. A shared enum would force either dropped labels or a
  lossy mapping. Normalise at presentation time if ever needed.

---

## Capabilities over assumption

Backends differ in what they can do. Frigate has object-labelled event search; a
bare Hikvision NVR has motion events at best.

Two ways to handle that: call the method and catch the failure, or declare
support up front. This project declares:

```python
capabilities=[Capability.LIVE_SNAPSHOT, Capability.CLIP_EXPORT, ...]
```

Declaring wins because the failure is clean and *explainable*.
`CapabilityNotSupportedError: Camera 'stockroom' does not support event_search`
lets Claude tell the user which camera cannot do what and offer an alternative.
A caught 404 from a vendor API tells it nothing useful.

The fixture data is deliberately uneven for the same reason — one camera with
shorter retention, one without event search, one offline. Uniform fixtures only
ever exercise the happy path, and the degradation bugs then surface on a client
site.

---

## Errors are returned, not raised

The `@handled` decorator converts backend exceptions into text:

```python
except (CameraBackendError, TimeParseError, ValueError) as exc:
    return f"{type(exc).__name__}: {exc}"
```

A raised exception reaches Claude as an opaque failure. A returned string
reaches it as **information it can act on**.

### Exceptions carry recovery data

This is the substance of the decision. Compare:

```python
return []                          # user concludes nothing was recorded
raise FootageUnavailableError(...)  # "footage only goes back to 2 Sep"
```

- `CameraNotFoundError` carries the list of cameras that *do* exist
- `AmbiguousCameraError` carries the matches, so Claude can ask which one
- `FootageUnavailableError` carries the actual coverage window

The stakes are higher here than in ordinary CRUD. A user who gets an empty
result from a CCTV query concludes the incident was not captured, closes the
laptop, and loses the footage to retention. An empty list is an actively
dangerous answer.

Same reasoning drives `search_events` explaining *why* it found nothing and
pointing at `check_recording_gaps`. "No detections" and "the camera was not
recording" are completely different facts and must never look the same.

### `functools.wraps` is load-bearing

Without it the decorator erases the signature and every tool registers with zero
parameters. Easy to lose in a refactor; worth a test.

---

## Gaps are first-class

`find_recordings` returns a **list** of segments rather than a single range.

If footage were always continuous a start and end would do. Returning a list
forces every caller to confront that cameras drop offline, disks fill, and
motion-only recording leaves holes. The timeline the user imagines is unbroken;
the timeline on disk usually is not.

`Coverage` is separate from `Camera` for a related reason. `retention_days` is
configuration — what the system is *supposed* to keep. Coverage is what is
actually on disk right now, which shrinks when a disk fills or a camera was
offline for a week.

---

## Cost is a design axis

Frames cost roughly 1–1.5k tokens each. Nothing else in the server costs
meaningfully anything.

That asymmetry shapes the tool surface:

- Hard ceilings: 8 frames per call, 6-hour timelapse window, 900s clip export
- `clamp_window` trims and **says so** rather than failing or silently truncating
- Docstrings route Claude toward cheap tools first — `search_events` ends by
  suggesting `view_snapshot` at specific timestamps

The failure mode being prevented is Claude pulling frames blindly across a wide
window: expensive, and still likely to miss a four-second event between samples.
`view_timelapse` says explicitly in its docstring that it samples rather than
watches.

---

## Honesty about what is real

`playback_urls_resolve` exists because of a bug found by clicking a link.

`MockBackend.get_playback_url` returned `http://mock-nvr.local:5000/...` — a
well-formed URL with a correct epoch pointing at a hostname that does not exist.
Claude presented it as a working link.

The flag makes the backend declare whether its links resolve, and the tool layer
appends a warning when they do not. It stays useful with real hardware: a
Hikvision NVR at `192.168.1.64` is reachable from the office and nowhere else,
so it should be read from config rather than hardcoded `True`.

This is the same failure class as the empty-result problem and the timezone gap
bug: **output that looks like an answer and is not**. Nothing errors, nothing
logs, and the user acts on it. Those are the bugs worth hunting in this project,
because the domain makes them expensive — people make decisions about incidents
based on what this server tells them.

---

## Determinism in the mock

Events and gaps are seeded from a hash of camera id plus date:

```python
digest = hashlib.sha256(f"{camera_id}:{day}".encode()).hexdigest()
return random.Random(int(digest[:16], 16))
```

A fresh `Random` instance, not the global one — otherwise anything else in the
process calling `random.seed()` breaks reproducibility.

This makes the mock more than a development convenience. Because the same query
returns the same results every run, tests written against it are not *mock
tests* — they are a **contract conformance suite**. "Does `get_frame` return
UTC?", "does an out-of-retention request raise `FootageUnavailableError`?", "does
`find_recordings` surface gaps?" hold for every backend.

Point the same test file at `FrigateBackend` via a pytest fixture and every
future integration gets validated for free. That is the real payoff of the
abstraction, and the reason to write the suite before the first real backend
rather than after.

---

## Deliberate omissions

- **No resources, only tools.** Claude Desktop resources are user-attached; the
  model cannot fetch them autonomously. Anything Claude needs on its own must be
  a tool.
- **No write operations.** No PTZ, no deletion, no config changes. Read-only
  until there is a concrete reason otherwise, and then only with per-camera
  permission checks.
- **No natural-language date parsing.** Covered above.
- **No caching.** Premature until real backend latency is measured.
- **No database.** The NVR is the source of truth. The only local state is the
  audit log, which is append-only JSONL on purpose — it should be hard to
  rewrite.

---

## Open questions

Things genuinely undecided, to revisit with real hardware:

- **Offline camera semantics.** The mock returns frames for an offline camera. A
  real backend cannot. Does `get_frame` raise, or return the last known frame
  with a staleness warning?
- **Frame sampling strategy.** Even spacing is naive. Sampling around detected
  events would be far more useful, but couples `view_timelapse` to event search,
  which not every backend has.
- **Multi-camera correlation.** "Track this person across cameras" is the
  obvious next request and needs a design, not just another tool.
- **Where timezone lives.** Currently per-camera in the backend. For vendors
  that do not expose it, it has to come from local config — which means a
  camera-registry file the backend reads, not invents.
- **Export cleanup.** Clips accumulate in `exports/` with nothing pruning them.
