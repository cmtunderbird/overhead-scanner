# Pipeline

Raw frames in, archival master out. Every stage is an independently testable
function in [`scanner/pipeline/stages.py`](../scanner/pipeline/stages.py);
[`run.py`](../scanner/pipeline/run.py) sequences them and reports what happened.

```
undistort ─► flat-field ─► colour ─► rectify ─► stitch
          ─► deskew ─► crop ─► normalise ─► split
```

> **This is the tile-mode pipeline**, and it assumes the document is a plane.
> For bound books the rig runs in stereo mode, where `rectify_to_document` is
> replaced by surface recovery and flattening — see [`stereo.md`](stereo.md).
> That path currently lives beside this module rather than inside it.

**The order is not arbitrary.** Three constraints fix it:

- **Undistort first**, because everything downstream assumes a pinhole camera.
- **Flat-field before colour**, because a colour matrix fitted through a
  vignette is fitting the vignette.
- **Normalise after stitching**, so both halves get the same background model.
  Normalising per-tile makes the two halves disagree about what "paper" is, and
  the join moves.

---

## Usage

```python
from scanner.calib.store import RigCalibration
from scanner.pipeline.run import process_spread, PipelineConfig

rig = RigCalibration.load("calibration/rig.json")
result = process_spread({"cam0": img0, "cam1": img1}, rig, PipelineConfig())

result.master        # stitched, deskewed, cropped, normalised
result.pages         # [left, right] after the spine split
result.coverage      # which pixels any camera actually saw
result.report        # timings and diagnostics
result.save("masters/", "spread_0001")
```

Or from the command line:

```bash
python -m scanner process captures/ -k calibration/rig.json -o masters/
```

`frames` maps `camera_id → image`. **Cameras missing from `frames` are simply
skipped**, so a one-camera rig runs the identical code path as a two-camera one
and reports the absentees in `report["missing_cameras"]`. Nothing changes when
the second body arrives.

---

## Stage 1 — per-camera correction

```python
correct_frame(image, calib)   # undistort → flat-field → colour
```

`undistort` keeps the **original camera matrix** (`newCameraMatrix=K`) rather
than an optimised one. That matters: the document homography was solved against
undistorted pixels in this exact frame, so changing the camera matrix here would
silently invalidate it.

---

## Stage 2 — rectification

```python
warped, mask = rectify_to_document(corrected, calib, canvas_px, target_px_per_mm)
```

Warps one corrected frame onto the shared document canvas.

The homography maps document **millimetres** to image pixels; the canvas is in
document **pixels** at `target_px_per_mm`, so the composed transform is

```
H_docpx_to_img = H_doc_to_img @ diag(1/ppm, 1/ppm, 1)
```

applied with `WARP_INVERSE_MAP` (the matrix maps destination → source).

Interpolation is `INTER_LANCZOS4`. This is the one resampling step that touches
every pixel of the archival master, and it is deliberately *not* optimised —
cubic is 3× faster and visibly softer.

The returned **mask** is what makes blending honest. Without it, border
replication bleeds invented pixels into the seam. It is eroded by one pixel
because the outermost warped row is interpolated against nothing.

---

## Stage 3 — stitching

```python
image, coverage = blend_tiles(warped, masks, feather_px=60)
```

Weights come from a distance transform of each mask: every pixel is owned mostly
by whichever camera saw it furthest from its own frame edge, ramped linearly
over `feather_px`.

A linear ramp is the right choice **here specifically**. The tiles are
geometrically registered to sub-pixel by a calibrated homography, so what is
being hidden is a *photometric* step — a few DN of residual exposure or colour
difference — not a parallax error. Multi-band blending exists to hide parallax
and would only soften the join.

`overlap_columns(rig)` returns the canvas x-ranges where adjacent tiles overlap,
which is what `measure_seam` needs.

---

## Stage 4 — deskew

```python
angle = estimate_skew(image, max_deg=3.0, step=0.05)
image = rotate(image, angle)
```

Projection-profile variance: text lines produce a strongly peaked row profile
only when they are horizontal, so the score is `var(diff(row_sums))` maximised
over candidate angles.

Chosen over Hough line detection because it is robust on sparse pages — Hough
finds the page border, the platen edge or a table rule and confidently reports
their angle instead.

Searched **coarse-to-fine**: 0.5° over ±3°, then 0.05° in the winning bracket.
A flat sweep at 0.05° is 121 warps for no extra accuracy. The image is
downscaled to at most 900 px for the search.

`max_deg=3.0` is deliberate. After rectification the residual should be small —
a page laid down 10° crooked is a handling problem, and silently rotating it
would eat the spare field that deskew depends on.

---

## Stage 5 — crop

```python
cropped, (x, y, w, h) = crop_to_content(image, coverage, margin_px=4)
```

Crops to the paper, using the paper itself (luminance > 12) intersected with the
coverage mask — not to the canvas, which would leave black borders wherever the
page sat inside the field.

---

## Stage 6 — background normalisation

```python
image = normalise_background(image, sigma_frac=0.045, target=244, proxy_px=640)
```

Divides out the illumination field. The background is estimated by a
morphological **close** — which lifts the text out of the way so the blur sees
paper, not ink — followed by a heavy Gaussian, then the image is normalised to
a target paper level.

**Division, not subtraction.** Subtracting a background crushes the text along
with the shadow; dividing preserves local contrast because ink and paper scale
together.

This removes lamp falloff and the gutter shadow band. It is what
`test_end_to_end.py::test_background_normalisation_removes_the_gutter_shadow`
verifies, by measuring how flat the paper margin is before and after.

**Computed on a 640 px proxy and upsampled.** The background field is
low-frequency by construction, so this is numerically equivalent and about
fifty times faster — 6 969 ms → 386 ms on a 2291 × 1620 canvas.

Set `strength` below 1.0 to apply it partially, or `normalise_background=False`
in the config to skip it. For material where paper tone is meaningful — aged
manuscripts, coloured stock — you may want it off in the master and on only in
the access copy.

---

## Stage 7 — spine detection and split

```python
spine = detect_spine(image, search_frac=0.18)
pages = split_spread(image, spine.x_px, gutter_trim_px=0)
```

The gutter is the darkest, most text-free column near the centre. The search is
restricted to the middle 18 % of the width: a spread's spine is never at the
edge, and constraining it stops a dark plate on one page from stealing the
answer.

If the dip has less than 2 % contrast, `detect_spine` returns
`fallback=True` and the geometric centre — a loose sheet or a single page has no
spine, and inventing one would be worse than admitting it.

```python
SpineResult(x_px=3817, confidence=0.193, fallback=False)
```

Synthetic accuracy: **0.0 mm error** against the known spine position.

---

## Configuration

```python
@dataclass
class PipelineConfig:
    deskew: bool = True
    crop: bool = True
    normalise_background: bool = True
    split_spread: bool = True
    feather_px: int = 60
    max_skew_deg: float = 3.0
    gutter_trim_px: int = 0
    debug_dir: str | None = None      # write intermediates; slow
```

`debug_dir` writes `01_rect_<cam>.jpg`, `02_stitched.jpg` and
`03_normalised.jpg`. Use it once when something looks wrong, not in production.

---

## The report

Every run returns a diagnostics dict:

```python
{
  "cameras": {
    "cam0": {"tile_mm": [0.0, 220.0], "coverage_pct": 52.9, "ms": 412.7},
    "cam1": {"tile_mm": [200.0, 420.0], "coverage_pct": 52.9, "ms": 398.1}
  },
  "stitch_ms": 121.4,
  "canvas_px": [7636, 5400],
  "coverage_pct": 100.0,
  "skew_deg": -0.002,
  "deskew_ms": 90.3,
  "crop_box": [12, 8, 7610, 5384],
  "normalise_ms": 386.2,
  "spine": {"x_px": 3817, "x_mm": 209.92, "confidence": 0.1931, "fallback": false},
  "total_ms": 1549.3,
  "output_px": [7610, 5384],
  "dpi": 461.8,
  "megapixels": 40.97
}
```

Worth watching in production: `coverage_pct` below 99 means a camera moved or a
page is outside the field; `spine.fallback` true on a spread means detection
failed; `skew_deg` near ±3 means the page is crooked enough to be clipping.

---

## Sessions

```python
from scanner.pipeline.run import process_session

manifest = process_session(captures, rig, "masters/", PipelineConfig())
```

Processes a whole book and writes `manifest.json` with per-spread reports,
elapsed time and the mean. `SpreadResult.save()` writes the master and pages as
**lossless TIFF** — widely readable, and it will still open in thirty years,
which is the entire point of a master.

---

## Performance

On a 2291 × 1620 canvas (tests' reduced scale):

| Stage | Time |
|---|---|
| Per-camera correction + rectify (×2) | ~810 ms |
| Blend | 121 ms |
| Deskew | 90 ms |
| Normalise | 386 ms |
| Spine + split | ~5 ms |
| **Total** | **~1 550 ms** |

Full 24 MP frames scale roughly with pixel count. The blueprint's target is
≤ 3 s per spread at the `standard` capture profile, and capture — not
processing — is the binding constraint: 24 MB per ARW over ~15 MB/s of USB 2.0
PTP is about 2 s per camera, which is why `standard` is a single frame.

---

## What is deliberately not here

**Super-resolution and OCR.** They are derivatives, and the archival master is
written before either runs. They belong in Phase 5–6, downstream of this module,
reading masters from disk.

**Stereo dewarping.** Implemented, but in `scanner/stereo/` rather than here.
Wiring it in as a mode of `process_spread` — surface recovery replacing
rectification — is the next structural change.

**RAW decoding.** Frames are read as 8-bit BGR. A real RAW path belongs before
`correct_frame` and would need 16-bit colour LUTs; see
[`architecture.md` §8](architecture.md#8-extension-points).
