# Optics

The complete optical model, with derivations. Everything in this document is
implemented in [`scanner/geometry.py`](../scanner/geometry.py) and asserted in
[`tests/test_geometry.py`](../tests/test_geometry.py) — the numbers here are
printed by the code, not transcribed from a notebook.

```bash
python -m scanner geometry --compare
python -m scanner geometry -n 1 -f A4 --json
```

---

## 1. The problem

An A3 spread is 420 × 297 mm. The CZUR ET Ultra captures it at 441 DPI and
39.8 MP. To beat that with commodity parts and no moving mechanism, we need to
land more than 441 × 420/25.4 ≈ 7 290 pixels across the long axis, in one
exposure.

A single 24 MP APS-C sensor cannot do it. Two of them, each taking half the
long axis, comfortably can — but only if the sensor is held the right way up,
which turns out to be the whole argument.

---

## 2. Tiling

`n` cameras split the long axis into `n` tiles that overlap by `overlap` at each
of the `n − 1` internal joins:

```
n · w − (n − 1) · overlap = L
```

so

```
w = (L + (n − 1) · overlap) / n
```

For the design point (n = 2, L = 420 mm, overlap = 20 mm): **w = 220 mm**.

Tile *i* spans `[i·(w − overlap), i·(w − overlap) + w]`. Adjacent tile centres
are separated by `L − w`, which for the design point gives a **200 mm camera
baseline** — and because the cameras are parallel and vertical, that is also the
physical rail spacing. There is no toe-in.

---

## 3. Sensor orientation — the decisive choice

The Sony A6000 sensor is 6000 × 4000 px on 23.5 × 15.6 mm. A tile needs to cover
`w` millimetres across the split axis and `H = 297 mm` along the other.

There are two ways to point it:

| Orientation | Across the split axis | Along the short axis |
|---|---|---|
| **portrait** | the **4000 px** axis spans `w` | the 6000 px axis spans `H` |
| **landscape** | the 6000 px axis spans `w` | the 4000 px axis spans `H` |

Sampling density is set by whichever axis runs out first:

```
px_per_mm = min( across_px / w , along_px / H )
```

**Two cameras, w = 220 mm:**

- portrait: `min(4000/220, 6000/297) = min(18.18, 20.20) = 18.18 px/mm` → **461.8 DPI**
- landscape: `min(6000/220, 4000/297) = min(27.27, 13.47) = 13.47 px/mm` → 342 DPI

**One camera, w = 420 mm:**

- portrait: `min(4000/420, 6000/297) = 9.52 px/mm` → 242 DPI
- landscape: `min(6000/420, 4000/297) = 13.47 px/mm` → **342 DPI**

The choice flips. Stated as an aspect-ratio problem: splitting the long axis
asks the sensor for a **297 : 220 = 1.35** aspect against its native 1.50 — a
comfortable fit that wastes 11 % of the field. Splitting the short axis instead
asks for **420 : 158 = 2.65** against 1.50, throws away most of the frame and
lands near 340 DPI.

This is not a stylistic preference. It is the difference between beating the
CZUR and losing to it. `RigGeometry.orientation="auto"` (the default) evaluates
both and picks the better one; `resolved_orientation` and `binding_axis` report
what it chose and why.

```python
>>> BLUEPRINT_V1.resolved_orientation, BLUEPRINT_V1.binding_axis
('portrait', 'short')
>>> SINGLE_CAMERA_A3.resolved_orientation
'landscape'
```

---

## 4. Resolution

```
px_per_mm = 18.1818            DPI = px_per_mm × 25.4 = 461.8
spread    = 420 × 18.18  ×  297 × 18.18  =  7636 × 5400 px  =  41.23 MP
```

Against the CZUR's 441 DPI / 39.8 MP, that is **+4.7 % linear resolution and
+3.6 % pixels**, with no tiling stage and no moving parts.

---

## 5. Magnification and working distance

The binding axis is the short sensor axis (15.6 mm over the 220 mm tile):

```
m = sensor_mm(binding) / object_mm(binding) = 15.6 / 220 = 0.07091
```

Equivalently `m = pitch × px_per_mm`, which is how the code computes it.

Thin-lens object distance from the lens principal plane:

```
d_o = f · (1 + 1/m) = 30 × (1 + 14.103) = 453.1 mm
```

Focal length expressed in pixels — the `f` of the pinhole camera matrix, and the
number every calibration and stereo formula actually wants:

```
f_px = f / pitch = 30 mm / 3.90 µm = 7692 px
```

### Spare field

The non-binding axis over-covers:

```
field_along = sensor_mm(other) / m = 23.5 / 0.07091 = 331.4 mm
spare       = 331.4 − 297 = 34.4 mm   (11 % of the frame)
```

**This is not waste to be optimised away.** It is the margin that lets deskew
and crop operate on a page that is never perfectly square to the rig. Removing
it would mean any book laid down 1° crooked loses a corner.

---

## 6. Depth of field

```
DOF = 2 · N · c · (1 + m) / m²
```

with `N` the f-number and `c` the circle of confusion. The conventional APS-C
CoC (19 µm, ~4.9 px) is a print-viewing criterion and far too loose here — it
would claim ~64 mm of depth of field. A document scanner is judged at the pixel
level, so the model uses **c = 1.72 px ≈ 6.7 µm**:

```
DOF = 2 × 8 × 0.0067 × 1.0709 / 0.005028 = 22.86 mm   (±11.4 mm)
```

The platen keeps every page inside that band, which is what removes the need
for focus stacking *and* for 3D dewarping in the same stroke.

---

## 7. Diffraction — why f/8 and not f/11

The Airy disc diameter is `2.44 · λ · N`. At λ = 550 nm:

| Aperture | DOF | Airy disc | Verdict |
|---|---|---|---|
| f/4 | 11.4 mm | 1.38 px | sharp, but too shallow to be safe |
| f/5.6 | 16.0 mm | 1.93 px | usable |
| **f/8** | **22.9 mm** | **2.75 px** | **the design point — already diffraction limited** |
| f/11 | 31.4 mm | 3.79 px | resolution you paid for, thrown away |
| f/16 | 45.7 mm | 5.51 px | soft |

`RigGeometry.diffraction_limited` is `True` once the Airy disc exceeds two
pixels. **Do not stop past f/8.** Every further stop buys depth of field you do
not need behind a platen and costs resolution you bought the sensor for.

---

## 8. Free stereo across the gutter

The overlap band exists for blending, but the two cameras looking into it from
200 mm apart also form a stereo pair — positioned, conveniently, exactly over
the spine where page curvature is worst.

```
δz = z² · δu / (f_px · b)
   = 453.1² × 0.2 / (7692 × 200)
   = 0.0267 mm = 26.7 µm
```

with `δu = 0.2 px` sub-pixel disparity accuracy. That is laser-triangulation
precision, for free, from hardware that has to be there anyway.

It is **not currently used** — the platen makes the page flat, so there is
nothing to measure. It is held in reserve as a dewarping input if the platen
ever proves impractical (fragile bindings, oversized volumes).

The same overlap gives something more immediately useful: **real-pixel finger
and glare removal**. An obstruction in one camera's overlap region is simply
absent from the other's, so it can be replaced with photographed data rather
than inpainted — which is what the CZUR does.

---

## 9. Precision budget for dewarping

Retained from the design phase, and the reason the platen is worth its €15.

To resample text without visible distortion at ~500 DPI, the surface height
must be known well enough that the induced lateral error stays under about half
a pixel ≈ 0.025 mm. Height error `δz` at surface tilt `θ` produces lateral error
`δz · tan θ`. At a typical 30° page tilt:

```
δz ≲ 0.05 mm  (50 µm)
```

Near the gutter, where θ approaches 70–80°, the requirement tightens to about
**10 µm**.

For context on why the project moved away from depth cameras: passive stereo on
an OAK-D Lite at this standoff gives ~1250 µm and returns nothing at all on
blank paper; active stereo on an OAK-D Pro gives ~370 µm. A €15 DOE line-laser
module gives ~9 µm. See [`decisions.md`](decisions.md).

---

## 10. The overlap trade-off

| Overlap | Tile width | DPI | Spread | Working dist. | DOF | Baseline | Stereo δz |
|---|---|---|---|---|---|---|---|
| 10 mm | 215.0 mm | 472.6 | 43.2 MP | 443.5 mm | 21.9 mm | 205 mm | 24.9 µm |
| **20 mm** | **220.0 mm** | **461.8** | **41.2 MP** | **453.1 mm** | **22.9 mm** | **200 mm** | **26.7 µm** |
| 30 mm | 225.0 mm | 451.6 | 39.4 MP | 462.7 mm | 23.9 mm | 195 mm | 28.5 µm |
| 40 mm | 230.0 mm | 441.7 | 37.7 MP | 472.3 mm | 24.9 mm | 190 mm | 30.5 µm |

More overlap means a wider tile, so lower DPI, a longer working distance and
slightly more depth of field. Design point **20–30 mm**: it clears the CZUR on
both DPI and megapixels while leaving a usable band. Below 10 mm the band is too
narrow to blend reliably; at 40 mm you have already lost.

The interactive version of this table, with a live slider, is in
[`scanner-blueprint.html`](scanner-blueprint.html) (sheet 6).

---

## 11. Configuration comparison

| Configuration | DPI | Spread | Tile | WD | DOF | Spare field | Orientation |
|---|---|---|---|---|---|---|---|
| **2 cameras, A3** | **461.8** | 41.2 MP | 220 mm | 453 mm | 22.9 mm | 34 mm | portrait |
| 1 camera, A3 | 342.1 | 22.6 MP | 420 mm | 601 mm | 41.0 mm | 150 mm | landscape |
| **1 camera, A4** | **483.8** | 22.6 MP | 297 mm | 434 mm | 20.9 mm | 106 mm | landscape |
| *CZUR ET Ultra* | *441* | *39.8 MP* | — | — | — | — | — |

**One body on A4 already beats the CZUR.** The second camera buys full A3
spreads in a single exposure; it is not what makes the rig useful.

Watch the working distance: **434 mm** for single-camera A4 versus **453 mm**
for two-camera A3. Set the mast for whichever format you are shooting, or the
page will sit outside the ±11 mm depth-of-field band.

---

## 12. Using the model

```python
from scanner.geometry import RigGeometry, Lens, A3, A4

g = RigGeometry(n_cameras=2, overlap_mm=20.0, document=A3)
print(g.report())
print(g.summary())        # JSON-serialisable dict

g.dpi                     # 461.8
g.tiles                   # [Tile(0, 0.0, 220.0), Tile(1, 200.0, 420.0)]
g.baseline_mm             # 200.0
g.working_distance_mm     # 453.1
g.focal_px                # 7692.3
g.diffraction_limited     # True
g.stereo_dz_um            # 26.7  (inf for a single camera)

# any other rig
RigGeometry(n_cameras=1, document=A4).dpi                  # 483.8
RigGeometry(n_cameras=3, overlap_mm=15.0).dpi              # three-way split
RigGeometry(lens=Lens(focal_mm=35.0, f_number=5.6)).report()
```

The constructor validates: `n_cameras ≥ 1`, `overlap_mm ≥ 0`, and an overlap so
large that tiles would not reduce the field is rejected outright.

### Sensors and formats

`Sensor`, `Lens` and `DocumentFormat` are plain frozen dataclasses — a different
body or a different paper size is a one-line change:

```python
from scanner.geometry import Sensor, DocumentFormat, RigGeometry

A7C = Sensor("Sony A7C", long_px=6000, short_px=4000, long_mm=35.9, short_mm=23.9)
LEDGER = DocumentFormat("Ledger", 431.8, 279.4)

RigGeometry(sensor=A7C, document=LEDGER).report()
```

---

## Reference values

For a Sony A6000 at 30 mm f/8 on A3 with two cameras and 20 mm overlap:

| Quantity | Value | Source |
|---|---|---|
| Pixel pitch | 3.92 µm (long) / 3.90 µm (short) | 23.5 mm ÷ 6000 px |
| Focal length in pixels | 7 692 px | 30 mm ÷ 3.90 µm |
| Magnification | 0.07091 | 15.6 mm ÷ 220 mm |
| Working distance | 453.1 mm | f · (1 + 1/m) |
| Sampling | 18.18 px/mm = 461.8 DPI | 4000 px ÷ 220 mm |
| Spread | 7636 × 5400 px = 41.23 MP | 420 × 297 mm at 18.18 px/mm |
| Field, other axis | 331.4 mm (34.4 mm spare) | 23.5 mm ÷ m |
| Depth of field | 22.86 mm (±11.4 mm) | 2Nc(1+m)/m², c = 1.72 px |
| Airy disc | 10.7 µm = 2.75 px | 2.44 λN at 550 nm |
| Stereo δz over the gutter | 26.7 µm | z²δu / (f_px·b), δu = 0.2 px |
