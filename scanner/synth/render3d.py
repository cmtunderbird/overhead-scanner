"""
Photographing a curved page.

The flat renderer in `camera.py` warps by a homography, which is exactly
the statement "the document is a plane".  For a book that is thick enough
to matter, it is not, so this module casts rays instead: for every image
pixel it finds where that ray meets the book surface, and reads the page
texture at the corresponding *paper* coordinate.

That gives three things the dewarper can be graded against:

    image        what the camera records
    depth        true height of the surface at every pixel
    page map     true paper coordinate (s, y) at every pixel

The last one is the answer key.  A dewarper's job is to recover it from
the images alone, and here we can subtract.

Coordinate note: this renderer is physically correct, so for a camera
above the platen looking down, world +y appears going *up* the image.
The flat renderer's document canvas has +y going down.  They are related
by a flip, and both are internally consistent; the calibrated transforms
absorb it.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .camera import CameraDefects, _apply_distortion, _photometry
from .surface import BookSurface


@dataclass
class CameraPose:
    """Where a camera is and what it is aimed at, in platen millimetres."""

    position_mm: tuple[float, float, float]
    target_mm: tuple[float, float, float] = (210.0, 148.5, 0.0)

    def rotation(self) -> np.ndarray:
        """World -> camera rotation, OpenCV convention (z forward, y down)."""
        C = np.asarray(self.position_mm, dtype=np.float64)
        T = np.asarray(self.target_mm, dtype=np.float64)
        z = T - C
        z /= np.linalg.norm(z)
        # Image "down" follows world -y, which is what a camera above the
        # platen actually does.  Any other choice is left-handed.
        ref = np.array([0.0, -1.0, 0.0])
        y = ref - np.dot(ref, z) * z
        if np.linalg.norm(y) < 1e-9:
            ref = np.array([0.0, 0.0, 1.0])
            y = ref - np.dot(ref, z) * z
        y /= np.linalg.norm(y)
        x = np.cross(y, z)
        return np.stack([x, y, z])

    @property
    def centre(self) -> np.ndarray:
        return np.asarray(self.position_mm, dtype=np.float64)

    def distance_to_target(self) -> float:
        return float(np.linalg.norm(self.centre - np.asarray(self.target_mm)))

    def world_to_camera(self, pts: np.ndarray) -> np.ndarray:
        return (np.asarray(pts) - self.centre) @ self.rotation().T


def intersect_surface(
    origin: np.ndarray,
    directions: np.ndarray,
    surface: BookSurface,
    *,
    iterations: int = 12,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Ray-surface intersection, vectorised.

    Solve  C_z + t d_z = h(C_x + t d_x)  by Newton from the flat-plane
    guess.  The height field is smooth and shallow relative to the
    standoff, so this converges in a handful of iterations for every
    pixel at once.

    Returns (points, valid).
    """
    C = np.asarray(origin, dtype=np.float64)
    d = np.asarray(directions, dtype=np.float64)
    dz = d[..., 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(np.abs(dz) > 1e-12, (0.0 - C[2]) / dz, np.nan)

    for _ in range(iterations):
        x = C[0] + t * d[..., 0]
        g = (C[2] + t * dz) - surface.height(x)
        gp = dz - surface.slope(x) * d[..., 0]
        step = np.where(np.abs(gp) > 1e-12, g / gp, 0.0)
        t = t - np.clip(step, -200.0, 200.0)

    pts = C[None, ...] + t[..., None] * d
    resid = np.abs(pts[..., 2] - surface.height(pts[..., 0]))
    valid = np.isfinite(t) & (t > 0) & (resid < 0.05)
    return pts, valid


def _pixel_rays(K: np.ndarray, dist: np.ndarray, w: int, h: int) -> np.ndarray:
    """Unit ray direction in camera coordinates for every (distorted) pixel."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    grid = np.stack([xs, ys], axis=-1).reshape(-1, 1, 2)
    norm = cv2.undistortPoints(grid, K, dist).reshape(h, w, 2)
    d = np.concatenate([norm, np.ones((h, w, 1))], axis=-1)
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


@dataclass
class SurfaceCapture:
    """One rendered frame plus everything the dewarper must reproduce."""

    image: np.ndarray
    #: Height of the surface at each pixel, mm above the platen. NaN where no hit.
    height_mm: np.ndarray
    #: Distance along the optical axis, mm.
    depth_mm: np.ndarray
    #: True paper coordinate at each pixel: arc length from the spine, mm.
    page_s_mm: np.ndarray
    #: True paper coordinate at each pixel: y, mm.
    page_y_mm: np.ndarray
    #: Platen x at each pixel, mm.
    platen_x_mm: np.ndarray
    valid: np.ndarray
    K: np.ndarray
    dist: np.ndarray
    pose: CameraPose
    px_per_mm_nominal: float

    @property
    def R(self) -> np.ndarray:
        return self.pose.rotation()

    @property
    def C(self) -> np.ndarray:
        return self.pose.centre

    def projection(self) -> np.ndarray:
        """3x4 projection matrix, world mm -> image px."""
        R = self.R
        t = -R @ self.C
        return self.K @ np.hstack([R, t.reshape(3, 1)])


def render_surface(
    page_bgr: np.ndarray,
    page_px_per_mm: float,
    surface: BookSurface,
    K: np.ndarray,
    dist: np.ndarray,
    pose: CameraPose,
    size: tuple[int, int],
    defects: CameraDefects | None = None,
    *,
    doc_height_mm: float = 297.0,
    focus_height_mm: float = 0.0,
    f_number: float = 8.0,
    pixel_pitch_mm: float = 0.0039,
    defocus_bands: int = 6,
    seed: int = 0,
) -> SurfaceCapture:
    """
    Photograph a curved page.

    `page_bgr` is the **flat** sheet: its width is the paper's arc length,
    not its projected width.  The spine sits at its horizontal centre.
    """
    d = defects or CameraDefects()
    rng = np.random.default_rng(seed)
    w, h = size

    # -- 1. where does every pixel land on the book? ------------------------
    rays_cam = _pixel_rays(K, dist, w, h)
    R = pose.rotation()
    rays_world = rays_cam @ R  # R^T applied on the right
    pts, valid = intersect_surface(pose.centre, rays_world, surface)

    x_w, y_w, z_w = pts[..., 0], pts[..., 1], pts[..., 2]
    s_mm = surface.arc_length(x_w)

    # -- 2. sample the flat page at those paper coordinates ----------------
    ph, pw = page_bgr.shape[:2]
    paper_len = pw / page_px_per_mm
    map_x = ((s_mm + paper_len / 2.0) * page_px_per_mm).astype(np.float32)
    map_y = (y_w * page_px_per_mm).astype(np.float32)

    inside = (
        valid
        & (map_x >= 0) & (map_x <= pw - 1)
        & (map_y >= 0) & (map_y <= ph - 1)
        & (y_w >= 0) & (y_w <= doc_height_mm)
    )
    img = cv2.remap(
        page_bgr, map_x, map_y, cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=(28, 28, 30),
    ).astype(np.float32)
    img[~inside] = (28, 28, 30)

    # -- 3. defocus that varies with height -------------------------------
    cam_pts = (pts - pose.centre) @ R.T
    depth = cam_pts[..., 2]
    z_focus = pose.distance_to_target() - focus_height_mm
    # Magnification at the focus plane, read from the actual camera matrix:
    #   f_px = K[0,0],  m = f_px * pitch / z_focus
    m_focus = float(K[0, 0]) * pixel_pitch_mm / max(z_focus, 1e-6)
    coc_px = np.abs(depth - z_focus) * m_focus * m_focus / f_number / pixel_pitch_mm
    coc_px = np.where(inside, coc_px, 0.0)

    sigma_base = d.blur_sigma_px
    if defocus_bands > 1 and np.nanmax(coc_px) > 0.6:
        top = float(np.nanpercentile(coc_px[inside], 99.5)) if inside.any() else 0.0
        edges = np.linspace(0.0, max(top, 1e-6), defocus_bands + 1)
        out = np.zeros_like(img)
        wsum = np.zeros(img.shape[:2], np.float32)
        for i in range(defocus_bands):
            centre = 0.5 * (edges[i] + edges[i + 1])
            sigma = float(np.hypot(sigma_base, centre / 2.0))
            layer = cv2.GaussianBlur(img, (0, 0), sigma) if sigma > 0.05 else img
            half = 0.5 * (edges[i + 1] - edges[i]) + 1e-6
            wt = np.clip(1.0 - np.abs(coc_px - centre) / (2 * half), 0.0, 1.0)
            out += layer * wt[..., None]
            wsum += wt
        img = out / np.maximum(wsum, 1e-6)[..., None]
    elif sigma_base > 0.05:
        img = cv2.GaussianBlur(img, (0, 0), sigma_base)

    # -- 4. sensor ---------------------------------------------------------
    out8 = _photometry(img, d, rng)

    nan = np.where(inside, 1.0, np.nan)
    return SurfaceCapture(
        image=out8,
        height_mm=(z_w * nan).astype(np.float32),
        depth_mm=(depth * nan).astype(np.float32),
        page_s_mm=(s_mm * nan).astype(np.float32),
        page_y_mm=(y_w * nan).astype(np.float32),
        platen_x_mm=(x_w * nan).astype(np.float32),
        valid=inside,
        K=K, dist=dist, pose=pose,
        px_per_mm_nominal=float(K[0, 0]) / max(z_focus, 1e-6),
    )


# --------------------------------------------------------------------------
# building a converged stereo pair
# --------------------------------------------------------------------------


def stereo_pair_poses(
    baseline_mm: float,
    height_mm: float,
    target_mm: tuple[float, float, float] = (210.0, 148.5, 0.0),
) -> tuple[CameraPose, CameraPose]:
    """
    Two cameras straddling the spine, both aimed at the page centre.

    Converged rather than parallel: with both cameras required to cover
    the whole spread, parallel mounting would need a field almost twice
    as wide as the page and would throw away most of the sensor.
    """
    tx, ty, _ = target_mm
    return (
        CameraPose((tx - baseline_mm / 2.0, ty, height_mm), target_mm),
        CameraPose((tx + baseline_mm / 2.0, ty, height_mm), target_mm),
    )


def camera_matrix(
    focal_mm: float, pixel_pitch_mm: float, size: tuple[int, int],
    cx_offset_px: float = 0.0, cy_offset_px: float = 0.0,
) -> np.ndarray:
    w, h = size
    f_px = focal_mm / pixel_pitch_mm
    return np.array([
        [f_px, 0.0, w / 2.0 + cx_offset_px],
        [0.0, f_px, h / 2.0 + cy_offset_px],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)
