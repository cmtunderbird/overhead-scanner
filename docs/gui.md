# The operator interface

A browser UI for running the rig: live view, focus assist, capture, and the
geometry the rig is currently configured for. One HTML file, no build step, no
npm, no CDN.

```bash
python -m scanner gui --mock                      # try it now, no hardware
python -m scanner gui --node cam0=http://pi1:8000 --node cam1=http://pi2:8000
python -m scanner gui --node ... --host 0.0.0.0   # reach it from a tablet
```

Then open `http://localhost:8800`.

---

## What it is, structurally

The GUI is a **proxy, not a page that talks to the cameras**. The browser only
ever talks to the laptop; the laptop talks to the nodes.

```
browser ──HTTP──► scanner gui (laptop) ──HTTP──► node (Pi) ──PTP──► A6000
```

Three reasons it is built this way, and they are the same three reasons the
capture path is:

- **The camera network need not be routable from the browser.** The Pis can sit
  on a private wired segment with only the laptop bridging to it, which is what
  you want when you are moving 24 MB per frame.
- **One place for timeouts and error text.** A node that has lost its PTP
  session produces one message, in one format, whoever asked.
- **It works from a phone.** `--host 0.0.0.0` and the interface is usable from
  anything on the LAN, with no client install and no per-device configuration.

It adds no state of its own beyond the current rig settings, the peak focus
scores, and the open `CaptureSession` — everything durable is still files on
disk and the session manifest.

---

## The four panels

### capture

Profile, spread count, output folder, and the button. Throughput appears as
soon as the first spread lands, and every captured frame shows in the strip
along the bottom.

The numbers to watch are `mean_s` and `mb_per_s`. **The sustainable burst size
is set by one inequality: transfer time ≤ page-turn time.** If `mean_s` is
creeping up across a book, the queue is growing and the profile is too rich for
the page-turn rate.

### focus

This is the panel that earns the interface.

> **Live view is roughly 1/35 of the sensor's pixels and cannot show you
> focus.** A preview can look perfectly sharp on a frame that is visibly soft at
> full size, because the downscale is itself a low-pass filter.

So `GET /focus` **takes a real frame** — a full-resolution capture through the
same path as a scan — and scores five regions of it with Tenengrad (mean squared
Sobel gradient, normalised by image variance so it is not merely an exposure
meter).

**The absolute number means nothing.** It depends on the page, the lighting and
the lens. What means something is the *shape of the curve as you turn the ring*,
so the interface holds the peak per region and shows you the current score as a
percentage of it:

| Reading | What it means |
|---|---|
| 100 % of peak, and rising as you turn | keep going |
| 100 %, and it stops rising | you are at focus |
| falling below 100 % | you have gone past — come back |

Press **Reset peaks** when you change page, lighting or working distance;
otherwise you are comparing against a peak from a different scene.

Five regions, not one, because **the corners are the number that decides whether
the kit lens stays**. A centre-sharp, corner-soft frame is a lens verdict, and
it is invisible on a single central score. The zones are drawn on the returned
frame so you can see exactly which patch produced which number.

Exposure comes back with it: mean level and the fraction of clipped highlights
and shadows. Clipped highlights on paper mean the flat-field has nothing left to
work with — no correction recovers a channel that saturated.

**Auto-repeat** re-measures every 4 s so you can turn the ring with both hands
and watch the bars. Each repeat is a real shutter actuation; use it while
focusing, not while thinking.

### rig

Coverage mode, format, aperture, baseline, overlap — and the full
`RigGeometry.report()` recomputed live as you change them. It is a **preview
until you press Apply**: the server evaluates the configuration without
committing it, so you can compare modes without disturbing a session in
progress.

Switching between `stereo` and `tile` here shows the trade-off the rig is built
around, in the numbers rather than in prose:

| | stereo | tile |
|---|---|---|
| Resolution, A3 | 342 DPI | 462 DPI |
| Working distance | 601 mm | 453 mm |
| Depth measured over | 100 % of the page | 4.8 % (the overlap band) |

An impossible configuration is reported rather than swallowed — stereo with a
zero baseline comes back as an error string, not as a plausible-looking answer.

### cameras

Node URLs (one per line) and ISO/shutter/aperture applied to every body at once.
Results are per camera: a Sony that refuses a setting because the mode dial is
not on **M** reports the refusal rather than silently ignoring it.

---

## Endpoints

Everything the page does is available directly, which makes the interface
scriptable and the failures diagnosable with `curl`.

| Method | Path | Does |
|---|---|---|
| GET | `/` | the page |
| GET | `/api/state` | nodes, rig, geometry summary, throughput |
| POST | `/api/nodes` | replace the node list (starts a new session) |
| GET/POST | `/api/rig` | read / change coverage, format, optics, profile |
| GET | `/api/geometry` | **non-committing** preview of a configuration |
| GET | `/api/preview/{cam}` | one JPEG live-view frame |
| GET | `/api/stream/{cam}?fps=` | MJPEG relay of the node's stream |
| POST | `/api/connect/{cam}` | re-open a dropped PTP session |
| GET | `/api/focus/{cam}` | **full capture**, five region scores, exposure, JPEG |
| POST | `/api/focus/{cam}/reset` | clear held peaks |
| POST | `/api/capture` | capture N spreads |
| GET | `/api/session/manifest` | every record so far |
| POST | `/api/session/reset` | start a new session |
| GET | `/api/thumb?path=` | downscaled preview of a captured file |
| GET | `/healthz` | liveness |

Node-side additions that make this possible are `GET /stream` and `GET /focus`
on the node itself — see [`api.md`](api.md).

---

## Details that are deliberate

**The camera panel is rebuilt only when a node's identity or readiness
changes.** The state poll runs every 6 s; rebuilding the panel on every poll
would tear down the live view four times a minute, which is precisely when you
are looking at it.

**The focus overlay is positioned against the image, not the frame.** The
element carrying the zone boxes is given the image's own aspect ratio, so the
box the operator sees is the box the score came from. Positioning against a
letterboxed container would put the "bottom right" box somewhere that is not the
bottom right of the sensor, which would be worse than showing nothing.

**Thumbnails are decoded server-side and downscaled to 200 px.** The strip must
stay cheap; a 24 MP frame per capture in the footer would make the page unusable
after ten spreads. RAW files that OpenCV cannot decode show as `raw` rather than
as a broken image.

**No `localStorage`, no CDN, no framework.** The interface has to work on a
laptop on an isolated camera network with no internet, and it has to still open
in five years.

---

## Testing

`tests/test_gui.py` runs **real uvicorn mock nodes in-process** and points the
GUI at them over loopback. Testing the proxy with a `TestClient` alone would
prove nothing: every interesting failure lives in the hop between the laptop and
the node.

```bash
python -m pytest tests/test_gui.py -q      # 24 tests, ~10 s
```

Covered: focus scoring separates sharp from blurred and is monotonic in blur; a
soft corner is localised to *that* corner; an unreachable node is reported
rather than raised; the geometry preview does not commit; `/focus` really does
consume a shutter actuation and returns more pixels than the preview; peaks are
held and reset; captures land on disk and the manifest finds them.
