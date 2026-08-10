"""
Camera simulator.

Takes a page rendered in document space and produces what a Sony A6000
mounted on the rig would actually record: correct framing for its tile,
lens distortion, vignetting, a per-body colour cast, mounting error,
defocus and sensor noise.

Every capture carries a `CaptureTruth` holding the exact K, distortion
coefficients and document->image homography used.  That is what makes it
possible to prove the calibration and pipeline are right before a real
photograph exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

import cv2
import numpy as np

from ..geometry import RigGeometry, BLUEPRINT_V1
from ..metrics.colour import srgb_to_linear, linear_to_srgb


@dataclass
class CameraDefects:
    """
    Everything that makes a real camera not a pinhole.

    Defaults are representative of a Sony E 18-55 kit lens at 30 mm f/8 --
    modest barrel distortion, mild vignetting, a small colour cast.
    """

    # Brown-Conrady radial/tangential distortion (normalised coords).
    k1: float = -0.085
    k2: float = 0.021
    p1: float = 0.0004
    p2: float = -0.0003

    # Vignetting: I *= 1 - a*r^2 - b*r^4, r normalised to the corner.
    vignette_a: float = 0.22
    vignette_b: float = 0.10

    # Per-body colour: linear RGB channel gains.  Two bodies of the same
    # model differ by a few percent -- which is exactly the visible seam
    # down the middle of a spread if you skip colour matching.
    gain_r: float = 1.0
    gain_g: float = 1.0
    gain_b: float = 1.0
    exposure: float = 1.0

    # Mounting error, relative to nominal.
    rotation_deg: float = 0.0
    dx_mm: float = 0.0
    dy_mm: float = 0.0
    scale_err: float = 0.0  # fractional, e.g. 0.002 = 0.2 % too big

    # Principal point offset from frame centre, in pixels (at full scale).
    cx_offset_px: float = 0.0
    cy_offset_px: float = 0.0

    # Optics + sensor.
    blur_sigma_px: float = 0.62  # combined lens + AA + diffraction
    defocus_mm: float = 0.0  # page height error away from the focal plane
    read_noise_dn: float = 1.1
    shot_noise_k: float = 0.035  # sigma = k*sqrt(signal), in DN

    def scaled(self, s: float) -> "CameraDefects":
        """Rescale pixel-valued defects for a reduced-resolution render."""
        out = CameraDefects(**asdict(self))
        out.cx_offset_px *= s
        out.cy_offset_px *= s
        out.blur_sigma_px *= s
        return out


#: Two bodies that are the same model but not the same camera.
BODY_A = CameraDefects(
    k1=-0.085, k2=0.021, gain_r=1.000, gain_g=1.000, gain_b=1.000,
    exposure=1.00, rotation_deg=0.18, dx_mm=0.6, dy_mm=-0.4,
    cx_offset_px=14.0, cy_offset_px=-9.0,
)
BODY_B = CameraDefects(
    k1=-0.079, k2=0.018, gain_r=1.035, gain_g=0.995, gain_b=0.962,
    exposure=0.96, rotation_deg=-0.24, dx_mm=-0.9, dy_mm=0.5,
    cx_offset_px=-11.0, cy_offset_px=12.0, vignette_a=0.25,
)


@dataclass
class CaptureTruth:
    """Ground truth for one simulated frame."""

    tile_index: int
    width_px: int
    height_px: int
    px_per_mm: float
    #: Camera matrix used, 3x3.
    K: np.ndarray
    #: Distortion coefficients (k1,k2,p1,p2,0).
    dist: np.ndarray
    #: Ideal (undistorted) document-mm -> image-px homography, 3x3.
    H_doc_to_img: np.ndarray
    #: Tile extent on the document long axis, mm.
    tile_x0_mm: float
    tile_x1_mm: float
    doc_short_mm: float
    defects: CameraDefects = field(default_factory=CameraDefects)

    @property
    def H_img_to_doc(self) -> np.ndarray:
        return np.linalg.inv(self.H_doc_to_img)

    def to_json(self) -> dict:
        return {
            "tile_index": self.tile_index,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "px_per_mm": self.px_per_mm,
            "K": self.K.tolist(),
            "dist": self.dist.ravel().tolist(),
            "H_doc_to_img": self.H_doc_to_img.tolist(),
            "tile_x0_mm": self.tile_x0_mm,
            "tile_x1_mm": self.tile_x1_mm,
            "doc_short_mm": self.doc_short_mm,
            "defects": asdict(self.defects),
        }


def _nominal_homography(
    geom: RigGeometry, tile_index: int, ppm: float, w: int, h: int
) -> np.ndarray:
    """Document mm -> image px, perfectly mounted camera."""
    tile = geom.tiles[tile_index]
    cx_mm = tile.centre_mm
    cy_mm = geom.document.short_mm / 2.0
    return np.array(
        [
            [ppm, 0.0, w / 2.0 - ppm * cx_mm],
            [0.0, ppm, h / 2.0 - ppm * cy_mm],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def _perturbation(d: CameraDefects, ppm: float, w: int, h: int) -> np.ndarray:
    """Mounting error, applied about the frame centre, in image px."""
    th = np.radians(d.rotation_deg)
    s = 1.0 + d.scale_err
    c, sn = np.cos(th) * s, np.sin(th) * s
    cx, cy = w / 2.0, h / 2.0
    R = np.array(
        [
            [c, -sn, cx - c * cx + sn * cy + d.dx_mm * ppm],
            [sn, c, cy - sn * cx - c * cy + d.dy_mm * ppm],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return R


def _apply_distortion(
    img: np.ndarray, K: np.ndarray, dist: np.ndarray
) -> np.ndarray:
    """
    Bend a perfect image the way a real lens would.

    `undistortPoints` maps observed (distorted) pixels back to ideal ones,
    which is precisely the sampling map remap() needs to synthesise a
    distorted frame from an ideal one.
    """
    h, w = img.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    grid = np.stack([xs, ys], axis=-1).reshape(-1, 1, 2)
    ideal = cv2.undistortPoints(grid, K, dist, P=K).reshape(h, w, 2)
    return cv2.remap(
        img,
        ideal[..., 0].copy(),
        ideal[..., 1].copy(),
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _vignette_map(shape: tuple[int, int], d: CameraDefects) -> np.ndarray:
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2.0, h / 2.0
    r2 = ((xs - cx) ** 2 + (ys - cy) ** 2) / (cx * cx + cy * cy)
    v = 1.0 - d.vignette_a * r2 - d.vignette_b * r2 * r2
    return np.clip(v, 0.0, 1.0).astype(np.float32)


def _photometry(img_encoded: np.ndarray, d: CameraDefects,
                rng: np.random.Generator) -> np.ndarray:
    """
    Apply vignetting, channel gains and exposure -- in LINEAR light.

    This is not a detail.  A real sensor collects photons linearly and
    gamma-encodes afterwards, so vignetting and white balance are linear
    operations.  Simulating them on gamma-encoded values produces an
    image that no linear colour matrix can correct, which would make the
    calibration look broken when it is the simulator that is wrong.
    """
    lin = srgb_to_linear(np.clip(img_encoded, 0, 255) / 255.0)
    lin *= _vignette_map(lin.shape[:2], d)[..., None]
    gains = np.array([d.gain_b, d.gain_g, d.gain_r], dtype=np.float64)  # BGR
    lin *= gains[None, None, :] * d.exposure
    out = linear_to_srgb(lin) * 255.0
    noise = rng.normal(0.0, 1.0, out.shape)
    out = out + noise * (d.read_noise_dn + d.shot_noise_k * np.sqrt(np.clip(out, 0, None)))
    return np.clip(out, 0, 255).astype(np.uint8)


def simulate_capture(
    page_bgr: np.ndarray,
    page_px_per_mm: float,
    geom: RigGeometry = BLUEPRINT_V1,
    tile_index: int = 0,
    defects: CameraDefects | None = None,
    *,
    scale: float = 1.0,
    seed: int = 0,
) -> tuple[np.ndarray, CaptureTruth]:
    """
    Render what one camera records.

    `scale` shrinks the output resolution without changing the field of
    view, so tests can run at 1/8 scale and still exercise every stage.
    The document is assumed flat -- a platen build.
    """
    if tile_index >= geom.n_cameras:
        raise IndexError(f"tile {tile_index} but rig has {geom.n_cameras} cameras")

    d = (defects or CameraDefects()).scaled(scale)
    rng = np.random.default_rng(seed)

    # Frame size in the scene-oriented frame: width spans the split axis.
    s = geom.sensor
    if geom.resolved_orientation == "portrait":
        across_px, along_px = s.short_px, s.long_px
    else:
        across_px, along_px = s.long_px, s.short_px
    w = int(round(across_px * scale))
    h = int(round(along_px * scale))
    ppm = geom.px_per_mm * scale

    # -- geometry ----------------------------------------------------------
    H_nom = _nominal_homography(geom, tile_index, ppm, w, h)
    H_doc_to_img = _perturbation(d, ppm, w, h) @ H_nom

    # page pixels -> document mm
    S = np.array(
        [
            [1.0 / page_px_per_mm, 0.0, 0.0],
            [0.0, 1.0 / page_px_per_mm, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    H_page_to_img = H_doc_to_img @ S

    ideal = cv2.warpPerspective(
        page_bgr,
        H_page_to_img,
        (w, h),
        flags=cv2.INTER_AREA if page_px_per_mm > ppm else cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(28, 28, 30),  # the table, not the page
    ).astype(np.float32)

    # -- lens --------------------------------------------------------------
    fpx = geom.focal_px * scale
    K = np.array(
        [
            [fpx, 0.0, w / 2.0 + d.cx_offset_px],
            [0.0, fpx, h / 2.0 + d.cy_offset_px],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    dist = np.array([d.k1, d.k2, d.p1, d.p2, 0.0], dtype=np.float64)

    img = _apply_distortion(ideal, K, dist)

    # Defocus: a page off the focal plane blurs by roughly
    # (defocus * m^2 / N) millimetres of circle, converted to pixels.
    sigma = d.blur_sigma_px
    if d.defocus_mm:
        m = geom.magnification
        coc_mm = abs(d.defocus_mm) * m * m / geom.lens.f_number
        coc_px = coc_mm / (geom._binding_pitch_mm) * scale
        sigma = float(np.hypot(sigma, coc_px / 2.0))
    if sigma > 0.05:
        img = cv2.GaussianBlur(img, (0, 0), sigma)

    # -- sensor ------------------------------------------------------------
    out = _photometry(img, d, rng)

    truth = CaptureTruth(
        tile_index=tile_index,
        width_px=w,
        height_px=h,
        px_per_mm=ppm,
        K=K,
        dist=dist,
        H_doc_to_img=H_doc_to_img,
        tile_x0_mm=geom.tiles[tile_index].x0_mm,
        tile_x1_mm=geom.tiles[tile_index].x1_mm,
        doc_short_mm=geom.document.short_mm,
        defects=d,
    )
    return out, truth


def simulate_rig(
    page_bgr: np.ndarray,
    page_px_per_mm: float,
    geom: RigGeometry = BLUEPRINT_V1,
    bodies: list[CameraDefects] | None = None,
    *,
    scale: float = 1.0,
    seed: int = 0,
) -> list[tuple[np.ndarray, CaptureTruth]]:
    """Simulate every camera in the rig against the same page."""
    if bodies is None:
        bodies = [BODY_A, BODY_B][: geom.n_cameras]
        while len(bodies) < geom.n_cameras:
            bodies.append(CameraDefects())
    return [
        simulate_capture(
            page_bgr, page_px_per_mm, geom, i, bodies[i], scale=scale, seed=seed + i
        )
        for i in range(geom.n_cameras)
    ]


def simulate_flat_field(
    geom: RigGeometry,
    defects: CameraDefects,
    *,
    scale: float = 1.0,
    level: int = 200,
    seed: int = 0,
) -> np.ndarray:
    """An evenly lit blank sheet, as shot by this body. Flat-field reference."""
    s = geom.sensor
    if geom.resolved_orientation == "portrait":
        across_px, along_px = s.short_px, s.long_px
    else:
        across_px, along_px = s.long_px, s.short_px
    w, h = int(round(across_px * scale)), int(round(along_px * scale))
    d = defects.scaled(scale)
    rng = np.random.default_rng(seed)

    img = np.full((h, w, 3), float(level), dtype=np.float32)
    return _photometry(img, d, rng)


# --------------------------------------------------------------------------
# Planar target at an arbitrary pose -- for intrinsics calibration
# --------------------------------------------------------------------------


@dataclass
class BoardPose:
    """Rigid pose of a planar calibration target in camera coordinates."""

    #: Rotation about x, y, z in degrees, applied in that order.
    rx_deg: float = 0.0
    ry_deg: float = 0.0
    rz_deg: float = 0.0
    #: Translation of the board origin, mm, in camera coordinates.
    tx_mm: float = 0.0
    ty_mm: float = 0.0
    tz_mm: float = 450.0

    def rvec_tvec(self) -> tuple[np.ndarray, np.ndarray]:
        rx, ry, rz = (
            np.radians(self.rx_deg),
            np.radians(self.ry_deg),
            np.radians(self.rz_deg),
        )
        Rx = np.array([[1, 0, 0], [0, np.cos(rx), -np.sin(rx)], [0, np.sin(rx), np.cos(rx)]])
        Ry = np.array([[np.cos(ry), 0, np.sin(ry)], [0, 1, 0], [-np.sin(ry), 0, np.cos(ry)]])
        Rz = np.array([[np.cos(rz), -np.sin(rz), 0], [np.sin(rz), np.cos(rz), 0], [0, 0, 1]])
        R = Rz @ Ry @ Rx
        t = np.array([[self.tx_mm], [self.ty_mm], [self.tz_mm]], dtype=np.float64)
        return R, t


def simulate_board_capture(
    board_bgr: np.ndarray,
    board_px_per_mm: float,
    geom: RigGeometry,
    pose: BoardPose,
    defects: CameraDefects | None = None,
    *,
    scale: float = 1.0,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Photograph a planar calibration target held at an arbitrary pose.

    Intrinsics cannot be solved from a single fronto-parallel plane -- the
    focal length and the distance trade off exactly.  You need tilt.  This
    is what you will actually do tomorrow: hold a printed ChArUco board at
    a dozen angles and let `calibrate_intrinsics` sort it out.

    Returns (image, K, dist).
    """
    d = (defects or CameraDefects()).scaled(scale)
    rng = np.random.default_rng(seed)

    s = geom.sensor
    if geom.resolved_orientation == "portrait":
        across_px, along_px = s.short_px, s.long_px
    else:
        across_px, along_px = s.long_px, s.short_px
    w, h = int(round(across_px * scale)), int(round(along_px * scale))

    fpx = geom.focal_px * scale
    K = np.array(
        [[fpx, 0.0, w / 2.0 + d.cx_offset_px],
         [0.0, fpx, h / 2.0 + d.cy_offset_px],
         [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    dist = np.array([d.k1, d.k2, d.p1, d.p2, 0.0], dtype=np.float64)

    R, t = pose.rvec_tvec()
    # Planar target: z = 0, so the projection collapses to a homography
    # from board mm to image px.
    Rt = np.hstack([R[:, :1], R[:, 1:2], t])
    H_mm_to_img = K @ Rt
    H_mm_to_img /= H_mm_to_img[2, 2]

    # Centre the board on its own origin so the pose rotates about its middle.
    bh, bw = board_bgr.shape[:2]
    S = np.array(
        [[1.0 / board_px_per_mm, 0.0, -0.5 * bw / board_px_per_mm],
         [0.0, 1.0 / board_px_per_mm, -0.5 * bh / board_px_per_mm],
         [0.0, 0.0, 1.0]]
    )
    H_page_to_img = H_mm_to_img @ S

    ideal = cv2.warpPerspective(
        board_bgr, H_page_to_img, (w, h),
        flags=cv2.INTER_AREA, borderMode=cv2.BORDER_CONSTANT,
        borderValue=(40, 40, 44),
    ).astype(np.float32)

    img = _apply_distortion(ideal, K, dist)
    if d.blur_sigma_px > 0.05:
        img = cv2.GaussianBlur(img, (0, 0), d.blur_sigma_px)
    return _photometry(img, d, rng), K, dist


def default_calibration_poses(
    geom: RigGeometry, n: int = 14, seed: int = 3
) -> list[BoardPose]:
    """
    A spread of poses that conditions the calibration well: tilts in both
    axes, a couple of rolls, and some depth variation.
    """
    rng = np.random.default_rng(seed)
    z0 = geom.working_distance_mm
    poses = []
    for i in range(n):
        poses.append(
            BoardPose(
                rx_deg=float(rng.uniform(-26, 26)),
                ry_deg=float(rng.uniform(-26, 26)),
                rz_deg=float(rng.uniform(-18, 18)),
                tx_mm=float(rng.uniform(-0.10, 0.10) * z0),
                ty_mm=float(rng.uniform(-0.10, 0.10) * z0),
                tz_mm=float(z0 * rng.uniform(0.86, 1.14)),
            )
        )
    return poses
