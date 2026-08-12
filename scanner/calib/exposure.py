"""
Exposure calibration: let the camera's own meter do the arithmetic.

The operator problem this solves
--------------------------------
`expprogram` is **read-only** over PTP, so the mode dial has to be moved by
hand. That gives a cheap, accurate procedure:

  1. Dial to **A**, set f/8 and ISO 100, aim at the page under the real
     lighting.  The camera solves for the one unknown: shutter.
  2. Take one frame.  Read back what it chose.
  3. Correct it, switch to **M**, and dial the corrected values.

Step 3 is the part that needs care, and it is why this module exists.

Why the metered value is wrong for documents
--------------------------------------------
A reflected-light meter renders whatever it sees as **middle grey**.  Aim it
at white paper and it faithfully makes the paper grey -- roughly two stops
underexposed.  Copying the metered shutter into M therefore produces dark
scans with the paper sitting near 118/255 instead of near 230/255.

Two ways to correct it, and this module prefers the second:

* **Assumed** -- add a fixed +2 stops.  The usual rule of thumb.  Works,
  but it is a guess about the paper's reflectance and the meter's pattern.
* **Measured** -- look at the frame that was actually taken, find where the
  paper landed, and compute exactly how many stops move it to target.  This
  self-corrects for the meter's behaviour, the paper, and the lighting.

`recommend()` uses the measured path whenever a histogram is available and
says so in `basis`, falling back to the assumed one otherwise.  A
recommendation that cannot say which basis it used is not trustworthy.

Paper level, not mean level
---------------------------
The mean brightness of a page is dragged down by the text, and by however
much dark background surrounds the page in frame.  Neither is what we are
exposing for.  The **90th percentile** is used instead as the estimate of
paper white: high enough to sit on the paper rather than the ink, low enough
not to be pinned by a specular highlight.

Gamma
-----
Levels come from a JPEG preview, which is gamma-encoded (sRGB, ~2.2).
Doubling the exposure does *not* double the stored level.  Stops must be
computed in linear light, so levels are linearised before the ratio is
taken.  Skipping this underestimates the correction by more than half.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field

#: sRGB is close enough to a 2.2 power law for exposure work.
GAMMA = 2.2

#: Where we want paper white to sit: bright, with headroom before clipping.
TARGET_PAPER_LEVEL = 230.0

#: Percentile of the frame taken to *be* the paper. See module docstring.
PAPER_PERCENTILE = 90.0

#: The rule-of-thumb correction, used only when nothing was measured.
ASSUMED_STOPS = 2.0

#: Above this, the frame is already clipping and must come down.
CLIP_LIMIT_PCT = 1.0


# ---------------------------------------------------------------- shutter ----

def parse_shutter(value) -> float:
    """
    Seconds from the many ways a shutter speed is written.

    Sony over PTP is inconsistent by design: the same body reports whole
    seconds as ``'2'``, sub-second as ``'1/250'``, and -- confusingly --
    also as unreduced fractions like ``'5/10'`` for 1/2 s.  A parser that
    handles only ``1/x`` silently mis-reads the slow end of the range.
    """
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if not s or s in {"bulb", "auto", "unknown"}:
        raise ValueError(f"not a shutter speed: {value!r}")
    s = s.rstrip("s").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", s)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        if den == 0:
            raise ValueError(f"zero denominator: {value!r}")
        return num / den
    return float(s)


def format_shutter(seconds: float) -> str:
    """Human form, in the same style the body uses."""
    if seconds >= 1:
        return f"{seconds:g}"
    return f"1/{round(1 / seconds):g}"


def usable_choices(choices) -> list[tuple[float, str]]:
    """Parse a body's shutter list, dropping what cannot be used."""
    out = []
    for c in choices:
        try:
            out.append((parse_shutter(c), str(c)))
        except ValueError:
            continue          # 'Bulb', 'Auto', junk
    return sorted(out)


def snap_to_choices(target_s: float, choices) -> tuple[float, str] | None:
    """
    Nearest available shutter *in stops*, not in seconds.

    Nearest-in-seconds is wrong: between 1/60 and 1/125, plain subtraction
    is dominated by the slow end and picks a value up to half a stop out.
    Exposure error is logarithmic, so the metric must be too.
    """
    usable = usable_choices(choices)
    if not usable or target_s <= 0:
        return None
    return min(usable, key=lambda v: abs(math.log2(v[0] / target_s)))


# -------------------------------------------------------------- exposure ----

def ev100(aperture: float, shutter_s: float, iso: float = 100.0) -> float:
    """Exposure value normalised to ISO 100."""
    if aperture <= 0 or shutter_s <= 0 or iso <= 0:
        raise ValueError("aperture, shutter and ISO must be positive")
    return math.log2(aperture ** 2 / shutter_s) - math.log2(iso / 100.0)


def linearise(level: float) -> float:
    """Gamma-encoded 0-255 level to linear light."""
    return (max(level, 1e-6) / 255.0) ** GAMMA


def stops_to_target(current_level: float,
                    target_level: float = TARGET_PAPER_LEVEL) -> float:
    """
    Stops of extra exposure to move `current_level` to `target_level`.

    Positive means "give it more light" (a longer shutter).
    """
    return math.log2(linearise(target_level) / linearise(current_level))


# --------------------------------------------------------------- reading ----

@dataclass
class FrameStats:
    """What a metering frame actually looks like."""
    paper_level: float
    mean_level: float
    clipped_high_pct: float
    clipped_low_pct: float


@dataclass
class ExposureReading:
    """What the body chose, plus what the frame looks like."""
    iso: float
    aperture: float
    shutter_s: float
    stats: FrameStats | None = None

    @property
    def ev(self) -> float:
        return ev100(self.aperture, self.shutter_s, self.iso)


@dataclass
class Recommendation:
    """Settings to dial in M, and the reasoning behind them."""
    iso: float
    aperture: float
    shutter_s: float
    shutter_label: str
    stops_applied: float
    #: "measured" (from the frame's histogram) or "assumed" (+2 rule).
    basis: str = "assumed"
    warnings: list[str] = field(default_factory=list)
    #: Exposure requested before snapping to what the body offers.
    requested_shutter_s: float = 0.0

    @property
    def snap_error_stops(self) -> float:
        if not self.requested_shutter_s:
            return 0.0
        return math.log2(self.shutter_s / self.requested_shutter_s)


def recommend(reading: ExposureReading,
              choices,
              *,
              target_level: float = TARGET_PAPER_LEVEL,
              iso: float | None = None,
              aperture: float | None = None) -> Recommendation:
    """
    Turn one A-mode metering frame into settings for M.

    `iso` and `aperture` override what was metered -- use them when the
    metering frame was shot at settings you do not intend to keep. Exposure
    is held constant across the swap, so changing either moves the shutter
    to compensate rather than silently changing the result.
    """
    warnings: list[str] = []

    if reading.stats is not None:
        stops = stops_to_target(reading.stats.paper_level, target_level)
        basis = "measured"
        if reading.stats.clipped_high_pct > CLIP_LIMIT_PCT:
            warnings.append(
                f"metering frame already clips "
                f"{reading.stats.clipped_high_pct:.1f}% of highlights; "
                f"detail there is gone and cannot be recovered by exposure"
            )
    else:
        stops = ASSUMED_STOPS
        basis = "assumed"
        warnings.append(
            "no histogram available, so the standard +2 stop white-paper "
            "correction was assumed rather than measured"
        )

    out_iso = float(iso if iso is not None else reading.iso)
    out_ap = float(aperture if aperture is not None else reading.aperture)

    # Hold total exposure constant while swapping ISO/aperture, then apply
    # the correction. Doing this in EV keeps the three axes consistent.
    target_ev = reading.ev - stops
    shutter = (out_ap ** 2) / (2 ** target_ev * (out_iso / 100.0))

    snapped = snap_to_choices(shutter, choices)
    if snapped is None:
        warnings.append("body offered no usable shutter speeds")
        label, secs = format_shutter(shutter), shutter
    else:
        secs, label = snapped

    rec = Recommendation(
        iso=out_iso,
        aperture=out_ap,
        shutter_s=secs,
        shutter_label=label,
        stops_applied=stops,
        basis=basis,
        warnings=warnings,
        requested_shutter_s=shutter,
    )

    if abs(rec.snap_error_stops) > 0.34:
        warnings.append(
            f"nearest available shutter is {rec.snap_error_stops:+.2f} stops "
            f"from ideal; adjust ISO or lighting to land closer"
        )
    if secs > 0.5:
        warnings.append(
            f"{label}s is long enough that vibration will blur the page; "
            f"more light, or a wider aperture, is the fix -- not a steadier hand"
        )
    return rec


# ------------------------------------------------------- frame inspection ----

def histogram_stats(image_bytes: bytes,
                    percentile: float = PAPER_PERCENTILE) -> FrameStats:
    """Paper level, mean and clipping from a JPEG (or any PIL-readable image)."""
    from PIL import Image

    im = Image.open(io.BytesIO(image_bytes)).convert("L")
    hist = im.histogram()
    total = sum(hist)
    if total == 0:
        raise ValueError("empty image")

    mean = sum(i * c for i, c in enumerate(hist)) / total

    want = total * percentile / 100.0
    running = 0
    paper = 255.0
    for level, count in enumerate(hist):
        running += count
        if running >= want:
            paper = float(level)
            break

    return FrameStats(
        paper_level=paper,
        mean_level=mean,
        clipped_high_pct=sum(hist[250:]) / total * 100.0,
        clipped_low_pct=sum(hist[:5]) / total * 100.0,
    )


def extract_preview(raw: bytes) -> bytes | None:
    """
    Pull the largest embedded JPEG out of a raw file.

    ARW is TIFF-based and carries several JPEGs (thumbnail, preview, and on
    some bodies a full-size one). Rather than walk the IFD chain -- which
    varies by model and firmware -- scan for JPEG markers and take the
    biggest, which is always the most useful one. Dependency-free, and it
    does not care which Sony wrote the file.
    """
    best: bytes | None = None
    start = 0
    while True:
        i = raw.find(b"\xff\xd8\xff", start)
        if i < 0:
            break
        j = raw.find(b"\xff\xd9", i)
        if j < 0:
            break
        candidate = raw[i:j + 2]
        if best is None or len(candidate) > len(best):
            best = candidate
        start = j + 2
    return best


# ------------------------------------------------------------ EXIF (TIFF) ----

_EXIF_IFD = 0x8769
_TAG_EXPOSURE_TIME = 0x829A
_TAG_FNUMBER = 0x829D
_TAG_ISO = 0x8827

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def read_exif_exposure(raw: bytes) -> dict[str, float]:
    """
    ISO, f-number and exposure time from a TIFF-based raw, no dependencies.

    Deliberately minimal: it walks IFD0, follows the EXIF pointer, and reads
    three tags. Anything unparseable is omitted rather than guessed, so a
    caller can tell "not present" from "zero".
    """
    if len(raw) < 8:
        return {}
    if raw[:2] == b"II":
        end = "<"
    elif raw[:2] == b"MM":
        end = ">"
    else:
        return {}

    import struct

    def u16(off): return struct.unpack_from(end + "H", raw, off)[0]
    def u32(off): return struct.unpack_from(end + "I", raw, off)[0]

    try:
        if u16(2) != 42:
            return {}
        out: dict[str, float] = {}

        def walk(ifd_off: int, depth: int = 0):
            if depth > 2 or ifd_off <= 0 or ifd_off + 2 > len(raw):
                return
            count = u16(ifd_off)
            for k in range(count):
                e = ifd_off + 2 + k * 12
                if e + 12 > len(raw):
                    return
                tag, typ, n = u16(e), u16(e + 2), u32(e + 4)
                size = _TYPE_SIZES.get(typ, 0) * n
                val_off = e + 8 if size <= 4 else u32(e + 8)
                if size > 4 and (val_off + size) > len(raw):
                    continue
                if tag == _EXIF_IFD:
                    walk(u32(e + 8), depth + 1)
                elif tag == _TAG_ISO and typ == 3:
                    out["iso"] = float(u16(val_off))
                elif tag in (_TAG_FNUMBER, _TAG_EXPOSURE_TIME) and typ == 5:
                    num, den = u32(val_off), u32(val_off + 4)
                    if den:
                        key = "aperture" if tag == _TAG_FNUMBER else "shutter_s"
                        out[key] = num / den

        walk(u32(4))
        return out
    except (struct.error, IndexError):
        return {}


def read_frame(raw: bytes) -> ExposureReading | None:
    """Everything needed for a recommendation, from the raw file alone."""
    exif = read_exif_exposure(raw)
    if not {"aperture", "shutter_s"} <= exif.keys():
        return None
    stats = None
    preview = extract_preview(raw)
    if preview:
        try:
            stats = histogram_stats(preview)
        except Exception:  # noqa: BLE001
            stats = None
    return ExposureReading(
        iso=exif.get("iso", 100.0),
        aperture=exif["aperture"],
        shutter_s=exif["shutter_s"],
        stats=stats,
    )
