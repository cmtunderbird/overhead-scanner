"""
Flattening the page.

Once the surface is known, dewarping is not an image-processing problem
any more -- it is a lookup.  For every pixel of the *flat* output page we
know its paper coordinate (s, y), the developable mapping tells us where
that piece of paper physically is in space, and the camera model tells us
which pixel photographed it.

    flat page (s, y)  --developable-->  3-D (x, y, z)  --camera-->  image px

Nothing is estimated at this stage, which is the point: all the
uncertainty lives in the surface, and it has already been measured.

Because both cameras see the whole page, each output pixel has two
candidate sources.  `fuse_views` picks per pixel, weighting by how
squarely each camera faces the paper there -- near the spine that is
decisively the near-side camera.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class DewarpResult:
    image: np.ndarray
    #: Per-pixel weight used from each view, when fusing.
    weights: list[np.ndarray] | None
    valid: np.ndarray
    page_px_per_mm: float
    s_range_mm: tuple[float, float]
    y_range_mm: tuple[float, float]

    @property
    def size_mm(self) -> tuple[float, float]:
        return (self.s_range_mm[1] - self.s_range_mm[0],
                self.y_range_mm[1] - self.y_range_mm[0])


def _project(pts_world, K, dist, R, C):
    rvec, _ = cv2.Rodrigues(np.asarray(R, dtype=np.float64))
    tvec = (-np.asarray(R) @ np.asarray(C)).reshape(3, 1)
    uv, _ = cv2.projectPoints(
        pts_world.reshape(-1, 1, 3).astype(np.float64), rvec, tvec, K, dist)
    return uv.reshape(pts_world.shape[:-1] + (2,))


def page_grid(
    surface,
    s_range_mm: tuple[float, float],
    y_range_mm: tuple[float, float],
    page_px_per_mm: float,
):
    """3-D position of every pixel of the flattened output page."""
    s = np.arange(s_range_mm[0], s_range_mm[1], 1.0 / page_px_per_mm)
    y = np.arange(y_range_mm[0], y_range_mm[1], 1.0 / page_px_per_mm)
    S, Y = np.meshgrid(s, y)                     # (rows=y, cols=s)
    X = surface.x_of_arc_length(S)
    Z = surface.height(X)
    return np.stack([X, Y, Z], axis=-1), s, y


def dewarp_view(
    image: np.ndarray,
    surface,
    K, dist, R, C,
    *,
    s_range_mm: tuple[float, float],
    y_range_mm: tuple[float, float],
    page_px_per_mm: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Resample one view onto the flat page.

    Returns (image, valid, obliquity) where obliquity is the cosine of the
    angle between the camera ray and the surface normal -- 1.0 when the
    camera is looking straight at that piece of paper, and the natural
    weight for fusing two views.
    """
    pts, s, y = page_grid(surface, s_range_mm, y_range_mm, page_px_per_mm)
    uv = _project(pts, K, dist, R, C)
    mx = uv[..., 0].astype(np.float32)
    my = uv[..., 1].astype(np.float32)

    h, w = image.shape[:2]
    valid = (mx >= 0) & (mx <= w - 1) & (my >= 0) & (my <= h - 1)
    out = cv2.remap(image, mx, my, cv2.INTER_LANCZOS4,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    out[~valid] = 0

    slope = surface.slope(pts[..., 0])
    n = np.stack([-slope, np.zeros_like(slope), np.ones_like(slope)], axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    v = np.asarray(C) - pts
    v /= np.linalg.norm(v, axis=-1, keepdims=True)
    obliquity = np.clip((n * v).sum(axis=-1), 0.0, 1.0)

    return out, valid, obliquity.astype(np.float32)


def fuse_views(
    views: list[tuple[np.ndarray, np.ndarray, np.ndarray]],
    *,
    sharpness: float = 4.0,
) -> DewarpResult | tuple:
    """
    Combine dewarped views, weighting each by how squarely it faces the
    paper.

    `sharpness` controls how decisively the better view wins.  It is a
    soft choice rather than a hard one so that the transition across the page
    is not itself a seam.
    """
    imgs = [v[0].astype(np.float32) for v in views]
    valids = [v[1] for v in views]
    obls = [v[2] for v in views]

    w = [np.where(vd, np.power(np.clip(o, 0, 1), sharpness), 0.0).astype(np.float32)
         for vd, o in zip(valids, obls)]
    total = np.sum(w, axis=0)
    any_valid = total > 1e-6
    total = np.maximum(total, 1e-6)

    acc = np.zeros_like(imgs[0])
    for im, wi in zip(imgs, w):
        acc += im * wi[..., None]
    fused = np.clip(acc / total[..., None], 0, 255).astype(np.uint8)
    fused[~any_valid] = 0
    return fused, any_valid, [wi / total for wi in w]


def dewarp_stereo(
    image_a: np.ndarray,
    image_b: np.ndarray,
    cam_a: dict,
    cam_b: dict,
    surface,
    *,
    s_range_mm: tuple[float, float],
    y_range_mm: tuple[float, float],
    page_px_per_mm: float,
    fuse: bool = True,
) -> DewarpResult:
    """Dewarp both views onto the flat page and fuse them."""
    va = dewarp_view(image_a, surface, cam_a["K"], cam_a["dist"], cam_a["R"],
                     cam_a["C"], s_range_mm=s_range_mm, y_range_mm=y_range_mm,
                     page_px_per_mm=page_px_per_mm)
    vb = dewarp_view(image_b, surface, cam_b["K"], cam_b["dist"], cam_b["R"],
                     cam_b["C"], s_range_mm=s_range_mm, y_range_mm=y_range_mm,
                     page_px_per_mm=page_px_per_mm)
    if not fuse:
        return DewarpResult(va[0], None, va[1], page_px_per_mm, s_range_mm, y_range_mm)
    img, valid, weights = fuse_views([va, vb])
    return DewarpResult(img, weights, valid, page_px_per_mm, s_range_mm, y_range_mm)


def arc_length_error(true_surface, fitted_surface, x_mm: np.ndarray) -> dict:
    """
    The number that decides whether a dewarp is any good.

    Paper coordinate error in millimetres: if the recovered arc length is
    wrong by ds, every glyph there is displaced by ds.  Height error only
    matters through this.
    """
    st = np.asarray(true_surface.arc_length(x_mm), dtype=np.float64)
    sf = np.asarray(fitted_surface.arc_length(x_mm), dtype=np.float64)
    # remove any constant offset: a rigid shift of the whole page is a
    # crop, not a distortion
    err = (sf - st) - np.median(sf - st)
    return {
        "rms_mm": float(np.sqrt(np.mean(err ** 2))),
        "max_mm": float(np.max(np.abs(err))),
        "p95_mm": float(np.percentile(np.abs(err), 95)),
        "error_mm": err,
    }
