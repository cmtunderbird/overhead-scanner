# Day one — the camera has arrived

Ordered so that each step's output is what the next step needs, and so the
things that are painful to redo happen before the things that depend on them.

Budget about three hours for the first pass. Most of it is waiting for
downloads and moving a piece of card around.

---

## 0. Before you open the box (20 min, do it tonight)

**Print the calibration target.**

```bash
python -m scanner board -o charuco_a4.png --dpi 600
```

- Print at **100 % scale**. "Fit to page" silently rescales it, and that
  error goes into every calibration you will ever do with it.
- **Measure a square with a ruler.** It should be 26.0 mm. If it is 25.4,
  your printer scaled it and the whole calibration will be 2.4 % wrong.
- Glue it to something rigid and flat — foam board, MDF, a clipboard.
  A curled sheet is a curved lens, and the solver will happily fit the curl
  into your distortion coefficients.

**Confirm the software runs.**

```bash
pip install -r requirements.txt
python -m pytest -q
python -m scanner selftest
```

If the selftest is green, everything downstream is already proven except
the camera itself.

**And have a look at the interface you will be driving tomorrow:**

```bash
python -m scanner gui --mock          # http://localhost:8800
```

It starts simulated cameras in the same process, so every control works
tonight. See [`gui.md`](gui.md).

---

## 1. Talk to the camera (30 min)

On the machine the camera is plugged into:

```bash
sudo apt install -y libgphoto2-dev
pip install gphoto2
```

**On the camera body, before anything else:**

| Setting | Value | Why |
|---|---|---|
| USB Connection | **PC Remote** | Otherwise it enumerates as mass storage and gphoto2 sees nothing |
| Mode dial | **M** | Sony refuses aperture/shutter over PTP in any other mode |
| Auto Review | **Off** | Blocks the next command while it shows you the shot |
| Pre-AF | **Off** | Hunts between frames and moves your focus |
| Focus | **MF** | Manual. **Not DMF** — see below |

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


**If you get "Could not claim the USB device":**

```bash
systemctl --user mask gvfs-gphoto2-volume-monitor
pkill -f gvfs-gphoto2
```

This is gvfs grabbing the camera the instant it appears. It is the single
most common first-hour failure, and it is why the blueprint specifies Pi OS
**Lite** — no desktop, no gvfs, no problem.

**Then:**

```bash
gphoto2 --auto-detect
SCANNER_BACKEND=gphoto2 python -m scanner node --camera-id cam0 --port 8000
curl localhost:8000/status | python -m json.tool
```

`"connected": true` and a real model string means you are past the hard part.

---

## 2. Set the lens, once, and lock it (20 min)

This is the step that is annoying to redo, because everything after it
depends on the lens never moving again.

1. Zoom to **30 mm**. Check the printed scale on the barrel.
2. Set **f/8**. Not f/11 — the Airy disc is already 2.75 px at f/8, and
   stopping further buys depth of field you do not need behind a platen
   while costing resolution you paid for.
3. Focus manually on a sheet at the working height. **Which distance depends on
   the mode you are building toward:** ~434 mm for A4 single-camera, ~453 mm
   for A3 tiling, **~601 mm for A3 stereo**. With one body in hand, A4 at
   434 mm is the useful configuration; check with
   `python -m scanner geometry -n 1 -f A4`. Use focus magnification and the
   fine print on your test page.
4. **Tape the zoom ring and the focus ring.** Gaffer tape, not masking.
   Two millimetres of zoom creep invalidates the intrinsics, the
   homography and the DPI simultaneously, and you will not notice until
   the seam stops lining up.

Confirm the framing:

```bash
python -m scanner geometry -n 1 -f A4     # 484 DPI, WD 434 mm
```

**Do step 3 from the browser.** In another terminal:

```bash
python -m scanner gui --node cam0=http://localhost:8000 -f A4
```

Open `http://localhost:8800`, go to the **focus** tab and press *Auto-repeat*.
It takes a real full-resolution frame every four seconds and scores five
regions of it, holding the peak per region.

> **The live view cannot show you focus** — it is about 1/35 of the sensor's
> pixels, and the downscale is itself a low-pass filter. The bars are what to
> watch: turn the ring until they stop rising, and if they start falling you
> have gone past. Press *Reset peaks* whenever you change the page, the
> lighting or the height.

Watch the corners, not just the centre. Corner sharpness is the number that
decides whether the kit lens stays.

Then stop the auto-repeat before you walk away — each measurement is a real
shutter actuation.

---

## 3. Intrinsics (30 min)

Shoot **12–16 views** of the ChArUco board. The rules that matter:

- **Tilt it.** 20–30° in both axes. Intrinsics cannot be solved from a
  fronto-parallel plane — focal length and distance trade off exactly, and
  you get a confident, wrong answer. `calibrate_intrinsics` warns you if
  every view was flat, but it cannot invent the information.
- **Fill the corners.** Distortion is largest at the frame edge; a stack
  that only covers the middle leaves `k1` unconstrained.
- Vary the distance a little, ±15 %.
- Keep it sharp and evenly lit. A blurred board gives blurred corners.

```bash
# shoot them however you like -- tethered, or with the node:
python -m scanner capture --node cam0=http://localhost:8000 -c 16 --interactive
```

Then calibrate (see `scanner/calib/intrinsics.py`; a `scanner calibrate`
subcommand is the obvious next addition). **Read the diagnostics, not just
the RMS.** A low RMS from six flat views is a fit, not a calibration. In
the synthetic rehearsal, 12 well-spread views give RMS ≈ 0.10 px and
recover the focal length to 0.04 %.

---

## 4. Document alignment (15 min)

Lay the board **flat on the platen**, at a known position, and shoot it
once. `solve_document_homography` turns that into the millimetre-to-pixel
map, and `mounting_error` tells you in plain units how far off the mount is:

```
cam0: rms 0.216 px | mount dx -1.56 dy +1.76 mm rot -0.232 deg scale +0.050 %
```

Those are numbers you can act on with an Allen key. If `scale_error_pct`
is more than a few tenths, your working distance is not what you think it
is. If `keystone` is not near zero, the camera is not perpendicular to the
platen.

---

## 5. Flat-field and colour (15 min)

- **Flat-field:** three shots of an evenly lit blank sheet at the working
  height. `calibrate_flat_field` smooths hard on purpose — you want the
  lamp and lens falloff, not the dust.
- **Colour:** shoot a chart. With one camera this only fixes absolute
  colour; its real job starts with the second body, where an unmatched
  pair puts a visible band straight down the middle of every spread.

---

## 6. First real page (10 min)

```bash
python -m scanner capture --node cam0=http://localhost:8000 -c 1 -o captures/
python -m scanner process captures/ -k calibration/rig.json -o masters/
python -m scanner measure masters/00000_master.tif --edge <x,y,w,h> --px-per-mm 19.1
```

Print a page with a slanted edge and a ruler on it — `scanner.synth.page`
generates exactly that — and you can measure your real MTF50 and real DPI
on day one instead of guessing.

**Targets:** MTF50 ≥ 0.30 cy/px, measured DPI within 1 % of prediction.

---

## What to expect, honestly

| | Likely |
|---|---|
| MTF50 at the centre | 0.30–0.38 cy/px — a kit lens at f/8 is decent, not stellar |
| MTF50 at the corners | Lower. This is the number that decides whether the kit lens stays |
| Measured DPI | Within 1 % of prediction, or your working distance is off |
| First-hour blocker | gvfs claiming the USB device |
| Second-hour blocker | Realising you focused on the platen surface, not the paper |

If corner MTF50 comes in badly — under ~0.22 — that is the argument for a
prime lens, and it is worth measuring before spending anything.

---

## The one thing not to skip

**Tape the zoom ring.** Everything in `calibration/` becomes fiction the
moment it moves, and nothing in the software can detect that it happened.
