"""
The book as a 3-D surface.

An open book page is a **developable surface**: paper bends but does not
stretch.  That single physical fact is what makes the problem tractable
-- the surface is a generalised cylinder whose generators run parallel to
the spine, so its height depends on one coordinate, and the mapping back
to a flat page is pure arc length.

Two coordinate systems, and keeping them straight is the whole job:

    page coordinates   (s, y)   millimetres along the *paper*, the flat
                                sheet as it was printed.  This is what a
                                dewarper must recover.

    platen coordinates (x, y)   millimetres on the flat support, plus a
                                height z = h(x).  This is where the
                                cameras see it.

They differ because the paper is longer than its projection: s is arc
length along the curve, x is its shadow.  That difference *is*
foreshortening, and it is why a curved page photographed from above
loses resolution near the spine no matter how good the optics are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

Profile = Literal["sech", "exp", "gauss", "flat"]


@dataclass
class BookSurface:
    """
    A cylindrical page surface, symmetric about the spine.

    `rise_mm` is the height of the surface at the spine above the platen;
    `decay_mm` is how quickly it falls away.  Together they describe how
    thick and how tightly bound the book is:

        30 mm paperback, relaxed     rise 15, decay 30
        60 mm hardback               rise 30, decay 40
        90 mm tome, tight binding    rise 45, decay 35

    `flat` gives back the old plane, so every code path can be exercised
    with and without curvature.
    """

    spine_x_mm: float = 210.0
    rise_mm: float = 30.0
    decay_mm: float = 40.0
    profile: Profile = "sech"
    #: Optional cross-spine modulation: pages curl less at the head and
    #: tail of the book than at mid-height.  0 disables it.
    y_taper: float = 0.0
    doc_height_mm: float = 297.0

    # -- height field ------------------------------------------------------

    def height(self, x, y=None):
        """Height above the platen, in mm. Vectorised."""
        x = np.asarray(x, dtype=np.float64)
        if self.profile == "flat" or self.rise_mm == 0.0:
            return np.zeros_like(x)
        u = np.abs(x - self.spine_x_mm) / max(self.decay_mm, 1e-9)
        if self.profile == "sech":
            h = self.rise_mm / np.cosh(u)
        elif self.profile == "exp":
            h = self.rise_mm * np.exp(-u)
        else:  # gauss
            h = self.rise_mm * np.exp(-0.5 * u * u)
        if self.y_taper and y is not None:
            y = np.asarray(y, dtype=np.float64)
            c = self.doc_height_mm / 2.0
            t = 1.0 - self.y_taper * (np.abs(y - c) / c) ** 2
            h = h * np.clip(t, 0.0, 1.0)
        return h

    def slope(self, x, eps: float = 1e-4):
        """dh/dx, by central difference -- robust across all profiles."""
        x = np.asarray(x, dtype=np.float64)
        return (self.height(x + eps) - self.height(x - eps)) / (2 * eps)

    def normal(self, x):
        """Unit surface normal in the x-z plane (generators run along y)."""
        m = self.slope(x)
        n = np.stack([-m, np.zeros_like(m), np.ones_like(m)], axis=-1)
        return n / np.linalg.norm(n, axis=-1, keepdims=True)

    def tilt_deg(self, x):
        """Angle between the surface normal and vertical, degrees."""
        return np.degrees(np.arctan(np.abs(self.slope(x))))

    # -- arc length: the developable mapping -------------------------------

    def _grid(self, x_min: float, x_max: float, n: int = 20001):
        xs = np.linspace(x_min, x_max, n)
        ds = np.sqrt(1.0 + self.slope(xs) ** 2)
        s = np.concatenate([[0.0], np.cumsum(0.5 * (ds[1:] + ds[:-1]) * np.diff(xs))])
        # measure arc length outward from the spine, signed
        s0 = np.interp(self.spine_x_mm, xs, s)
        return xs, s - s0

    def arc_length(self, x, x_min: float = -50.0, x_max: float = 470.0):
        """
        Signed arc length from the spine to x, along the surface.

        This is the paper coordinate: two points the same arc length
        apart were the same distance apart when the sheet was flat, no
        matter how the page is bent between them.
        """
        xs, s = self._grid(x_min, x_max)
        return np.interp(np.asarray(x, dtype=np.float64), xs, s)

    def x_of_arc_length(self, s, x_min: float = -50.0, x_max: float = 470.0):
        """Invert `arc_length` -- paper coordinate back to platen x."""
        xs, sg = self._grid(x_min, x_max)
        return np.interp(np.asarray(s, dtype=np.float64), sg, xs)

    # -- page <-> platen ---------------------------------------------------

    def to_page(self, x, y):
        """Platen (x, y) -> flat page (s, y). `s` is measured from the spine."""
        return self.arc_length(x), np.asarray(y, dtype=np.float64)

    def to_platen(self, s, y):
        """Flat page (s, y) -> 3-D point on the surface (x, y, z)."""
        x = self.x_of_arc_length(s)
        return x, np.asarray(y, dtype=np.float64), self.height(x)

    def page_span_mm(self, x_min: float, x_max: float) -> float:
        """How long the paper is between two platen positions."""
        return float(self.arc_length(x_max) - self.arc_length(x_min))

    # -- reporting ---------------------------------------------------------

    def stretch_at(self, x) -> np.ndarray:
        """
        Paper-per-platen-millimetre: ds/dx = sqrt(1 + h'^2).

        The reciprocal is the sampling density loss.  At ds/dx = 2 the
        page is being sampled at half the nominal DPI, and no algorithm
        recovers what was never sampled.
        """
        return np.sqrt(1.0 + self.slope(x) ** 2)

    def summary(self, doc_long_mm: float = 420.0) -> dict:
        xs = np.linspace(0.0, doc_long_mm, 2001)
        h = self.height(xs)
        tilt = self.tilt_deg(xs)
        stretch = self.stretch_at(xs)
        return {
            "profile": self.profile,
            "spine_x_mm": self.spine_x_mm,
            "rise_mm": round(float(h.max()), 2),
            "decay_mm": self.decay_mm,
            "max_tilt_deg": round(float(tilt.max()), 1),
            "paper_length_mm": round(self.page_span_mm(0.0, doc_long_mm), 1),
            "projected_length_mm": round(doc_long_mm, 1),
            "hidden_by_curvature_mm": round(
                self.page_span_mm(0.0, doc_long_mm) - doc_long_mm, 1
            ),
            "worst_sampling_loss_pct": round(100 * (1 - 1 / stretch.max()), 1),
        }

    def report(self, doc_long_mm: float = 420.0, dof_half_mm: float = 11.4) -> str:
        s = self.summary(doc_long_mm)
        xs = np.linspace(0.0, doc_long_mm, 4001)
        out = xs[self.height(xs) > dof_half_mm]
        band = (out.max() - out.min()) if len(out) else 0.0
        return "\n".join([
            f"book surface: {s['profile']}, rise {s['rise_mm']:g} mm, "
            f"decay {s['decay_mm']:g} mm",
            f"  max tilt            {s['max_tilt_deg']:g} deg",
            f"  paper length        {s['paper_length_mm']:g} mm "
            f"(projects to {s['projected_length_mm']:g} mm)",
            f"  curvature hides     {s['hidden_by_curvature_mm']:g} mm of paper",
            f"  worst sampling loss {s['worst_sampling_loss_pct']:g} %",
            f"  outside +/-{dof_half_mm:g} mm DOF over {band:.0f} mm "
            f"({100 * band / doc_long_mm:.0f} % of the spread)",
        ])


#: Representative books, for tests and for the selftest.
FLAT = BookSurface(profile="flat", rise_mm=0.0)
PAPERBACK = BookSurface(rise_mm=15.0, decay_mm=30.0)
HARDBACK = BookSurface(rise_mm=30.0, decay_mm=40.0)
TOME = BookSurface(rise_mm=45.0, decay_mm=35.0)

BOOKS = {"flat": FLAT, "paperback": PAPERBACK, "hardback": HARDBACK, "tome": TOME}


def fit_developable(
    x_samples: np.ndarray,
    z_samples: np.ndarray,
    *,
    x_min: float,
    x_max: float,
    n_bins: int = 160,
    smooth_mm: float = 12.0,
    min_per_bin: int = 4,
) -> "SampledSurface":
    """
    Fit a developable surface to a cloud of triangulated points.

    This is where the physics earns its keep.  Dense stereo on a page
    gives a noisy point per textured pixel; the developable constraint
    collapses that to a single smooth profile h(x), because every point
    at the same x must be at the same height.  Averaging thousands of
    noisy samples per bin is what turns ~50 um of per-point depth noise
    into a surface good enough to resample text against.

    Robust to outliers by construction: a median per bin, then a
    smoothing pass whose width is set by how fast a real page can bend.
    """
    x = np.asarray(x_samples, dtype=np.float64).ravel()
    z = np.asarray(z_samples, dtype=np.float64).ravel()
    ok = np.isfinite(x) & np.isfinite(z) & (x >= x_min) & (x <= x_max)
    x, z = x[ok], z[ok]
    if x.size < n_bins:
        raise ValueError(f"too few points to fit a surface: {x.size}")

    edges = np.linspace(x_min, x_max, n_bins + 1)
    centres = 0.5 * (edges[1:] + edges[:-1])
    idx = np.clip(np.digitize(x, edges) - 1, 0, n_bins - 1)

    prof = np.full(n_bins, np.nan)
    for b in range(n_bins):
        sel = z[idx == b]
        if sel.size >= min_per_bin:
            prof[b] = np.median(sel)

    good = np.isfinite(prof)
    if good.sum() < 8:
        raise ValueError("surface fit: not enough populated bins")
    prof = np.interp(centres, centres[good], prof[good])

    # Smooth with a window matched to the tightest bend paper can take.
    bin_mm = (x_max - x_min) / n_bins
    k = max(3, int(round(smooth_mm / bin_mm)) | 1)
    w = np.hanning(k)
    w /= w.sum()
    prof = np.convolve(np.pad(prof, k // 2, mode="edge"), w, mode="valid")

    return SampledSurface(centres, prof)


@dataclass
class SampledSurface:
    """A height profile recovered from measurement, with the same API."""

    x_mm: np.ndarray
    z_mm: np.ndarray

    def height(self, x, y=None):
        return np.interp(np.asarray(x, dtype=np.float64), self.x_mm, self.z_mm)

    def slope(self, x, eps: float = 1e-4):
        x = np.asarray(x, dtype=np.float64)
        return (self.height(x + eps) - self.height(x - eps)) / (2 * eps)

    def stretch_at(self, x):
        return np.sqrt(1.0 + self.slope(x) ** 2)

    @property
    def spine_x_mm(self) -> float:
        """The spine is the height maximum of the recovered profile."""
        return float(self.x_mm[int(np.argmax(self.z_mm))])

    def _grid(self, x_min=None, x_max=None, n: int = 20001):
        lo = self.x_mm[0] if x_min is None else x_min
        hi = self.x_mm[-1] if x_max is None else x_max
        xs = np.linspace(lo, hi, n)
        ds = np.sqrt(1.0 + self.slope(xs) ** 2)
        s = np.concatenate([[0.0], np.cumsum(0.5 * (ds[1:] + ds[:-1]) * np.diff(xs))])
        return xs, s - np.interp(self.spine_x_mm, xs, s)

    def arc_length(self, x, x_min=None, x_max=None):
        xs, s = self._grid(x_min, x_max)
        return np.interp(np.asarray(x, dtype=np.float64), xs, s)

    def x_of_arc_length(self, s, x_min=None, x_max=None):
        xs, sg = self._grid(x_min, x_max)
        return np.interp(np.asarray(s, dtype=np.float64), sg, xs)
