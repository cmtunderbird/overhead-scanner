"""
Recovering the page surface by plane sweep.

The obvious approach is block matching or SGBM on a rectified pair.  It
works, but it throws away the strongest fact we have: the page is a
*developable surface with generators parallel to the spine*, so its
height depends on x alone.  Every pixel in a column at platen x sits at
the same height.

So instead of matching patches and then fitting a surface to a noisy
cloud, we sweep candidate heights and, for each one, correlate the
**entire page column** between the two views.  Hundreds of rows of
evidence collapse into one number per (x, height), which is why this is
far more robust than per-pixel matching on a page whose margins have no
texture at all:

  * a blank column has no signal either way, and is reported as such
    rather than producing a confident wrong depth;
  * a column with a single line of text still matches, because the
    correlation runs over the whole column height;
  * the answer is already a surface, so no separate outlier rejection is
    needed.

The cost is that it cannot represent cockling or a dog-eared corner --
anything that varies along y.  For those, this profile is the initial
estimate a finer method refines.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..synth.surface import SampledSurface


@dataclass
class SweepResult:
    """The recovered page surface, plus how much to believe it."""

    surface: SampledSurface
    x_mm: np.ndarray
    height_mm: np.ndarray
    #: Peak normalised correlation per column, 0..1.
    confidence: np.ndarray
    #: Width of the correlation peak per column, mm. Wide == ambiguous.
    peak_width_mm: np.ndarray
    #: Columns whose correlation peak was too weak or too broad to trust.
    unreliable: np.ndarray
    height_grid_mm: np.ndarray
    #: Full cost volume, (n_x, n_h) -- useful for diagnosis.
    cost: np.ndarray

    @property
    def spine_x_mm(self) -> float:
        return self.surface.spine_x_mm

    def report(self) -> str:
        good = ~self.unreliable
        return "\n".join([
            f"plane sweep: {len(self.x_mm)} columns, "
            f"{self.height_grid_mm.size} height hypotheses "
            f"({self.height_grid_mm[0]:.0f}..{self.height_grid_mm[-1]:.0f} mm)",
            f"  reliable columns   {100 * good.mean():.0f} % "
            f"(rejected {int((~good).sum())} as texture-free)",
            f"  mean peak ncc      {np.nanmean(self.confidence[good]):.3f}",
            f"  mean peak width    {np.nanmean(self.peak_width_mm[good]):.2f} mm",
            f"  recovered rise     {np.nanmax(self.height_mm):.2f} mm "
            f"at x = {self.spine_x_mm:.1f} mm",
        ])


def _project(pts_world: np.ndarray, K, dist, R, C) -> np.ndarray:
    """World mm -> image px, distortion included."""
    rvec, _ = cv2.Rodrigues(R)
    tvec = (-R @ C).reshape(3, 1)
    uv, _ = cv2.projectPoints(
        pts_world.reshape(-1, 1, 3).astype(np.float64), rvec, tvec, K, dist
    )
    return uv.reshape(pts_world.shape[:-1] + (2,))


def _sample(gray: np.ndarray, uv: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    mx = uv[..., 0].astype(np.float32)
    my = uv[..., 1].astype(np.float32)
    out = cv2.remap(
        gray, mx, my, cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=np.nan,
    )
    inside = (mx >= 0) & (mx <= w - 1) & (my >= 0) & (my <= h - 1)
    return np.where(inside, out, np.nan)


def sweep_surface(
    image_a: np.ndarray,
    image_b: np.ndarray,
    cam_a: dict,
    cam_b: dict,
    *,
    x_range_mm: tuple[float, float] = (0.0, 420.0),
    y_range_mm: tuple[float, float] = (10.0, 287.0),
    height_range_mm: tuple[float, float] = (-5.0, 60.0),
    x_step_mm: float = 1.5,
    height_step_mm: float = 0.25,
    n_y_samples: int = 420,
    min_confidence: float = 0.35,
    max_peak_width_mm: float = 6.0,
    peak_drop: float = 0.06,
    spline_smooth: float = 0.02,
    smooth_mm: float = 10.0,
) -> SweepResult:
    """
    Recover h(x) by correlating page columns between two views.

    `cam_a` / `cam_b` are dicts with keys K, dist, R, C -- the camera
    matrix, distortion, world->camera rotation and camera centre in
    platen millimetres.
    """
    ga = cv2.cvtColor(image_a, cv2.COLOR_BGR2GRAY).astype(np.float32) if image_a.ndim == 3 else image_a.astype(np.float32)
    gb = cv2.cvtColor(image_b, cv2.COLOR_BGR2GRAY).astype(np.float32) if image_b.ndim == 3 else image_b.astype(np.float32)

    xs = np.arange(x_range_mm[0], x_range_mm[1] + 1e-9, x_step_mm)
    hs = np.arange(height_range_mm[0], height_range_mm[1] + 1e-9, height_step_mm)
    ys = np.linspace(y_range_mm[0], y_range_mm[1], n_y_samples)

    nx, nh = xs.size, hs.size
    cost = np.full((nx, nh), np.nan, dtype=np.float32)

    # grid of (x, y) shared by every hypothesis; only z changes
    X, Y = np.meshgrid(xs, ys, indexing="ij")          # (nx, ny)

    for j, hgt in enumerate(hs):
        pts = np.stack([X, Y, np.full_like(X, hgt)], axis=-1)
        ua = _project(pts, cam_a["K"], cam_a["dist"], cam_a["R"], cam_a["C"])
        ub = _project(pts, cam_b["K"], cam_b["dist"], cam_b["R"], cam_b["C"])
        sa = _sample(ga, ua)
        sb = _sample(gb, ub)

        ok = np.isfinite(sa) & np.isfinite(sb)
        n = ok.sum(axis=1)
        a = np.where(ok, sa, 0.0)
        b = np.where(ok, sb, 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            ma = a.sum(axis=1) / n
            mb = b.sum(axis=1) / n
            da = np.where(ok, sa - ma[:, None], 0.0)
            db = np.where(ok, sb - mb[:, None], 0.0)
            num = (da * db).sum(axis=1)
            den = np.sqrt((da * da).sum(axis=1) * (db * db).sum(axis=1))
            ncc = np.where(den > 1e-6, num / den, np.nan)
        cost[:, j] = np.where(n > n_y_samples * 0.3, ncc, np.nan)

    # -- extract the ridge -------------------------------------------------
    #
    # Peak *height* is the wrong confidence measure and it took a bad
    # surface to notice.  Two smooth gradients correlate at 0.95 whatever
    # height you assume them to be at, so a blank margin scores as highly
    # as a page of text.  What separates them is peak *sharpness*: a
    # textured column has a correlation peak ~1.5 mm wide, a blank one is
    # flat across the whole search range.
    filled = np.nan_to_num(cost, nan=-2.0)
    best = np.argmax(filled, axis=1)
    peak = filled[np.arange(nx), best]

    width = np.full(nx, np.inf)
    for i in range(nx):
        c = filled[i]
        if not np.isfinite(peak[i]) or peak[i] <= 0:
            continue
        above = np.where(c > peak[i] - peak_drop)[0]
        if above.size:
            width[i] = (above.max() - above.min()) * height_step_mm

    unreliable = (peak < min_confidence) | (width > max_peak_width_mm)
    if unreliable.all():
        raise ValueError(
            "plane sweep found no reliable columns -- check the calibration, "
            "the height search range, and that both views see the page"
        )

    # sub-step refinement by a parabola through the correlation peak
    height = hs[best].astype(np.float64)
    for i in range(nx):
        j = best[i]
        if 0 < j < nh - 1 and not unreliable[i]:
            c0, c1, c2 = filled[i, j - 1], filled[i, j], filled[i, j + 1]
            denom = c0 - 2 * c1 + c2
            if abs(denom) > 1e-9:
                height[i] += height_step_mm * 0.5 * (c0 - c2) / denom

    # Blank regions carry no depth information, so do not invent any --
    # interpolate the surface across them.  A smoothing spline rather than
    # a straight line, because the largest gap is the gutter itself, which
    # sits on the apex of the curve; a chord across a peak cuts the corner
    # and would systematically under-read the spine height.
    good = ~unreliable
    w = np.clip(peak[good] - min_confidence, 1e-3, None)
    try:
        from scipy.interpolate import UnivariateSpline
        spl = UnivariateSpline(xs[good], height[good], w=w / w.mean(),
                               k=3, s=len(w) * spline_smooth)
        height = spl(xs)
    except Exception:  # scipy missing or degenerate -- fall back
        height = np.interp(xs, xs[good], height[good])
        k = max(3, int(round(smooth_mm / x_step_mm)) | 1)
        win = np.hanning(k); win /= win.sum()
        height = np.convolve(np.pad(height, k // 2, mode="edge"), win, mode="valid")

    return SweepResult(
        surface=SampledSurface(xs, height),
        x_mm=xs,
        height_mm=height,
        confidence=peak,
        peak_width_mm=width,
        unreliable=unreliable,
        height_grid_mm=hs,
        cost=cost,
    )


def cameras_from_capture(cap) -> dict:
    """Pull the {K, dist, R, C} a sweep needs out of a SurfaceCapture."""
    return {"K": cap.K, "dist": cap.dist, "R": cap.R, "C": cap.C}
