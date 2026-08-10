# Blueprint v1.1 — Stereoscopic Overhead Document Scanner
## Twin Sony A6000 · Dual Raspberry Pi 5 · Laptop GPU pipeline

**Status: specification of record.** Supersedes Blueprint v1.0. v1.0 remains
in the repository as the tiling design and as the record of how the project
got here; where the two disagree, this document wins.

---

## 0. What changed, and why

**Blueprint v1.0 described a tiling rig.** Two cameras, each taking half the
spread with a 20 mm overlap, producing 462 DPI across an A3 page assumed to be
flat. The stereo band was described as "free stereo, held in reserve."

That was a drift from the project's purpose. This is a **stereoscopic** scanner:
the two cameras exist so the page's three-dimensional shape can be *measured*
and the page flattened computationally. A 20 mm overlap out of 420 mm means
95 % of the spread is seen by one camera only, so there is no depth to recover
and no dewarping is possible. Stereoscopic operation is not an extension of the
v1.0 geometry — it replaces the tiling.

Two supporting errors in the record, both now corrected:

**Addendum C rejected stereo on evidence that did not generalise.** It measured
an OAK-D Lite (~1250 µm) and an OAK-D Pro (~370 µm) against a 50 µm budget and
concluded that "dropping stereo is an upgrade." Two A6000s at a 300 mm baseline
give **32 µm**. The rejection was correct about that hardware and wrong as a
general claim.

**The pipeline assumed a plane everywhere, not just at the gutter.**
`rectify_to_document` is a homography — a plane-to-plane map. It treats the
entire spread, gutter included, as flat. A thick book's page is not a plane and
cannot be pressed into one near the spine.

---

## 1. Two modes, one rig

The rig now has two configurations. Both are supported; the choice is a real
engineering trade, not a preference.

| | **Stereo** (default for books) | **Tile** (flat material) |
|---|---|---|
| Each camera covers | the whole spread | half the spread |
| Resolution | 342 DPI, 22.6 MP | **462 DPI, 41.2 MP** |
| Depth coverage | **100 %** | 5 % (the overlap strip) |
| Depth precision | 47 µm at 200 mm baseline | 27 µm, but only in the strip |
| Working distance | 601 mm | 453 mm |
| Depth of field | **±20.5 mm** | ±11.4 mm |
| Camera mounting | converged, ~19° | parallel, toe-in 0° |
| Curved pages | **measured and flattened** | assumed flat — breaks |
| Use for | bound books, anything with curvature | loose sheets, thin material under a platen |

```python
from scanner.geometry import RigGeometry, A3

RigGeometry(coverage="stereo", stereo_baseline_mm=200.0, document=A3)   # books
RigGeometry(coverage="tile", overlap_mm=20.0, document=A3)              # flat
```

---

## 2. Why there is no middle ground

The obvious compromise is to widen the overlap until stereo covers the curved
region. It is the worst available choice:

| Coverage | Overlap | DPI | Stereo cover | Baseline | δz |
|---|---|---|---|---|---|
| tile | 20 mm | 462 | 5 % | 200 mm | 27 µm |
| tile | 100 mm | 391 | 24 % | 160 mm | 46 µm |
| tile | 200 mm | 342 | 48 % | 110 mm | 85 µm |
| tile | 300 mm | 342 | 71 % | 60 mm | **157 µm** |
| **stereo** | full | 342 | **100 %** | 200 mm | **47 µm** |
| **stereo** | full | 342 | 100 % | 300 mm | **31 µm** |

Depth precision gets *worse* as coverage improves, then recovers abruptly. The
reason is structural:

> **When tiling, the baseline *is* the tile separation — it is not yours to
> choose.** Widen the tiles to gain overlap and the cameras move toward each
> other, collapsing the baseline. Only when both cameras see the whole page
> does the baseline become a free parameter.

You either tile for maximum resolution and accept a flat-page assumption, or
you go to full overlap and get depth everywhere. Everything between is worse on
both axes.

---

## 3. Optical configuration — stereo mode

```
2 × Sony A6000 @ 30 mm f/8  →  A3 spread, full overlap
  orientation        landscape (limited by the short sensor axis)
  RESOLUTION         342.1 DPI   (5657 × 4000 px, 22.63 MP per camera)
  working distance   601.2 mm
  baseline           200 mm      convergence 18.9°
  depth of field     40.95 mm (±20.5 mm)
  Airy disc          2.75 px     diffraction limited
  depth precision    47 µm over 100 % of the page
```

**Cameras must be converged, not parallel.** Parallel mounting with both
cameras required to cover the full spread would need a field almost twice the
page width and would throw away most of the sensor. The cross rail was specced
with adjustable baseline *and* toe-in; this is what that adjustability is for.

**The longer standoff nearly doubles depth of field** — ±20.5 mm against
±11.4 mm — so a thick book is far closer to being in focus before any dewarping
happens. This partly offsets the resolution loss and was not anticipated.

**Baseline choice.** 200 mm meets the 50 µm budget; 300 mm gives 31 µm. Wider
baselines improve depth but increase obliquity and occlusion risk near a steep
gutter. 200–300 mm is the working range.

---

## 4. The resolution cost, and how to recover it

342 DPI is below the CZUR's 441. This is the honest price of measuring the page
instead of assuming it. Two routes back:

**Fuse the two dewarped views.** Both cameras see every point, on different
sampling grids, from different angles. After flattening into a common frame
that is genuine sub-pixel diversity — unlike stacking from a rigid tripod,
which is noise reduction only because nothing dithers. The fusion mechanism is
implemented (`fuse_views`); whether it recovers meaningful resolution is
**not yet measured** and should not be claimed until it is.

**Four cameras, two stereo pairs**, each pair covering one half-spread:
≈462 DPI *and* full stereo (≈36 µm at a 150 mm baseline). Two more bodies.
This is the configuration that beats the CZUR on resolution and geometry
simultaneously, and it is the endgame the rail should be built to reach.

Note also that the comparison is not like-for-like: on a curved page the CZUR's
441 DPI is itself degraded by foreshortening and by interpolating between three
laser profiles.

---

## 5. Surface model

An open page is a **developable surface** — paper bends but does not stretch.
Its generators run parallel to the spine, so height depends on one coordinate:

```
z = h(x)
```

and the mapping back to a flat sheet is pure arc length:

```
s(x) = ∫ √(1 + h′(x)²) dx     measured from the spine
```

`s` is the paper coordinate — where a glyph sat when the sheet was printed.
`x` is its shadow on the platen. The difference between them **is**
foreshortening. A 60 mm hardback is 427.3 mm of paper projecting to 420 mm;
curvature hides 7.3 mm.

Reference books modelled in `scanner/synth/surface.py`:

| Book | Rise | Decay | Max tilt | Outside ±11.4 mm DOF |
|---|---|---|---|---|
| 30 mm paperback | 15 mm | 30 mm | 14° | 11 % of the spread |
| 60 mm hardback | 30 mm | 40 mm | 21° | 31 % |
| 90 mm tome | 45 mm | 35 mm | 33° | 34 % |

---

## 6. What a homography cannot do

A point *h* above the plane, *x* off the optical axis, is displaced by
`x·h/(z₀−h)`. That is pure perspective. No plane-to-plane transform corrects a
per-point height:

| Page rise | Displacement at x = 50 mm | at x = 100 mm |
|---|---|---|
| 10 mm | 1.1 mm | 2.3 mm (41 px) |
| 30 mm | 3.6 mm | 7.1 mm (129 px) |

In the tiling configuration the spine sits ~100 mm off-axis for **both**
cameras, on opposite sides, so they displace it in opposite directions. At a
30 mm rise the two views disagree by 14.2 mm — **258 px**, against an
acceptance criterion of < 1 px.

That disagreement is not noise. It is the parallax that measures the height,
and using it is the entire point of the rig.

---

## 7. Surface recovery — plane sweep

The generic approach is SGBM on a rectified pair, then fit a surface to the
point cloud. That discards the strongest available fact: **every pixel in a
column at platen x sits at the same height.**

Instead: sweep candidate heights; for each, project a whole column of page
positions into both views and correlate what they sample. Hundreds of rows
collapse into one number per `(x, height)`.

**Confidence is peak sharpness, not peak height.** A textured column peaks over
~1.5 mm; a blank one is flat across the entire search range while still scoring
0.95, because two smooth gradients correlate beautifully at any assumed depth.
Columns failing the sharpness test are interpolated across under the
developable prior rather than believed.

The largest rejected gap is the gutter itself, which sits on the *apex* of the
curve, so a confidence-weighted smoothing spline is used rather than a chord —
a straight line across a peak cuts the corner and under-reads the spine height.

This matters less than it appears: arc length integrates the **slope**, not the
height, so a wrong apex costs little. Measured: 94 µm rms height error but only
**71 µm rms paper-coordinate error**.

---

## 8. Flattening and fusion

Once the surface is known, dewarping is a lookup rather than an estimate:

```
flat page (s, y)  ──developable──▶  3-D (x, y, z)  ──camera──▶  image px
```

All the uncertainty lives in the surface, and it has already been measured.

Each output pixel has two candidate sources, weighted by how squarely each
camera faces the paper there:

| Distance from spine | Near camera | Far camera |
|---|---|---|
| 10 mm | **21.5°** off-normal | 40.9° |
| 30 mm | **12.8°** | 31.9° |
| 60 mm | **5.7°** | 24.4° |

The near-side camera always wins on its own page half — the tiling assignment,
arrived at from physics rather than convenience. The far camera earns its place
through depth and through filling what the near one occludes or glares out.

---

## 9. Measured results (synthetic, 60 mm hardback)

| | Recovered | True |
|---|---|---|
| Spine rise | 30.50 mm | 30.00 mm |
| Spine position | 210.5 mm | 210.0 mm |
| Height error | 94 µm rms, 524 µm max | — |
| **Paper-coordinate error** | **71 µm rms, 93 µm max** | — |

Residual geometric distortion after flattening, measured by block phase
correlation against ground truth. A rigid shift of the whole page is a crop,
not a distortion, so it is removed before reporting:

| | Raw | Residual distortion |
|---|---|---|
| **Stereo dewarp** | 1.95 px | **0.08 px median, 0.36 max** |
| Homography (assumed flat) | 14.09 px | **8.74 px median, 25.4 max** |

Roughly **110× less residual distortion** — 19 µm in physical units.

---

## 10. Hardware, amended

Unchanged from v1.0 except where noted.

| Component | Spec | Change |
|---|---|---|
| 2 × Sony A6000 | 24.3 MP APS-C | — |
| 2 × E-mount kit lens | locked at 30 mm, f/8 | — |
| **Cross rail** | adjustable baseline **and toe-in** | Toe-in is now *required*, not optional |
| Mast height | **601 mm** stereo / 453 mm tile | Must reach both |
| 2 × Raspberry Pi 5 + AI HAT+ | 8 GB, Pi OS Lite | — |
| Platen | 3 mm acrylic | Now **optional** — used for flat material, omitted for books |
| Lamps | high-CRI, diffuse, grazing | — |

**The platen changes role.** In v1.0 it was load-bearing: it made the page flat
so the homography was valid. In stereo mode it is optional, and for a fragile
or thick binding it should be omitted — which was the point of the objection
that produced this revision.

---

## 11. Acceptance criteria, amended

| Metric | Tile mode | Stereo mode |
|---|---|---|
| Measured DPI | ≥ 450 | ≥ 335 |
| MTF50, page corners | ≥ 0.30 cy/px | ≥ 0.30 cy/px |
| Stitch seam error | < 1 px | n/a |
| **Residual distortion after dewarp** | n/a | **< 1 px** |
| **Recovered paper coordinate** | n/a | **< 150 µm rms** |
| Colour ΔE between cameras | < 2 | < 2 |
| Throughput at `standard` | ≤ 3 s/spread | ≤ 3 s/spread |

**The benchmark that settles the project** is unchanged in spirit but now has a
sharper form: the same 20-page **bound** book on both machines, measuring
MTF50, OCR character error rate, colour ΔE, and — the one that matters here —
**dewarp residual near the gutter**, which is where a laser-triangulating
scanner interpolating between three profiles should be beatable by dense
stereo.

---

## 12. What is built, and what is not

**Built and verified against ground truth:** developable surface model with
arc-length unwrap; ray-traced curved-page renderer carrying true height and
true paper coordinate; plane-sweep surface recovery with sharpness-based
confidence; flattening and obliquity-weighted fusion; stereo coverage mode in
the optical model. 106 tests.

**Not built:**

- **Real stereo calibration.** Relative pose between the bodies is assumed, not
  solved. A `stereoCalibrate` path is the next piece and is required before any
  real capture.
- **Not wired into `pipeline/run.py`.** The stereo path exists alongside the
  tiling pipeline rather than inside it.
- **View fusion is unmeasured** — the mechanism exists, the resolution recovery
  number does not.
- **Surface model is h(x) only** — no cockling, no dog-eared corners, no
  self-occlusion handling for near-vertical gutters.
- **Nothing has seen a real photograph.** Every number here comes from the
  simulator. It carries ground truth, so the numbers are trustworthy as far as
  the *model* goes — a real page has effects the model does not include.

---

## 13. Retained from v1.0 without change

Capture architecture (two Pi nodes, sequence-counter pairing, mock/gphoto2
backends), camera settings, throughput budget, colour and flat-field
calibration, the metrics harness, the archival-master/access-copy split, and
the Phase 5–6 plans. See [`blueprint-v1.0.md`](blueprint-v1.0.md).

---

*Blueprint v1.1 · 2026-08-10 · all dimensions in millimetres*
