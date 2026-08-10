"""
Synthetic test pages, rendered in document millimetre space.

The point of these is *ground truth*.  Every page carries a `PageTruth`
describing exactly where its slanted edges, colour patches and fiducials
are, so the metrics harness can be validated against a known answer
before it is ever pointed at a real photograph.

Everything is specified in millimetres; `px_per_mm` only decides how
finely it is rasterised.
"""

from __future__ import annotations

import glob
import math
from functools import lru_cache
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 24-patch chart in sRGB, the classic values.  Used for colour matching
# between the two bodies and for dE reporting.
COLORCHECKER_SRGB = [
    (115, 82, 68), (194, 150, 130), (98, 122, 157), (87, 108, 67),
    (133, 128, 177), (103, 189, 170), (214, 126, 44), (80, 91, 166),
    (193, 90, 99), (94, 60, 108), (157, 188, 64), (224, 163, 46),
    (56, 61, 150), (70, 148, 73), (175, 54, 60), (231, 199, 31),
    (187, 86, 149), (8, 133, 161), (243, 243, 242), (200, 200, 200),
    (160, 160, 160), (122, 122, 121), (85, 85, 85), (52, 52, 52),
]

_SERIF = "LiberationSerif-Regular"
_SANS = "LiberationSans-Regular"
_MONO = "LiberationMono-Regular"


@lru_cache(maxsize=8)
def _font_path(name: str) -> str:
    hits = glob.glob(f"/usr/share/fonts/**/{name}.ttf", recursive=True)
    if not hits:
        hits = glob.glob("/usr/share/fonts/**/*.ttf", recursive=True)
    if not hits:
        raise RuntimeError("no TrueType fonts available for page synthesis")
    return sorted(hits)[0]


@lru_cache(maxsize=64)
def _font(name: str, px: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_font_path(name), max(4, int(round(px))))


@dataclass
class SlantedEdge:
    """A knife edge for ISO 12233 SFR measurement."""

    #: Centre of the edge patch, in document mm.
    cx_mm: float
    cy_mm: float
    #: Patch is square, this is its side, in mm.
    size_mm: float
    #: Tilt from vertical, degrees.  ~5 deg is the ISO recommendation.
    angle_deg: float
    #: True if the dark side is on the left/top.
    dark_first: bool = True

    def roi_px(self, px_per_mm: float) -> tuple[int, int, int, int]:
        """(x, y, w, h) of the patch in pixels, for the metrics harness."""
        half = self.size_mm / 2.0
        x = int(round((self.cx_mm - half) * px_per_mm))
        y = int(round((self.cy_mm - half) * px_per_mm))
        s = int(round(self.size_mm * px_per_mm))
        return x, y, s, s


@dataclass
class ColourPatch:
    cx_mm: float
    cy_mm: float
    size_mm: float
    srgb: tuple[int, int, int]

    def roi_px(self, px_per_mm: float) -> tuple[int, int, int, int]:
        half = self.size_mm / 2.0
        x = int(round((self.cx_mm - half) * px_per_mm))
        y = int(round((self.cy_mm - half) * px_per_mm))
        s = int(round(self.size_mm * px_per_mm))
        return x, y, s, s


@dataclass
class PageTruth:
    """Ground truth that travels with a synthetic page."""

    width_mm: float
    height_mm: float
    px_per_mm: float
    edges: list[SlantedEdge] = field(default_factory=list)
    patches: list[ColourPatch] = field(default_factory=list)
    #: Corner fiducial centres in document mm, TL TR BR BL.
    fiducials_mm: list[tuple[float, float]] = field(default_factory=list)
    #: Distance between the two ruler ticks used for the DPI check, mm.
    ruler_span_mm: float = 0.0
    ruler_ends_mm: tuple[tuple[float, float], tuple[float, float]] | None = None
    #: Spine position in document mm (for spread pages), else None.
    spine_mm: float | None = None

    def fiducials_px(self) -> np.ndarray:
        return np.array(
            [[x * self.px_per_mm, y * self.px_per_mm] for x, y in self.fiducials_mm],
            dtype=np.float64,
        )


LOREM = (
    "The precision budget is what turns an opinion into a measurement. "
    "An open book page is a developable surface: paper bends but does not "
    "stretch, and that single physical fact constrains the whole problem. "
    "Height error at surface tilt produces lateral error, so the tolerable "
    "height error follows directly from the resampling pitch. Everything "
    "else in the design is downstream of that one number. "
)


def _draw_text_block(
    d: ImageDraw.ImageDraw,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    ppm: float,
    pt: float = 9.0,
    leading: float = 1.45,
    font_name: str = _SERIF,
) -> None:
    """Fill a rectangle with justified-ish body text at a real point size."""
    px_size = pt / 72.0 * 25.4 * ppm  # points -> mm -> px
    f = _font(font_name, round(px_size, 1))
    line_h = px_size * leading
    words = (LOREM * 12).split()
    x0, y0 = x_mm * ppm, y_mm * ppm
    max_w = w_mm * ppm
    y = y0
    i = 0
    while y + line_h < (y_mm + h_mm) * ppm and i < len(words):
        line = ""
        while i < len(words):
            trial = (line + " " + words[i]).strip()
            if d.textlength(trial, font=f) > max_w:
                break
            line = trial
            i += 1
        if not line:  # a single word wider than the column
            line = words[i]
            i += 1
        d.text((x0, y), line, font=f, fill=(20, 20, 22))
        y += line_h


def _draw_slanted_edge(
    img: Image.Image, e: SlantedEdge, ppm: float, supersample: int
) -> None:
    """
    Draw a hard straight edge, antialiased by supersampling a signed
    distance field.

    Rendering this properly matters: a jaggy or soft edge would make the
    SFR measurement meaningless -- it would measure the renderer, not the
    lens.
    """
    x, y, w, h = e.roi_px(ppm)
    if w < 8 or h < 8:
        return
    ss = max(1, supersample)
    th = math.radians(e.angle_deg)
    cos_t, sin_t = math.cos(th), math.sin(th)
    cx, cy = w * ss / 2.0, h * ss / 2.0

    ys, xs = np.mgrid[0 : h * ss, 0 : w * ss]
    # Edge runs along (sin, cos) -- near-vertical for small angles.
    dist = (xs + 0.5 - cx) * cos_t - (ys + 0.5 - cy) * sin_t
    dark, light = 26.0, 242.0
    field = np.where(dist < 0.0, dark, light).astype(np.float32)
    if not e.dark_first:
        field = (dark + light) - field
    # Box-downsample: exact area coverage, so the edge profile is a clean
    # ramp exactly one output pixel wide.
    field = field.reshape(h, ss, w, ss).mean(axis=(1, 3))
    arr = np.clip(field, 0, 255).astype(np.uint8)
    img.paste(Image.fromarray(np.dstack([arr] * 3)), (x, y))


def _draw_furniture(
    img: Image.Image,
    d: ImageDraw.ImageDraw,
    truth: PageTruth,
    x_mm: float,
    w_mm: float,
    top_mm: float,
    bot_mm: float,
    ppm: float,
    supersample: int,
) -> None:
    """
    Lay the measurement furniture into one column: two slanted edges, a
    24-patch colour chart and a ruler.

    Every tile gets its own complete set, so a single camera's frame is
    self-sufficient for MTF, colour and DPI measurement.  That is what
    makes per-body calibration possible without stitching first.
    """
    edge_mm = min(15.0, w_mm * 0.22)
    ey = top_mm + edge_mm / 2.0
    for k, ex_frac in enumerate((0.18, 0.62)):
        e = SlantedEdge(
            cx_mm=x_mm + w_mm * ex_frac + edge_mm / 2,
            cy_mm=ey,
            size_mm=edge_mm,
            angle_deg=5.0 if k == 0 else -5.0,
        )
        _draw_slanted_edge(img, e, ppm, supersample)
        truth.edges.append(e)

    # colour chart, 6 x 4, sized to the column
    gap_frac = 0.22
    patch_mm = min(9.0, (w_mm * 0.82) / (6 + 5 * gap_frac))
    gap = patch_mm * gap_frac
    grid_w = 6 * patch_mm + 5 * gap
    grid_h = 4 * patch_mm + 3 * gap
    ox = x_mm + (w_mm - grid_w) / 2.0
    oy = top_mm + edge_mm + 6.0
    for k, srgb in enumerate(COLORCHECKER_SRGB):
        r, c = divmod(k, 6)
        px0 = ox + c * (patch_mm + gap)
        py0 = oy + r * (patch_mm + gap)
        d.rectangle(
            [px0 * ppm, py0 * ppm, (px0 + patch_mm) * ppm, (py0 + patch_mm) * ppm],
            fill=srgb,
        )
        truth.patches.append(
            ColourPatch(px0 + patch_mm / 2, py0 + patch_mm / 2, patch_mm, srgb)
        )

    # ruler: two heavy end ticks a known distance apart
    ruler_y = min(bot_mm, oy + grid_h + 9.0)
    x_a, x_b = x_mm + 2.0, x_mm + w_mm - 2.0
    span = x_b - x_a
    lw = max(1, int(round(0.22 * ppm)))
    d.line([x_a * ppm, ruler_y * ppm, x_b * ppm, ruler_y * ppm],
           fill=(20, 20, 22), width=lw)
    n = int(span // 10)
    for t in range(n + 1):
        tx = x_a + t * 10.0
        tall = 3.4 if t % 5 == 0 else 1.9
        d.line([tx * ppm, (ruler_y - tall) * ppm, tx * ppm, ruler_y * ppm],
               fill=(20, 20, 22), width=lw)
    # end marks: filled squares, unambiguous to locate
    for tx in (x_a, x_b):
        d.rectangle(
            [(tx - 1.1) * ppm, (ruler_y - 6.4) * ppm,
             (tx + 1.1) * ppm, (ruler_y - 4.2) * ppm],
            fill=(10, 10, 12),
        )
    if truth.ruler_span_mm == 0.0:
        truth.ruler_span_mm = span
        truth.ruler_ends_mm = ((x_a, ruler_y - 5.3), (x_b, ruler_y - 5.3))


def render_page(
    width_mm: float = 210.0,
    height_mm: float = 297.0,
    px_per_mm: float = 18.18,
    *,
    spread: bool = False,
    supersample: int = 4,
    seed: int = 0,
    paper_rgb: tuple[int, int, int] = (246, 244, 238),
) -> tuple[np.ndarray, PageTruth]:
    """
    Render a synthetic document page.

    Returns (BGR uint8 image, PageTruth).  Image is in document space:
    pixel (0,0) is document (0 mm, 0 mm).

    `spread=True` lays out two pages with a gutter, mimicking an open book.
    """
    rng = np.random.default_rng(seed)
    ppm = px_per_mm
    W = int(round(width_mm * ppm))
    H = int(round(height_mm * ppm))

    img = Image.new("RGB", (W, H), paper_rgb)
    d = ImageDraw.Draw(img)

    truth = PageTruth(width_mm=width_mm, height_mm=height_mm, px_per_mm=ppm)

    # -- paper texture: very low-amplitude noise, keeps flat-field honest ---
    tex = rng.normal(0.0, 1.6, size=(H, W, 1))
    base = np.asarray(img).astype(np.float32) + tex
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)

    # ---- column layout ---------------------------------------------------
    margin = 12.0
    if spread:
        half = width_mm / 2.0
        cols = [
            (margin, half - margin - 6.0),
            (half + 6.0, half - margin - 6.0),
        ]
        truth.spine_mm = half
    else:
        cols = [(margin, width_mm - 2 * margin)]

    # Vertical bands as fractions of page height.  Explicit and
    # non-overlapping: text that collides with test furniture makes the
    # furniture unmeasurable.
    H_HEAD = 0.045
    BODY_TOP, BODY_BOT = 0.075, 0.44
    MID_TOP, MID_BOT = 0.465, 0.575
    FINE_TOP, FINE_BOT = 0.60, 0.68
    FURN_TOP, FURN_BOT = 0.71, 0.975

    for cx, cw in cols:
        d.text(
            (cx * ppm, H_HEAD * height_mm * ppm),
            "On Measured Optics",
            font=_font(_SANS, round(15 / 72 * 25.4 * ppm, 1)),
            fill=(15, 15, 18),
        )
        _draw_text_block(
            d, cx, BODY_TOP * height_mm, cw,
            (BODY_BOT - BODY_TOP) * height_mm, ppm, pt=9.0,
        )
        _draw_text_block(
            d, cx, MID_TOP * height_mm, cw,
            (MID_BOT - MID_TOP) * height_mm, ppm, pt=7.0,
        )
        d.text(
            (cx * ppm, (FINE_TOP - 0.018) * height_mm * ppm),
            "4 pt fine print -- the real resolution test:",
            font=_font(_SANS, round(7 / 72 * 25.4 * ppm, 1)),
            fill=(60, 60, 64),
        )
        _draw_text_block(
            d, cx, FINE_TOP * height_mm, cw,
            (FINE_BOT - FINE_TOP) * height_mm, ppm,
            pt=4.0, leading=1.30, font_name=_MONO,
        )
        _draw_furniture(
            img, d, truth, cx, cw,
            FURN_TOP * height_mm, FURN_BOT * height_mm, ppm, supersample,
        )
        d = ImageDraw.Draw(img)

    # -- corner fiducials: filled circles, easy to centroid ----------------
    fid_r = 2.2
    fid_inset = 5.0
    corners = [
        (fid_inset, fid_inset),
        (width_mm - fid_inset, fid_inset),
        (width_mm - fid_inset, height_mm - fid_inset),
        (fid_inset, height_mm - fid_inset),
    ]
    for fx, fy in corners:
        d.ellipse(
            [(fx - fid_r) * ppm, (fy - fid_r) * ppm,
             (fx + fid_r) * ppm, (fy + fid_r) * ppm],
            fill=(10, 10, 12),
        )
        truth.fiducials_mm.append((fx, fy))

    # -- gutter shading on spreads ----------------------------------------
    if spread:
        arr = np.asarray(img).astype(np.float32)
        xs = np.arange(W)
        centre = W / 2.0
        sigma = 9.0 * ppm
        shade = 1.0 - 0.30 * np.exp(-0.5 * ((xs - centre) / sigma) ** 2)
        arr *= shade[None, :, None]
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    bgr = np.asarray(img)[:, :, ::-1].copy()
    return bgr, truth


def render_charuco(
    squares_x: int = 9,
    squares_y: int = 6,
    square_mm: float = 30.0,
    px_per_mm: float = 8.0,
) -> tuple[np.ndarray, dict]:
    """
    Render a ChArUco calibration board, plus the parameters needed to
    detect it again.  Printed at A3 this is the real calibration target.
    """
    import cv2

    adict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_250)
    board = cv2.aruco.CharucoBoard(
        (squares_x, squares_y), square_mm, square_mm * 0.75, adict
    )
    w = int(round(squares_x * square_mm * px_per_mm))
    h = int(round(squares_y * square_mm * px_per_mm))
    img = board.generateImage((w, h), marginSize=int(round(4 * px_per_mm)))
    meta = {
        "squares_x": squares_x,
        "squares_y": squares_y,
        "square_mm": square_mm,
        "marker_mm": square_mm * 0.75,
        "dictionary": "DICT_5X5_250",
        "px_per_mm": px_per_mm,
    }
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), meta
