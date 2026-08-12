# API reference

Three surfaces: the **node HTTP API** (one per camera), the **orchestrator**
(the laptop side), and the **command line**.

---

## Node HTTP API

One node runs beside each camera. It is deliberately thin — it holds no session
state beyond the camera itself, because the orchestrator owns the sequence
counter and the pairing. If a node reboots mid-book you lose one spread, not the
run.

```bash
uvicorn scanner.node.server:app --host 0.0.0.0 --port 8000
# or
python -m scanner node --backend gphoto2 --camera-id cam0 --tile 0 --port 8000
```

Interactive docs are served at `/docs` (Swagger) and `/redoc`.

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `SCANNER_CAMERA_ID` | derived from hostname | `cam0`, `cam1`, … |
| `SCANNER_TILE` | inferred from the id | which tile this camera covers |
| `SCANNER_BACKEND` | `auto` | `mock`, `gphoto2`, or `auto` |
| `SCANNER_MOCK_SCALE` | `0.25` | render scale for the mock backend |

`auto` uses gphoto2 if `python-gphoto2` imports, otherwise the mock. Role is
derived from the hostname so **both Pis can run the identical SD image**.

### `GET /status`

```json
{
  "camera_id": "cam0",
  "backend": "gphoto2",
  "connected": true,
  "model": "Sony Alpha-A6000",
  "settings": {
    "iso": 200, "shutter": "1/125", "aperture": "8.0",
    "capture_target": "card", "image_format": "RAW"
  },
  "frames_captured": 42,
  "last_error": "",
  "busy": false,
  "tile_index": 0,
  "staging_free_mb": 7945,
  "staging_volatile": false,
  "staged_frames": 3,
  "frames_evicted": 0,
  "frames_recovered": 0,
  "session_reopens": 42,
  "usb_resets": 0,
  "focal_length_mm": 30.0,
  "expected_focal_length_mm": 30.0,
  "optical_alarm": "",
  "profiles": {"standard": 1, "clean": 3, "max": 9}
}
```

The fields worth understanding, because each exists to stop something silent:

| Field | Reading it |
|---|---|
| `staging_volatile` | `true` means staged frames will **not** survive a restart of the service — and `Restart=always` means there will be one. Should be `false` on a provisioned node |
| `staged_frames` | Frames captured and not yet claimed. This is the number that fills; `staging_free_mb` is only its consequence |
| `frames_evicted` | The cap has **dropped** this many frames. Non-zero means something captured without claiming — the orchestrator died mid-run, or is calling `GET /files` and never `DELETE` |
| `frames_recovered` | Frames adopted from a previous run of this process. A receipt, not a fault: the service restarted with frames outstanding and they survived |
| `session_reopens` | PTP sessions opened. Rises once per capture by design — `session_per_capture` is why capture works at all |
| `usb_resets` | Bus re-enumerations. The recovery tier below reconnect, and the only one that clears a frame stuck in the body's RAM. Rising on an otherwise healthy node is a cable or a power problem |
| `optical_alarm` | Non-empty means the lens moved since calibration. Capture goes on working, which is exactly why it needs saying out loud |

Never fails because the camera is missing — `connected: false` is a status, not
an error. A node that refuses to start when its camera is unplugged is a node
you cannot debug remotely.

### `POST /connect`

Force a reconnect. Reconnects with exponential backoff (3 attempts). Returns the
new status, or **503** with a diagnostic message.

### `POST /config`

```json
{"iso": 400, "shutter": "1/160", "aperture": "8.0"}
```

All fields optional; supplied ones are merged over current settings. Returns the
resulting status.

> Sony bodies refuse aperture and shutter over PTP unless the mode dial is on
> **M**. The gphoto2 backend records the refusal in `last_error` rather than
> pretending it worked.

### `GET /preview`

Returns `image/jpeg` — a small frame for **framing**. Never used for
measurement, and never for focus: see `/focus` below.

### `GET /stream?fps=4.0`

`multipart/x-mixed-replace; boundary=frame` — the preview, repeated. An MJPEG
stream costs nothing on the client (every browser decodes it natively, with no
JavaScript) and degrades gracefully: a dropped frame is a dropped frame, not a
broken session.

Rate-limited server-side by `fps`. The default of 4 is chosen so live view does
not compete with a capture for USB bandwidth.

### `GET /focus`

**Takes a real, full-resolution frame** and scores it.

```json
{
  "camera_id": "cam0",
  "size": [6000, 4000],
  "regions": [{"name": "top left", "score": 25.86}, "..."],
  "exposure": {"mean": 189.1, "clipped_high_pct": 0.0,
               "clipped_low_pct": 0.0, "histogram": ["..."]},
  "jpeg_b64": "..."
}
```

This is the one endpoint that deliberately costs a shutter actuation, and it has
to. **Live view is roughly 1/35 of the sensor's pixels, and downscaling is
itself a low-pass filter** — a preview can look perfectly sharp on a frame that
is visibly soft at full size. Scoring the preview would produce a number that
tracks the preview's sharpness, not the camera's.

`score` is Tenengrad — mean squared Sobel gradient, normalised by image variance
so it does not merely restate exposure. Five regions, because a centre-sharp,
corner-soft frame is a lens verdict and is invisible on one central score.

The absolute value means nothing; the change as you turn the ring does. The GUI
holds the per-region peak and shows the current score against it — see
[`gui.md`](gui.md).

### `POST /capture`

```json
{"seq": 17, "profile": "standard"}
```

| Profile | Frames | Use |
|---|---|---|
| `standard` | 1 | default |
| `clean` | 3 | faded, low-contrast or very fine print |
| `max` | 9 | rare pages that genuinely warrant it |

```json
{
  "seq": 17,
  "camera_id": "cam0",
  "profile": "standard",
  "files": [{
    "file_id": "cam0/000017/0/capt_DSC00001.ARW",
    "camera_id": "cam0", "seq": 17, "frame_index": 0,
    "size_bytes": 24117248, "latency_s": 1.83,
    "content_type": "image/x-sony-arw", "path": "/capt_DSC00001.ARW"
  }]
}
```

`seq` is **echoed, never invented**. Pairing depends on it.

`file_id` is minted by the node; `path` is where the frame sat on the camera.
They are separate because **the camera-side name is not unique**: with
`session_per_capture` the body restarts its own numbering every session, so
every frame of a run arrives as `capt_DSC00001.ARW`.

Capture retries once through `capture_with_retry`, so a dropped PTP session —
which the A6000 does when idle — is recovered transparently and does not surface
as an error.

| Status | Meaning |
|---|---|
| **400** | unknown profile |
| **409** | the frame is byte-identical to the previous one — the body served a frame from its own volatile buffer. Retrying will return the same one; the fix is a USB re-enumeration, **not** a power cycle |
| **412** | the focal length no longer matches the calibrated one. The camera is working perfectly and is pointed at the wrong magnification |
| **507** | staging cannot hold the frames. No shutter actuation was spent |
| **503** | unrecoverable camera error |

### `GET /files/{file_id}`

Returns the bytes. **404** if unknown — released, evicted, or from a run whose
staging was lost.

**Reading does not release.** A GET that consumed would make a retried request
after a network timeout destroy a page silently, and would put the only copy of
a frame at the mercy of the flakiest part of the system.

### `DELETE /files/{file_id}`

Claim a frame: *"I have this, you may forget it."*

```json
{"file_id": "cam0/000017/0/capt_DSC00001.ARW", "existed": true, "staged_frames": 3}
```

**Always 200, idempotent.** An orchestrator whose release timed out will retry
it; answering 404 the second time invites the one reflex that loses data —
treating a frame already safely taken as one worth re-fetching. `existed`
carries the distinction for anyone who needs it.

This is the route whose absence left `release_cache()` in the backend with no
caller, and staging filling at ~320 frames with nothing able to free it. The
design follows libgphoto2's own: `--capture-image-and-download` deletes by
default, and on Sony that delete never reaches the camera — it is a host-side
eviction — with a bounded LRU behind it.

### `DELETE /files`

Drop everything. For the end of a run, or a clean start.

### `GET /staged`

```json
{
  "camera_id": "cam0",
  "file_ids": ["cam0/000017/0/capt_DSC00001.ARW"],
  "count": 1, "cap": 240, "evicted": 0, "recovered": 0,
  "staging_free_mb": 7945, "staging_volatile": false
}
```

The orchestrator's recovery path after **its own** restart: ask what survived
rather than assume. `evicted` is non-zero only when the cap has dropped frames,
which means something captured without claiming.

### `POST /optics`

```json
{"expected_focal_length_mm": 30.0}
```

Record the focal length the rig was calibrated at. Set it once, after the zoom
is taped. Every captured frame's EXIF is then checked against it, and a power
cycle that resets the E PZ 16-50 to 16 mm stops the run on the next frame
instead of quietly producing several hundred pages at the wrong magnification.

Zero disables the check, and is the right default for an uncalibrated rig: an
alarm nobody has calibrated for gets ignored, including on the day it is right.

### `GET /healthz`

`{"ok": true}` — liveness only, does not touch the camera.

---

## Camera backends

`mock` and `gphoto2` implement the same abstract base class and are
interchangeable. Nothing above `CameraBackend` knows which it is talking to.

```python
class CameraBackend:
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    @property
    def connected(self) -> bool: ...
    def reconnect(self, attempts=3, backoff_s=0.5) -> None: ...
    def model(self) -> str: ...
    def apply_settings(self, settings: CameraSettings) -> None: ...
    def capture(self, seq: int, frames: int = 1) -> list[CapturedFile]: ...
    def capture_with_retry(self, seq, frames=1, attempts=2) -> list[CapturedFile]: ...
    def read_file(self, file_id: str) -> bytes: ...
    def preview(self) -> bytes: ...
    def status(self) -> CameraStatus: ...
```

Exceptions: `CameraError`, `CameraBusy`, `CameraDisconnected`. The last one is
not exceptional — it is Tuesday — and every backend must recover from it.

### The mock backend

Not a stub that returns a fixed image. It renders a **different page each
capture**, models transfer time against a real 24.5 MB / 9.25 MB/s budget
(measured on the rig, 2026-08-12), and can
be told to drop its PTP session periodically:

```python
MockCamera("cam0", tile_index=0, scale=0.25, transfer_mb_s=15.0,
           file_mb=24.0, drop_every=5)
```

If the orchestrator is correct against this, it is correct against a Sony. That
is the entire value of building Phase 0 first.

### The gphoto2 backend

Uses **`python-gphoto2` bindings, not the CLI** — every CLI invocation
re-initialises the USB session, which costs about a second and makes burst
capture impossible.

```bash
sudo apt install -y libgphoto2-dev
pip install gphoto2
```

It raises actionable errors for the two failure modes you will actually hit —
gvfs holding the USB device, and no camera found — with the fix in the message.
See [`troubleshooting.md`](troubleshooting.md).

---

## Orchestrator

```python
from scanner.orchestrator.session import CaptureSession, NodeSpec

session = CaptureSession(
    [NodeSpec("cam0", "http://pi1:8000", 0),
     NodeSpec("cam1", "http://pi2:8000", 1)],
    out_dir="captures/", profile="standard")

session.require_ready()                       # fail now, not on page 200
session.configure_all(iso=200, aperture="8.0")

rec = session.capture_spread()
rec.complete        # True if every node returned files
rec.files           # {"cam0": [path, ...], "cam1": [...]}
rec.errors          # {"cam1": "capture: ..."} on partial failure

session.capture_many(20, on_spread=lambda r: print(r.seq, r.elapsed_s))
print(session.throughput())
session.write_manifest()
```

### Two rules it is built around

**Pairing is by a sequence counter issued here, never by timestamp.** Nothing in
the scene moves, so the frames do not need to be simultaneous to a millisecond —
they need to be *identifiable*. A counter is exact and immune to clock skew, NTP
steps and network jitter. Timestamp pairing works in the lab and fails on page
300.

**A node failure loses one spread, not the session.** Each capture is
independent; the manifest records what succeeded and what did not, and a partial
spread is written as partial rather than silently dropped.

Cameras are triggered concurrently on a thread pool, then files are pulled down
per node. Files land in `out_dir/<seq:05d>/<camera_id>_<frame>.arw`, which is
exactly the layout `scanner process` expects.

### Throughput

```python
{"spreads": 20, "complete": 20, "failed": 0,
 "mean_s": 2.31, "p95_s": 2.88, "max_s": 3.02,
 "mean_mb": 48.2, "mb_per_s": 20.9}
```

The inequality that matters: **transfer time per spread must stay below
page-turn time**, or the queue grows without bound. Measured at **2.65 s per
frame** (9.25 MB/s, no card buffer, transfer serial with capture), a 4 s page
turn sustains **one frame** — which is why `standard` is one frame.

---

## Command line

```
python -m scanner geometry     what the optics can do
python -m scanner board        render a printable ChArUco target
python -m scanner node         run a camera node
python -m scanner capture      drive a capture session
python -m scanner process      raw frames -> archival masters
python -m scanner measure      MTF50 / DPI on an image
python -m scanner selftest     rehearse the whole rig, no hardware
python -m scanner gui          the operator interface in a browser
```

### `geometry`

```
-n, --cameras N        number of cameras (default 2)
-o, --overlap MM       gutter overlap (default 20)
-f, --format NAME      "A3 spread" | "A4" | "A4 portrait" | "US Letter"
    --focal MM         lens focal length (default 30)
    --aperture N       f-number (default 8)
    --compare          table against the blueprint and single-camera options
    --json             machine-readable summary
```

### `board`

```
-o, --out PATH         output image (default charuco_a4.png)
    --squares-x N      default 7
    --squares-y N      default 10
    --square-mm MM     default 26.0
    --margin MM        quiet zone, default 6.0
    --dpi N            print resolution, default 600
```

Prints the physical size and a reminder to print at 100 % scale and verify with
a ruler.

### `node`

```
    --host HOST        default 0.0.0.0
    --port PORT        default 8000
    --camera-id ID     overrides hostname derivation
    --tile N           which tile this camera covers
    --backend B        mock | gphoto2 | auto
    --log-level LEVEL  default info
```

### `capture`

```
    --node [id=]URL    repeatable; "cam0=http://pi1:8000"
-o, --out DIR          default captures/
-c, --count N          spreads to capture
-p, --profile P        standard | clean | max
    --iso N            applied to every node
    --shutter S
    --aperture A
    --interactive      pause between spreads to turn the page
    --force            capture even if a node reports it is not ready
```

### `process`

```
INPUT                  a directory of spread directories, or one spread
-k, --calibration PATH rig.json
-o, --out DIR          default masters/
    --no-deskew
    --no-normalise
    --no-split
    --debug-dir DIR    write intermediates
```

### `measure`

```
IMAGE
    --edge x,y,w,h     repeatable; a slanted-edge ROI
    --scale ax,ay,bx,by,span_mm
    --px-per-mm N      also report MTF50 in lp/mm
```

### `selftest`

```
-n, --cameras N        default 2
-o, --overlap MM       default 20
    --scale F          render scale; 1.0 is full 24 MP and slow (default 0.25)
    --out DIR          default selftest_out/
-q, --quiet
```

Exit code 0 if every acceptance check passes, 1 otherwise — so it works as a CI
gate.

### `gui`

```
    --node [camN=]URL  repeatable; the camera nodes
    --mock             spin up simulated cameras in this process
    --mock-cameras N   default 2
    --mock-scale F     render scale for the mocks (default 0.12)
    --host ADDR        default 127.0.0.1; use 0.0.0.0 to reach it from a tablet
    --port N           default 8800
-o, --out DIR          capture folder (default captures/)
-p, --profile NAME     standard | clean | max
    --coverage MODE    stereo | tile
-f, --format NAME      "A3 spread" | "A4" | ...
    --overlap MM       tile mode
    --baseline MM      stereo mode
    --focal MM         default 30
    --aperture N       default 8
```

`--mock` needs no hardware at all: it starts simulated camera nodes inside the
same process and points the interface at them. See [`gui.md`](gui.md).

---

## Not yet implemented

- **`scanner calibrate`** — the library calls work and are exercised by the
  tests, but there is no CLI wrapper. See
  [`calibration.md` §6](calibration.md#6-complete-example) for the script.
- **`scanner align`** — same, for the document homography step.
- **OCR and PDF/A export** — Phase 5, not started.
- **Super-resolution** — Phase 6, not started.
