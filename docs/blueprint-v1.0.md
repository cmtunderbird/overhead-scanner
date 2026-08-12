# DIY Overhead Document Scanner — Blueprint v1.0
## Twin Sony A6000 · Dual Raspberry Pi 5 · Laptop GPU pipeline

**Status: build document.** Supersedes Blueprint v0.1, v0.2 and Addenda A–D. Those remain in the project as the decision record; nothing in them is authoritative any more except where restated here.

**Goal:** match and exceed the CZUR ET Ultra (441 DPI, 39.8 MP per A3 spread) with an open, reconfigurable pipeline.

**Result: ~462 DPI and ~41 MP per A3 spread, in a single simultaneous exposure, from real optical pixels — no tiling stage, no moving parts, no upscaling.**

Companion drawing set: `scanner-blueprint.html` — six sheets (plan, front elevation, side elevation, capture architecture, pipeline, numbers) with a live geometry calculator.

---

## 1. Configuration

| Component | Spec | Role |
|---|---|---|
| 2 × Sony A6000 | 24.3 MP APS-C, 6000×4000, 3.92 µm | Capture, one per half-spread |
| 2 × E-mount kit lens | 18-55 OSS or 16-50 PZ, **locked at 30 mm, f/8** | Imaging |
| 2 × Raspberry Pi 5 | 8 GB, Pi OS **Lite** 64-bit | Camera servers |
| 2 × AI HAT+ | Hailo-8, 26 TOPS | Live QA per node |
| Gigabit switch + wired LAN | — | Transport |
| Acer Nitro V15 | RTX 5060, 8 GB VRAM | Orchestration + batch processing |
| Rigid frame + cross rail | Adjustable baseline and toe-in | Geometry |
| Acrylic/glass platen | 3 mm | Page flattening |
| 2 × lamp bars | High-CRI, diffuse | Illumination |

**Indicative cost ≈ €1,500**, against ~€600 for a CZUR that cannot be reconfigured, cannot be moved closer for higher DPI on smaller originals, and cannot run your pipeline.

---

## 2. Optical geometry

Two cameras side by side over a flat A3 area, splitting the **420 mm long axis**. Tile width `w = (420 + overlap)/2`; the 4000-pixel sensor axis spans `w`.

| Overlap | Tile width | DPI | Spread px | Working dist. | DOF @ f/8 | Stereo δz |
|---|---|---|---|---|---|---|
| 10 mm | 215 mm | 473 | 43.2 MP | 443 mm | 21.8 mm | 25 µm |
| **20 mm** | **220 mm** | **462** | **41.2 MP** | **453 mm** | **22.8 mm** | **27 µm** |
| 30 mm | 225 mm | 452 | 39.4 MP | 463 mm | 23.8 mm | 29 µm |
| 40 mm | 230 mm | 442 | 37.7 MP | 472 mm | 24.9 mm | 31 µm |
| *CZUR ET Ultra* | — | *441* | *39.8 MP* | — | — | — |

**Design point: 20–30 mm overlap.** Clears the CZUR on both axes while leaving a usable stereo band across the gutter. Below 10 mm the band is too narrow to blend reliably; above 40 mm you fall behind the machine you are trying to beat.

### Derivation

| Quantity | Value | Source |
|---|---|---|
| Pixel pitch | 3.92 µm | 23.5 mm ÷ 6000 px |
| Focal length in pixels | 7 692 px | 30 mm ÷ 3.90 µm |
| Magnification | 0.0709 | 15.6 mm sensor ÷ 220 mm tile |
| Working distance | 453 mm | f · (1 + 1/m) |
| Field on the 6000 px axis | 331 mm | 23.5 mm ÷ m — vs 297 mm needed |
| Depth of field, f/8 | 22.8 mm | 2Nc(1+m)/m², c = 1.7 px |
| Airy disc, f/8, 550 nm | 10.7 µm = 2.7 px | 2.44 λN |
| Stereo δz over the gutter | 26.7 µm | z²δu / (f<sub>px</sub>·b), δu = 0.2 px |

**Do not stop past f/8.** The Airy disc is already 2.7 px wide. Every further stop buys depth of field you do not need behind a platen and costs resolution you bought the sensor for.

**The 34 mm of spare field is not waste to be optimised away.** It is the margin that lets deskew and crop operate on a page that is never perfectly square to the rig.

### Why the long axis gets split

Splitting the 420 mm axis asks the sensor for a 297 : 220 = **1.35** aspect against its native 1.50 — a comfortable fit, 11 % of field unused. Splitting the 297 mm axis instead asks for 420 : 158 = **2.65** against 1.50, throwing away most of the frame and landing near 340 DPI. The choice is not stylistic; it is the difference between beating the CZUR and losing to it.

### Free stereo over the gutter

With a 200 mm baseline, 24 MP sensors and a 30 mm lens at 453 mm:

```
δz = z²·δu / (f·b) = 453² × 0.2 / (7692 × 200) ≈ 27 µm
```

Laser-triangulation precision, in the overlap strip, positioned exactly over the spine — where curvature is worst. Available as a dewarp input if the platen is ever inconvenient. Requires no extra hardware. It also gives **real-pixel finger and glare removal**: an obstruction in one camera's overlap region is simply absent in the other's, so it is replaced with photographed data rather than inpainted, which is what the CZUR does.

---

## 3. Mechanical design

- **Rigidity beats stabilisation.** Rigidity is what keeps intrinsics and the stitching homography valid between sessions.
- **Cross rail with adjustable baseline and toe-in.** Cameras parallel and vertical for documents — toe-in 0°. The adjustability costs €40–80 now and is the only element that would be genuinely expensive to retrofit.
- **Tape the zoom rings.** A zoom that creeps 2 mm silently invalidates intrinsics, homography and DPI at once. This is the easiest way to wreck a calibrated two-camera rig.
- **Electronic first-curtain shutter on, remote triggering only.** Nothing touches the frame at exposure time.
- **Platen.** Flattens the page, which removes the need for 3D dewarping and collapses the depth-of-field problem in the same stroke. Highest-leverage €15 in the build.
- **Lamps at grazing angle, off-axis**, so the specular lobe reflects clear of both lenses. Cross-polarisation (film on lamps + polariser on lenses, ~€20) is the fallback if reflections persist — purely optical, no electronics.

---

## 4. Capture architecture

```
Acer Nitro V15 (RTX 5060)
  ├── LAN ── Pi 5 #1 + Hailo ── USB ── A6000 #1 ── left tile
  └── LAN ── Pi 5 #2 + Hailo ── USB ── A6000 #2 ── right tile
```

Two identical nodes; role assigned by hostname. Same SD image on both.

**Node software:** `libgphoto2` + **`python-gphoto2`** (bindings, not the CLI — each CLI call re-initialises the USB session), FastAPI + uvicorn, Samba or rsync for staging.

**Node API**

```
GET  /status          model, connection, settings
POST /config          iso, shutter, aperture
GET  /preview         low-res frame for framing
POST /capture         {profile, seq}  → file ids
GET  /files/{id}
WS   /events          progress, errors
```

**Pairing: by a sequence counter issued from the laptop, never by timestamp.** Network jitter of a few milliseconds is irrelevant — nothing in the scene moves.

**Camera-side settings (non-negotiable, capture hangs without them):**

- USB Connection → **PC Remote**
- Auto Review → **Off**
- Mode dial → **M**
- **Pre-AF → Off**
- Focus → **DMF**
- `capturetarget` → **card**, set explicitly

**Three gotchas, each worth an evening if missed:**

1. `gvfs-gphoto2-volume-monitor` claims the camera the instant it enumerates → "Could not claim the USB device." **Pi OS Lite has no desktop, which sidesteps it.** This is why Lite is specified.
2. The A6000 drops its PTP session when idle. Build reconnect-on-failure in from day one.
3. `capturetarget` defaults vary and silently change whether files land in camera RAM or on the card — which changes all your timing.

**Capture profiles**

| Profile | Frames/camera | Use |
|---|---|---|
| `standard` | **1** | Default |
| `clean` | 3 | Faded, low-contrast, very fine print |
| `max` | 9 | Rare pages that genuinely warrant it |

Stacking on this rig is **noise reduction only** — no IBIS and a rigid mount means no sub-pixel dither, so no true multi-frame resolution gain. √N and nothing more.

---

## 5. Throughput budget

**Measured, 2026-08-12, on `scanner-node-0` (Pi 5 + ILCE-6000 over USB 2.0
PTP).** This section previously assumed 10–15 MB/s and a card buffer. Both were
wrong, and the conclusion changes with them.

| | Measured |
|---|---|
| Frame size | **24.53 MB** (compressed ARW, fixed size) |
| Per frame, end to end | **~2.65 s** (2.478–2.739 over five consecutive) |
| Throughput | **9.25 MB/s** |
| `clean` profile, 3 frames | **7.96 s** — 2.495 / 2.621 / 2.629 |

For comparison the same body through WSL2 + USB/IP on the laptop managed
7.0 MB/s, so the node is the faster transport.

**There is no burst discount.** Three frames cost three times one frame, to
within 0.1 s. Nothing is pipelined and nothing is hidden.

```
1 frame/camera : 24.5 MB  → ~2.65 s
3 frames       : 73.6 MB  → ~7.96 s   (measured)
9 frames       : 220.7 MB → ~24 s     (extrapolated linearly)
```

### There is no card buffer

The original plan was to shoot to card and drain in the background, letting a
~21-RAW buffer absorb any burst. **The ILCE-6000 has no `capturetarget` key at
all** — not defaulted, absent from the config tree — so frames stream to the
host and transfer is **serial with capture**. Fitting an SD card does not change
this: the card is not even exposed over PTP in PC Remote mode. The buffer that
was supposed to absorb bursts does not exist.

### What that leaves

**The sustainable burst is still set by one inequality: transfer time ≤
page-turn time**, or the queue grows without bound. At 2.65 s per frame and a
4 s page turn that is **one frame** — a second only if the operator is slower
than 5.3 s per page. Hence `standard` = 1, now for a measured reason rather than
an assumed one.

`clean` and `max` remain available and work correctly, but they are **not**
sustainable at page-turn cadence. They are for the rare page that justifies
stopping for it.

**A 400-page book** = 200 spreads ≈ **9.8 GB** of ARW at `standard`, and
**~9 minutes** of pure capture time — which is not the constraint. Staging is:
the node holds frames in a tmpfs capped at 8 GB, or about **320 frames**, and
nothing frees them automatically. The orchestrator must collect them as it goes.

---

## 6. Calibration — none of it optional

Do this once, properly, after the zoom rings are taped.

1. **Intrinsics + distortion, per camera** — ChArUco, full model. Kit lenses at 30 mm have modest distortion; it must still be removed.
2. **Extrinsics / stitching homography** — fixed and calibrated, so no feature-based stitching is needed at runtime. Sub-pixel, computed once.
3. **Flat-field / vignetting, per camera** — evenly lit blank sheet at working height.
4. **Colour matching between the two bodies** — ColorChecker under both, build per-camera profiles. **Skip this and every spread has a visible seam down the middle** where one camera renders a shade warmer than the other. This is the single most commonly botched step in twin-camera rigs.
5. **Exposure matching** — measure and correct the offset between bodies.

Verify with a **USAF 1951 target and a slanted-edge (ISO 12233) SFR measurement**. Every DPI claim in this document gets a measured MTF50 number attached, or it is a guess.

---

## 7. Processing pipeline

```
capture ──▶ ingest & pair ──▶ per-camera correction ──▶ rectify + stitch
                                (distortion, flat-field, colour)
        ──▶ deskew & crop ──▶ split spread ──▶ background normalisation
        ──▶ [optional 2× SR] ──▶ OCR ──▶ export
```

- **Burst merge** (profile > 1): align and average. Denoise only.
- **Rectify + stitch:** fixed homography, blend across the overlap.
- **Dewarp:** unnecessary with the platen. Content-based (UVDoc class) available for unflattened material; the overlap stereo strip is available as a geometric input if that proves insufficient.
- **Background normalisation:** low-pass the paper background and divide out — removes illumination gradients and residual shadow.
- **OCR:** PaddleOCR + Tesseract 5, combined for language breadth.
- **Export:** PDF/A-2b with invisible text layer, ALTO/hOCR sidecars, lossless TIFF masters.

**Live QA on the Hailo nodes.** Each Pi runs segmentation, boundary check and blur scoring on its own half, returning a dewarped thumbnail in ~200 ms — so bad captures surface immediately rather than 400 pages later, and the laptop GPU stays free for batch work.

---

## 8. Compute budget — the 8 GB VRAM constraint

| Item | Memory | Verdict |
|---|---|---|
| 24 MP frame, RGB fp16 | 144 MB | fine |
| 2× SR output, 82 MP | ~500 MB | fine |
| RRDBNet activations at 41 MP input | ~5.2 GB per feature map | will not fit |
| **Tiled at 512–1024 px** | **< 1.5 GB peak** | the answer |

**Tiling is mandatory, driven by activation memory, not output size.**

**2× rather than 4× — the right call.** At 4×, a page is ~1.15 GB as TIFF and a 400-page book is ~460 GB with 5–10 hours of GPU time. At 2× a page is ~25 MB as high-quality JPEG and a book is **~10 GB**.

- Single-pass 2× ESRGAN: ~5–10 s/page → **batchable overnight**
- Three-member ensemble (bicubic anchor + 2× ESRGAN + cascade, fused under data-consistency back-projection, disagreement map retained as a hallucination confidence signal): ~15–45 s/page → **on demand, per page**

---

## 9. Output tracks

- **Archival master** — native ~462 DPI, stitched, deskewed, cropped, colour-managed. Every pixel traceable to a photon. Lossless TIFF.
- **Access copy** — 2× enhanced, searchable PDF/A.

Never conflate them. Learned SR is a derivative, not a master.

---

## 10. Build plan

| Phase | Deliverable | Gate |
|---|---|---|
| **0** | Full pipeline running against sample images | Runs end to end with no hardware |
| 1 | One Pi + one camera, `/capture` + `/files` | One page, end to end, from the laptop |
| 2 | Frame, rail, lamps, platen; both nodes | Both halves captured simultaneously |
| 3 | Full calibration | Seam invisible; ΔE < 2 between cameras |
| 4 | Stitch, deskew, split, normalise | Measured ≥ 450 DPI over a full A3 spread |
| 5 | OCR + export | CER ≤ CZUR on a shared 20-page test set |
| 6 | 2× SR track, live QA on Hailo | — |

**Phase 0 is the immediate work and needs no hardware at all.** Everything after it is a config change pointing the same code at a real camera.

---

## 11. Acceptance criteria

| Metric | Target |
|---|---|
| Measured DPI, A3 spread | ≥ 450 |
| MTF50, slanted edge, page corners | ≥ 0.30 cy/px |
| Stitch seam error | < 1 px |
| Colour ΔE between cameras | < 2 |
| Throughput | ≤ 3 s/spread at `standard` |
| OCR CER | ≤ CZUR on shared test set |

**The benchmark that settles it:** the same 20-page book on both machines, measuring MTF50, OCR character error rate, colour ΔE and dewarp residual. Everything else is marketing.

---

## Appendix A — The gutter, honestly

"Gutter removal" bundles three distinct failures. Conflating them is why naive implementations disappoint.

**(a) Geometric — the page curves into the spine.** Solved by the platen. Without a platen: the overlap stereo band measures the curve at ~27 µm precision directly over the spine, and a developable-surface fit regularises it (paper bends but does not stretch, so the surface has few real degrees of freedom).

**(b) Photometric — the gutter sits in its own shadow.** This is ambient occlusion. Background/illumination-field normalisation removes the band without crushing the text. If it persists, independently switchable lights let you pick, per pixel, the illumination that best reaches into the fold.

**(c) Foreshortening — not solvable, by anyone.** As the page tilts to angle θ, page-referred sampling density falls by cos θ. At 60° you have half your DPI; at 80°, under a fifth. **Text within a few millimetres of the spine is compressed below the resolution needed to read it, and no algorithm recovers information that was never sampled.** CZUR cannot do this either. The only fix is physical: press the book flatter, or accept the loss. This is the one place where "match and exceed CZUR" means matching a limitation rather than beating it.

## Appendix B — Retained from the OAK line: the laser option

If the platen is ever abandoned, the depth half of the problem is cheap and well understood, and does **not** require a depth camera:

- A **DOE multi-line green laser module** (€10–20) projects 7–15 parallel lines in one frame — 3–5× CZUR's three-line sampling density, no moving parts. Green (520 nm) beats red: higher QE, and the Bayer array has twice as many green pixels, so centroid precision improves for free.
- **Mount two, on opposite sides.** Near a steep gutter one laser is occluded by the page curl; the opposite one still reaches. €15 fixes a real failure mode.
- Precision: `δz = z²·δu/(f·b)` with sub-pixel line-centroid accuracy of ~0.1 px lands around **10 µm** — comfortably inside the 50 µm budget that resampling text at 500 DPI demands.
- Adds one calibration step: **laser plane extrinsics**, fitted from a checkerboard at 6–10 known poses.
- Class 2 (<1 mW) modules, pointed down at a table.

**Total incremental cost: about €30.** Held in reserve; not needed for the platen build.

## Appendix C — Why not an OAK camera (decision record)

The project began on a Luxonis OAK-D Lite and worked through the alternatives. The summary of Addenda A–D:

| Option | Cost | A4 single shot | Reaches CZUR? | Mechanics needed |
|---|---|---|---|---|
| OAK-D Lite + lasers | ~€90 | fixed focus blocks it | with a motorised mosaic | Z axis + rotary arm |
| OAK-D / S2 + lasers | ~€360 | 347 DPI / 11.6 MP | ✗ needs 2×2 mosaic | rotary arm |
| OAK-D Pro AF + lasers | ~€485 | 338 DPI / 11.0 MP | ✗ needs 2×2 mosaic | rotary arm |
| OAK-1 MAX + lasers | ~€285 | 514 DPI / 25.5 MP | A4 only | none for A4 |
| OAK 4 D + lasers | ~€1,280 | 684 DPI / 45.3 MP | ✓ | none |
| **Twin A6000 (this build)** | **~€1,500** | **~700 DPI** | **✓ full A3 spread** | **none** |

Three findings drove the move away from the OAK line:

1. **Fixed focus on the Lite** made the working distance a mechanical problem rather than a setting.
2. **Active stereo does not rescue dewarping.** The OAK-D Pro's dot projector genuinely solves the "blank paper has no texture" problem — but its 75 mm baseline overflows the disparity search at scanning standoff, forcing 400p, which lands around 370 µm precision against a 50 µm budget. *You can add lasers to any camera for €30; you cannot add pixels to a 12 MP sensor for any price.*
3. **Resolution is bought once, at purchase.** No sub-€1,000 OAK reaches the CZUR on a full A3 spread without moving parts. Two second-hand 24 MP bodies do, in one exposure.

The OAK-D Lite still has a job: hand detection and page-turn triggering, mounted off to the side at 50 cm+ where its fixed focus is irrelevant.

---

## 12. Open items

1. Confirm both kit lenses are the **same model** — matched glass makes calibration substantially easier.
2. Verify `manualfocusdrive` exposure via gphoto2 on your lens (enables scripted focus bracketing if ever needed).
3. **The WebCam Super Resolution notes** — still outstanding, and now the only remaining unknown in the SR stage design.

---

*Blueprint v1.0 · 2026-08-10 · all dimensions in millimetres*
