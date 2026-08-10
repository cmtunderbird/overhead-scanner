# Status

*Last updated: 2026-08-10*

**Phase 0 is complete. Phase 1 is half-built — the software half.** The first
camera body arrives imminently; nothing in the software is waiting on it except
real photons.

---

## Against the blueprint's build plan

| Phase | Gate | State |
|---|---|---|
| **0** | Full pipeline running against sample images | ✅ **Done** |
| **1** | One Pi + one camera, `/capture` + `/files` end to end | 🟡 Software complete and tested against a simulated body; **awaiting hardware** |
| **2** | Frame, rail, lamps, platen; both nodes live | ⬜ Not started — nothing physical exists |
| **3** | Full calibration; seam invisible, ΔE < 2 | 🟡 Code complete, proven synthetically; **needs real photons** |
| **4** | Stitch, deskew, split, normalise; measured ≥ 450 DPI on real A3 | 🟡 Pipeline exists; the measurement has not happened |
| **5** | OCR + export; CER ≤ CZUR | ⬜ **Not started** |
| **6** | 2× SR track, live QA on Hailo | ⬜ **Not started** |

---

## What is built and verified

Every row below is measured by code in this repository, against ground truth
from the camera simulator. `python -m scanner selftest` reproduces all of it.

| Capability | Verified to |
|---|---|
| Optical model | Reproduces every blueprint figure exactly |
| Intrinsics + distortion (ChArUco) | RMS 0.10 px; focal length to 0.04 %; `k1` to 0.1 % |
| Document alignment | RMS 0.22 px; mounting rotation to 0.016°; scale to 0.05 % |
| Flat-field | Corner/centre ratio within 5 % of unity after correction |
| Colour, per camera | ΔE2000 0.16 / 0.34 |
| **Cross-camera colour (the seam)** | **ΔE2000 0.32** |
| Rectify + stitch | 100 % coverage, sub-pixel registration |
| Deskew | Residual 0.00° |
| Spine detection and split | 0.0 mm error |
| Background normalisation | Gutter shadow measurably reduced |
| Measured vs predicted DPI | 0.04 % |
| MTF50 harness | Accurate to 1–2 % from 0.06 to 0.58 cy/px |
| CIEDE2000 | Matches all nine Sharma reference pairs to 4×10⁻⁵ |
| Node API | 12 tests over real HTTP, including PTP-drop recovery |
| Orchestrator | 8 tests against two live uvicorn servers |
| Calibration persistence | Byte-identical round trip |
| Throughput | 1.5 s per spread (was 10.1 s) |

**89 tests, ~55 s. 17/17 acceptance checks.**

---

## What is deliberately not built

Being explicit, because a repository that looks complete but is not is worse
than one that admits its gaps.

### Missing CLI commands

- **`scanner calibrate`** — the library calls work and are exercised by tests,
  but there is no CLI wrapper. Calibration today needs a short script (given in
  full in [`calibration.md` §6](calibration.md#6-complete-example)). **This is
  the highest-value next task** — it is the friction between tomorrow morning
  and a real calibration.
- **`scanner align`** — same, for the document homography step.

### Phase 5 — output. Not started.

No OCR, no PDF/A writer, no ALTO/hOCR sidecars. The pipeline stops at the
archival master, which is the right place for it to stop, but it means there is
currently no searchable output at all.

Planned: PaddleOCR + Tesseract 5 combined for language breadth, PDF/A-2b with an
invisible text layer.

### Phase 6 — enhancement. Not started.

No super-resolution, no live QA on the Hailo accelerators. Design work is done
(see [`decisions.md` D12](decisions.md#d12-2-super-resolution-not-4)): 2× rather
than 4×, tiled at 512–1024 px because activation memory, not output size, is the
binding constraint on 8 GB of VRAM.

### Other known gaps

- **RAW decoding.** Frames are read as 8-bit BGR. A real ARW path belongs before
  `correct_frame` and needs 16-bit colour LUTs.
- **Burst merge.** `clean` and `max` profiles capture multiple frames and the
  orchestrator stores them all, but the pipeline currently processes the first.
  Align-and-average is not implemented.
- **Content-based dewarping.** Unnecessary with a platen; hooks are documented
  but nothing is written.
- **No CI.** The test suite runs locally. `selftest` returns a non-zero exit
  code on failure and would work as a CI gate.

---

## Hardware status

| Item | State |
|---|---|
| Acer Nitro V15 (RTX 5060) | ✅ In hand — the orchestrator machine |
| Sony A6000 #1 | 🟡 Arriving |
| Sony A6000 #2 | ⬜ Not ordered |
| E-mount kit lenses ×2 | ⬜ Assumed to come with the bodies — **confirm both are the same model**, matched glass makes calibration much easier |
| Raspberry Pi 5 ×2 | ⬜ Status unconfirmed |
| AI HAT+ (Hailo-8) ×2 | ⬜ Status unconfirmed |
| Frame, cross rail | ⬜ Not built |
| Platen (3 mm acrylic) | ⬜ Not sourced |
| Lamps, diffusers | ⬜ Not sourced |
| ChArUco target | ⬜ **Print tonight** — `python -m scanner board` |

Note the software runs the node fine on the laptop directly; the Pis are for the
final two-camera rig, not for first light.

---

## The near-term path

**Tonight**

1. Print the ChArUco board at 100 % scale and **measure a square with a ruler**
2. `pip install -r requirements.txt && python -m scanner selftest`

**When the camera arrives** — [`day-one.md`](day-one.md) has the detail

3. `sudo apt install libgphoto2-dev && pip install gphoto2`; set the body to PC
   Remote, mode M, Auto Review off, Pre-AF off
4. Lens to 30 mm f/8, focus at **434 mm** for A4, then **tape both rings**
5. 12–16 tilted board views → intrinsics; read the diagnostics, not just the RMS
6. Board flat on the platen → document homography; read `mounting_error`
7. Flat-field and colour
8. First real page: measure MTF50 and DPI rather than guessing

A single body on A4 gives **484 DPI**, already past the CZUR's 441 — so Phase 1
and a genuinely usable scanner arrive at the same time.

---

## Open questions from the blueprint

1. **The WebCam Super Resolution notes** — still outstanding, and now the last
   real unknown in the Phase 6 design.
2. **Are both kit lenses the same model?** Matched glass makes calibration
   substantially easier.
3. **Book thickness range** — sets whether the platen alone is sufficient or a
   V-cradle is needed for tight bindings.
