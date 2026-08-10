"""
Measured DPI and stitch-seam error.

A DPI figure computed from the optical model is a prediction.  These
functions measure it from the actual pixels, which is the only version
that belongs in an acceptance report.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class ScaleResult:
    px_per_mm: float
    dpi: float
    #: Pixel distance between the two marks actually found.
    distance_px: float
    #: The nominal separation they were told to expect, mm.
    span_mm: float
    #: Refined mark centres, image px.
    mark_a_px: tuple[float, float]
    mark_b_px: tuple[float, float]

    def error_vs(self, expected_dpi: float) -> float:
        """Fractional error against a predicted DPI."""
        return (self.dpi - expected_dpi) / expected_dpi


def _refine_dark_blob(
    gray: np.ndarray, cx: float, cy: float, search_px: int
) -> tuple[float, float]:
    """
    Intensity-weighted centroid of the darkest blob near (cx, cy).

    Weighting by darkness gives a sub-pixel answer, which is what makes a
    DPI measurement good to better than 0.1 %.
    """
    h, w = gray.shape[:2]
    x0, x1 = int(max(0, cx - search_px)), int(min(w, cx + search_px + 1))
    y0, y1 = int(max(0, cy - search_px)), int(min(h, cy + search_px + 1))
    roi = gray[y0:y1, x0:x1].astype(np.float64)
    if roi.size == 0:
        raise ValueError("search window fell outside the image")

    weight = roi.max() - roi
    thr = 0.55 * weight.max()
    weight = np.where(weight >= thr, weight, 0.0)
    total = weight.sum()
    if total <= 0:
        raise ValueError("no dark mark found in the search window")

    ys, xs = np.mgrid[y0:y1, x0:x1]
    return float((weight * xs).sum() / total), float((weight * ys).sum() / total)


def measure_scale(
    image: np.ndarray,
    expected_a_px: tuple[float, float],
    expected_b_px: tuple[float, float],
    span_mm: float,
    search_px: int = 40,
) -> ScaleResult:
    """
    Measure px/mm from two marks a known distance apart.

    `expected_*_px` only needs to be within `search_px`; the centroid does
    the rest.
    """
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    a = _refine_dark_blob(gray, *expected_a_px, search_px=search_px)
    b = _refine_dark_blob(gray, *expected_b_px, search_px=search_px)
    dist = float(np.hypot(b[0] - a[0], b[1] - a[1]))
    ppm = dist / span_mm
    return ScaleResult(
        px_per_mm=ppm,
        dpi=ppm * 25.4,
        distance_px=dist,
        span_mm=span_mm,
        mark_a_px=a,
        mark_b_px=b,
    )


@dataclass
class SeamResult:
    #: Residual misregistration between the two tiles, px.
    shift_px: float
    dx_px: float
    dy_px: float
    #: Normalised cross-correlation peak in the overlap, 0..1.
    correlation: float
    #: Mean absolute luminance difference across the overlap, 0..255.
    mean_abs_diff: float
    #: Ratio of gradient energy on the seam line to its neighbourhood.
    #: 1.0 means the seam is invisible; > 1.2 means you can see it.
    seam_visibility: float


def measure_seam(
    tile_a_doc: np.ndarray,
    tile_b_doc: np.ndarray,
    overlap_x0: int,
    overlap_x1: int,
) -> SeamResult:
    """
    Measure how well two tiles agree, both already warped into document
    space so the overlap columns correspond.

    `shift_px` is the acceptance number: the blueprint wants < 1 px.
    """
    if tile_a_doc.shape != tile_b_doc.shape:
        raise ValueError("tiles must be warped to a common document canvas")
    x0, x1 = int(overlap_x0), int(overlap_x1)
    if x1 - x0 < 8:
        raise ValueError("overlap too narrow to measure")

    def prep(t):
        g = t if t.ndim == 2 else cv2.cvtColor(t, cv2.COLOR_BGR2GRAY)
        return g[:, x0:x1].astype(np.float32)

    A, B = prep(tile_a_doc), prep(tile_b_doc)
    valid = (A > 1) & (B > 1)
    if valid.sum() < 64:
        raise ValueError("overlap contains no data in both tiles")

    Am = np.where(valid, A, 0.0)
    Bm = np.where(valid, B, 0.0)

    win = cv2.createHanningWindow((Am.shape[1], Am.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(Am * win, Bm * win)

    diff = np.abs(Am - Bm)[valid]
    mad = float(diff.mean())

    # Seam visibility: gradient energy exactly on the join, against the
    # local background.  A blend that leaves a step shows up here even
    # when the registration is perfect.
    mid = (x0 + x1) // 2
    g = cv2.cvtColor(tile_a_doc, cv2.COLOR_BGR2GRAY) if tile_a_doc.ndim == 3 else tile_a_doc
    gb = cv2.cvtColor(tile_b_doc, cv2.COLOR_BGR2GRAY) if tile_b_doc.ndim == 3 else tile_b_doc
    blend = np.where(np.arange(g.shape[1])[None, :] < mid, g, gb).astype(np.float32)
    gx = np.abs(cv2.Sobel(blend, cv2.CV_32F, 1, 0, ksize=3))
    band = 3
    on = gx[:, mid - band : mid + band + 1].mean()
    near = np.concatenate(
        [
            gx[:, max(0, mid - 8 * band) : mid - 2 * band],
            gx[:, mid + 2 * band : mid + 8 * band],
        ],
        axis=1,
    )
    around = float(near.mean()) if near.size else 1.0
    visibility = float(on / max(around, 1e-6))

    return SeamResult(
        shift_px=float(np.hypot(dx, dy)),
        dx_px=float(dx),
        dy_px=float(dy),
        correlation=float(response),
        mean_abs_diff=mad,
        seam_visibility=visibility,
    )
