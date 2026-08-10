# Calibration

Four independent things get measured, in this order, and each one's output is
what the next one needs. None of it is optional; skipping any step produces a
specific, recognisable defect described below.

| Step | Solves | Target | Cost |
|---|---|---|---|
| 1. Intrinsics | `K`, distortion coefficients | ChArUco board, 12–16 tilted views | ~30 min |
| 2. Document alignment | mm → px homography, per camera | one board flat on the platen | ~15 min |
| 3. Flat-field | vignetting and lamp falloff | 3 shots of a blank sheet | ~10 min |
| 4. Colour | 3×3 matrix, per camera | colour chart | ~15 min |

Do it once, properly, **after** the zoom and focus rings are taped. Everything
here becomes fiction the moment the lens moves, and no software can detect that
it happened.

---

## 0. The target

```bash
python -m scanner board -o charuco_a4.png --dpi 600
```

Default is a **7 × 10 ChArUco board of 26 mm squares** — a 182 × 260 mm pattern
that prints on A4 with a 6 mm quiet zone, and fills a useful fraction of the
frame at the rig's working distance.

ChArUco rather than a plain chessboard because it tolerates partial views: you
can push the board into a frame corner, where distortion is largest and points
matter most, and still get a solve.

### Three rules for the printed target

**Print at 100 % scale.** "Fit to page" silently rescales, and that error goes
into everything downstream.

**Measure a square with a ruler.** It should be 26.0 mm. If it is 25.4, your
printer scaled it by 97.7 % and every subsequent measurement is wrong by the
same factor — invisibly, because the calibration will be perfectly
self-consistent.

**Mount it on something rigid and flat.** Foam board, MDF, a clipboard. A
curled sheet is a curved lens, and the solver will happily absorb the curl into
your distortion coefficients.

> **The bug that motivated `rendered_extent_mm()`**
>
> `cv2.aruco.CharucoBoard.generateImage(outSize, marginSize)` treats `outSize`
> as the **total image size including the margin**. Sizing it to the pattern
> alone draws the squares 6.6 % small. That is a pure scale error in the
> document homography and is invisible until you physically measure a page —
> it first showed up here as a "−6.5 % mounting scale error" that was actually
> a rendering bug. `BoardSpec.rendered_extent_mm(margin)` reports the true
> physical extent of the rendered image so the two can never be confused again.

---

## 1. Intrinsics

```python
from scanner.calib.intrinsics import A4_BOARD, calibrate_intrinsics

calib, diag = calibrate_intrinsics(images, A4_BOARD, camera_id="cam0")
```

Returns a `CameraCalibration` and a diagnostics dict. **Read the diagnostics.**

```python
{
  "views_supplied": 16, "views_used": 16,
  "corners_per_view": [52, 46, 49, ...],
  "rms_px": 0.103,
  "per_view_mean_px": [...],   # find the one bad frame
  "worst_view": 7,
  "tilt_deg_min": 8.1, "tilt_deg_max": 32.3,
  "tilt_warning": None,
}
```

### How to shoot the views

**Tilt the board — 20–30° in both axes.** This is the one that matters.
Intrinsics cannot be solved from a fronto-parallel plane: focal length and
distance trade off exactly, and you get a confident, wrong answer. If every
view is flat, `tilt_warning` fires:

> *the board was nearly flat in every view; focal length is poorly conditioned
> — tilt it 20–30 degrees and reshoot*

**Fill the corners.** Distortion is largest at the frame edge. A stack that
only covers the middle leaves `k1` unconstrained.

**Vary distance by ±15 %.** Helps separate focal length from principal point.

**Keep it sharp and evenly lit.** A blurred board gives blurred corners, and
sub-pixel refinement cannot recover what the optics threw away.

**12–16 views.** Six is the enforced minimum (`min_views`) and is not enough
for a rig you intend to trust.

### What good looks like

From the synthetic rehearsal with 12–16 well-spread views:

| Quantity | Achieved |
|---|---|
| Reprojection RMS | 0.10 px |
| `fx`, `fy` recovery | 0.04 % error |
| `cx` recovery | 0.07 % |
| `cy` recovery | 0.57 % |
| `k1` recovery | 0.1 % |

`k2`, `p1` and `p2` are recovered less precisely — `k2` trades against `k1`, and
tangential terms are small on a well-centred lens. This is normal and does not
matter: what the pipeline consumes is the *undistortion map*, and that is
dominated by `k1`.

`k3` is fixed to zero by default (`fix_k3=True`). A kit lens at 30 mm does not
need a sixth-order term, and fitting one on limited data mostly fits noise.

### Reading a bad result

| Symptom | Cause |
|---|---|
| RMS > 1 px | Motion blur, a mis-detected board, or the wrong `BoardSpec` |
| `tilt_warning` set | Every view fronto-parallel — focal length is unconstrained |
| `views_used` ≪ `views_supplied` | Board out of frame, underexposed, or too small in frame |
| Low RMS but wrong focal length | Almost always the flat-stack problem, or a mis-scaled print |
| One `per_view_mean_px` ≫ others | Drop that frame (`worst_view`) and refit |

---

## 2. Document alignment

This is the step that replaces runtime feature-based stitching.

Lay the board **flat on the platen**, at a known position, and shoot it once
with each camera.

```python
from scanner.calib.align import BoardPlacement, solve_document_homography, mounting_error

place = BoardPlacement(origin_mm=(x0, y0), rotation_deg=0.0)
result = solve_document_homography(image, calib, place, A4_BOARD)
print(result.rms_px, result.n_points, result.covered_mm)

print(mounting_error(calib, doc_short_mm=297.0))
```

`origin_mm` is the document coordinate of the board pattern's own (0, 0) outer
corner. The board's chessboard corners are already expressed in board
millimetres, so the mapping is a rigid transform.

The image may be raw (distorted); detected corners are undistorted
**analytically** rather than by undistorting the whole frame and re-detecting,
which avoids a resampling step between the measurement and the model.

### Why millimetres matter

Because the homography is solved in physical units, the two cameras end up in a
shared metric frame automatically. There is no separate extrinsics step, no
camera-to-camera transform to estimate, and no scale ambiguity. This is the
invariant the whole stitching design rests on.

### `mounting_error` — numbers you can act on

```
cam0: rms 0.216 px | mount dx -1.56 dy +1.76 mm rot -0.232 deg scale +0.050 %
```

| Field | Means | Act when |
|---|---|---|
| `dx_mm`, `dy_mm` | Camera is off its nominal tile centre | > ~5 mm — slide it on the rail |
| `rotation_deg` | Body rotated about the optical axis | > ~1° — the spare field is being eaten |
| `scale_error_pct` | Working distance is not what you think | > ~0.3 % — re-measure the mast height |
| `keystone` | Camera is not perpendicular to the platen | Non-zero — shim the mount |

Software corrects all of these. But large values mean the spare field is being
consumed by correction rather than by page skew, and a keystone term means the
depth of field is not parallel to the page — which no homography fixes.

In the synthetic rehearsal, mounting rotation is recovered to **0.016°** and
scale to **0.05 %**.

---

## 3. Flat-field

```python
from scanner.calib.field import calibrate_flat_field

gain = calibrate_flat_field([shot1, shot2, shot3], calib)
```

Three shots of an evenly lit blank sheet at the working height. The map is
heavily smoothed on purpose — we want the lens and lamp falloff, not the dust
on the sheet — and normalised so the frame centre is gain 1.0, which keeps
exposure unchanged.

Gains are clipped to [1/4, 4] so a dark corner cannot explode into noise
amplification.

**Skip this and** the two tiles are darker at their outer edges, so the
stitched spread has a bright band down the middle where two frame-centres meet.
It looks like a lighting problem and is not.

Note the correction is applied in encoded space and this is exact: a linear
vignette `v` appears as `v^(1/γ)` after encoding, which is still multiplicative.

---

## 4. Colour

```python
from scanner.calib.field import calibrate_colour

M = calibrate_colour(image, patch_rois, reference_srgb, calib)
```

Fits a least-squares 3×3 mapping measured → reference **in linear light**.
Fitting in gamma space makes the matrix depend on exposure, so it stops working
the moment the lamps change.

### Why this is the step people regret skipping

Two A6000 bodies of the same model differ by a few percent per channel. In
isolation that is invisible. On a stitched spread it lands as a **straight
vertical band down the middle of every page** — and a hard edge is far more
visible than a gradual shift of the same magnitude.

`test_colour.py` quantifies it: the simulator's second body (gains 1.035 /
0.995 / 0.962 — entirely realistic) produces a mean ΔE2000 around 1. That is
under the ΔE < 2 acceptance threshold and still visible as a seam, which is why
the threshold is on the *cross-camera* difference and not just the absolute one.

```python
from scanner.metrics import cross_camera_delta_e
cross_camera_delta_e(image_a, rois_a, image_b, rois_b)   # the number that matters
```

Synthetic result after correction: per-camera ΔE **0.16 / 0.34**, cross-camera
**0.32**.

---

## 5. Persisting it

```python
from scanner.calib.store import RigCalibration

rig = RigCalibration(doc_long_mm=420, doc_short_mm=297,
                     target_px_per_mm=18.1818, overlap_mm=20.0)
rig.cameras["cam0"] = calib
rig.save("calibration/rig.json")     # writes rig.json + rig.npz
print(rig.summary())

rig = RigCalibration.load("calibration/rig.json")
```

Keep the JSON in version control. When a seam starts misbehaving in three
months, the diff against today's file is the fastest way to find out what moved.

---

## 6. Complete example

There is no `scanner calibrate` subcommand yet — it is the next thing on the
roadmap. Until then:

```python
"""Calibrate one camera end to end."""
import cv2, glob
from scanner.geometry import RigGeometry, A4
from scanner.calib.intrinsics import A4_BOARD, calibrate_intrinsics
from scanner.calib.align import BoardPlacement, solve_document_homography, mounting_error
from scanner.calib.field import calibrate_flat_field, calibrate_colour
from scanner.calib.store import RigCalibration
from scanner.synth.page import COLORCHECKER_SRGB

geom = RigGeometry(n_cameras=1, document=A4)

# 1. intrinsics
views = [cv2.imread(p) for p in sorted(glob.glob("calib/board_*.jpg"))]
calib, diag = calibrate_intrinsics(views, A4_BOARD, "cam0")
print(f"rms {diag['rms_px']:.3f} px, {diag['views_used']} views, "
      f"tilt {diag['tilt_deg_min']}-{diag['tilt_deg_max']} deg")
assert diag["tilt_warning"] is None, diag["tilt_warning"]

calib.tile_x0_mm, calib.tile_x1_mm = geom.tiles[0].x0_mm, geom.tiles[0].x1_mm

# 2. alignment -- board laid with its pattern origin at (40, 20) mm
flat_shot = cv2.imread("calib/board_on_platen.jpg")
align = solve_document_homography(
    flat_shot, calib, BoardPlacement(origin_mm=(40.0, 20.0)), A4_BOARD)
print(f"align rms {align.rms_px:.3f} px")
print(mounting_error(calib, geom.document.short_mm))

# 3. flat-field
calibrate_flat_field([cv2.imread(p) for p in glob.glob("calib/blank_*.jpg")], calib)

# 4. colour -- rois located however you prefer
calibrate_colour(cv2.imread("calib/chart.jpg"), patch_rois, COLORCHECKER_SRGB, calib)

rig = RigCalibration(doc_long_mm=geom.document.long_mm,
                     doc_short_mm=geom.document.short_mm,
                     target_px_per_mm=geom.px_per_mm,
                     overlap_mm=geom.overlap_mm,
                     provenance={"operator": "catalin", "lens": "E 18-55 @ 30 mm f/8"})
rig.cameras["cam0"] = calib
rig.save("calibration/rig.json")
print(rig.summary())
```

To see the whole flow working end to end against simulated cameras, read
[`scanner/selftest.py`](../scanner/selftest.py) — it is the same sequence with
ground truth attached at every step.

---

## 7. When to recalibrate

| Event | Intrinsics | Alignment | Flat-field | Colour |
|---|---|---|---|---|
| Zoom or focus ring moved | ✅ | ✅ | ✅ | — |
| Camera bumped on the rail | — | ✅ | — | — |
| Mast height changed | — | ✅ | — | — |
| Aperture changed | — | — | ✅ | — |
| Lamps moved or replaced | — | — | ✅ | ✅ |
| Different body swapped in | ✅ | ✅ | ✅ | ✅ |
| Nothing changed, 3 months passed | — | worth verifying | — | — |

Alignment is cheap — one photograph — so verify it whenever a seam looks
suspicious. Intrinsics are half an hour, so tape the rings.
