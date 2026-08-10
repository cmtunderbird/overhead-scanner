"""
Intrinsics and distortion, from a ChArUco board.

ChArUco rather than a plain chessboard because it tolerates partial views
and occlusion: you can fill the frame corner with the board and still get
a solve, which is exactly where distortion is largest and where you most
need points.

You cannot solve intrinsics from a single fronto-parallel plane -- focal
length and distance trade off exactly.  Tilt the board.  `board_poses`
in the simulator shows the spread that conditions it well.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .store import CameraCalibration


@dataclass(frozen=True)
class BoardSpec:
    """
    A ChArUco target.  The default prints on A4 with a comfortable margin
    and fills a useful fraction of the frame at the rig's working distance.
    """

    squares_x: int = 7
    squares_y: int = 10
    square_mm: float = 26.0
    marker_ratio: float = 0.75
    dictionary: str = "DICT_5X5_250"

    @property
    def marker_mm(self) -> float:
        return self.square_mm * self.marker_ratio

    @property
    def size_mm(self) -> tuple[float, float]:
        return (self.squares_x * self.square_mm, self.squares_y * self.square_mm)

    def aruco_dict(self):
        return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.dictionary))

    def board(self):
        return cv2.aruco.CharucoBoard(
            (self.squares_x, self.squares_y),
            self.square_mm,
            self.marker_mm,
            self.aruco_dict(),
        )

    def rendered_extent_mm(self, margin_mm: float = 6.0) -> tuple[float, float]:
        """
        Physical size of the *rendered image*, margin included.

        Not the same as `size_mm`, which is the printed pattern only.
        Confusing the two puts a scale error straight into the document
        homography, so the margin is reported explicitly.
        """
        w, h = self.size_mm
        return (w + 2 * margin_mm, h + 2 * margin_mm)

    def render(self, px_per_mm: float = 8.0, margin_mm: float = 6.0) -> np.ndarray:
        """
        Render the board for printing or simulation.

        The returned image spans `rendered_extent_mm(margin_mm)`, not
        `size_mm` -- the quiet zone is part of the picture.
        """
        # NOTE: generateImage's outSize INCLUDES the margin.  Sizing it to
        # the pattern alone silently shrinks the printed squares -- which
        # puts a pure scale error into every homography solved against the
        # board, and it is invisible until you measure the page.
        margin_px = int(round(margin_mm * px_per_mm))
        w = int(round(self.squares_x * self.square_mm * px_per_mm)) + 2 * margin_px
        h = int(round(self.squares_y * self.square_mm * px_per_mm)) + 2 * margin_px
        img = self.board().generateImage((w, h), marginSize=margin_px)
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    def pattern_offset_mm(self, margin_mm: float = 6.0) -> tuple[float, float]:
        """Offset of the pattern origin inside the rendered image."""
        return (margin_mm, margin_mm)


A4_BOARD = BoardSpec()


@dataclass
class Detection:
    """ChArUco corners found in one frame."""

    corners: np.ndarray  # Nx1x2 float32, image px
    ids: np.ndarray  # Nx1 int32, charuco corner ids
    n: int


def _charuco_detector(spec: BoardSpec) -> "cv2.aruco.CharucoDetector":
    det_params = cv2.aruco.DetectorParameters()
    det_params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    det_params.adaptiveThreshWinSizeMax = 45
    charuco_params = cv2.aruco.CharucoParameters()
    charuco_params.tryRefineMarkers = True
    return cv2.aruco.CharucoDetector(
        spec.board(), charuco_params, det_params
    )


def detect_board(
    image: np.ndarray, spec: BoardSpec = A4_BOARD, min_corners: int = 12
) -> Detection | None:
    """
    Find ChArUco corners in one frame.  Returns None if too few were seen
    to be worth using.
    """
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corners, ids, _, _ = _charuco_detector(spec).detectBoard(gray)
    if ids is None or len(ids) < min_corners:
        return None
    return Detection(corners=corners, ids=ids, n=int(len(ids)))


def calibrate_intrinsics(
    images: list[np.ndarray],
    spec: BoardSpec = A4_BOARD,
    camera_id: str = "cam0",
    *,
    min_views: int = 6,
    fix_k3: bool = True,
) -> tuple[CameraCalibration, dict]:
    """
    Solve K and the distortion coefficients from a stack of board views.

    Returns (calibration, diagnostics).  Read the diagnostics: a low RMS
    from six flat views is a fit, not a calibration.
    """
    if not images:
        raise ValueError("no images supplied")
    h, w = images[0].shape[:2]
    board = spec.board()

    obj_points, img_points, used, n_corners = [], [], [], []
    for i, img in enumerate(images):
        if img.shape[:2] != (h, w):
            raise ValueError("all calibration images must be the same size")
        det = detect_board(img, spec)
        if det is None:
            continue
        op, ip = board.matchImagePoints(det.corners, det.ids)
        if op is None or len(op) < 12:
            continue
        obj_points.append(op.astype(np.float32))
        img_points.append(ip.astype(np.float32))
        used.append(i)
        n_corners.append(int(len(op)))

    if len(obj_points) < min_views:
        raise ValueError(
            f"only {len(obj_points)} usable views of the board "
            f"(need {min_views}); check lighting, focus, and that most of "
            f"the board is in frame"
        )

    flags = cv2.CALIB_FIX_K3 if fix_k3 else 0
    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, (w, h), None, None, flags=flags
    )

    # Per-view reprojection error, so one bad frame can be found and dropped.
    per_view = []
    for op, ip, rv, tv in zip(obj_points, img_points, rvecs, tvecs):
        proj, _ = cv2.projectPoints(op, rv, tv, K, dist)
        err = np.linalg.norm(proj.reshape(-1, 2) - ip.reshape(-1, 2), axis=1)
        per_view.append(float(err.mean()))

    # How much tilt did we actually see?  A flat stack gives a bad focal length.
    tilts = []
    for rv in rvecs:
        R, _ = cv2.Rodrigues(rv)
        tilts.append(float(np.degrees(np.arccos(np.clip(abs(R[2, 2]), -1, 1)))))

    diagnostics = {
        "views_supplied": len(images),
        "views_used": len(obj_points),
        "used_indices": used,
        "corners_per_view": n_corners,
        "rms_px": float(rms),
        "per_view_mean_px": per_view,
        "worst_view": int(used[int(np.argmax(per_view))]) if per_view else -1,
        "tilt_deg_min": round(min(tilts), 1) if tilts else None,
        "tilt_deg_max": round(max(tilts), 1) if tilts else None,
        "tilt_warning": (
            "the board was nearly flat in every view; focal length is "
            "poorly conditioned -- tilt it 20-30 degrees and reshoot"
            if tilts and max(tilts) < 12.0
            else None
        ),
    }

    calib = CameraCalibration(
        camera_id=camera_id,
        image_size=(w, h),
        K=np.asarray(K, dtype=np.float64),
        dist=np.asarray(dist, dtype=np.float64).ravel(),
        reprojection_rms=float(rms),
        notes=f"charuco {spec.squares_x}x{spec.squares_y} @ {spec.square_mm} mm",
    )
    return calib, diagnostics


def undistort(image: np.ndarray, calib: CameraCalibration) -> np.ndarray:
    """
    Remove lens distortion, keeping the original camera matrix so the
    homography solved against undistorted pixels stays valid.
    """
    return cv2.undistort(image, calib.K, calib.dist, None, calib.K)


def undistort_points(pts: np.ndarray, calib: CameraCalibration) -> np.ndarray:
    """Undistort an (N,2) array of pixel coordinates."""
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    out = cv2.undistortPoints(p, calib.K, calib.dist, P=calib.K)
    return out.reshape(-1, 2)
