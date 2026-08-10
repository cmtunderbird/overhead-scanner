# Overhead Document Scanner

Twin Sony A6000 · dual Raspberry Pi 5 · **~462 DPI / 41 MP per A3 spread, in one exposure, no moving parts.**

Target: match and exceed the CZUR ET Ultra (441 DPI, 39.8 MP) with an open, reconfigurable pipeline. See `docs/blueprint-v1.0.md` for the full build spec and `docs/scanner-blueprint.html` for the dimensioned drawing set.

---

## What works right now, with no hardware

Everything except pointing a lens at paper. The repo contains a camera simulator good enough to calibrate against, so the entire chain — intrinsics, distortion, alignment, flat-field, colour, stitching, deskew, spine split, background normalisation, and the measurement harness — is built and tested before the first body arrives.

```bash
pip install -r requirements.txt
python -m scanner selftest
```

That rehearses the whole rig and prints the blueprint's acceptance table:

```
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

Nothing in that table is asserted. Every row is measured from synthetic pixels by the same code that will measure real ones.

---

## The idea

The simulator is not a stub that returns a fixed image. It renders a document in millimetre space, photographs it through a modelled lens with real distortion, vignetting, a per-body colour cast, mounting error and sensor noise — and hands back the exact `K`, distortion coefficients and homography it used.

That ground truth is what makes Phase 0 worth doing. You can prove the calibration recovers the camera, prove the stitch registers to sub-pixel, and prove the MTF measurement is accurate, *before* any of it can be blamed on a wobbly tripod.

```
              synth.page  ──►  synth.camera  ──►  raw frames
                (truth)         (truth: K, dist, H)     │
                                                        ▼
   calib.intrinsics ──► calib.align ──► calib.field ──► RigCalibration
                                                        │
                                                        ▼
     pipeline.stages: correct ─► rectify ─► stitch ─► deskew ─► crop
                      ─► normalise ─► split                    │
                                                               ▼
                        metrics: MTF50 · DPI · seam · dE  ──► acceptance
```

---

## Layout

| Module | What it is |
|---|---|
| `scanner/geometry.py` | The optical model. Single source of truth for every number in the build. |
| `scanner/synth/` | Synthetic pages and the camera simulator, both carrying ground truth. |
| `scanner/calib/` | Intrinsics (ChArUco), document alignment, flat-field, colour. Persists to JSON. |
| `scanner/pipeline/` | The deterministic chain, raw frames to archival master. |
| `scanner/metrics/` | ISO 12233 slanted-edge MTF, measured DPI, seam error, CIEDE2000. |
| `scanner/node/` | FastAPI camera server + interchangeable `mock` / `gphoto2` backends. |
| `scanner/orchestrator/` | Session driver: sequence-counter pairing, retry, manifest. |
| `scanner/selftest.py` | The full rehearsal above. |

---

## Commands

```bash
# what the optics can do, for any configuration
python -m scanner geometry --compare
python -m scanner geometry -n 1 -f A4          # one camera, A4

# a printable calibration target -- print at 100 % scale
python -m scanner board -o charuco_a4.png

# run a camera node (on the Pi, or here with the mock backend)
python -m scanner node --backend mock --port 8000

# drive a session
python -m scanner capture --node cam0=http://pi1:8000 -c 20 --interactive

# raw frames -> archival masters
python -m scanner process captures/ -k calibration/rig.json -o masters/

# measure a real photograph
python -m scanner measure master.tif --edge 100,200,180,180 --px-per-mm 18.18
```

---

## What the optics say

```
2 x Sony A6000 @ 30 mm f/8  ->  A3 spread
  orientation        portrait  (limited by the short sensor axis)
  tile width         220 mm      baseline 200 mm

  RESOLUTION         461.8 DPI   (7636 x 5400 px, 41.23 MP)

  working distance   453.1 mm
  field (other axis) 331.4 mm (34.4 mm spare)
  depth of field     22.86 mm (+/-11.4 mm)
  Airy disc          2.75 px   <-- diffraction limited
  stereo dz          26.7 um
```

| config | DPI | MP | tile | WD | DOF |
|---|---|---|---|---|---|
| blueprint v1.0 (2 cam) | **462** | 41.2 | 220 | 453 | 22.9 |
| 1 camera, A3 | 342 | 22.6 | 420 | 601 | 40.9 |
| **1 camera, A4** | **484** | 22.6 | 297 | 434 | 20.9 |
| *CZUR ET Ultra* | *441* | *39.8* | — | — | — |

**One body already beats the CZUR on A4** — 484 DPI against 441. You do not need the second camera to start scanning usefully; you need it for full A3 spreads.

The orientation row is not cosmetic. Splitting the 420 mm axis asks the sensor for a 297:220 = 1.35 aspect against its native 1.50. Splitting the short axis instead asks for 2.65, throws away most of the frame and lands near 340 DPI. `geometry.py` picks the better one and tells you which axis is limiting.

---

## Notes from building it

Four things cost real time, and all four are now encoded in the tests so they cannot come back.

**`generateImage`'s `outSize` includes the margin.** Sizing it to the pattern alone silently shrinks the printed squares by 6.6 %, which goes straight into the document homography as a pure scale error and is invisible until you measure the page. `BoardSpec.rendered_extent_mm()` exists so this cannot be got wrong twice.

**Photometry has to be simulated in linear light.** A gain applied to gamma-encoded values is not what a sensor does, and no linear colour matrix can undo it — the calibration looks broken when the simulator is what is wrong. Moving vignetting and channel gains into linear light took mean ΔE from 3.4 to 0.16.

**A slanted-edge MTF oracle must include the pixel aperture.** Comparing against a pure Gaussian makes a correct implementation look 5 % low; and `cv2.GaussianBlur` at small σ is a 3-tap discrete kernel that passes far more at Nyquist than the continuous formula predicts, which makes it look 40 % high. Against the kernel OpenCV actually applies, times the 1-pixel box, the implementation is accurate to 1–2 % from 0.06 to 0.58 cy/px.

**Estimating the background field at full resolution costs seconds per spread and buys nothing.** It is low-frequency by construction. Computing it on a 640 px proxy took one spread from 10.1 s to 1.5 s.

---

## When the camera arrives

See `docs/day-one.md`. The short version: print the board, tape the zoom ring, shoot a dozen tilted board views, run `scanner calibrate`, and the mock backend becomes a real one with a single environment variable.

```bash
SCANNER_BACKEND=gphoto2 python -m scanner node
```

Nothing else changes. That is the whole point of having built this first.

---

## Tests

```bash
python -m pytest -q          # 89 tests, ~55 s
```

They are not smoke tests. `test_sfr.py` checks the MTF implementation against an analytic oracle across seven blur levels and five edge angles; `test_colour.py` checks CIEDE2000 against the nine Sharma reference pairs to 1e-3; `test_end_to_end.py` calibrates a rig from scratch and confirms the measured DPI matches the optical model to within 1 %.
