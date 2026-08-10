# Stereoscopic page flattening

**What the scanner is actually for.** Two cameras that both see the whole
spread can measure the page's three-dimensional shape and flatten it
computationally — which is what makes a thick book scannable without
pressing it flat.

This document records the geometry that makes it possible, the algorithm,
and what it measures.

---

## 1. The design drifted, and this corrects it

Addendum C concluded that "dropping stereo is an upgrade" and moved to
laser triangulation. Blueprint v0.2 then adopted twin bodies for
**tiling**, and the stereo band survived only as a 20 mm strip described
as "held in reserve". That is what the first implementation built.

With 20 mm of overlap out of 420, **95 % of the spread is seen by one
camera only.** There is no depth to recover across the page, so
stereoscopic dewarping is not something that geometry can be extended
into. It changes the tiling.

Addendum C's rejection of stereo was correct *about the hardware it
tested* — an OAK-D Lite gives ~1250 µm, an OAK-D Pro ~370 µm, against a
50 µm budget. It was wrong as a general claim. Two A6000s at a 300 mm
baseline give **32 µm**.

---

## 2. The middle ground is strictly dominated

Widening the overlap to buy stereo coverage looks like the obvious
compromise. It is the worst available choice:

| Coverage | Overlap | DPI | Stereo cover | Baseline | δz |
|---|---|---|---|---|---|
| tile | 20 mm | 462 | 5 % | 200 mm | 27 µm |
| tile | 100 mm | 391 | 24 % | 160 mm | 46 µm |
| tile | 200 mm | 342 | 48 % | 110 mm | 85 µm |
| tile | 300 mm | 342 | 71 % | 60 mm | **157 µm** |
| **stereo** | full | 342 | **100 %** | 200 mm | **47 µm** |
| **stereo** | full | 342 | 100 % | 300 mm | **31 µm** |

Depth precision gets *worse* as coverage improves, then recovers
abruptly. The reason is structural, and it is now expressed in the
model:

> **When tiling, the baseline is the tile separation — you do not get to
> choose it.** Widen the tiles for overlap and the cameras move toward
> each other, so the baseline collapses. Only when both cameras see the
> whole page does the baseline become a free parameter.

`RigGeometry.baseline_mm` returns the forced value in `coverage="tile"`
and `stereo_baseline_mm` in `coverage="stereo"`. The collapse is asserted
in `test_tiling_forces_the_baseline_but_stereo_frees_it`.

---

## 3. What full-overlap stereo costs and buys

```
python -m scanner geometry   # (stereo mode: RigGeometry(coverage="stereo"))
```

```
2 x Sony A6000 @ 30 mm f/8  ->  A3 spread
  orientation        landscape  (limited by the short sensor axis)
  RESOLUTION         342.1 DPI   (5657 x 4000 px, 22.63 MP)
  working distance   601.2 mm
  depth of field     40.95 mm (+/-20.5 mm)
  stereo dz          47 um over 100 % of the page, convergence 18.9 deg
```

| | Tiling | Full-overlap stereo |
|---|---|---|
| Resolution | **462 DPI** | 342 DPI |
| Stereo coverage | 5 % | **100 %** |
| Depth precision | 27 µm (over 5 %) | **47 µm (everywhere)** |
| Depth of field | ±11.4 mm | **±20.5 mm** |
| Working distance | 453 mm | 601 mm |
| Curved pages | broken | **corrected** |

Two things are easy to miss. The longer standoff nearly **doubles the
depth of field**, so a thick book is far closer to being in focus before
any dewarping happens. And the cameras must be **converged**, not
parallel — parallel mounting would need a field almost twice the page
width and would throw away most of the sensor. The cross rail was specced
with adjustable baseline *and* toe-in, so it reaches this.

The resolution cost is real: 342 DPI is below the CZUR's 441. Two routes
back, one measurable now and one requiring hardware:

- **Fuse the two dewarped views.** Both cameras see every point, on
  different sampling grids, from different angles. After flattening into
  a common frame that is genuine sub-pixel diversity — unlike stacking
  from a rigid tripod, which is noise reduction only. `fuse_views` does
  the compositing; whether it recovers meaningful resolution is a
  measurement not yet made.
- **Four cameras, two stereo pairs**, each pair covering one half-spread:
  ~462 DPI *and* full stereo (≈36 µm at a 150 mm baseline).

---

## 4. Why dense stereo beats three laser lines

CZUR projects three laser lines across the page, recovers three height
profiles, and interpolates between them. That is a reasonable engineering
choice and it is also a very sparse measurement of a surface.

Passive stereo gives a depth estimate everywhere there is texture, which
on a text page is most of it. The comparison is not close in sample
count — and the developable constraint means the surface needs far fewer
degrees of freedom than the text does, so the samples are used
efficiently.

What lasers have that stereo does not is **texture independence**. A
blank margin gives stereo nothing. That is a real limitation and §6
describes how it is handled rather than hidden.

---

## 5. The algorithm

### The physics first

An open page is a **developable surface**: paper bends but does not
stretch. Its generators run parallel to the spine, so the height depends
on one coordinate:

```
z = h(x)
```

and the mapping back to a flat sheet is pure arc length:

```
s(x) = integral sqrt(1 + h'(x)^2) dx     from the spine
```

`s` is the paper coordinate — where a glyph was when the sheet was
printed. `x` is its shadow on the platen. The difference between them
*is* foreshortening. A 60 mm hardback is 427.3 mm of paper projecting to
420 mm; curvature hides 7.3 mm.

### Plane sweep over whole page columns

The obvious approach is SGBM on a rectified pair, then fit a surface to
the point cloud. That discards the strongest fact available: **every
pixel in a column at platen x sits at the same height.**

So instead: sweep candidate heights, and for each one project a whole
column of page positions into both views and correlate what they sample.
Hundreds of rows of evidence collapse into one number per `(x, height)`.

```python
from scanner.stereo import sweep_surface
sw = sweep_surface(image_a, image_b, cam_a, cam_b,
                   x_range_mm=(5, 415), height_range_mm=(-5, 45))
print(sw.report())
```

```
plane sweep: 274 columns, 201 height hypotheses (-5..45 mm)
  reliable columns   70 % (rejected 83 as texture-free)
  mean peak ncc      0.945
  mean peak width    1.66 mm
  recovered rise     30.50 mm at x = 210.5 mm        [true: 30.0 at 210.0]
```

### Peak height is not confidence

The first version of this used the correlation peak *value* as
confidence, and produced a confidently wrong surface with a mean NCC of
0.949. The cost curves showed why:

| x | true h | recovered | peak width |
|---|---|---|---|
| 120 mm | 6.3 mm | 6.5 mm | 1.5 mm |
| 180 mm | 23.4 mm | 23.5 mm | 1.5 mm |
| 240 mm | 23.0 mm | 23.0 mm | 1.5 mm |
| 210 mm (gutter) | 30.0 mm | 32.8 mm | **43.5 mm** |
| 380 mm (blank margin) | 0.9 mm | 1.0 mm | **43.0 mm** |

Every textured column was already correct to ~0.2 mm. The failures were
exactly the texture-free ones — the dark gutter and the outer margins —
and they scored 0.85–0.95 anyway, because **two smooth gradients
correlate beautifully at any assumed depth.**

What separates them is peak *sharpness*. A textured column peaks over
~1.5 mm; a blank one is flat across the entire 50 mm search range. The
confidence test is now peak width, and columns that fail it are
interpolated across rather than believed.

### Interpolating the gutter

The largest rejected gap is the gutter itself, which sits on the *apex*
of the curve. A straight chord across a peak cuts the corner and
systematically under-reads the spine height, so the reliable columns feed
a confidence-weighted smoothing spline instead.

This matters less than it appears. Arc length integrates the **slope**,
not the height, so getting the apex slightly wrong costs little: the
recovered surface has 94 µm rms height error but only **71 µm rms paper-
coordinate error**.

### Flattening

Once the surface is known, dewarping is a lookup, not an estimate:

```
flat page (s, y)  --developable-->  3-D (x, y, z)  --camera-->  image px
```

Every output pixel's paper coordinate determines where that paper
physically is, and the camera model says which pixel photographed it.
All the uncertainty lives in the surface, and it has already been
measured.

### Fusing the two views

Both cameras see every point, so each output pixel has two candidate
sources weighted by how squarely each camera faces the paper there:

| Distance from spine | Near camera | Far camera |
|---|---|---|
| 10 mm | **21.5°** off-normal | 40.9° |
| 30 mm | **12.8°** | 31.9° |
| 60 mm | **5.7°** | 24.4° |

The near-side camera always wins on its own page half — which is the
tiling assignment, arrived at from physics rather than convenience. The
far camera earns its place through depth, and through filling what the
near one occludes or glares out.

---

## 6. Results

Synthetic 60 mm hardback (30 mm spine rise), two converged A6000s at a
200 mm baseline, rendered at 1/4 scale.

### Surface recovery

| | Achieved |
|---|---|
| Recovered spine rise | 30.50 mm (true 30.00) |
| Recovered spine position | 210.5 mm (true 210.0) |
| Height error | 94 µm rms, 524 µm max |
| **Paper-coordinate error** | **71 µm rms, 93 µm max** |

### Geometric distortion after flattening

Measured by block phase-correlation against the ground-truth dewarp. A
rigid shift of the whole page is a crop, not a distortion, so it is
removed before reporting:

| | Raw | Residual distortion |
|---|---|---|
| **Stereo dewarp** | 1.95 px | **0.08 px median, 0.36 max** |
| Homography (page assumed flat) | 14.09 px | **8.74 px median, 25.4 max** |

**Roughly 110× less residual distortion**, and 19 µm in physical units.
The stereo residual is almost entirely the rigid 1.95 px offset.

### What a homography leaves behind

A homography is a plane-to-plane map. A point *h* above the plane, *x*
off the optical axis, is displaced by `x·h/(z₀−h)` — pure perspective,
which no plane-to-plane transform can correct:

| Page rise | at x = 50 mm | at x = 100 mm |
|---|---|---|
| 10 mm | 1.1 mm | 2.3 mm (41 px) |
| 30 mm | 3.6 mm | 7.1 mm (129 px) |

In a *tiling* rig the spine sits ~100 mm off-axis for both cameras on
opposite sides, so they displace it in opposite directions: at a 30 mm
rise the two views disagree by 14.2 mm — **258 px**, against an
acceptance criterion of < 1 px. That disagreement is not noise. It is
the parallax that measures the height, and using it is the whole idea.

---

## 7. Limits, stated plainly

**Textureless regions carry no depth.** The gutter and blank margins are
interpolated from their neighbours under the developable prior. On a
page that is mostly blank — a plate, a title page — the surface is
essentially extrapolated. Lasers do not have this problem, which is why
the €30 DOE module stays on the table as a supplement rather than a
replacement.

**The surface model is `h(x)` only.** It cannot represent cockling, a
dog-eared corner, or a page that curls differently at the head and tail.
`BookSurface.y_taper` renders that case so it can be measured; recovering
it needs a 2-D surface and is not implemented.

**Self-occlusion is not handled.** At the tilts a real book reaches
(≤ 33° in the modelled range) no part of the page hides another from a
near-nadir camera. A tightly bound tome approaching vertical at the spine
would break that assumption, and the ray-surface intersection would
silently return the first root.

**Resolution is scale-dependent.** Recovery degrades with pixel count —
dropping the render scale from 0.25 to 0.20 roughly quadruples the
paper-coordinate error. The real rig runs at full resolution and should
do better than the figures here, but that is an expectation, not a
measurement.

**None of this has seen a real photograph.** Every number above comes
from the simulator. It carries ground truth, which is why the numbers can
be trusted as far as the *model* goes — but a real page has effects the
model does not include.

---

## 8. Using it

```python
from scanner.stereo import sweep_surface, dewarp_stereo, cameras_from_capture
from scanner.synth.render3d import stereo_pair_poses, camera_matrix, render_surface
from scanner.synth.surface import BOOKS

# recover the surface from a stereo pair
sw = sweep_surface(img_a, img_b, cam_a, cam_b,
                   x_range_mm=(5, 415), height_range_mm=(-5, 45))

# flatten and fuse
page = dewarp_stereo(img_a, img_b, cam_a, cam_b, sw.surface,
                     s_range_mm=(-190, 190), y_range_mm=(8, 289),
                     page_px_per_mm=18.18)
```

`cam_a` / `cam_b` are `{K, dist, R, C}` — camera matrix, distortion,
world→camera rotation, and camera centre in platen millimetres.

To generate a test case with ground truth:

```python
surf = BOOKS["hardback"]                 # or paperback / tome / flat
paper = surf.page_span_mm(0, 420)        # the page is LONGER than its shadow
page, truth = render_page(paper, 297.0, px_per_mm=6.0, spread=True)
pa, pb = stereo_pair_poses(baseline_mm=200.0, height_mm=600.0)
cap = render_surface(page, 6.0, surf, K, dist, pa, size, defects)
cap.height_mm, cap.page_s_mm      # the answer key
```

---

## 9. What is not done

- **Calibrating a real stereo pair.** Relative pose between the bodies
  must be solved, not assumed. The existing per-camera document
  homographies give it up to the platen plane; a proper `stereoCalibrate`
  path is the next piece.
- **Wiring it into `pipeline/run.py`.** The stereo path exists alongside
  the tiling pipeline, not inside it.
- **Measuring whether view fusion recovers resolution.** The mechanism is
  there; the number is not.
- **A y-varying surface**, for cockling and corners.
