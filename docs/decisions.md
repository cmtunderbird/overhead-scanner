# Decision record

Why the project is shaped the way it is. Each entry states the decision, the
reasoning, and — where it applies — what would change it.

**D16 reverses part of D1 and supersedes D3–D5 for bound material.** The
superseded entries are kept rather than edited, because how a project reached a
wrong conclusion is worth more than a clean record.

---

## D16. The rig is stereoscopic, not a tiling rig

**Decision:** the two cameras both cover the whole spread, so the page's 3-D
surface can be measured and flattened. Tiling becomes a second mode for flat
material.

**This reverses the effective design of D1–D5.** Those entries treated the twin
bodies as a way to get 462 DPI across a plane, with stereo relegated to a 20 mm
strip "held in reserve". With 20 mm of overlap out of 420, **95 % of the spread
is seen by one camera only** — there is no depth to recover, and no dewarping
is possible. Stereoscopic operation is not an extension of that geometry; it
replaces it.

### Why the earlier rejection was wrong

Addendum C measured an OAK-D Lite (~1250 µm) and an OAK-D Pro (~370 µm) against
a 50 µm budget and concluded "dropping stereo is an upgrade". Correct about
that hardware; wrong as a general claim. Two A6000s at a 300 mm baseline give
**32 µm** — the sensors and the standoff were never the problem, the depth
cameras were.

### The structural result that forces the choice

| Coverage | Overlap | DPI | Stereo cover | Baseline | δz |
|---|---|---|---|---|---|
| tile | 20 mm | 462 | 5 % | 200 mm | 27 µm |
| tile | 200 mm | 342 | 48 % | 110 mm | 85 µm |
| tile | 300 mm | 342 | 71 % | 60 mm | **157 µm** |
| **stereo** | full | 342 | **100 %** | 200 mm | **47 µm** |

Depth precision gets *worse* as coverage improves, then recovers abruptly.
**When tiling, the baseline is the tile separation — it is not yours to
choose.** Widen the tiles for overlap and the cameras converge, collapsing the
baseline. Only full overlap frees it. There is no useful middle ground.

### What it costs and what it buys

342 DPI instead of 462 — below the CZUR's 441, and the honest price of
measuring the page rather than assuming it. Against that: depth everywhere,
a working distance that nearly doubles depth of field to ±20.5 mm, and residual
distortion of **0.08 px** where a homography leaves **8.74 px**.

Routes back to the resolution: fusing the two dewarped views (mechanism built,
recovery unmeasured), or four cameras as two stereo pairs (~462 DPI *and* full
stereo).

**What would change this:** nothing about the geometry. If the material is
genuinely flat, use tile mode — that is why both exist.

---

## D1. Twin Sony A6000 rather than a depth camera
*Conclusion stands; the reasoning about stereo is superseded by D16.*

**Decision:** two 24 MP APS-C bodies, not a Luxonis OAK.

The project began on an OAK-D Lite and worked through the alternatives across
four design addenda. Three findings drove the move:

**Fixed focus on the Lite** made the working distance a mechanical problem
rather than a setting — it would have needed a motorised Z-mast.

**Active stereo does not rescue dewarping.** The OAK-D Pro's IR dot projector
genuinely solves the "blank paper has no texture" problem, which passive stereo
cannot. But its 75 mm baseline overflows the disparity search at scanning
standoff, forcing 400p, which lands around **370 µm** precision against a
**50 µm** budget. Passive stereo on the Lite gives ~1250 µm *and* returns nothing
on blank paper.

**Resolution is bought once, at purchase.** No sub-€1000 OAK reaches the CZUR on
a full A3 spread without moving parts.

| Option | Cost | A4 single shot | Reaches CZUR on A3? | Mechanics |
|---|---|---|---|---|
| OAK-D Lite + lasers | ~€90 | fixed focus blocks it | with a motorised mosaic | Z axis + rotary arm |
| OAK-D / S2 + lasers | ~€360 | 347 DPI / 11.6 MP | ✗ needs a 2×2 mosaic | rotary arm |
| OAK-D Pro AF + lasers | ~€485 | 338 DPI / 11.0 MP | ✗ needs a 2×2 mosaic | rotary arm |
| OAK-1 MAX + lasers | ~€285 | 514 DPI / 25.5 MP | A4 only | none for A4 |
| OAK 4 D + lasers | ~€1 280 | 684 DPI / 45.3 MP | ✓ | none |
| **Twin A6000 (chosen)** | **~€1 500** | **~700 DPI** | **✓ full spread** | **none** |

> *You can add lasers to any camera for €30. You cannot add pixels to a 12 MP
> sensor for any price.*

The OAK-D Lite still has a job: hand detection and page-turn triggering, mounted
off to the side at 50 cm+ where its fixed focus is irrelevant.

---

## D2. Split the long axis, not the short one

**Decision:** the 420 mm axis is divided between the cameras; the sensor is held
portrait.

Splitting the long axis asks the sensor for a **297 : 220 = 1.35** aspect against
its native 1.50 — a comfortable fit wasting 11 % of the field. Splitting the
short axis asks for **420 : 158 = 2.65**, throws away most of the frame, and
lands near **342 DPI** instead of 462.

This is the difference between beating the CZUR and losing to it. The model
evaluates both orientations and picks the better one automatically, which is how
it correctly flips to landscape for a single camera. Derivation in
[`optics.md` §3](optics.md#3-sensor-orientation--the-decisive-choice).

---

## D3. A platen, not 3D dewarping
*Superseded by D16 for bound material. The platen is now optional: it is used
for flat material and omitted for books, where curvature is measured instead of
suppressed. What follows was the reasoning while the rig was plane-based.*

**Decision:** flatten the page with 3 mm acrylic and skip surface reconstruction
entirely.

The platen collapses two problems at once: the page stays inside the ±11 mm
depth-of-field band, *and* there is no curvature to model. It is the
highest-leverage €15 in the build.

**What it does not fix — and nothing can:** as a page tilts to angle θ,
page-referred sampling density falls by cos θ. At 60° you have half your DPI; at
80°, under a fifth. Text within a few millimetres of an unflattened spine is
compressed below the resolution needed to read it, and no algorithm recovers
information that was never sampled. The CZUR cannot do this either. This is the
one place where "match and exceed" means matching a limitation.

**What would change this:** fragile bindings that must not be pressed. Two
fallbacks are characterised and ready — the free overlap stereo band at 27 µm
(D4), and a €30 DOE line-laser pair at ~10 µm (see
[`optics.md` §9](optics.md#9-precision-budget-for-dewarping)).

---

## D4. Overlap the tiles by 20 mm
*Applies to tile mode only. In stereo mode the overlap is total and the
baseline is an independent parameter — see D16.*

**Decision:** 20 mm of overlap across the gutter, costing ~2 % of DPI.

It buys three things: a blending margin, real-pixel finger and glare removal
(an obstruction in one camera's overlap is absent from the other's, so it is
replaced with photographed data rather than inpainted, which is what the CZUR
does), and a free 200 mm-baseline stereo band at **27 µm** precision positioned
exactly over the spine.

Design range 20–30 mm. Below 10 mm the band is too narrow to blend reliably;
above 40 mm the DPI falls below the CZUR's 441.

---

## D5. Fixed calibrated homography, not feature-based stitching
*Applies to tile mode only. A homography is a plane-to-plane map and cannot
correct a page that is not a plane; stereo mode resamples against a measured
surface instead — see D16.*

**Decision:** the join is solved once at calibration and never recomputed.

Feature matching at runtime makes the seam a function of page content — it
wanders between pages, fails on blank margins, and cannot be validated. A
calibrated homography is measured once, is checkable (`mounting_error` reports
it in millimetres and degrees), and is identical on every page.

Because it is solved in **millimetres**, the two cameras land in a shared metric
frame automatically: no separate extrinsics step, no scale ambiguity.

**What would change this:** a rig that flexes. If the seam drifts between
sessions the answer is a stiffer frame, not adaptive stitching.

---

## D6. Sequence-counter pairing, never timestamps

**Decision:** the orchestrator issues a monotonic counter; nodes echo it.

Nothing in the scene moves between the two exposures, so frames do not need to
be simultaneous to a millisecond — they need to be *identifiable*. A counter is
exact and immune to clock skew, NTP steps and network jitter. Timestamp pairing
works in the lab and fails on page 300 of a real run.

---

## D7. Mock-first development

**Decision:** build a ground-truth camera simulator before touching hardware.

The simulator returns the exact `K`, distortion and homography used to make each
frame, so every claim can be checked against a known answer. Two of the four
significant bugs found during the build were in **test oracles**, not in the
implementation — a distinction that is invisible without ground truth, and where
"fixing" the code to match would have shipped a systematic error.

It also means the mock and gphoto2 backends are interchangeable by construction,
so the day the camera arrives is a configuration change rather than an
integration project.

---

## D8. f/8, and not a stop further

**Decision:** the aperture is fixed at f/8.

At f/8 the Airy disc is already 2.75 px — the system is diffraction limited.
Every further stop buys depth of field that the platen makes unnecessary and
costs resolution the sensor was bought for.

| Aperture | DOF | Airy disc |
|---|---|---|
| f/5.6 | 16.0 mm | 1.93 px |
| **f/8** | **22.9 mm** | **2.75 px** |
| f/11 | 31.4 mm | 3.79 px |

---

## D9. A circle of confusion of 1.72 px, not 19 µm

**Decision:** depth of field is computed against a pixel-level criterion.

The conventional APS-C CoC (19 µm, ~4.9 px) is a *print-viewing* criterion. Used
here it would claim ~64 mm of depth of field and let pages sit visibly soft. A
document scanner is judged at the pixel level, so the model uses c = 1.72 px
≈ 6.7 µm, giving ±11.4 mm — which the platen comfortably satisfies.

---

## D10. Photometry in linear light

**Decision:** vignetting, channel gains and exposure are applied to linear
values, then re-encoded.

A real sensor collects photons linearly and gamma-encodes afterwards, so these
are linear operations. Simulating them on gamma-encoded values produces an image
that **no linear colour matrix can correct** — which made the calibration look
broken when the simulator was what was wrong. Fixing it took mean ΔE from 3.4 to
0.16.

Corollary: colour matrices are fitted and applied in linear light too, otherwise
the matrix depends on exposure and stops working when the lamps change.

Flat-field correction, by contrast, is applied in *encoded* space and this is
exact — a linear vignette `v` becomes `v^(1/γ)` after encoding, still
multiplicative.

---

## D11. Lossless TIFF masters, JPEG-free

**Decision:** the archival master is a lossless TIFF; the access copy is
separate and derived.

TIFF is lossless, widely readable, and will still open in thirty years — which
is the entire point of a master. Super-resolution and OCR run downstream and
never feed back.

> Never conflate them. Learned super-resolution is a derivative, not a master.

---

## D12. 2× super-resolution, not 4×

**Decision:** when Phase 6 arrives, the access copy is 2×.

At 4×, a page is ~1.15 GB as TIFF; a 400-page book is ~460 GB and 5–10 hours of
GPU time. At 2× a page is ~25 MB as high-quality JPEG and a book is ~10 GB,
which batches overnight.

Tiling at 512–1024 px is **mandatory** and is driven by *activation* memory, not
output size: an RRDBNet feature map at 41 MP input is ~5.2 GB, which will not fit
in 8 GB of VRAM. Tiled, peak usage is under 1.5 GB.

---

## D13. Pi OS Lite, specifically

**Decision:** the nodes run Raspberry Pi OS **Lite**, not the desktop image.

`gvfs-gphoto2-volume-monitor` claims the camera the instant it enumerates, and
every subsequent command fails with "Could not claim the USB device". Lite has no
desktop, therefore no gvfs, therefore no problem. This is the single most common
first-hour failure with tethered cameras and the fix is an install-time choice.

---

## D14. `standard` capture profile is one frame

**Decision:** the default captures a single frame per camera.

Stacking on this rig is **noise reduction only** — no IBIS and a rigid mount mean
no sub-pixel dither, so there is no true multi-frame resolution gain, only √N on
noise.

And throughput is bounded by one inequality: transfer time per spread must stay
below page-turn time, or the queue grows without bound. At 24 MB per ARW over
the measured 9.25 MB/s of USB 2.0 PTP and a 4 s page turn, that is one frame
maximum sustainable. `clean` (3) and `max` (9) exist for pages that warrant
them.

---

## D15. Estimate low-frequency fields at low resolution

**Decision:** background normalisation and skew estimation run on downscaled
proxies.

The illumination field is low-frequency by construction; estimating it at full
resolution is numerically equivalent and about fifty times slower. This one
change took a spread from 10.1 s to 1.5 s.

The deliberate exception is `rectify_to_document`, which uses `INTER_LANCZOS4`
(3× slower than cubic). It is the one resampling step that touches every pixel of
the master, and sharpness there is the product.

---

## D17. Plane sweep over page columns, not SGBM

**Decision:** recover the surface by sweeping candidate heights and correlating
whole page columns between the two views, rather than block-matching a
rectified pair and fitting a surface to the point cloud.

Generic stereo discards the strongest fact available: the page is developable
with generators parallel to the spine, so **every pixel in a column at platen x
sits at the same height**. Sweeping collapses hundreds of rows of evidence into
one number per `(x, height)`, which is what makes it survive a page whose
margins have no texture at all.

**Confidence is peak sharpness, not peak height.** The first implementation
gated on the correlation peak's *value* and produced a confidently wrong
surface at a mean NCC of 0.949. Every textured column was already correct to
~0.2 mm; the failures were the dark gutter and the blank margins, which still
scored 0.85–0.95 because **two smooth gradients correlate beautifully at any
assumed depth**. A textured column peaks over ~1.5 mm; a blank one is flat
across the entire 50 mm search range.

**What would change this:** needing a surface that varies along y — cockling, a
dog-eared corner. The column sweep cannot represent that and would become the
initial estimate for a 2-D method rather than the answer.

---

## D18. The platen is now optional

**Decision:** the platen is used for flat material and omitted for books.

In v1.0 it was load-bearing — it made the page flat so the homography was
valid. That is exactly what fails on a thick binding, which physically cannot
lie flat near the spine and should not be forced to. In stereo mode curvature
is measured, so the platen's only remaining job is convenience.

Foreshortening remains unsolvable by anyone: as a page tilts to angle θ,
sampling density falls by cos θ, and no algorithm recovers information that was
never sampled. Measuring the surface fixes the *geometry*, not the *sampling*.

---

## D19. Focus assist scores a real capture, not the live view

**Decision:** the operator interface's focus aid takes a full-resolution frame
through the normal capture path and scores five regions of it — it does not
score the preview, even though the preview is free and the capture costs a
shutter actuation.

**Live view is roughly 1/35 of the sensor's pixels, and downscaling is itself a
low-pass filter.** A preview can look perfectly sharp on a frame that is
visibly soft at full size, because the resampling has already removed the
frequencies that would reveal the softness. A focus score computed on it tracks
the preview's sharpness, which is not the quantity anyone wants.

**The absolute number is meaningless; the derivative is not.** Tenengrad
normalised by image variance still depends on the page, the lighting and the
lens. So the interface holds the per-region peak and reports the current score
as a percentage of it: you turn the ring, the bars rise, they stop rising, you
have found focus. Going past shows as a fall — which is the only way to know,
and the reason peaks are held rather than recomputed.

**Five regions, not one**, because a centre-sharp corner-soft frame is a lens
verdict — and per [`day-one.md`](day-one.md) the corner MTF50 is the number
that decides whether the kit lens stays. One central score cannot see it.

**What would change this:** a body whose live view is full-sensor readout at
native resolution, or a lens with a focus motor the node can drive, at which
point the loop closes in software and the operator stops turning anything.

---

## D20. The GUI proxies the nodes rather than letting the browser reach them

**Decision:** the browser talks only to the laptop; the laptop talks to the
nodes. No client-side call ever names a node URL.

Three reasons, and they are the same three that shaped the capture path:

- **The camera network need not be routable from the browser.** The Pis can sit
  on a private wired segment with only the laptop bridging to it — which is
  what you want when the segment is carrying 24 MB per frame and you are
  holding the throughput inequality tight.
- **One place for timeouts and error text.** A node that has lost its PTP
  session produces one message, in one format, whoever asked for it.
- **It works from a phone.** `--host 0.0.0.0` and the interface is usable from
  anything on the LAN with no client install.

The cost is one extra hop for the MJPEG relay, which is bandwidth the laptop
has. The interface is also **one HTML file with no build step, no npm and no
CDN**: it has to work on an isolated camera network with no internet, and it
has to still open in five years.

---

## Superseded

**Blueprint v0.1 / v0.2 and Addenda A–D** are retained in the project history as
the decision record for D1. `docs/blueprint-v1.0.md` is the specification of
record; nothing in the earlier documents is authoritative except where restated
there.
