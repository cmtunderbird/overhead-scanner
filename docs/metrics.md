# Metrics

The acceptance criteria, as runnable code. Four independent measurements:
**sharpness**, **scale**, **registration** and **colour**.

The design rule for this package is that it depends on nothing but numpy and
OpenCV. You must be able to point it at a JPEG from any source — this rig, a
phone, a competitor's scanner — and get a comparable number. That is what makes
"we beat the CZUR" a claim rather than an opinion.

---

## 1. Sharpness — ISO 12233 slanted edge

```python
from scanner.metrics import measure_sfr, measure_edges

r = measure_sfr(roi)                     # one near-vertical edge
r.mtf50        # 0.309  cycles per pixel
r.mtf20        # 0.541
r.angle_deg    # 5.02   measured edge tilt
r.contrast     # 0.89   sanity check on the ROI
r.mtf_at(0.25) # interpolate the curve anywhere
r.lp_per_mm(18.18)   # MTF50 in line pairs per mm on the page

results = measure_edges(image, [(x, y, w, h), ...])   # skips ROIs that fail
```

### Why MTF and not "does it look sharp"

DPI says how finely the page was *sampled*. MTF50 says how much of that sampling
*survived* the lens, the focus, the diffraction and the shake. A 500 DPI capture
through a soft lens carries less information than a 400 DPI capture through a
good one, and only one of those numbers tells you so.

**Acceptance threshold: MTF50 ≥ 0.30 cy/px at the page corners.**

### How it works

1. Convert the ROI to luminance and **linearise** (`power(v, 2.2)`). SFR on
   gamma-encoded data reads high, because the encoding steepens the edge. This
   is the most common way to publish an MTF number that is simply wrong.
2. Find the sub-pixel edge position in each row by derivative centroid, and fit
   a line. The row is lightly smoothed *for the position estimate only* — noise
   does not bias the centroid but it scatters it, and a scattered fit smears the
   projected ESF, which reads as a blurrier lens.
3. Project every pixel onto the edge normal and bin into a **4× oversampled**
   edge spread function. This is what defeats aliasing and is why the edge must
   be tilted.
4. Differentiate to the line spread function, apply a Hamming window.
5. FFT, normalise at DC, and correct for the differencing aperture.
6. Interpolate the 0.5 and 0.2 crossings.

### Why the edge must be tilted 2–10°

A perfectly vertical edge samples the transition at one phase only, so there is
nothing to reconstruct. A tilted edge sweeps the phase across rows, which is
what gives the 4× oversampling. `measure_sfr` refuses angles below 0.4° or above
45° with an explanatory error rather than returning a plausible wrong number.

### Validation, and two traps in the oracle

`tests/test_sfr.py` checks the implementation against an analytic answer for a
synthetic edge blurred by a known amount. Getting the *oracle* right took two
corrections, and both are worth knowing:

**A slanted-edge measurement sees the whole system, including the pixel
aperture.** Comparing against a pure Gaussian `exp(−2π²σ²f²)` makes a correct
implementation look ~5 % low. The right oracle is the blur **times `sinc(f)`**,
the 1-pixel box.

**`cv2.GaussianBlur` at small σ is not a continuous Gaussian.** It builds a
discrete kernel — at σ = 0.4 that is three taps, roughly [0.106, 0.788, 0.106] —
which passes far more at Nyquist than the continuous formula predicts. Against
the continuous oracle the implementation looked **40 % high** at σ = 0.4.

Against the kernel OpenCV actually applies, times the pixel box:

| σ (px) | True MTF50 | Measured | Error |
|---|---|---|---|
| 0.35 | 0.5792 | 0.5701 | −1.6 % |
| 0.40 | 0.5321 | 0.5199 | −2.3 % |
| 0.50 | 0.3741 | 0.3666 | −2.0 % |
| 0.60 | 0.2902 | 0.2863 | −1.4 % |
| 0.70 | 0.2485 | 0.2461 | −1.0 % |
| 1.00 | 0.1800 | 0.1782 | −1.0 % |
| 2.00 | 0.0929 | 0.0919 | −1.0 % |
| 3.00 | 0.0623 | 0.0618 | −0.9 % |

**Accurate to 1–2 % from 0.06 to 0.58 cy/px**, which brackets the 0.30
acceptance threshold comfortably.

> Both apparent failures were in the oracle, not the implementation. Had they
> been "fixed" by adjusting the code to match, the harness would have shipped
> a systematic error and every sharpness number since would have been wrong.

### Practical notes

- Edge angle recovery is exact to 0.1°, and MTF50 varies less than 7 % across
  edge angles from 2° to 12° — the measurement does not depend on how carefully
  you laid the target down.
- Noise tolerance: stable to ~3 DN on a 160 px ROI. Beyond that, use a bigger
  ROI — averaging area is what buys noise immunity.
- Measure **corners, not just centre**. Corner MTF50 is what decides whether a
  kit lens stays; the centre of any lens at f/8 looks fine.

---

## 2. Scale — measured DPI

```python
from scanner.metrics import measure_scale

r = measure_scale(image, (ax, ay), (bx, by), span_mm=188.0, search_px=40)
r.px_per_mm      # 18.1745
r.dpi            # 461.63
r.distance_px    # 3416.81
r.mark_a_px      # refined sub-pixel centre
r.error_vs(461.8)  # -0.0004  (fractional)
```

Two marks a known distance apart. The initial guess only needs to be within
`search_px`; an intensity-weighted centroid of the darkest blob does the rest,
which is what makes the result good to better than 0.1 %.

The synthetic test page prints a ruler with heavy end marks in every column, so
a single tile is self-sufficient for a scale measurement — you do not need a
stitched spread to check a camera.

**End-to-end synthetic accuracy: 0.04 % against the optical model's prediction.**

Measuring this on a real page is the fastest way to catch a wrong working
distance, a mis-scaled printed target, or a lens that crept.

---

## 3. Registration — stitch seam

```python
from scanner.metrics import measure_seam
from scanner.pipeline.stages import overlap_columns

x0, x1 = overlap_columns(rig)[0]
r = measure_seam(tile_a_doc, tile_b_doc, x0, x1)
r.shift_px          # 0.08   ← the acceptance number, target < 1 px
r.dx_px, r.dy_px    # signed components
r.correlation       # phase-correlation peak
r.mean_abs_diff     # photometric agreement across the overlap
r.seam_visibility   # 1.0 = invisible, > 1.2 = you can see it
```

Both tiles must already be warped into document space so the overlap columns
correspond. Registration is measured by **phase correlation**, which gives a
sub-pixel shift; it is validated in the tests against known 1, 2 and 4 px
offsets and recovers them to ±0.25 px.

`seam_visibility` is a separate, complementary check: gradient energy exactly on
the join divided by its local neighbourhood. A blend that leaves a photometric
step shows up here even when registration is perfect — which is precisely the
failure mode of skipping colour calibration.

---

## 4. Colour — CIEDE2000

```python
from scanner.metrics import srgb_to_lab, delta_e_2000, chart_delta_e, cross_camera_delta_e

delta_e_2000(srgb_to_lab(a), srgb_to_lab(b))

chart_delta_e(image, rois, reference_srgb)
# {"mean": 0.157, "median": 0.14, "p95": 0.31, "max": 0.42,
#  "per_patch": [...], "measured_rgb": [...]}

cross_camera_delta_e(image_a, rois_a, image_b, rois_b)   # the seam number
```

CIEDE2000 rather than ΔE76 because it is perceptually uniform where it matters —
it correctly discounts hue error in dark and saturated colours, and weights
lightness error in the mid-tones, which is where paper lives.

Validated against all nine [Sharma et al.](https://hajim.rochester.edu/ece/sites/gsharma/ciede2000/)
reference pairs to within **4×10⁻⁵**.

Patches are sampled by **median** over a 40 %-inset region, so a dust speck or a
specular glint cannot move the answer.

### Which number matters

| Measurement | What it tells you |
|---|---|
| `chart_delta_e` | Absolute accuracy against sRGB references — depends on knowing your illuminant |
| **`cross_camera_delta_e`** | **How differently the two bodies render the same colours — this is the seam** |

**Acceptance: ΔE < 2 between cameras.** A few percent of per-channel mismatch is
normal between two copies of the same model and lands near ΔE 1 — visible on a
spread precisely because it is a straight edge down the middle rather than a
gradual shift.

Rough scale: ΔE 1 is a just-noticeable difference under ideal viewing, ΔE 2 is
noticeable side by side, ΔE 5 is obvious.

---

## 5. Running the whole acceptance suite

```bash
python -m scanner selftest
```

Runs all four measurement families against a simulated rig and prints:

```
acceptance  (synthetic rehearsal, scale 0.25, page rendered at 3.2x the capture sampling)
  cam0 reprojection rms                    0.103   < 0.5 px             PASS
  cam0 focal length error                  0.038   < 2 %                PASS
  cam0 alignment rms                       0.216   < 1.0 px             PASS
  cam0 mounting rotation error             0.016   < 0.05 deg           PASS
  cam0 chart dE (mean)                     0.157   < 2.0                PASS
  cross-camera dE (the seam)               0.321   < 2.0                PASS
  coverage                               100.000   > 99 %               PASS
  residual skew                            0.000   < 0.3 deg            PASS
  spine position error                     0.000   < 5 mm               PASS
  measured scale error                     0.040   < 1 %                PASS
  MTF50 (median of edges)                  0.309   >= 0.30 cy/px        PASS
  calibration round-trip               identical   identical            PASS
```

### One caveat about MTF in the rehearsal

The selftest renders its synthetic page at **3.2× the capture sampling**
(`sampling_headroom`). Below about 3×, the *page* band-limits the result and the
MTF50 figure reports the renderer rather than the optics. When headroom is
insufficient the check is reported as skipped (`--`) rather than passed, because
a green tick that measures the wrong thing is worse than no tick.

This is why the header line prints the headroom. At `--scale 1.0` the rehearsal
is a full 24 MP simulation and slow; at 0.22–0.25 it is a fast, honest proxy.

---

## 6. Measuring a real photograph

```bash
python -m scanner measure master.tif \
    --edge 1820,3400,320,320 \
    --edge 5610,3400,320,320 \
    --scale 240,5180,3660,5180,188 \
    --px-per-mm 18.18
```

```
master.tif: 7610 x 5384 px

sharpness (ISO 12233 slanted edge)
  roi (1820, 3400, 320, 320)  MTF50 0.3140 cy/px   MTF20 0.5502   edge +5.01 deg   5.7 lp/mm   PASS
  roi (5610, 3400, 320, 320)  MTF50 0.2870 cy/px   MTF20 0.5100   edge -4.98 deg   5.2 lp/mm   below 0.30 target

scale
  18.1745 px/mm = 461.6 DPI (3416.81 px over 188 mm)
```

Print a target page containing slanted edges, a colour chart and a ruler —
`scanner.synth.page.render_page()` generates exactly that — and you can measure
real MTF50, real DPI and real ΔE on day one instead of guessing.

---

## 7. Acceptance criteria summary

| Metric | Target | Where measured |
|---|---|---|
| Measured DPI, A3 spread | ≥ 450 | `measure_scale` |
| MTF50, slanted edge, page corners | ≥ 0.30 cy/px | `measure_sfr` |
| Stitch seam error | < 1 px | `measure_seam` |
| Colour ΔE between cameras | < 2 | `cross_camera_delta_e` |
| Throughput at `standard` | ≤ 3 s/spread | `CaptureSession.throughput()` |
| OCR character error rate | ≤ CZUR, shared test set | Phase 5, not yet built |

**The benchmark that settles the project:** the same 20-page book on both
machines, measuring MTF50, OCR CER, colour ΔE and dewarp residual. Everything
else is marketing.
