# Hardware

Bill of materials, camera settings, and the physical build.

---

## Bill of materials

| Component | Spec | Role | Indicative |
|---|---|---|---|
| 2 × Sony A6000 | 24.3 MP APS-C, 6000 × 4000, 3.92 µm pitch | Capture, one per half-spread | ~€600 used |
| 2 × E-mount kit lens | 18–55 OSS or 16–50 PZ, **locked at 30 mm, f/8** | Imaging | with bodies |
| 2 × Raspberry Pi 5 | 8 GB, **Pi OS Lite 64-bit** | Camera servers | ~€180 |
| 2 × AI HAT+ | Hailo-8, 26 TOPS | Live QA per node | ~€220 |
| 2 × PSU | **Official 27 W USB-C**, not a generic charger | Node power | ~€30 |
| 2 × Active Cooler | Pi 5 | The AI HAT+ ships no NPU heatsink | ~€15 |
| 2 × A2 microSD, 32 GB+ | — | Node boot media | ~€20 |
| Gigabit switch + wired LAN | — | Transport | ~€30 |
| Acer Nitro V15 | RTX 5060, 8 GB VRAM | Orchestration + batch processing | in hand |
| Rigid frame + cross rail | Adjustable baseline and toe-in | Geometry | ~€150 |
| Platen | 3 mm acrylic or glass | Page flattening | ~€15 |
| 2 × lamp bars | High-CRI, diffuse | Illumination | ~€80 |
| Polariser film + lens polarisers | *Optional fallback* | Glare | ~€20 |

**Indicative total ≈ €1 500**, against ~€600 for a CZUR that cannot be
reconfigured, cannot be moved closer for higher DPI on smaller originals, and
cannot run your pipeline.

**Pi OS Lite is not a preference.** `gvfs-gphoto2-volume-monitor` claims the
camera the instant it enumerates and every command then fails; Lite has no
desktop and therefore no gvfs. See [`decisions.md` D13](decisions.md#d13-pi-os-lite-specifically).

**The PSU is not a detail either.** A Pi 5 on anything other than a 5 A / 25 W
supply restricts *all* downstream USB peripherals to 600 mA combined — with a
tethered body and a PCIe NPU on the same board, that is the difference between a
node that works and one that browns out mid-book. The AI HAT+ occupies the single
PCIe lane, so an NVMe HAT cannot share it; boot from the SD card.

Note that first light needs none of the Pi hardware — the node runs fine on the
laptop with the camera plugged straight in.

---

## Geometry to build to

From `python -m scanner geometry` for the configuration you intend to shoot:

| | **Stereo, A3** | Tile, A3 | 1 camera, A4 |
|---|---|---|---|
| Working distance (lens principal plane → page) | **601 mm** | 453 mm | 434 mm |
| Camera baseline | **200–300 mm, free** | 200 mm, forced | — |
| Mounting | **converged ~19°** | parallel, toe-in 0° | — |
| Each camera covers | **the whole spread** | 220 mm tile | 297 mm |
| Depth of field at f/8 | **±20.5 mm** | ±11.4 mm | ±10.4 mm |
| Resolution | 342.1 DPI | 461.8 DPI | 483.8 DPI |

**The mast must reach 601 mm**, not just 453 — stereo mode needs the longer
standoff so both cameras can cover the whole spread. Build for the taller one.

Mixing up the two working distances is easy and produces a measured DPI that
disagrees with the model — see [`troubleshooting.md`](troubleshooting.md).

The dimensioned drawing set is [`scanner-blueprint.html`](scanner-blueprint.html):
plan view, both elevations, capture architecture, pipeline, and a numbers sheet
with a live overlap slider. Open it in a browser.

---

## Camera settings — non-negotiable

On each body, before anything else:

| Setting | Value | Why |
|---|---|---|
| USB Connection | **PC Remote** | Otherwise it enumerates as mass storage and gphoto2 sees nothing |
| Mode dial | **M** | Sony refuses aperture/shutter over PTP in any other mode |
| Auto Review | **Off** | Blocks the next command while it shows you the shot |
| Pre-AF | **Off** | Hunts between frames and moves your focus |
| Focus | **MF** | Manual. **Not DMF** — see below |
| `capturetarget` | **card**, set explicitly | The default varies and silently changes all your timing |

> **Focus must be MF, not DMF.** libgphoto2's `camera_sony_capture()` skips its
> entire focus-wait loop only when `FocusMode == 1`, which is Manual. DMF is not
> 1, so it re-enters the wait and costs up to a second a frame — and DMF holds an
> AF interlock that can refuse the shutter outright, which is recorded in
> `exposure-and-lighting.md` §3 as a capture that never happened. Advice
> recommending DMF (BYU's `a6000_ros`, 2018) was written against libgphoto2
> 2.5.21, where the focus wait ran unconditionally and capped at 1 s, so DMF cost
> nearly nothing. On a current stack it is a liability.
>
> **Leave a card in the body**, permanently. Not for storage — `capturetarget`
> is absent, nothing is written to it, and in PC Remote the card is not even
> visible over PTP. It is what makes the body capture reliably at all.


Electronic first-curtain shutter on. Remote trigger only — nothing touches the
frame at exposure time.

---

## The lens, and the one thing not to skip

1. Zoom to **30 mm** using the printed scale on the barrel.
2. Set **f/8**. Not f/11 — the Airy disc is already 2.75 px and the system is
   diffraction limited. Every further stop costs resolution and buys depth of
   field the platen makes unnecessary.
3. Focus manually on a sheet at the working height, using focus magnification
   and fine print.
4. **Tape the zoom ring and the focus ring.** Gaffer tape, not masking.

Two millimetres of zoom creep invalidates the intrinsics, the homography and the
DPI simultaneously — and nothing in the software can detect that it happened.
Everything in `calibration/` becomes fiction the moment the lens moves.

---

## Frame and mounting

**Rigidity beats stabilisation.** Rigidity is what keeps the intrinsics and the
stitching homography valid between sessions. A frame that flexes turns a
calibrated seam into a wandering one, and no adaptive stitching fixes that
properly.

- **Cross rail with adjustable baseline and toe-in.** Toe-in is **required**,
  not optional: in stereo mode both cameras must be converged on the page
  centre. Parallel mounting would need a field almost twice the page width and
  would throw away most of the sensor. Tile mode uses toe-in 0°. This is the
  one element that would be genuinely expensive to retrofit.
- Mount so the sensor's **short axis runs across the split** (portrait for the
  two-camera A3 configuration) — this is worth 462 DPI instead of 342.
- Verify with `mounting_error()` after alignment: it reports offset in
  millimetres, rotation in degrees and a keystone term. Non-zero keystone means
  the camera is not perpendicular to the platen — shim it, because a homography
  corrects the geometry but cannot make the depth-of-field plane parallel to the
  page.

---

## Lighting

- **Lamps low and wide**, so the specular lobe reflects clear of both lenses.
- **High CRI.** Colour calibration corrects a consistent cast; it cannot recover
  wavelengths a cheap LED never emitted.
- **Diffuse.** A bare source gives a bright hotspot the flat-field will fight.
- Nominal lamp height in the drawings is ~190 mm above the platen, out beyond
  the field edge.

**Cross-polarisation** (film on the lamps, polarisers on the lenses, ~€20) is the
fallback if reflections persist. It is purely optical, needs no electronics, and
is also a precondition if you ever pursue photometric shape recovery — which
assumes a Lambertian surface, and paper only is once the specular component is
removed.

---

## The platen — now optional

3 mm acrylic or glass over the page. In **tile mode** it is load-bearing: it
makes the page flat, which is what makes the homography valid.

In **stereo mode it is optional**, and for a thick or fragile binding it should
be left off — the curvature is measured instead of suppressed, which is the
entire reason the rig is stereoscopic. Use it for loose sheets and thin
material, where it is still the cheapest way to gain sharpness.

What it does not fix, and nothing can: **foreshortening near an unflattened
spine.** As the page tilts to angle θ, sampling density falls by cos θ — at 80°
you have under a fifth of your DPI, and no algorithm recovers information that
was never sampled. The CZUR cannot do this either.

For fragile bindings that must not be pressed, two fallbacks are characterised:
the free overlap stereo band (27 µm) and a €30 DOE line-laser pair (~10 µm). See
[`optics.md` §9](optics.md#9-precision-budget-for-dewarping).

---

## Node setup (Raspberry Pi)

Scripted. Full procedure and reasoning in [`node-setup.md`](node-setup.md).

```bash
# Raspberry Pi OS Lite 64-bit (Trixie), then on each Pi:
git clone https://github.com/cmtunderbird/overhead-scanner
cd overhead-scanner
chmod +x scripts/*.sh
sudo ./scripts/provision-node.sh --role 0 --ip 10.10.0.10/24 --ntp 10.10.0.1
sudo reboot
sudo ./scripts/check-node.sh          # exits non-zero if anything is not ready
```

`--role` sets the hostname, and the node server derives its camera id from that,
so both Pis run an identical card.

**The hand-typed recipe that used to be here no longer works.** Pi OS is now
Debian 13 Trixie, and it broke in three places: `pip install` into the system
Python is refused by PEP 668 (and `python3-pip` is not even present on Lite, so
`--break-system-packages` is unreachable); the Hailo driver package was renamed
from `hailo-dkms` to `hailort-pcie-driver`; and a unit whose `ExecStart` is
`/usr/bin/python3` cannot see the venv the code now has to live in. Each failure
looks like something else, which is why this is a script with a checker rather
than a code block. Details in [`node-setup.md`](node-setup.md).

Three things the script sets up that are easy to miss by hand:

- **A venv built with `--system-site-packages`.** `python3-hailort` installs
  `hailo_platform` into the system `dist-packages` and publishes no wheel, so an
  isolated venv cannot see the NPU at all.
- **The service user in `plugdev`.** systemd's `uaccess` tag grants a device ACL
  only to a user with an active local seat; over SSH there is none. This — not
  only gvfs — is what produces *Could not claim the USB device* on a headless Pi.
- **`opencv-contrib-python-headless`**, via
  [`requirements-node.txt`](../requirements-node.txt). The desktop build links
  `libGL.so.1`, which a Lite image does not have.

No `config.txt` edits are needed for the AI HAT+: it is a true HAT+, so the PCIe
connector enables itself and Gen 3 is applied automatically. `dtparam=pciex1_gen=3`
is for the M.2 AI Kit.

**Wired LAN, not Wi-Fi.** You are moving 24 MB per frame and the throughput
inequality is tight enough without contention. Static addresses with no gateway
and no DNS on the camera segment: a gateway that never answers ARP is the usual
cause of "it worked, then it stopped".

---

## Throughput budget

**Measured 2026-08-12** on the real rig: 24.53 MB per ARW at **9.25 MB/s**,
**~2.65 s per frame** end to end. The earlier 10–15 MB/s was an assumption.

| Profile | Frames/camera | Data | Time | Use |
|---|---|---|---|---|
| `standard` | 1 | 24.5 MB | **2.65 s** | default |
| `clean` | 3 | 73.6 MB | **7.96 s** (measured) | faded or very fine print |
| `max` | 9 | 220.7 MB | ~24 s (extrapolated) | rare pages only |

Three frames cost three times one frame to within 0.1 s: **there is no burst
discount**, and no card buffer to hide behind — this body has no
`capturetarget`, so transfer is serial with capture.

**The sustainable burst size is set by one inequality: transfer time ≤ page-turn
time**, or the queue grows without bound. At 2.65 s per frame and a 4 s page turn
that is **one frame**, with a second only if the operator is slower than 5.3 s
per page. Hence `standard` is one frame.

Stacking here is **noise reduction only**: no IBIS and a rigid mount mean no
sub-pixel dither, so there is no true multi-frame resolution gain, just √N.

A 400-page book is 200 spreads ≈ **9.4 GB** of ARW at `standard`, roughly **10
minutes** of capture.

---

## Assembly order

Build in an order where each step can be checked before the next depends on it.

1. **Baseboard and platen** — flat, and mark where the page registers.
2. **Column and cross rail** — set the height to the working distance for your
   primary format. Check with a tape measure before trusting anything.
3. **One camera** — mount, set the lens, tape the rings, calibrate, shoot a
   test page, measure DPI and MTF50. Do not add the second camera until the
   first one measures correctly.
4. **Lamps** — position, then re-shoot the flat-field.
5. **Second camera** — mount at the calibrated baseline, calibrate it
   independently, then check `cross_camera_delta_e` and `measure_seam`.
6. **Nodes and LAN** — move from a directly attached camera to the Pi nodes
   last. It is a transport change and should not alter a single measurement; if
   it does, something else is wrong.

---

## Acceptance

| Metric | Target |
|---|---|
| Measured DPI, A3 spread | ≥ 450 |
| MTF50, slanted edge, page corners | ≥ 0.30 cy/px |
| Stitch seam error | < 1 px |
| Colour ΔE between cameras | < 2 |
| Throughput at `standard` | ≤ 3 s/spread |
| OCR character error rate | ≤ CZUR, shared test set |

**The benchmark that settles it:** the same 20-page book on both machines,
measuring MTF50, OCR CER, colour ΔE and dewarp residual. Everything else is
marketing.
