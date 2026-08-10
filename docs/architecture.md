# Architecture

How the codebase is organised, what each layer is responsible for, and the
invariants that hold it together.

---

## 1. The organising principle

**Everything is built against ground truth first.**

The camera simulator does not exist to make tests pass in the absence of
hardware. It exists so that every claim the system makes about itself can be
checked against a known answer. When `calibrate_intrinsics` returns a focal
length, the test knows the true one. When the pipeline reports 461.8 DPI, the
measurement harness re-derives it from the pixels and the ruler printed on the
synthetic page.

That means bugs are attributable. If the calibration is 6 % off, it is the
calibration — not a wobbly tripod, not a smeared lens, not a mis-measured
board. Two of the four significant bugs found during the build were in *test
oracles*, not in the implementation; without ground truth that distinction
would have been invisible, and "fixing" the code to match a wrong oracle would
have shipped broken software.

---

## 2. Layers

```
┌──────────────────────────────────────────────────────────────────┐
│  geometry.py            the optical model                        │
│  no dependencies on anything else in the package                 │
└──────────────────────────────────────────────────────────────────┘
        ▲                    ▲                    ▲
        │                    │                    │
┌───────┴────────┐  ┌────────┴────────┐  ┌────────┴────────┐
│  synth/        │  │  calib/         │  │  node/          │
│  ground truth  │  │  measurement    │  │  acquisition    │
│  generators    │  │  of a real rig  │  │                 │
└───────┬────────┘  └────────┬────────┘  └────────┬────────┘
        │                    │                    │
        │           ┌────────▼────────┐  ┌────────▼────────┐
        │           │  pipeline/      │  │  orchestrator/  │
        │           │  raw → master   │  │  session driver │
        │           └────────┬────────┘  └─────────────────┘
        │                    │
        │           ┌────────▼────────┐
        └──────────►│  metrics/       │
                    │  acceptance     │
                    └─────────────────┘
```

**`geometry.py` is the root and depends on nothing.** Every other module asks it
for tile boundaries, sampling density, working distance or focal length. There
is no second place where a DPI is computed, and no module hard-codes 220 mm.

`metrics/` deliberately depends on nothing but numpy and OpenCV. It must be
possible to point it at a JPEG from any source — a real camera, a competitor's
scanner, a phone — and get a comparable number.

---

## 3. Module responsibilities

### `scanner/geometry.py`

The optical solution for a rig configuration. Frozen dataclasses (`Sensor`,
`Lens`, `DocumentFormat`, `RigGeometry`) where every derived quantity is a
property, so changing `overlap_mm` or `n_cameras` re-solves the whole geometry
consistently rather than leaving a stale field behind.

Key responsibility that is easy to miss: **choosing the sensor orientation**.
See [`optics.md` §3](optics.md#3-sensor-orientation--the-decisive-choice).

### `scanner/synth/`

`page.py` renders documents in **millimetre space** and returns a `PageTruth`
recording where the slanted edges, colour patches, fiducials, ruler ends and
spine actually are. `camera.py` photographs a page (or a tilted calibration
board) through a modelled lens and returns a `CaptureTruth` holding the exact
`K`, distortion coefficients and document→image homography used.

Modelled: radial and tangential distortion, vignetting, per-body channel gains
and exposure, mounting error (rotation, translation, scale), principal-point
offset, optical blur, defocus from the depth-of-field equation, shot and read
noise.

Photometry is applied in **linear light**, then re-encoded — see §6.

### `scanner/calib/`

Turns photographs of known targets into a `RigCalibration`.

| Module | Solves | From |
|---|---|---|
| `intrinsics.py` | `K`, distortion | 12–16 tilted ChArUco views |
| `align.py` | document mm → image px homography | one board laid flat on the platen |
| `field.py` | flat-field gain map, 3×3 colour matrix | blank sheet, colour chart |
| `store.py` | persistence, summary | JSON + npz sidecar |

`align.py` also provides `mounting_error()`, which decomposes the solved
homography into millimetres of offset, degrees of rotation, percent of scale
error and a keystone term — numbers you can act on with an Allen key rather
than a 3×3 matrix.

### `scanner/pipeline/`

`stages.py` holds each operation as an independently testable function;
`run.py` sequences them and produces a `SpreadResult` with a timing and
diagnostics report. See [`pipeline.md`](pipeline.md).

### `scanner/metrics/`

The acceptance criteria as runnable code: ISO 12233 slanted-edge MTF,
measured DPI from a printed ruler, stitch-seam registration by phase
correlation, and CIEDE2000. See [`metrics.md`](metrics.md).

### `scanner/node/`

A thin FastAPI service that owns exactly one camera and no session state. Two
interchangeable backends behind one abstract base class. See [`api.md`](api.md).

### `scanner/orchestrator/`

The laptop side: issues the sequence counter, fires every node concurrently,
pulls files down, records a manifest, and reports whether the run was actually
sustainable.

---

## 4. Invariants

These hold across the codebase and are worth preserving.

**1. Document space is millimetres, and it is the only shared frame.**
Cameras are related to each other *only* through the document plane. There is
no separate extrinsics step, no camera-to-camera transform, and no scale
ambiguity — solving each camera's homography in millimetres puts them in a
common metric frame automatically.

**2. Every calibrated transform maps document mm → *undistorted* image px.**
Undistortion is always applied first, or points are undistorted analytically
(`undistort_points`), which is more accurate than undistorting a whole frame
and re-detecting in the resampled image.

**3. Pairing is by sequence counter, never by timestamp.**
Nothing in the scene moves, so frames need to be *identifiable*, not
simultaneous. A counter is immune to clock skew, NTP steps and network jitter.

**4. The archival master is written before any learned model runs.**
Super-resolution and OCR are derivatives. They never feed back into the master.

**5. Backends are interchangeable.**
Nothing above `CameraBackend` may know whether it is talking to a simulation or
a Sony. The only difference is one environment variable.

**6. Reduced-scale rendering must not change the field of view.**
`scale` in the simulator shrinks pixel counts and rescales pixel-valued defects
(`CameraDefects.scaled()`), leaving geometry identical. Tests run at 1/6 scale
and exercise exactly the same code paths.

---

## 5. Configuration and persistence

`RigCalibration` serialises to human-readable JSON plus an `.npz` sidecar for
the flat-field maps (too large for JSON, and not meaningfully diffable anyway).

```json
{
  "version": 1,
  "created": "2026-08-10T15:29:08+00:00",
  "doc_long_mm": 420.0,
  "doc_short_mm": 297.0,
  "target_px_per_mm": 18.1818,
  "overlap_mm": 20.0,
  "dpi": 461.82,
  "provenance": {"source": "selftest", "scale": 0.25},
  "cameras": {
    "cam0": {
      "camera_id": "cam0",
      "image_size": [4000, 6000],
      "K": [[7692.3, 0.0, 2014.0], [0.0, 7692.3, 2991.0], [0.0, 0.0, 1.0]],
      "dist": [-0.0851, 0.0359, 0.0014, -0.0004, 0.0],
      "reprojection_rms": 0.103,
      "colour_matrix": [[...], [...], [...]],
      "H_doc_to_img": [[...], [...], [...]],
      "tile_x0_mm": 0.0,
      "tile_x1_mm": 220.0,
      "has_flat_field": true,
      "notes": "charuco 7x10 @ 26.0 mm"
    }
  }
}
```

A calibration is only useful if it survives a reboot and can be diffed against
last month's — hence JSON with a `created` stamp and a free-form `provenance`
block recording how it was produced.

`RigCalibration.summary()` prints a one-glance status showing, per camera,
whether it has a flat-field, a colour matrix and an alignment:

```
rig calibration  2026-08-10T15:29:08+00:00
  canvas   7636 x 5400 px @ 461.8 DPI
  document 420 x 297 mm, overlap 20 mm
  cam0       tile    0.0- 220.0 mm   rms 0.097 px  flat  colour  aligned
  cam1       tile  200.0- 420.0 mm   rms 0.096 px  flat  colour  aligned
```

---

## 6. Colour handling

A recurring source of subtle error, so it is worth stating the policy plainly.

- **Images are stored and passed around as 8-bit BGR** (OpenCV convention),
  sRGB-encoded.
- **Photometric operations happen in linear light.** Vignetting, channel gains
  and exposure are linear physical processes. The simulator decodes to linear,
  applies them, re-encodes, then adds noise in DN.
- **Colour matrices are fitted and applied in linear light.** Fitting in gamma
  space makes the matrix depend on exposure, so it stops working the moment the
  lamps change.
- **Transfer functions go through lookup tables** — 256-entry decode, 4096-entry
  encode. Calling `pow()` on a 40-megapixel array costs seconds per spread and
  buys precision an 8-bit output cannot carry. Round-trip error is ≤ 1 DN.
- **Flat-field correction is applied in encoded space, and this is exact.**
  If the vignette is a linear factor `v`, then in encoded space it becomes
  `v^(1/γ)` — still multiplicative. A multiplicative gain map fully corrects it.

---

## 7. Performance

Measured on a 2291 × 1620 canvas (the tests' reduced scale) before and after
optimisation:

| Stage | Before | After | How |
|---|---|---|---|
| Background normalisation | 6 969 ms | 386 ms | estimate the field on a 640 px proxy |
| Colour matrix | 2 034 ms | 397 ms | LUT both transfer functions |
| Skew estimation | 159 ms | 90 ms | coarse-to-fine search, capped proxy size |
| **Full spread** | **10 100 ms** | **1 549 ms** | |

Two general lessons encoded in the code: *low-frequency quantities should be
estimated at low resolution*, and *8-bit inputs do not justify floating-point
transfer functions*.

Deliberately **not** optimised: `rectify_to_document` uses `INTER_LANCZOS4`
(198 ms versus 61 ms for cubic). This is the one resampling step that touches
every output pixel of the archival master, and sharpness there is the product.

---

## 8. Extension points

**A different camera or paper size** — construct a new `Sensor` or
`DocumentFormat`; nothing else changes.

**A third camera** — `RigGeometry(n_cameras=3)` tiles correctly, `blend_tiles`
already handles N tiles by distance transform, and `overlap_columns` returns
every join. The orchestrator is N-node by construction.

**A real RAW pipeline** — currently frames are read as 8-bit BGR. The natural
insertion point is a decode step before `correct_frame`; everything downstream
is dtype-agnostic apart from the colour LUTs, which would need a 16-bit path.

**Dewarping without a platen** — the overlap stereo band is already computed by
the geometry model (`stereo_dz_um`). A depth stage would sit between
`rectify_to_document` and `blend_tiles`, warping each tile by the recovered
surface before compositing.

**A new metric** — add it to `scanner/metrics/`, export it from
`metrics/__init__.py`, and add a row to `selftest.py`'s `checks` list. The
acceptance table is just a list of `(name, value, target, ok)` tuples.

---

## 9. Testing strategy

| File | Proves |
|---|---|
| `test_geometry.py` | The model reproduces every blueprint figure; orientation selection never loses to a forced choice; nonsense configurations are rejected |
| `test_sfr.py` | MTF is accurate against an analytic oracle across seven blur levels, five edge angles and three noise levels — including near the 0.30 cy/px threshold |
| `test_colour.py` | CIEDE2000 matches the nine Sharma reference pairs to 1e-3; a channel cast is removed by the fitted matrix |
| `test_scale_and_seam.py` | Measured px/mm is sub-pixel and robust to the initial guess; a known misregistration is recovered |
| `test_node_api.py` | The HTTP surface behaves, including transparent recovery from a dropped PTP session |
| `test_orchestrator.py` | Two live uvicorn servers on real sockets, driven through a session, with one node deliberately dropping its camera |
| `test_end_to_end.py` | A rig calibrated from scratch produces a measured DPI within 1 % of the model |

Tests run at `scale = 0.16` (roughly 640 × 960 per frame) to keep the suite
under a minute. Because scale does not change the field of view, they exercise
the same code paths as a full 24 MP capture.

---

## 10. Conventions

- **Type hints everywhere**, `from __future__ import annotations` for clean
  unions on Python 3.10.
- **Dataclasses over dicts** for anything with a fixed shape.
- **Docstrings explain *why*.** The *what* is usually obvious from the
  signature; the reason a step exists, or why it is in that order, is not.
- **Errors are actionable.** `"no usable board detected -- is the board flat on
  the platen, fully lit, and inside this camera's tile?"` beats
  `AssertionError`.
- **No hidden global state.** The node keeps its camera on `app.state`, not a
  module global, so several nodes can run in one process — which is what lets
  the orchestrator tests be real.
