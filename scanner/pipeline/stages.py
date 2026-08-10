"""
Pipeline stages, each independently testable.

Order matters and is not arbitrary:

    undistort -> flat-field -> colour -> rectify -> stitch
    -> deskew -> crop -> normalise -> split

Undistort first, because everything downstream assumes a pinhole camera.
Flat-field before colour, because a colour matrix fitted through a
vignette is fitting the vignette.  Normalise after stitching, so both
halves get the same background model and the join does not shift.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..calib.field import apply_colour_matrix, apply_flat_field
from ..calib.intrinsics import undistort
from ..calib.store import CameraCalibration, RigCalibration


# --------------------------------------------------------------------------
# per-camera correction
# --------------------------------------------------------------------------


def correct_frame(image: np.ndarray, calib: CameraCalibration) -> np.ndarray:
    """Undistort, flat-field and colour-correct a single raw frame."""
    out = undistort(image, calib)
    out = apply_flat_field(out, calib.flat_field)
    out = apply_colour_matrix(out, calib.colour_matrix)
    return out


# --------------------------------------------------------------------------
# rectification into document space
# --------------------------------------------------------------------------


def rectify_to_document(
    image: np.ndarray,
    calib: CameraCalibration,
    canvas_px: tuple[int, int],
    target_px_per_mm: float,
    *,
    already_undistorted: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Warp one corrected frame onto the shared document canvas.

    Returns (warped, mask) where mask is 255 where this camera actually
    contributed pixels.  The mask is what makes blending honest -- without
    it the border replication bleeds into the seam.
    """
    if calib.H_doc_to_img is None:
        raise ValueError(f"{calib.camera_id}: not aligned; run solve_document_homography")
    src = image if already_undistorted else undistort(image, calib)

    S = np.array(
        [
            [1.0 / target_px_per_mm, 0.0, 0.0],
            [0.0, 1.0 / target_px_per_mm, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    H_docpx_to_img = calib.H_doc_to_img @ S

    warped = cv2.warpPerspective(
        src, H_docpx_to_img, canvas_px,
        flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0),
    )
    ones = np.full(src.shape[:2], 255, np.uint8)
    mask = cv2.warpPerspective(
        ones, H_docpx_to_img, canvas_px,
        flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    # Trim a hair off the border: the outermost warped pixels are
    # interpolated against nothing.
    mask = cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=1)
    return warped, mask


# --------------------------------------------------------------------------
# stitching
# --------------------------------------------------------------------------


def _feather_weights(masks: list[np.ndarray], feather_px: int) -> list[np.ndarray]:
    """
    Distance-transform weights: each pixel is owned mostly by whichever
    camera saw it furthest from its own frame edge.

    This is a linear ramp across the overlap, which is the right choice
    here: the tiles are geometrically registered to sub-pixel, so we are
    hiding a *photometric* step, not a parallax error.
    """
    weights = []
    for m in masks:
        d = cv2.distanceTransform((m > 0).astype(np.uint8), cv2.DIST_L2, 3)
        w = np.clip(d / max(feather_px, 1), 0.0, 1.0)
        weights.append(w.astype(np.float32))
    total = np.sum(weights, axis=0)
    total[total < 1e-6] = 1.0
    return [w / total for w in weights]


def blend_tiles(
    warped: list[np.ndarray],
    masks: list[np.ndarray],
    *,
    feather_px: int = 60,
) -> tuple[np.ndarray, np.ndarray]:
    """Composite rectified tiles into one image.  Returns (image, coverage)."""
    if not warped:
        raise ValueError("nothing to blend")
    if len(warped) == 1:
        return warped[0], masks[0]

    weights = _feather_weights(masks, feather_px)
    acc = np.zeros(warped[0].shape, np.float32)
    for img, w in zip(warped, weights):
        acc += img.astype(np.float32) * w[..., None]
    coverage = np.max(np.stack(masks), axis=0)
    return np.clip(acc, 0, 255).astype(np.uint8), coverage


def overlap_columns(rig: RigCalibration) -> list[tuple[int, int]]:
    """Canvas x-ranges where adjacent tiles overlap, in output pixels."""
    cams = rig.ordered()
    ppm = rig.target_px_per_mm
    out = []
    for a, b in zip(cams, cams[1:]):
        x0 = int(round(b.tile_x0_mm * ppm))
        x1 = int(round(a.tile_x1_mm * ppm))
        if x1 > x0:
            out.append((x0, x1))
    return out


# --------------------------------------------------------------------------
# geometry cleanup
# --------------------------------------------------------------------------


def estimate_skew(
    image: np.ndarray, max_deg: float = 3.0, step: float = 0.05,
    *, proxy_px: int = 900,
) -> float:
    """
    Find the small residual rotation of the text block.

    Projection-profile variance: text lines produce a strongly peaked row
    profile only when they are horizontal.  Robust on sparse pages, where
    Hough line detection wanders off onto the page border.

    Searched coarse-to-fine -- a flat sweep at 0.05 deg over +/-3 deg is
    121 warps for no extra accuracy.
    """
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h0, w0 = gray.shape[:2]
    sc = min(1.0, proxy_px / max(h0, w0))
    small = cv2.resize(gray, (max(16, int(w0 * sc)), max(16, int(h0 * sc))),
                       interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), 1.0)
    ink = 255.0 - small.astype(np.float32)
    ink -= ink.mean()
    ink = np.clip(ink, 0, None)
    h, w = ink.shape

    def score(a: float) -> float:
        M = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0)
        rot = cv2.warpAffine(ink, M, (w, h), flags=cv2.INTER_LINEAR)
        return float(np.var(np.diff(rot.sum(axis=1))))

    coarse = 0.5
    best = max(np.arange(-max_deg, max_deg + 1e-9, coarse), key=score)
    lo, hi = best - coarse, best + coarse
    best = max(np.arange(lo, hi + 1e-9, step), key=score)
    return float(best)


def rotate(image: np.ndarray, angle_deg: float) -> np.ndarray:
    if abs(angle_deg) < 1e-3:
        return image
    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    return cv2.warpAffine(
        image, M, (w, h), flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REPLICATE,
    )


def crop_to_content(
    image: np.ndarray, coverage: np.ndarray | None = None, margin_px: int = 4
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """
    Crop to the page, using the paper itself rather than the frame.

    Returns (cropped, (x, y, w, h)).
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    valid = gray > 12
    if coverage is not None:
        valid &= coverage > 0
    if not valid.any():
        return image, (0, 0, image.shape[1], image.shape[0])

    ys, xs = np.where(valid)
    x0, x1 = max(0, xs.min() - margin_px), min(image.shape[1], xs.max() + margin_px + 1)
    y0, y1 = max(0, ys.min() - margin_px), min(image.shape[0], ys.max() + margin_px + 1)
    return image[y0:y1, x0:x1], (int(x0), int(y0), int(x1 - x0), int(y1 - y0))


# --------------------------------------------------------------------------
# photometric cleanup
# --------------------------------------------------------------------------


def normalise_background(
    image: np.ndarray,
    *,
    sigma_frac: float = 0.045,
    target: int = 244,
    strength: float = 1.0,
    proxy_px: int = 640,
) -> np.ndarray:
    """
    Divide out the illumination field.

    Estimates the paper background by a morphological close (which lifts
    the text out of the way so the estimate sees paper, not ink) followed
    by a heavy blur, then normalises to it.  This removes lamp falloff and
    the gutter shadow band without crushing the text -- a subtraction
    would.

    The estimate is computed on a small proxy and upsampled.  The
    background field is low-frequency by construction, so this is
    numerically equivalent and about fifty times faster; doing it at full
    resolution costs seconds per spread and buys nothing.
    """
    img = image.astype(np.float32)
    single = img.ndim == 2
    if single:
        img = img[..., None]
    h, w = img.shape[:2]

    scale = min(1.0, proxy_px / max(h, w))
    if scale < 1.0:
        proxy = cv2.resize(img, (max(8, int(w * scale)), max(8, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
    else:
        proxy = img
    ph, pw = proxy.shape[:2]

    k = max(3, int(0.02 * max(ph, pw)) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    bg = cv2.morphologyEx(proxy, cv2.MORPH_CLOSE, kernel)
    bg = cv2.GaussianBlur(bg, (0, 0), max(1.0, sigma_frac * max(ph, pw)))
    if bg.ndim == 2:
        bg = bg[..., None]

    if scale < 1.0:
        bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
        if bg.ndim == 2:
            bg = bg[..., None]

    gain = target / np.maximum(bg, 1.0)
    gain = 1.0 + strength * (gain - 1.0)
    out = np.clip(img * gain, 0, 255)
    if single:
        out = out[..., 0]
    return out.astype(image.dtype)


# --------------------------------------------------------------------------
# spine
# --------------------------------------------------------------------------


@dataclass
class SpineResult:
    x_px: int
    confidence: float
    #: True if detection failed and the geometric centre was used.
    fallback: bool


def detect_spine(
    image: np.ndarray, search_frac: float = 0.18, smooth_frac: float = 0.01
) -> SpineResult:
    """
    Locate the gutter as the darkest, most text-free column near centre.

    Searches only the middle `search_frac` of the width: a spread's spine
    is never at the edge, and constraining the search stops a dark plate
    on one page from stealing the answer.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    h, w = gray.shape[:2]
    lo = int(w * (0.5 - search_frac / 2))
    hi = int(w * (0.5 + search_frac / 2))
    band = gray[int(h * 0.15) : int(h * 0.85), lo:hi].astype(np.float32)
    if band.size == 0:
        return SpineResult(w // 2, 0.0, True)

    profile = band.mean(axis=0)
    ksz = max(3, int(w * smooth_frac) | 1)
    profile = cv2.GaussianBlur(profile.reshape(1, -1), (ksz, 1), 0).ravel()

    idx = int(np.argmin(profile))
    depth = float(profile.max() - profile.min())
    contrast = depth / max(profile.max(), 1e-6)
    if contrast < 0.02:
        return SpineResult(w // 2, contrast, True)
    return SpineResult(lo + idx, contrast, False)


def split_spread(
    image: np.ndarray, spine_x: int, gutter_trim_px: int = 0
) -> list[np.ndarray]:
    """Cut a spread into two pages at the spine."""
    left = image[:, : max(1, spine_x - gutter_trim_px)]
    right = image[:, min(image.shape[1] - 1, spine_x + gutter_trim_px) :]
    return [left, right]
