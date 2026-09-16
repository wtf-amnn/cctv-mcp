# CCTV MCP – Testing Session

**Date:** 16 Sep 2026
**Server:** `cctv-mcp` (mock backend)
**Site:** Andheri Warehouse
**Timezone:** Asia/Kolkata (IST, UTC+5:30)

---

## Summary of findings

The time handling, retention checks, alias resolution and error handling all worked. The mock backend had several consistency bugs that should be fixed before connecting a real NVR.

### What worked

- Relative times ("yesterday at 3pm", "right now") resolved correctly to camera local time, with correct IST/UTC conversion.
- Aliases resolved correctly ("gate" → Front Gate, "loading dock" → Loading Bay).
- Retention limits were reported accurately (Loading Bay 14 days; the others 30 days).
- An unknown camera ("kitchen") was handled correctly: it's not in the camera list.
- The export length limit was enforced with a clear error (`ExportTooLargeError`, 900s / 15 min ceiling).
- Playback URLs carried correct Unix timestamps (e.g. `1789464600` = 15 Sep 15:00 IST; `1789486200` = 15 Sep 21:00 IST).
- The timelapse tool trimmed long windows to 6h and said so.
- The mock backend labelled its own placeholder links as not live.

### Bugs / inconsistencies in the mock backend

| # | Issue | Evidence |
|---|---|---|
| 1 | **Detections don't match frames** | Loading Bay car/motorcycle/bicycle events showed only person shapes or empty scenes; Front Gate 1:27 PM "person" frame was empty; 3:37 PM "car" frame was empty |
| 2 | **Detection inside a recording gap** | Front Gate truck at 18:52:23, but gap check reports no footage 18:51:14–19:03:14 |
| 3 | **Events change between queries** | Loading Bay, 6–10 PM search: car 18:32, motorcycle 19:19, bicycle 21:34. 4 PM–midnight search: car 17:05, motorcycle 18:38, bicycle 23:09. Same events, different times |
| 4 | **Event patterns recycled** | Loading Bay car (41s, 65%), motorcycle (6s, 77%), bicycle (30s, 78%) repeat in every window |
| 5 | **Frames identical across cameras** | Stockroom 00:00–06:00 frames matched Front Gate frames exactly: the image depends on timestamp only, not camera |
| 6 | **Frames returned inside a recording gap** | Loading Bay frames at 18:20 and 18:30 labelled RECORDED during gap 18:19:38–18:31:38 |
| 7 | **Offline camera returns live snapshot** | Parking is OFFLINE but returned a LIVE frame |
| 8 | **Offline camera reports footage up to now** | Parking status: footage held 17 Aug → 16 Sep 20:51 (current time) while offline |
| 9 | **Empty export file** | 5-min Front Gate export reported size 0.0 MB |
| 10 | **Implausible detections by location** | Trucks, bicycles and a motorcycle detected at Reception (indoor lobby) |

### Recommended fixes before going live

1. Generate events and frames from the same underlying data so they agree.
2. Make events stable (fixed IDs and times) regardless of query window.
3. Refuse snapshots and events inside recording gaps, or return a "no footage" error.
4. When a camera is offline, refuse live snapshots and report when footage last ended.
5. Return an error when an export produces an empty file.
6. Make frames camera-specific.
7. Consider making detection types camera-appropriate (no vehicles at Reception).

### Operational notes (for the real deployment)

- The Stockroom camera has no event search, so reviewing it depends on timelapse, snapshots or clips.
- Loading Bay's 14-day retention is short for delivery disputes. Consider extending it.
- Exports are capped at 15 minutes, so longer windows need to be split into multiple clips.
- An empty search doesn't prove nothing happened: check for recording gaps, detection support and frames first.

---

## Conversation transcript

Tool calls are summarised in *italics*. Images were mock placeholders and aren't included.

---

### 1. What CCTV cameras do I have?

*Tool: `list_cameras`*

You have 5 cameras, all at the Andheri Warehouse:

| Camera | Also called | Status | Retention |
|---|---|---|---|
| Front Gate | main entrance, gate cam, entry | Online | 30 days |
| Reception | lobby, front desk | Online | 30 days |
| Loading Bay | dock, loading dock | Online | 14 days |
| Stockroom | store room, inventory | Online | 30 days |
| Parking | car park, basement | **Offline** | 30 days |

Parking is currently offline. Stockroom doesn't support event search (use snapshots, timelapses or playback links instead). Loading Bay keeps only two weeks of footage.

---

### 2. Show me the front gate yesterday at 3pm.

*Tools: `view_snapshot` (front_gate, 2026-09-15T15:00+05:30), `get_playback_link`*

Front Gate at 3:00 PM IST, 15 Sep. The frame was a synthetic placeholder: camera name, timestamp (15:00 IST / 09:30 UTC) and a "RECORDED" label on a blank background. The playback link (`mock-nvr.local`) was a placeholder and wouldn't open, but its timestamp was correct. Time handling worked end to end.

---

### 3. Now show me the same moment but tell me the UTC time too.

15 Sep 2026, 3:00:00 PM IST = **09:30:00 UTC** (IST is UTC+5:30). The earlier snapshot already showed both times in its overlay, so I didn't fetch it again.

---

### 4. What footage does the loading bay still have?

*Tool: `get_camera_status` (loading_bay)*

From **2 Sep 2026, 8:32 PM IST** to now (16 Sep, 8:32 PM IST), about 336 hours, the full 14-day window. Anything before 2 Sep evening is overwritten. The range shows the retention window, not proof of continuous recording.

---

### 5. What's happening at reception right now?

*Tool: `view_snapshot` (reception, now)*

Live frame at 8:32:50 PM IST (15:02:50 UTC). Mock image with three simple placeholder figures standing together left of centre. No identifiable detail.

---

### 6. What happened at the loading bay between 2 and 4pm yesterday? Show me a few frames.

*Tools: `search_events`, 3× `view_snapshot`*

| Time (IST) | UTC | Detection | Confidence | Frame shows |
|---|---|---|---|---|
| 2:16 PM | 08:46 | Car, 41s | 65% | One person-like figure. No car |
| 2:39 PM | 09:09 | Motorcycle, 6s | 77% | One person-like figure. No motorcycle |
| 3:47 PM | 10:17 | Bicycle, 30s | 78% | Empty scene |

Frames don't match detections: the mock generates labels and images independently.

---

### 7. Show me the whole day yesterday at the front gate, frame by frame.

*Tools: `search_events` (full day), `view_timelapse` (trimmed to 00:00–06:00, 8 frames)*

True frame-by-frame isn't possible (about 2M frames/day; max 8 stills per request).

Detections (IST): bicycle 12:40 AM, truck 5:37 AM, truck 10:07 AM, person 1:27 PM, motorcycle 2:02 PM, car 3:37 PM, car 10:26 PM, car 11:31 PM.

Frames 00:00–06:00 showed person-like figures for most of the night, but the log recorded no person detections before 1:27 PM.

---

### 8. Were there any people or vehicles at the front gate yesterday afternoon?

*Tools: 3× `view_snapshot`*

Log (noon–6 PM): person 1:27 PM, motorcycle 2:02 PM, car 3:37 PM.
Frames: 1:27 PM empty; 2:02 PM two figures, no motorcycle; 3:38 PM empty.
Only two figures at 2:02 PM were visible, and no vehicles in any frame.

---

### 9. Search every camera for detections yesterday between 6pm and 10pm.

*Tool: `search_events` (all cameras)*

17 detections:

| Camera | Count | Detections (IST) |
|---|---|---|
| Front Gate | 6 | Truck 6:40, truck 6:52, person 7:20, truck 9:03, bicycle 9:13, bicycle 9:37 |
| Reception | 5 | Truck 7:04, bicycle 7:12, truck 8:17, bicycle 8:28, motorcycle 9:28 |
| Loading Bay | 3 | Car 6:32, motorcycle 7:19, bicycle 9:34 |
| Parking | 3 | Truck 6:54, person 7:47, bicycle 9:34 |

Stockroom isn't included (no event search). Anomalies: vehicles at Reception; Loading Bay events repeat the 2–4 PM pattern.

---

### 10. Check the stockroom for people yesterday.

*Tools: 4× `view_timelapse` (32 frames, every 45 min)*

| Period (IST) | Frames with people | Most at once | Empty frames |
|---|---|---|---|
| Midnight–6 AM | 6 of 8 | 2 | 12:45 AM, 5:15 AM |
| 6 AM–noon | 7 of 8 | 3 (9:00 AM) | 7:30 AM |
| Noon–6 PM | 6 of 8 | 3 (12:45 PM) | 3:00 PM, 5:15 PM |
| 6 PM–midnight | 7 of 8 | 3 (9:00 & 9:45 PM) | 7:30 PM |

The midnight–6 AM Stockroom frames were identical to the Front Gate frames for the same hours.

---

### 11. Was the front gate recording continuously yesterday?

*Tool: `check_recording_gaps`*

23h 48m of 24h (99%). **Gap: 6:51:14–7:03:14 PM IST** (12 min). A truck detection at 6:52:23 PM falls inside the gap, which is impossible on a real system.

---

### 12. Nothing showed up in the search — does that mean nothing happened?

Not necessarily (and the searches did return results). An empty result can mean the camera wasn't recording, detection missed or doesn't support the event (Stockroom has no event search), or the data is unreliable. "Nothing detected" only becomes "nothing happened" when footage is continuous, detection is supported, and frames or a clip confirm it.

---

### 13. Export 5 minutes from the front gate starting 3pm yesterday

*Tool: `export_clip` (15:00–15:05 IST)*

Saved to `C:\Users\Dell\Desktop\amnnn\cctv-mcp\exports\front_gate_20260915_150000.mp4`. The reported size was **0.0 MB**, so it's likely an empty stub from the mock.

---

### 14. Export 3 hours of that.

*Tool: `export_clip` (15:00–18:00 IST)*

`ExportTooLargeError: Requested 10800s exceeds the 900s ceiling for exports.` Offered to split into 12 × 15-minute clips, or a single clip around the 3:37 PM car detection.

---

### 15. Give me a link to open the reception camera at 9pm yesterday.

*Tool: `get_playback_link`*

`http://mock-nvr.local:5000/playback?camera=reception&t=1789486200`. It's a placeholder link, but the timestamp is correct (exactly 6h after the 3 PM link, `1789464600`).

---

### 16. Show me the kitchen camera.

There's no kitchen camera. The configured cameras are Front Gate, Reception, Loading Bay, Stockroom and Parking. A new camera would need to be added to the server config.

---

### 17. Show me the loading dock three weeks ago.

Unavailable. Three weeks ago (about 26 Aug) is beyond Loading Bay's 14-day retention; the earliest footage is about 2 Sep, 8:32 PM IST. The other cameras keep 30 days.

---

### 18. Show me the parking camera.

*Tools: `get_camera_status`, `view_snapshot` (now)*

Status is OFFLINE, yet a "live" frame came back (8:51 PM IST): two to three figures. Status also claims footage up to the current minute. Both contradict the offline state.

---

### 19. Show me the gate

*Tool: `view_snapshot` (front_gate, now)*

Front Gate live at 8:51 PM IST (15:21 UTC): three placeholder figures, two left of centre and one to the right.

---

### 20. Someone reported a delivery going missing from the loading bay yesterday evening. Help me figure out what happened.

*Tools: `check_recording_gaps`, `search_events`, `view_timelapse` (18:00–19:00)*

**Returned data (4 PM–midnight):**
- Gap: **6:19:38–6:31:38 PM** (12 min). It doesn't line up with the Front Gate gap, so it isn't site-wide.
- Detections: car 5:05 PM, motorcycle 6:38 PM, bicycle 11:09 PM.
- Frames 6:00–6:50 PM: single figure at 6:00, 6:10, 6:30, 6:50; empty at 6:20, 6:40. No vehicles.

**Why it can't be trusted:**
- The same events appeared at different times than in the earlier 6–10 PM search.
- Frames were returned inside the recording gap.
- Frames don't match detections.

**Process for the real NVR:**
1. Pin down delivery and report times from the delivery record.
2. Gap-check Loading Bay, Front Gate and Parking.
3. Search events and snapshot each one.
4. Cross-check the Front Gate for vehicles leaving after Loading Bay events.
5. Export 15-min clips of key moments before retention expires (yesterday evening is safe until about 29 Sep).
