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
  "profiles": {"standard": 1, "clean": 3, "max": 9}
}
```

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
    "file_id": "/store_00010001/DCIM/100MSDCF/DSC00042.ARW",
    "camera_id": "cam0", "seq": 17, "frame_index": 0,
    "size_bytes": 24117248, "latency_s": 1.83,
    "content_type": "image/x-sony-arw", "path": "..."
  }]
}
```

`seq` is **echoed, never invented**. Pairing depends on it.

Capture retries once through `capture_with_retry`, so a dropped PTP session —
which the A6000 does when idle — is recovered transparently and does not surface
as an error. Unknown profile → **400**. Unrecoverable camera error → **503**.

### `GET /files/{file_id}`

Returns the bytes. `file_id` may contain slashes (it is a camera path on the
gphoto2 backend). **404** if unknown.

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
capture**, models transfer time against a real 24 MB / 15 MB/s budget, and can
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
page-turn time**, or the queue grows without bound. At ~15 MB/s and a 4 s page
turn that is ~60 MB per camera — two to three frames maximum sustainable, which
is why `standard` is one frame.

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
