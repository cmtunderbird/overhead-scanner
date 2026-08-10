# Overhead Document Scanner

**A DIY overhead book scanner that beats the commercial machine it was designed against — twin Sony A6000 bodies, dual Raspberry Pi 5 nodes, ~462 DPI and 41 megapixels per A3 spread in a single simultaneous exposure, with no moving parts.**

The benchmark is the [CZUR ET Ultra](https://www.scannx.com/products/overhead-scanners/czur-et-ultra): 441 DPI, 39.8 MP per spread, ~€600. This build clears both figures, and every part of it is open, measurable and reconfigurable.

```
2 x Sony A6000 (IMX) @ 30 mm f/8  ->  A3 spread
------------------------------------------------------------------
  orientation        portrait  (limited by the short sensor axis)
  overlap            20 mm
  tile width         220 mm
  baseline           200 mm

  RESOLUTION         461.8 DPI   (7636 x 5400 px, 41.23 MP)

  magnification      0.07091
  working distance   453.1 mm
  field (other axis) 331.4 mm (34.4 mm spare)
  depth of field     22.86 mm (+/-11.4 mm)
  Airy disc          2.75 px   <-- diffraction limited
  stereo dz          26.7 um
```

---

## Table of contents

- [Status](#status)
- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Results](#results)
- [Documentation](#documentation)
- [Repository layout](#repository-layout)
- [Command reference](#command-reference)
- [Testing](#testing)
- [Design notes](#design-notes)
- [Roadmap](#roadmap)
- [Licence](#licence)

---

## Status

**Phase 0 is complete: everything that can be built without hardware is built, tested and verified.** The first camera body arrives imminently; nothing in the software is waiting on it except real photons.

| Phase | Gate | State |
|---|---|---|
| **0** | Full pipeline running against sample images | ✅ **Done** — 17/17 acceptance checks, 89 tests |
| **1** | One Pi + one camera, `/capture` + `/files` end to end | 🟡 Software complete and tested against a simulated body; awaiting hardware |
| **2** | Frame, rail, lamps, platen; both nodes live | ⬜ Not started — nothing physical exists yet |
| **3** | Full calibration; seam invisible, ΔE < 2 between bodies | 🟡 Code complete and proven synthetically; needs real photons |
| **4** | Stitch, deskew, split, normalise; measured ≥ 450 DPI on real A3 | 🟡 Same — pipeline exists, the measurement has not happened |
| **5** | OCR + export; CER ≤ CZUR on a shared test set | ⬜ Not started |
| **6** | 2× super-resolution track, live QA on the Hailo nodes | ⬜ Not started |

See [`docs/status.md`](docs/status.md) for the honest, itemised version — including what is deliberately missing.

---

## What it does

Photographs an open book from above with two cameras, each covering one half of the spread with a deliberate overlap across the gutter, and turns the pair into a single colour-managed, geometrically correct archival master.

- **No moving parts.** Both halves are exposed simultaneously. There is no scanning head, no rotary arm, no motorised mast.
- **No feature-based stitching.** The join is a fixed homography solved once at calibration, in millimetres. It does not wander from page to page.
- **Everything is measured, not asserted.** DPI, sharpness, seam registration and colour difference are all computed by code in this repository, using the same functions on synthetic and real images.
- **Reconfigurable.** Change the document format, the camera count or the overlap and the optical model re-solves the whole geometry — including which way up to hold the sensor.

---

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/cmtunderbird/overhead-scanner
cd overhead-scanner
pip install -r requirements.txt

python -m scanner selftest
```

`selftest` rehearses the entire rig against a simulated pair of cameras — calibration, alignment, flat-field, colour, capture, stitch, split, measurement — and prints the acceptance table:

```
acceptance  (synthetic rehearsal, scale 0.25, page rendered at 3.2x the capture sampling)
  ----------------------------------------------------------------------------
  cam0 reprojection rms                    0.103   < 0.5 px             PASS
  cam0 focal length error                  0.038   < 2 %                PASS
  cam1 reprojection rms                    0.107   < 0.5 px             PASS
  cam1 focal length error                  0.044   < 2 %                PASS
  cam0 alignment rms                       0.216   < 1.0 px             PASS
  cam0 mounting rotation error             0.016   < 0.05 deg           PASS
  cam1 alignment rms                       0.202   < 1.0 px             PASS
  cam1 mounting rotation error             0.008   < 0.05 deg           PASS
  cam0 chart dE (mean)                     0.157   < 2.0                PASS
  cam1 chart dE (mean)                     0.338   < 2.0                PASS
  cross-camera dE (the seam)               0.321   < 2.0                PASS
  coverage                               100.000   > 99 %               PASS
  residual skew                            0.000   < 0.3 deg            PASS
  spine position error                     0.000   < 5 mm               PASS
  measured scale error                     0.040   < 1 %                PASS
  MTF50 (median of edges)                  0.309   >= 0.30 cy/px        PASS
  calibration round-trip               identical   identical            PASS
  ----------------------------------------------------------------------------

  17 passed, 0 failed, 0 skipped   (39.5 s)
```

Nothing in that table is hard-coded. Every row is measured from synthetic pixels by the same code that will measure real ones.

**Other useful first commands:**

```bash
python -m scanner geometry --compare        # what the optics can do
python -m scanner board -o charuco_a4.png   # printable calibration target
python -m scanner node --backend mock       # run a camera node with no camera
```

---

## How it works

### The idea that makes Phase 0 worth doing

The camera simulator is not a stub that returns a fixed image. It renders a document in **millimetre space**, photographs it through a modelled lens with real radial and tangential distortion, vignetting, a per-body colour cast, mounting error, defocus and sensor noise — and hands back the exact `K`, distortion coefficients and document→image homography it used.

That ground truth is the whole point. It lets you prove that the calibration recovers the camera, that the stitch registers to sub-pixel, and that the MTF measurement is accurate, **before** any of it can be blamed on a wobbly tripod or a smeared lens. When the hardware arrives, the mock backend is swapped for gphoto2 with one environment variable and no other code changes.

### Data flow

```
      synth.page                 synth.camera                   raw frames
  document in mm space  ────►  modelled lens + sensor  ────►  (per camera)
   carries PageTruth            carries CaptureTruth
   (edges, patches,             (true K, dist, H)
    fiducials, spine)                                              │
                                                                   ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │ calib.intrinsics  ChArUco, tilted views      →  K, distortion     │
   │ calib.align       board flat on the platen   →  H (doc mm → px)   │
   │ calib.field       blank sheet + colour chart →  flat-field, CCM   │
   └───────────────────────────────────────────────────────────────────┘
                                    │  RigCalibration (JSON + npz)
                                    ▼
   pipeline.stages:  correct ─► rectify ─► stitch ─► deskew ─► crop
                     ─► normalise ─► split
                                    │
                     ┌──────────────┴──────────────┐
                     ▼                             ▼
            ARCHIVAL MASTER                  ACCESS COPY
       native DPI, lossless TIFF        2x SR, OCR, PDF/A  (Phase 6)
                     │
                     ▼
   metrics:  MTF50 (ISO 12233) · measured DPI · seam error · ΔE2000
                     │
                     ▼
              acceptance report
```

Everything to the left of the master/access split is deterministic and metric. Everything to the right is a derivative. The master is written before super-resolution is ever invoked, and the two are never conflated.

### Capture architecture

```
Acer Nitro V15 (RTX 5060, orchestrator)
  ├── LAN ── Pi 5 #1 + Hailo-8 ── USB PTP ── A6000 #1 ── left tile
  └── LAN ── Pi 5 #2 + Hailo-8 ── USB PTP ── A6000 #2 ── right tile
```

Two identical nodes; role assigned by hostname, same SD image on both. **Frames are paired by a sequence counter issued by the orchestrator, never by timestamp** — nothing in the scene moves, so the frames do not need to be simultaneous to a millisecond, they need to be *identifiable*. A counter is exact and immune to clock skew, NTP steps and network jitter. Timestamp pairing works in the lab and fails on page 300.

---

## Results

### What the optics give you

| Configuration | DPI | Spread | Tile | Working dist. | DOF @ f/8 | Orientation |
|---|---|---|---|---|---|---|
| **Blueprint v1.0 — 2 cameras, A3** | **461.8** | 41.2 MP | 220 mm | 453 mm | 22.9 mm | portrait |
| 1 camera, A3 spread | 342.1 | 22.6 MP | 420 mm | 601 mm | 41.0 mm | landscape |
| **1 camera, A4** | **483.8** | 22.6 MP | 297 mm | 434 mm | 20.9 mm | landscape |
| *CZUR ET Ultra (reference)* | *441* | *39.8 MP* | — | — | — | — |

**A single A6000 on A4 already beats the CZUR** — 484 DPI against 441. The second body buys full A3 spreads in one exposure; it is not what makes the rig useful. Note the working distance differs by configuration: **434 mm** for single-camera A4, **453 mm** for two-camera A3.

### The overlap trade-off

Overlap across the gutter costs resolution and buys a stereo band and blending margin:

| Overlap | Tile width | DPI | Spread | Working dist. | DOF | Baseline | Stereo δz |
|---|---|---|---|---|---|---|---|
| 10 mm | 215.0 mm | 472.6 | 43.2 MP | 443.5 mm | 21.9 mm | 205 mm | 24.9 µm |
| **20 mm** | **220.0 mm** | **461.8** | **41.2 MP** | **453.1 mm** | **22.9 mm** | **200 mm** | **26.7 µm** |
| 30 mm | 225.0 mm | 451.6 | 39.4 MP | 462.7 mm | 23.9 mm | 195 mm | 28.5 µm |
| 40 mm | 230.0 mm | 441.7 | 37.7 MP | 472.3 mm | 24.9 mm | 190 mm | 30.5 µm |

Design point **20–30 mm**. Below 10 mm the band is too narrow to blend reliably; above 40 mm you fall behind the machine you are trying to beat.

### Verified precision (synthetic, end to end)

| Quantity | Achieved | Target |
|---|---|---|
| Intrinsics reprojection RMS | 0.10 px | < 0.5 px |
| Focal length recovery | 0.04 % error | < 2 % |
| Distortion `k1` recovery | 0.1 % error | — |
| Document alignment RMS | 0.22 px | < 1.0 px |
| Mounting rotation recovery | 0.016° error | < 0.05° |
| Colour, per camera | ΔE2000 0.16 / 0.34 | < 2.0 |
| **Cross-camera ΔE (the seam)** | **0.32** | **< 2.0** |
| Measured vs predicted DPI | 0.04 % | < 1 % |
| Spine detection | 0.0 mm error | < 5 mm |
| Processing time per spread | 1.5 s (was 10.1 s) | — |

---

## Documentation

| Document | What is in it |
|---|---|
| [`docs/status.md`](docs/status.md) | Exactly what is built, what is not, and what is next |
| [`docs/optics.md`](docs/optics.md) | The complete optical model, with derivations and the orientation argument |
| [`docs/architecture.md`](docs/architecture.md) | Module-by-module design, invariants, extension points |
| [`docs/calibration.md`](docs/calibration.md) | The full calibration procedure, diagnostics and failure modes |
| [`docs/pipeline.md`](docs/pipeline.md) | Every processing stage, why it is in that order, and its parameters |
| [`docs/metrics.md`](docs/metrics.md) | The measurement harness — MTF, DPI, seam, ΔE — and how to read the numbers |
| [`docs/api.md`](docs/api.md) | Node HTTP API and orchestrator reference |
| [`docs/hardware.md`](docs/hardware.md) | Bill of materials, camera settings, assembly notes |
| [`docs/decisions.md`](docs/decisions.md) | Decision record — why twin A6000, why not an OAK, why a platen |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Real failure modes and their fixes |
| [`docs/day-one.md`](docs/day-one.md) | Step-by-step for the morning the camera arrives |
| [`docs/blueprint-v1.0.md`](docs/blueprint-v1.0.md) | The build specification of record |
| [`docs/scanner-blueprint.html`](docs/scanner-blueprint.html) | Dimensioned drawing set — plan, elevations, architecture, pipeline (open in a browser) |

---

## Repository layout

```
scanner/
├── geometry.py            the optical model — single source of truth
├── selftest.py            full-rig rehearsal + acceptance report
├── cli.py                 command line interface
├── synth/
│   ├── page.py            synthetic documents in mm space, carrying ground truth
│   └── camera.py          camera simulator: distortion, vignette, colour, noise
├── calib/
│   ├── intrinsics.py      ChArUco detection and camera calibration
│   ├── align.py           document homography + human-readable mounting error
│   ├── field.py           flat-field and colour matrix
│   └── store.py           CameraCalibration / RigCalibration, JSON + npz
├── pipeline/
│   ├── stages.py          correct, rectify, stitch, deskew, crop, normalise, split
│   └── run.py             the runner: spread and session level
├── metrics/
│   ├── sfr.py             ISO 12233 slanted-edge MTF
│   ├── colour.py          sRGB↔Lab, CIEDE2000, chart and cross-camera ΔE
│   └── scale.py           measured DPI and stitch-seam error
├── node/
│   ├── server.py          FastAPI camera node
│   └── backends/          base · mock · gphoto2  (interchangeable by design)
└── orchestrator/
    └── session.py         sequence-counter pairing, retry, manifest

tests/                     89 tests
docs/                      the documentation above
```

---

## Command reference

```
python -m scanner geometry     what the optics can do, for any configuration
python -m scanner board        render a printable ChArUco target
python -m scanner node         run a camera node (mock or gphoto2)
python -m scanner capture      drive a capture session across N nodes
python -m scanner process      raw frames -> archival masters
python -m scanner measure      MTF50 / DPI on an image
python -m scanner selftest     rehearse the whole rig, no hardware
```

Examples:

```bash
# compare configurations, including a single camera
python -m scanner geometry --compare
python -m scanner geometry -n 1 -f A4 --json

# a target you can print at 100 % scale
python -m scanner board -o charuco_a4.png --dpi 600

# run two mock nodes and capture 5 spreads across them
python -m scanner node --backend mock --camera-id cam0 --tile 0 --port 8000 &
python -m scanner node --backend mock --camera-id cam1 --tile 1 --port 8001 &
python -m scanner capture --node cam0=http://localhost:8000 \
                          --node cam1=http://localhost:8001 -c 5 -o captures/

# process them into masters
python -m scanner process captures/ -k calibration/rig.json -o masters/

# measure a real photograph
python -m scanner measure master.tif --edge 100,200,180,180 --px-per-mm 18.18
```

Full details in [`docs/api.md`](docs/api.md).

---

## Testing

```bash
python -m pytest -q          # 89 tests, ~55 s
```

These are not smoke tests:

- **`test_sfr.py`** validates the MTF implementation against an analytic oracle across seven blur levels and five edge angles, and checks accuracy specifically in the region of the 0.30 cy/px acceptance threshold.
- **`test_colour.py`** checks CIEDE2000 against the nine [Sharma et al.](https://hajim.rochester.edu/ece/sites/gsharma/ciede2000/) reference pairs to within 1×10⁻³.
- **`test_geometry.py`** asserts the optical model reproduces every published blueprint figure, and that automatic orientation selection never loses to a forced one.
- **`test_node_api.py`** exercises the node over real HTTP, including a deliberately dropped PTP session that must be recovered transparently.
- **`test_orchestrator.py`** runs two live uvicorn servers on real sockets and drives them through a session.
- **`test_end_to_end.py`** calibrates a two-camera rig from scratch out of simulated captures and confirms the **measured** DPI matches the optical model to within 1 %.

---

## Design notes

Four findings from the build, each now locked down by a test so it cannot come back. The full list is in [`docs/decisions.md`](docs/decisions.md); these are the ones that cost real time.

**`CharucoBoard.generateImage` sizes `outSize` *including* the margin.** Sizing it to the pattern alone silently shrinks the printed squares by 6.6 %. That goes straight into the document homography as a pure scale error, and it is invisible until you physically measure the page — it first appeared as a "−6.5 % mounting scale error" that was actually a rendering bug. `BoardSpec.rendered_extent_mm()` exists so this cannot be got wrong twice. *Practical consequence: measure a printed square with a ruler before shooting anything.*

**Photometry must be simulated in linear light.** Vignetting and channel gains are linear operations in a real sensor; applied to gamma-encoded values, no linear colour matrix can undo them. Fixing this took mean ΔE from 3.4 to 0.16 — the calibration was right all along, the simulator was wrong.

**A slanted-edge MTF oracle must include the pixel aperture.** A measurement sees the whole system, not just the optical blur. And `cv2.GaussianBlur` at small σ is a 3-tap discrete kernel that passes far more at Nyquist than the continuous Gaussian formula predicts. Against a pure-Gaussian oracle the implementation looked 5 % low, then 40 % high; against the kernel OpenCV actually applies, times the 1-pixel box, it is accurate to 1–2 % from 0.06 to 0.58 cy/px. **Both apparent bugs were in the oracle** — "fixing" the implementation to match would have shipped broken code.

**The illumination field is low-frequency by construction.** Estimating it at full resolution costs seconds per spread and buys nothing. Computing it on a 640 px proxy is numerically equivalent and took one spread from 10.1 s to 1.5 s.

---

## Roadmap

**Next, before the hardware:**

- `scanner calibrate` CLI subcommand wrapping the existing library calls — currently calibration requires a short script
- `scanner align` subcommand for the document homography step

**When the camera arrives** — follow [`docs/day-one.md`](docs/day-one.md):

1. Print and *measure* the ChArUco board
2. Set the lens to 30 mm f/8, focus, and tape both rings
3. Shoot 12–16 tilted board views, calibrate, read the diagnostics
4. Lay the board on the platen, solve the document homography, read the mounting error
5. Flat-field and colour
6. First real page — measure MTF50 and DPI on day one rather than guessing

**Phase 5 — output:** PaddleOCR + Tesseract 5, PDF/A-2b with an invisible text layer, ALTO/hOCR sidecars.

**Phase 6 — enhancement:** 2× super-resolution (tiled at 512–1024 px; tiling is mandatory and driven by activation memory, not output size), live QA on the Hailo nodes.

**Still open from the blueprint:** the WebCam Super Resolution notes, the last unknown in the SR stage design.

---

## Licence

MIT — see [`LICENSE`](LICENSE).
