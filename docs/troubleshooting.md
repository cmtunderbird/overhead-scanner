# Troubleshooting

Failure modes that are real, in the order you are likely to hit them.

---

## Camera and connection

### "Could not claim the USB device"

The single most common first-hour failure. `gvfs-gphoto2-volume-monitor` grabs
the camera the instant it enumerates, and every gphoto2 command then fails.

```bash
systemctl --user mask gvfs-gphoto2-volume-monitor
pkill -f gvfs-gphoto2
```

**Pi OS Lite has no desktop and therefore no gvfs**, which is precisely why the
blueprint specifies Lite. On a desktop distro you must mask it every time.

### `gphoto2 --auto-detect` finds nothing

In order of likelihood:

1. **USB Connection is not set to PC Remote.** On the A6000: *Menu → Setup →
   USB Connection → PC Remote*. In any other mode it enumerates as mass storage
   and gphoto2 correctly ignores it.
2. Camera is asleep, or off.
3. A charge-only USB cable. More common than anyone admits.
4. Another process holds the device — see above.

### The session drops during a run

Expected. The A6000 drops its PTP session when idle; it is not exceptional.
`capture_with_retry` reconnects once with exponential backoff and the run
continues. If it happens on *every* capture, suspect a marginal cable or a USB
hub without its own power.

### Aperture or shutter changes are refused

Sony bodies will not accept these over PTP unless the **mode dial is on M**. The
backend records the refusal in `last_error` rather than pretending it worked —
check `GET /status`.

### Captures are far slower than expected

`capturetarget` defaults vary by body and firmware and silently decide whether
files land in camera RAM or on the card. Set it explicitly (the backend does
this on connect). Then check your actual transfer rate against `throughput()` —
USB 2.0 PTP on these bodies runs a measured 9.25 MB/s, so a 24.5 MB ARW is
~2.65 s and no
amount of software makes it faster.

---

## Calibration

### `only N usable views of the board (need 6)`

The board was not detected in enough frames. Check, in order: is the whole board
in frame; is it in focus; is it evenly lit without glare; is the `BoardSpec` the
same one you printed?

### `tilt_warning` is set

> *the board was nearly flat in every view; focal length is poorly conditioned —
> tilt it 20–30 degrees and reshoot*

Take this seriously. Intrinsics **cannot** be solved from a fronto-parallel
plane — focal length and distance trade off exactly. You will get a confident,
wrong answer with a low RMS.

### Reprojection RMS is low but the focal length is wrong

Two causes, both silent:

1. **Flat stack** — see above.
2. **The printed board is not the size you told the code it is.** Measure a
   square with a ruler. If you printed with "fit to page" it will be ~2.4 %
   small, and every measurement inherits that error while remaining perfectly
   self-consistent.

### `mounting_error` reports several percent of scale error

Either the working distance is not what you think, or the board is mis-scaled.
Check the ruler first — it is the cheaper hypothesis.

> This exact symptom appeared during development as −6.5 % and was neither: it
> was `CharucoBoard.generateImage` sizing `outSize` to include the margin, so
> the rendered squares were 6.6 % small. See
> [`calibration.md` §0](calibration.md#0-the-target).

### `no usable board detected` when solving the document homography

The board must be **flat on the platen**, fully lit, and inside that camera's
tile. With two cameras, each one needs its own shot with the board in its half.

### `keystone` is not near zero

The camera is not perpendicular to the platen. A homography corrects the
geometry, but it cannot make the depth-of-field plane parallel to the page — one
side of the frame will be softer. Shim the mount.

---

## Stitching and output

### A visible band down the middle of every spread

The classic twin-camera failure, and it has two independent causes. Check them
separately:

| Cause | Diagnostic | Fix |
|---|---|---|
| Colour mismatch between bodies | `cross_camera_delta_e` > 2 | Colour calibration (`calibrate_colour`) |
| Vignetting | `seam_visibility` > 1.2 with ΔE fine | Flat-field (`calibrate_flat_field`) |

A hard vertical edge is far more visible than a gradual shift of the same
magnitude, which is why a ΔE of ~1 — well under the threshold in isolation — can
still read as a seam.

### Seam is misregistered (`shift_px` > 1)

Geometry, not photometry. Re-solve the document homography; check
`mounting_error` for a camera that has moved on the rail. If it drifts between
sessions, the frame is flexing.

### `coverage_pct` below 99

A camera moved, the page is outside the field, or the document format in the
calibration does not match what you are actually shooting.

### `spine.fallback` is true

Detection found no significant dip near the centre. Normal for a loose sheet or
a single page. On an actual spread it means the gutter is not dark enough —
usually because the lamps are too frontal — or the page is badly off-centre.

### Deskew reports close to ±3°

It has hit the search limit and is probably clipping. Straighten the page;
`max_deg` is deliberately small because a page 10° crooked is a handling problem
and correcting it would consume the spare field.

### The output looks washed out

Background normalisation is doing its job too enthusiastically for the material.
Reduce `strength` below 1.0, or set `normalise_background=False` — for aged
manuscripts or coloured stock the paper tone is information, not a defect.

---

## Measurement

### MTF50 is much lower at the corners than the centre

Expected on a kit lens, and it is the number that decides whether the lens
stays. Below about 0.22 cy/px at the corners is the argument for a prime — worth
measuring before spending anything.

### MTF50 looks impossibly high

Almost always a missing linearisation. SFR on gamma-encoded data reads high
because the encoding steepens the edge. `measure_sfr` linearises with
`gamma=2.2` by default; if your source is already linear, pass `gamma=1.0`.

### `edge angle X deg is unusable`

The target was laid down too straight or too skewed. ISO 12233 wants roughly
2–10° off vertical: a perfectly vertical edge samples one phase only and there is
nothing to reconstruct.

### `roi too small for SFR` / `edge too close to the roi border`

The ROI needs ≥ 16 px per side and enough clean run either side of the edge.
Bigger ROIs also average down noise — the measurement degrades above ~3 DN on a
small patch.

### Measured DPI disagrees with the model by more than 1 %

Working distance is wrong, or the reference you are measuring against is
mis-scaled. Check the physical mast height against
`RigGeometry.working_distance_mm` for the format you are actually shooting —
**434 mm for single-camera A4, 453 mm for two-camera A3.** Mixing these up is
easy and produces exactly this symptom.

---

## Selftest and tests

### `MTF50 (median of edges)` shows `--` instead of PASS

The rehearsal ran at a scale where the synthetic page is rendered at less than
3× the capture sampling, so the *page* band-limits the result. The check is
skipped rather than passed, because a green tick measuring the wrong thing is
worse than no tick. Raise `--scale`.

### The test suite is slow

It runs at `scale = 0.16` and takes ~105 s. Most of that is the end-to-end and
orchestrator tests, which spin real uvicorn servers. Run a single file while
iterating:

```bash
python -m pytest tests/test_sfr.py -q
```

### `RuntimeError: no TrueType fonts available for page synthesis`

**Fixed** — but if you are on an older checkout, this is what a Windows or
macOS machine hits the first time it runs `scanner gui --mock` or the
selftest. Font lookup searched `/usr/share/fonts` only, so a laptop that
ships Arial rather than Liberation Sans had, as far as the renderer was
concerned, no fonts at all.

It is worth being clear about what it was *not*: it had nothing to do with
missing cameras or nodes. Only the **mock** backend draws synthetic pages, so
the failure appeared on every `/preview`, `/stream` and `/focus` call and
looked like a camera problem.

Font resolution now indexes the platform's font directories, accepts
metric-compatible substitutes (Arial, Times, Courier, DejaVu, Noto, Free\*),
and falls back to Pillow's built-in face rather than raising. Nothing
*measured* on a synthetic page is drawn with a font — the slanted edges,
colour patches, fiducials and ruler are all geometry — so a substitute face
changes the texture and never the ground truth. `git pull`.

### On Windows: `PermissionError: [WinError 5] Access is denied` in dozens of tests

Look at *where* it is denied. If the path is
`%LOCALAPPDATA%\Temp\pytest-of-<user>`, this is not the test suite — pytest
cannot create its temp root, so every test taking a `tmp_path` fails **at
setup**. It usually means that directory was created once by an elevated
process and your normal user can no longer write to it.

```powershell
python -m pytest -q --basetemp=.pytest-tmp
```

Or delete `%LOCALAPPDATA%\Temp\pytest-of-<user>` from an administrator
prompt. Nothing in the codebase is involved either way.

### `cv2.aruco has no attribute 'interpolateCornersCharuco'`

You are on an OpenCV that still has the legacy ChArUco API and something is
calling it — this codebase uses the modern `CharucoDetector` and
`board.matchImagePoints`. Ensure `opencv-contrib-python>=4.8`; `cv2.aruco` is
**not** in the base `opencv-python` package at all.

---

## Getting more information

```bash
# what the optics should be doing
python -m scanner geometry --compare

# is the node alive and does it see a camera
curl localhost:8000/status | python -m json.tool

# write pipeline intermediates
python -m scanner process captures/ -k rig.json -o out/ --debug-dir debug/

# does the whole chain still work
python -m scanner selftest
```

`selftest` is the fastest way to establish whether a problem is in the software
or in the physical rig. If it passes and your real captures do not, the software
is fine.
