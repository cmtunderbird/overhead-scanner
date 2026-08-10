"""
Slanted-edge spatial frequency response, after ISO 12233.

This is the only honest way to answer "is it sharp?".  A DPI figure says
how finely the page was sampled; MTF50 says how much of that sampling
survived the lens, the focus, the diffraction and the shake.  The
blueprint's acceptance criterion -- MTF50 >= 0.30 cy/px at the page
corners -- is measured here.

The implementation is validated in the test suite against a synthetic
edge blurred by a known Gaussian, for which the answer is analytic:

    MTF(f) = exp(-2 pi^2 sigma^2 f^2)   =>   MTF50 = sqrt(ln 2 / 2) / (pi sigma)
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

#: Oversampling factor for the edge spread function.  ISO uses 4.
BINS_PER_PX = 4


@dataclass
class SFRResult:
    """Outcome of one slanted-edge measurement."""

    #: Spatial frequency axis, cycles per pixel.
    freq: np.ndarray
    #: Modulation transfer, normalised to 1.0 at DC.
    mtf: np.ndarray
    #: Frequency at which MTF falls to 0.5, cycles per pixel.
    mtf50: float
    #: Frequency at which MTF falls to 0.2.
    mtf20: float
    #: Measured edge angle from vertical, degrees.
    angle_deg: float
    #: Contrast across the edge, 0..1 -- a sanity check on the ROI.
    contrast: float

    def mtf_at(self, f: float) -> float:
        return float(np.interp(f, self.freq, self.mtf))

    def lp_per_mm(self, px_per_mm: float) -> float:
        """MTF50 expressed in line pairs per millimetre on the page."""
        return self.mtf50 * px_per_mm


def _to_luminance(roi: np.ndarray, gamma: float = 2.2) -> np.ndarray:
    """
    Grayscale, linearised.

    Linearising matters: SFR on gamma-encoded data reads high, because
    the encoding steepens the edge.  Skipping this is the most common way
    to publish an MTF number that is simply wrong.
    """
    a = roi.astype(np.float64)
    if a.ndim == 3:
        # BGR -> luminance
        a = 0.0722 * a[..., 0] + 0.7152 * a[..., 1] + 0.2126 * a[..., 2]
    a = np.clip(a / 255.0, 1e-6, 1.0)
    return np.power(a, gamma)


def _edge_positions(lin: np.ndarray) -> np.ndarray:
    """
    Sub-pixel edge location for each row, by derivative centroid.

    The row is lightly smoothed *for the position estimate only*.  Noise
    does not bias the centroid, but it does scatter it, and a scattered
    line fit smears the projected ESF -- which reads as a blurrier lens.
    The smoothing never touches the data the ESF is built from.
    """
    smooth = cv2.GaussianBlur(lin.astype(np.float32), (5, 1), 0)
    d = np.abs(np.diff(smooth, axis=1))
    # Suppress noise far from the edge: keep the strongest lobe per row.
    xs = np.arange(d.shape[1]) + 0.5
    w = d - d.min(axis=1, keepdims=True)
    w[w < 0.25 * w.max(axis=1, keepdims=True)] = 0.0
    denom = w.sum(axis=1)
    denom[denom == 0] = np.nan
    return (w * xs).sum(axis=1) / denom


def measure_sfr(roi: np.ndarray, gamma: float = 2.2) -> SFRResult:
    """
    Measure SFR from a region of interest containing a single near-vertical
    edge.  For a horizontal edge, pass `roi.T` (or a transposed slice).
    """
    if roi.ndim not in (2, 3):
        raise ValueError("roi must be 2-D or 3-D")
    lin = _to_luminance(roi, gamma)
    h, w = lin.shape
    if h < 16 or w < 16:
        raise ValueError(f"roi too small for SFR: {w}x{h}, need >= 16 px")

    # -- 1. fit the edge --------------------------------------------------
    rows = np.arange(h)
    pos = _edge_positions(lin)
    ok = np.isfinite(pos)
    if ok.sum() < 8:
        raise ValueError("no usable edge found in roi")
    slope, intercept = np.polyfit(rows[ok], pos[ok], 1)
    angle_deg = float(np.degrees(np.arctan(slope)))
    if abs(angle_deg) < 0.4 or abs(angle_deg) > 45.0:
        raise ValueError(
            f"edge angle {angle_deg:.2f} deg is unusable; "
            "ISO 12233 wants roughly 2-10 degrees off vertical"
        )

    # -- 2. project every pixel onto the edge normal ----------------------
    xs = np.arange(w)[None, :].repeat(h, axis=0)
    edge_x = (slope * rows + intercept)[:, None]
    dist = xs - edge_x  # signed distance in px, along the normal

    # -- 3. bin into a 4x oversampled edge spread function -----------------
    lo, hi = np.ceil(dist.min()), np.floor(dist.max())
    span = min(abs(lo), abs(hi))
    if span < 6:
        raise ValueError("edge too close to the roi border")
    lo, hi = -span, span
    nbins = int((hi - lo) * BINS_PER_PX)
    idx = np.clip(((dist - lo) * BINS_PER_PX).astype(int), 0, nbins - 1)

    flat_i, flat_v = idx.ravel(), lin.ravel()
    total = np.bincount(flat_i, weights=flat_v, minlength=nbins)
    count = np.bincount(flat_i, minlength=nbins)
    empty = count == 0
    esf = np.where(empty, np.nan, total / np.maximum(count, 1))
    if empty.any():  # fill the odd empty bin by interpolation
        good = ~empty
        esf = np.interp(np.arange(nbins), np.arange(nbins)[good], esf[good])

    contrast = float(
        (np.nanmax(esf) - np.nanmin(esf)) / max(np.nanmax(esf), 1e-9)
    )

    # -- 4. differentiate to the line spread function ---------------------
    lsf = np.gradient(esf)
    # Centre and window: a Hamming window suppresses the ringing that the
    # finite ROI would otherwise inject into the transform.
    centre = float((np.arange(nbins) * np.abs(lsf)).sum() / np.abs(lsf).sum())
    n = np.arange(nbins)
    half = nbins / 2.0
    win = 0.54 + 0.46 * np.cos(np.pi * np.clip((n - centre) / half, -1, 1))
    lsf = lsf * win

    # -- 5. transform ------------------------------------------------------
    spectrum = np.abs(np.fft.rfft(lsf))
    if spectrum[0] <= 0:
        raise ValueError("degenerate edge: zero DC")
    mtf = spectrum / spectrum[0]
    dx = 1.0 / BINS_PER_PX  # bin width, px
    freq = np.fft.rfftfreq(nbins, d=dx)  # cycles per pixel

    # Correct for the differencing aperture (np.gradient is a central
    # difference over 2*dx).
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.sinc(freq * 2.0 * dx)
    corr[corr < 0.2] = 0.2
    mtf = mtf / corr

    keep = freq <= 1.0  # up to 2x Nyquist is plenty
    freq, mtf = freq[keep], mtf[keep]

    return SFRResult(
        freq=freq,
        mtf=mtf,
        mtf50=_crossing(freq, mtf, 0.5),
        mtf20=_crossing(freq, mtf, 0.2),
        angle_deg=angle_deg,
        contrast=contrast,
    )


def _crossing(freq: np.ndarray, mtf: np.ndarray, level: float) -> float:
    """First downward crossing of `level`, linearly interpolated."""
    below = np.where(mtf < level)[0]
    if len(below) == 0:
        return float(freq[-1])
    i = below[0]
    if i == 0:
        return 0.0
    f0, f1 = freq[i - 1], freq[i]
    m0, m1 = mtf[i - 1], mtf[i]
    if m0 == m1:
        return float(f0)
    return float(f0 + (m0 - level) * (f1 - f0) / (m0 - m1))


def gaussian_mtf50(sigma_px: float) -> float:
    """Analytic MTF50 of a Gaussian blur -- the test-suite oracle."""
    return float(np.sqrt(np.log(2.0) / 2.0) / (np.pi * sigma_px))


def measure_edges(
    image: np.ndarray,
    rois: list[tuple[int, int, int, int]],
    *,
    gamma: float = 2.2,
    inset: float = 0.12,
) -> list[SFRResult]:
    """
    Measure a list of (x, y, w, h) edge ROIs, skipping any that fail.

    `inset` trims the ROI edges, where the synthetic patch meets the page
    and the profile is not a clean step.
    """
    out: list[SFRResult] = []
    H, W = image.shape[:2]
    for (x, y, w, h) in rois:
        dx, dy = int(w * inset), int(h * inset)
        x0, y0 = max(0, x + dx), max(0, y + dy)
        x1, y1 = min(W, x + w - dx), min(H, y + h - dy)
        if x1 - x0 < 16 or y1 - y0 < 16:
            continue
        try:
            out.append(measure_sfr(image[y0:y1, x0:x1], gamma))
        except ValueError:
            continue
    return out
